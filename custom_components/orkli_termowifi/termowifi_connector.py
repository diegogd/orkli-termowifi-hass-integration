"""Module provides classes and methods to interact with the Termowifi system.

See PROTOCOL.md in the repository root for the frame format and registers.
"""

import asyncio
import contextlib
import enum
import logging
import socket

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN
from .termowifi_tools import (
    temperature_from_value,
    value_from_temperature,
    value_to_ambient,
    value_to_humidity,
)

_LOGGER = logging.getLogger(__name__)

# Frame: 3B | DEST | ORIGIN | CMD | D1 | D2 | CHK, CHK = sum(bytes 1..5) & 0xFF
FRAME_START = 0x3B
FRAME_LENGTH = 7

ADDR_TERMOWIFI = 0x01
ADDR_APP = 0xFE

CMD_ACK = 0x01
CMD_WRITE = 0x04

# Per zone registers, D1 = zone * 4 + register
MAX_ZONES = 8
REG_STATE = 0
REG_MODE = 1
REG_SETPOINT = 2
REG_AMBIENT = 3

# Global registers
REG_STATUS_DUMP = 0x23
REG_DEWPOINT = 0x32
REG_AVAILABLE_MODES = 0x35
REG_HUMIDITY_BASE = 0x64

# Zone discovery, same as the app: two passes waiting 3 seconds per zone
DISCOVERY_PASSES = 2
DISCOVERY_INTERVAL = 3.0

# Heating modes are even, cooling modes are odd. Each one has a counterpart
# using the same emitter: floor (2/3), air (4/1) and floor + air (6/5).
MODE_HEAT_FLOOR = 0x02
MODE_COOL_FLOOR = 0x03
MODE_COUNTERPART = {1: 4, 4: 1, 2: 3, 3: 2, 5: 6, 6: 5}


def checksum(payload: bytes) -> int:
    """Calculate the checksum of the frame bytes after the start byte."""
    return sum(payload) & 0xFF


def build_frame(dest: int, origin: int, command: int, data1: int, data2: int):
    """Build a frame for the Termowifi bus."""
    payload = bytes([dest, origin, command, data1, data2])
    return bytes([FRAME_START]) + payload + bytes([checksum(payload)])


def format_frame(frame: bytes) -> str:
    """Format a frame as space separated hex bytes."""
    return " ".join(f"{byte:02X}" for byte in frame)


class State(enum.Enum):
    """Enum representing the state of the system."""

    ON = 1
    OFF = 0


class OperationState(enum.Enum):
    """Enum representing the operation state of the system."""

    HEAT = 0
    COOL = 1


class TraceGenerator:
    """Generate command traces for the Termowifi system."""

    def __init__(self, room_id) -> None:
        """Initialize the TraceGenerator with room id."""
        self.room_id = room_id

    def write_trace(self, register, value):
        """Generate a trace writing a value in a zone register."""
        return build_frame(
            ADDR_TERMOWIFI, ADDR_APP, CMD_WRITE, self.room_id * 4 + register, value
        )

    def switch_trace(self, state: State, raw_state=None):
        """Generate the switch command trace.

        The app sets bits 0 and 1 to turn on and clears bit 0 to turn off.
        """
        current = 0x02 if raw_state is None else raw_state
        value = current | 0x03 if state == State.ON else current & 0x02
        return self.write_trace(REG_STATE, value)

    def switch_operation_mode(self, state: OperationState, raw_mode=None):
        """Generate the operation mode command trace.

        Keeps the fan speed (high nibble) and the emitter of the current mode.
        """
        if raw_mode is None or not raw_mode & 0x0F:
            mode = MODE_HEAT_FLOOR if state == OperationState.HEAT else MODE_COOL_FLOOR
            return self.write_trace(REG_MODE, mode)

        mode = raw_mode & 0x0F
        is_cool = bool(mode & 0x01)
        if is_cool != (state == OperationState.COOL):
            mode = MODE_COUNTERPART.get(mode, mode)
        return self.write_trace(REG_MODE, (raw_mode & 0xF0) | mode)

    def change_temperature_trace(self, temperature):
        """Generate the change temperature command trace."""
        return self.write_trace(REG_SETPOINT, value_from_temperature(temperature))

    def info_trace(self):
        """Generate the info trace command.

        Writing 0 in the ambient register requests the zone status.
        """
        return self.write_trace(REG_AMBIENT, 0x00)


class Room:
    """Representing a room in the Termowifi system."""

    def __init__(self, id) -> None:
        """Initialize the Room with id, host, and port.

        Args:
            id (int): The room id.
        """
        self.name = f"Room {id}"
        self.id = id
        self.state = None
        self.operation_state = None
        self.temperature = None
        self.conf_temperature = None
        self.humidity = None
        self.raw_state = None
        self.raw_mode = None
        self.trace_generator = TraceGenerator(id)
        self.updated_callback = None

    def update_register(self, register, value):
        """Update the room with a value reported for one of its registers."""
        value_changed = False

        if register == REG_STATE:
            self.raw_state = value
            new_state = State.ON if value & 0x01 else State.OFF
            value_changed = self.state != new_state
            self.state = new_state
            _LOGGER.debug("[%s] State: %s (0x%02x)", self.name, self.state, value)
        elif register == REG_MODE:
            if not value & 0x0F:
                _LOGGER.debug("[%s] Ignoring empty mode 0x%02x", self.name, value)
                return
            self.raw_mode = value
            new_operation_state = (
                OperationState.COOL if value & 0x01 else OperationState.HEAT
            )
            value_changed = self.operation_state != new_operation_state
            self.operation_state = new_operation_state
            _LOGGER.debug(
                "[%s] Operation state: %s (0x%02x)",
                self.name,
                self.operation_state,
                value,
            )
        elif register == REG_SETPOINT:
            if value == 0:
                return
            new_conf_temperature = temperature_from_value(value)
            value_changed = self.conf_temperature != new_conf_temperature
            self.conf_temperature = new_conf_temperature
            _LOGGER.debug(
                "[%s] Configured temperature: %s (0x%02x)",
                self.name,
                self.conf_temperature,
                value,
            )
        elif register == REG_AMBIENT:
            new_temperature = value_to_ambient(value)
            # The app shows values over 50ºC as no data
            if value == 0 or new_temperature > 50:
                return
            value_changed = self.temperature != new_temperature
            self.temperature = new_temperature
            _LOGGER.debug(
                "[%s] Temperature: %s (0x%02x)", self.name, self.temperature, value
            )

        if value_changed and self.updated_callback:
            self.updated_callback()

    def update_humidity(self, value):
        """Update the room humidity."""
        if value in (0x00, 0xFF):
            return
        new_humidity = value_to_humidity(value)
        value_changed = self.humidity != new_humidity
        self.humidity = new_humidity
        _LOGGER.debug("[%s] Humidity: %s (0x%02x)", self.name, self.humidity, value)

        if value_changed and self.updated_callback:
            self.updated_callback()

    def print_room_details(self) -> None:
        """Print the details of the room."""
        _LOGGER.info("%s is %s at %sºC", self.name, self.state, self.temperature)
        _LOGGER.info("Configured temperature is %sºC", self.conf_temperature)
        _LOGGER.info("Operation state is %s", self.operation_state)
        _LOGGER.info("Humidity is %s%%", self.humidity)


class TermowifiConnector:
    """Handle the connection and interaction with the Termowifi system."""

    def __init__(
        self,
        host: str,
        port: int,
        hass: HomeAssistant,
    ) -> None:
        """Initialize the TermowifiConnector with host and port."""
        self.rooms: dict[int, Room] = {}

        self.host = host
        self.port = port
        self.hass = hass
        self.reader = None
        self.writer = None

        self._writer_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._reader_task = None
        self._discovery_task = None

    async def connect(self):
        """Ensure the connection is established and supervisor is running."""
        if self._reader_task is None or self._reader_task.done():
            self._reader_task = self.hass.loop.create_task(
                self._reader_loop(), name="termowifi-reader"
            )

        # Give it a moment to connect if it's new
        await asyncio.sleep(0.1)

    async def async_close(self):
        """Close the connection and cancel reader and discovery tasks."""
        for task in (self._discovery_task, self._reader_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        await self._async_cleanup()

    async def _async_cleanup(self):
        """Clean up socket resources."""
        _LOGGER.debug("Cleaning up connection resources")
        if self.writer is not None:
            self.writer.close()
            with contextlib.suppress(OSError):
                await self.writer.wait_closed()
        self.writer = None
        self.reader = None

    async def _reader_loop(self):
        """Supervisor loop to handle connection and reading."""
        backoff = 1
        while True:
            try:
                async with self._connect_lock:
                    if self.writer is None:
                        _LOGGER.debug(
                            "Attempting to connect to %s:%s", self.host, self.port
                        )
                        # Add timeout to connection attempt
                        self.reader, self.writer = await asyncio.wait_for(
                            asyncio.open_connection(self.host, self.port), timeout=10
                        )

                        # Set TCP Keepalive to detect dead connections
                        sock = self.writer.get_extra_info("socket")
                        if sock is not None:
                            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                            # Linux-specific options (often available on other *nix)
                            if hasattr(socket, "TCP_KEEPIDLE"):
                                sock.setsockopt(
                                    socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 60
                                )
                            if hasattr(socket, "TCP_KEEPINTVL"):
                                sock.setsockopt(
                                    socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10
                                )
                            if hasattr(socket, "TCP_KEEPCNT"):
                                sock.setsockopt(
                                    socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3
                                )

                        _LOGGER.info("Connected to %s:%s", self.host, self.port)
                        backoff = 1  # Reset backoff on success

                await self._read_socket()

            except asyncio.CancelledError:
                _LOGGER.debug("Reader loop cancelled")
                break
            except (ConnectionError, OSError, asyncio.TimeoutError) as err:
                _LOGGER.error("Connection error: %s. Retrying in %ss", err, backoff)
                await self._async_cleanup()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            except Exception:
                _LOGGER.exception("Unexpected error in reader loop")
                await self._async_cleanup()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _read_socket(self):
        """Continuously read from the socket and process responses."""
        buffer = bytearray()
        while True:
            # Wait for data with a timeout (watchdog)
            # If no data is received within 150 seconds (2.5x standard polling interval),
            # consider the connection dead and reconnect.
            data = await asyncio.wait_for(self.reader.read(1024), timeout=150)
            if not data:
                _LOGGER.warning("Connection closed by server")
                break

            buffer += data
            self.process_buffer(buffer)

    def process_buffer(self, buffer: bytearray):
        """Extract and process the complete frames in the buffer.

        Resynchronizes on the start byte and the checksum, so a lost or extra
        byte only discards the affected frame.
        """
        while len(buffer) >= FRAME_LENGTH:
            if buffer[0] != FRAME_START:
                start = buffer.find(FRAME_START)
                discarded = len(buffer) if start == -1 else start
                _LOGGER.info(
                    "Discarded bytes out of frame: %s",
                    format_frame(buffer[:discarded]),
                )
                del buffer[:discarded]
                continue

            frame = bytes(buffer[:FRAME_LENGTH])
            if frame[6] != checksum(frame[1:6]):
                _LOGGER.info(
                    "Discarded frame with invalid checksum: %s", format_frame(frame)
                )
                del buffer[:1]
                continue

            del buffer[:FRAME_LENGTH]
            self.process_frame(frame)

    def process_frame(self, frame: bytes):
        """Process a valid frame received from the socket."""
        dest, origin, command, data1, data2 = frame[1:6]

        if origin != ADDR_TERMOWIFI:
            # Commands sent by us or by the Orkli app
            _LOGGER.debug("Ignoring frame from 0x%02x: %s", origin, format_frame(frame))
        elif command == CMD_ACK:
            _LOGGER.debug("Acknowledged command: %s", format_frame(frame))
        elif command == CMD_WRITE and dest == ADDR_TERMOWIFI:
            self._process_register(data1, data2)
        else:
            _LOGGER.debug("Unknown frame: %s", format_frame(frame))

    def _process_register(self, register, value):
        """Process a status report of a register."""
        if register < MAX_ZONES * 4:
            room_id, zone_register = divmod(register, 4)
            room = self.rooms.get(room_id)
            if room is None:
                # The app considers a zone exists when it reports a setpoint
                if zone_register != REG_SETPOINT or value == 0:
                    _LOGGER.debug(
                        "Ignoring register %s=0x%02x of unknown room %s",
                        zone_register,
                        value,
                        room_id,
                    )
                    return
                room = self._add_room(room_id)
            room.update_register(zone_register, value)
        elif REG_HUMIDITY_BASE <= register < REG_HUMIDITY_BASE + MAX_ZONES:
            room = self.rooms.get(register - REG_HUMIDITY_BASE)
            if room is not None:
                room.update_humidity(value)
        elif register == REG_DEWPOINT:
            _LOGGER.debug("Dew point alarm: 0x%02x", value)
        elif register == REG_AVAILABLE_MODES:
            _LOGGER.debug("Available modes: 0x%02x", value)
        else:
            _LOGGER.debug("Unknown register 0x%02x with value 0x%02x", register, value)

    def _add_room(self, room_id) -> Room:
        """Register a new room and notify the climate platform."""
        _LOGGER.debug("Adding room id: %s", room_id)
        room = Room(room_id)
        self.rooms[room_id] = room
        async_dispatcher_send(self.hass, f"{DOMAIN}_new_room", room)
        return room

    async def _async_send_trace(self, message, repeat=3):
        """Send a message to the server."""
        if self.writer is None:
            await self.connect()

        # Wait for a potential reconnection in progress
        async with self._connect_lock:
            if self.writer is None:
                _LOGGER.warning("Cannot send trace: Not connected")
                return

            message_bytes = bytes(message)

            async with self._writer_lock:
                for _ in range(repeat):
                    self.writer.write(message_bytes)
                    await self.writer.drain()
            _LOGGER.debug("Sent: %s", format_frame(message_bytes))

    def get_rooms(self):
        """Get the list of rooms."""
        return self.rooms.values()

    async def update_room(self, room_id):
        """Update a single room sending a refresh command for a single node."""
        room = self.rooms.get(room_id)
        if room:
            await self._async_send_trace(
                room.trace_generator.info_trace(),
                repeat=1,
            )

    async def update_rooms(self):
        """Update the list of rooms from the Termowifi system."""
        rooms = list(self.rooms.values())
        for room in rooms:
            await self._async_send_trace(
                room.trace_generator.info_trace(),
                repeat=1,
            )

    async def async_initialize(self):
        """Initialize the Termowifi system and discover rooms."""
        request = build_frame(ADDR_TERMOWIFI, ADDR_APP, CMD_WRITE, REG_STATUS_DUMP, 0)
        _LOGGER.debug("Sending initialization trace: %s", format_frame(request))
        await self._async_send_trace(request, repeat=2)

        self._discovery_task = self.hass.loop.create_task(
            self._async_discover_rooms(), name="termowifi-discovery"
        )

    async def _async_discover_rooms(self):
        """Request the status of every zone, the same way the app does.

        A second pass retries the zones whose request or response was lost.
        """
        for _ in range(DISCOVERY_PASSES):
            for room_id in range(MAX_ZONES):
                if room_id in self.rooms:
                    continue
                await self._async_send_trace(
                    TraceGenerator(room_id).info_trace(), repeat=1
                )
                await asyncio.sleep(DISCOVERY_INTERVAL)

    async def _async_write(self, room: Room, trace):
        """Send a write command and request the updated room status."""
        await self._async_send_trace(trace, repeat=2)
        # The app only does this after a setpoint change, but the write is not
        # always reported back, so the zone is refreshed after every write
        await self._async_send_trace(room.trace_generator.info_trace(), repeat=1)

    async def set_temperature(self, *, room_id, temperature):
        """Set the desired temperature for the room."""
        room: Room = self.rooms.get(room_id)
        if room:
            trace = room.trace_generator.change_temperature_trace(temperature)
            await self._async_write(room, trace)

    async def set_state(self, *, room_id, state: State):
        """Set the state (ON/OFF) for the room."""
        room: Room = self.rooms.get(room_id)
        if room:
            trace = room.trace_generator.switch_trace(state, room.raw_state)
            await self._async_write(room, trace)

    async def set_operation_mode(self, *, room_id, operation_state: OperationState):
        """Set the operation mode (HEAT/COOL) for the room."""
        room: Room = self.rooms.get(room_id)
        if room:
            if room.operation_state == operation_state:
                return
            trace = room.trace_generator.switch_operation_mode(
                operation_state, room.raw_mode
            )
            await self._async_write(room, trace)
