"""Module provides classes and methods to interact with the Termowifi system."""

import asyncio
import contextlib
import enum
import logging

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


def _valid_header_response(self, response):
    """Check if the response has a valid header."""
    trace_type = None
    if response[:4] == TraceHeader.VALID_ANSWER.value:
        trace_type = TraceHeader.VALID_ANSWER
    elif response[:4] == TraceHeader.VALID_CONFIRMATION.value:
        trace_type = TraceHeader.VALID_CONFIRMATION

    return trace_type


class TraceHeader(enum.Enum):
    """Constants for trace headers."""

    SEND_COMMAND = bytes([0x3B, 0x01, 0xFE, 0x04])
    VALID_ANSWER = bytes([0x3B, 0x01, 0x01, 0x04])
    VALID_CONFIRMATION = bytes([0x3B, 0xFE, 0x01, 0x01])


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

    def switch_trace(self, state: State):
        """Generate the switch command trace."""
        base_command = 0x00
        cid = base_command + (self.room_id * 4)
        if state == State.ON:
            data1 = 0x03
        else:
            data1 = 0x02
        checksum = (cid + data1 + 0x03) % 256

        return TraceHeader.SEND_COMMAND.value + bytes([cid, data1, checksum])

    def switch_operation_mode(self, state: OperationState):
        """Generate the switch command trace."""
        base_command = 0x01
        cid = base_command + (self.room_id * 4)
        if state == OperationState.HEAT:
            data1 = 0x02
        elif state == OperationState.COOL:
            data1 = 0x03
        checksum = (cid + data1 + 0x03) % 256

        return TraceHeader.SEND_COMMAND.value + bytes([cid, data1, checksum])

    def change_temperature_trace(self, temperature):
        """Generate the change temperature command trace."""
        base_command = 0x02
        cid = base_command + (self.room_id * 4)
        data1 = value_from_temperature(temperature)
        checksum = (cid + data1 + 0x03) % 256

        return TraceHeader.SEND_COMMAND.value + bytes([cid, data1, checksum])

    def info_trace(self):
        """Generate the info trace command."""
        base_command = 0x03
        cid = base_command + (self.room_id * 4)
        data1 = 0x00
        checksum = (cid + data1 + 0x03) % 256
        # concat write header with id, data1, checksum
        return TraceHeader.SEND_COMMAND.value + bytes([cid, data1, checksum])


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
        self.trace_generator = TraceGenerator(id)
        self.updated_callback = None

    def parse_response(self, response, header_type: TraceHeader = None):
        """Parse the response from the Termowifi system."""
        processed = False
        value_changed = False
        command = response[4]
        value = response[5]
        checksum = response[6]
        checksum_diff = 0x00 if header_type == TraceHeader.VALID_CONFIRMATION else 0x06

        processed = True

        # Info response
        base_info = self.id * 4
        # Status ON/OFF
        if command == base_info and checksum == (command + value + checksum_diff) % 256:
            if value == 0x03:
                if self.state != State.ON:
                    value_changed = True
                self.state = State.ON
            elif value == 0x02:
                if self.state != State.OFF:
                    value_changed = True
                self.state = State.OFF
            _LOGGER.debug(
                "[%s] State: %s (cmd: 0x%02x, val: 0x%02x)",
                self.name,
                self.state,
                command,
                value,
            )
        # Heat/Cold status
        elif (
            command == base_info + 1
            and checksum == (command + value + checksum_diff) % 256
        ):
            if value == 0x02:
                if self.operation_state != OperationState.HEAT:
                    value_changed = True
                self.operation_state = OperationState.HEAT
            elif value == 0x03:
                if self.operation_state != OperationState.COOL:
                    value_changed = True
                self.operation_state = OperationState.COOL
            _LOGGER.debug("[%s] Operation state: %s", self.name, self.operation_state)
        # Configured temperature
        elif (
            command == base_info + 2
            and checksum == (command + value + checksum_diff) % 256
        ):
            new_conf_temperature = temperature_from_value(value)
            if self.conf_temperature != new_conf_temperature:
                value_changed = True
            self.conf_temperature = new_conf_temperature
            _LOGGER.debug(
                "[%s] Configured temperature: %s", self.name, self.conf_temperature
            )
        # Room temperature
        elif (
            command == base_info + 3
            and checksum == (command + value + checksum_diff) % 256
        ):
            new_temperature = value_to_ambient(value)
            if self.temperature != new_temperature:
                value_changed = True
            self.temperature = new_temperature
            _LOGGER.debug("[%s] Temperature: %s", self.name, self.temperature)
        # Humidity
        elif (
            command == self.id + 0x64
            and checksum == (command + value + checksum_diff) % 256
        ):
            new_humidity = value_to_humidity(value)
            if self.humidity != new_humidity:
                value_changed = True
            self.humidity = new_humidity
            _LOGGER.debug(
                "[%s] Humidity: %s (cmd: 0x%02x, val: 0x%02x)",
                self.name,
                self.humidity,
                command,
                value,
            )
        else:
            _LOGGER.debug(
                "[%s] Unknown command: 0x%02x (%s) with value 0x%02x (%s) [checksum: 0x%02x]",
                self.name,
                command,
                command,
                value,
                value,
                checksum,
            )
            processed = False

        if processed and value_changed and self.updated_callback:
            self.updated_callback()

        return processed

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
        self._reader_task = None

    async def connect(self):
        """Establish a connection to the server."""
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        _LOGGER.debug("Connected to %s:%s", self.host, self.port)

        # Lanzamos el listener en background
        if self._reader_task is None or self._reader_task.done():
            self._reader_task = self.hass.loop.create_task(
                self._reader_loop(), name="termowifi-reader"
            )
            # self._reader_task = self.hass.async_create_task(self._reader_loop())

    async def async_close(self):
        """Close the connection and cancel reader task."""
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()

        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task

    async def _reader_loop(self):
        """Background task to continuously read from the socket and process responses."""
        buffer = b""
        try:
            while True:
                data = await self.reader.read(1024)
                if not data:
                    _LOGGER.warning("Connection closed by server")
                    break

                buffer += data

                while len(buffer) >= 7:
                    response = buffer[:7]
                    buffer = buffer[7:]
                    # _LOGGER.info("Received: %s", response.hex())

                    self.process_socket_response(response)
        except asyncio.CancelledError:
            _LOGGER.debug("Reader task cancelled")
        except (ConnectionError, OSError) as err:
            _LOGGER.error("Network error in reader loop: %s", err)
        except asyncio.IncompleteReadError as err:
            _LOGGER.error("Incomplete read error in reader loop: %s", err)

    def process_socket_response(self, response):
        """Process a response received from the socket."""
        valid_header_response = _valid_header_response(self, response)
        if valid_header_response is not None:
            if not self._parse_response(response, valid_header_response):
                processed = False
                for room in self.rooms.values():
                    if room.parse_response(response, valid_header_response):
                        processed = True
                        break
                if not processed:
                    command = response[4]
                    value = response[5]
                    checksum = response[6]
                    _LOGGER.warning(
                        "Unprocessed valid response: command=0x%02x, value=0x%02x, checksum=0x%02x",
                        command,
                        value,
                        checksum,
                    )
            else:
                _LOGGER.debug("Processed response: %s", response.hex())
        elif response[:4] == TraceHeader.SEND_COMMAND.value:
            _LOGGER.debug("Acknowledged sent command: %s", response.hex())
        else:
            # _LOGGER.info("Discarded invalid response: %s", response.hex())
            # Displace trace in groups of bytes in a single log entry
            _LOGGER.info(
                "Discarded invalid response: %s",
                " ".join(
                    [response[i : i + 1].hex().upper() for i in range(len(response))]
                ),
            )

    async def _async_send_trace(self, message, repeat=3):
        """Send a message to the server."""
        if self.writer is None:
            await self.connect()

        message_bytes = bytes(message)

        async with self._writer_lock:
            for _ in range(repeat):
                self.writer.write(message_bytes)
                await self.writer.drain()
        _LOGGER.debug("Sent: %s", message_bytes.hex())

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
        # 3B 01 FE 04 23 00 26
        request = TraceHeader.SEND_COMMAND.value + bytes([0x23, 0x00, 0x26])
        _LOGGER.debug("Sending initialization trace: %s", request.hex())
        await self._async_send_trace(request, repeat=2)

    def _parse_response(self, response, header_type: TraceHeader = None):
        """Parse the response from the Termowifi system."""
        processed = False
        cid = response[4]
        data = response[5]
        checksum = response[6]
        # Checksum diff varies based on header type
        checksum_diff = 0x00 if header_type == TraceHeader.VALID_CONFIRMATION else 0x06
        checksum_calc = (cid + data + checksum_diff) % 256

        if checksum != checksum_calc:
            _LOGGER.warning("Invalid response checksum: %s", response.hex())
            return processed

        if cid >= 0x32 and cid <= 0x36:
            # New room detected
            if data == 0x00:
                room_id = cid - 0x32
                _LOGGER.debug("Room found with id: %s", room_id)
                processed = True
                if room_id not in self.rooms:
                    _LOGGER.debug("Adding room id: %s", room_id)
                    room = Room(room_id)
                    self.rooms[room_id] = room
                    async_dispatcher_send(self.hass, f"{DOMAIN}_new_room", room)
            else:
                _LOGGER.warning("Invalid room identification: %s", response.hex())

        return processed

    async def set_temperature(self, *, room_id, temperature):
        """Set the desired temperature for the room."""
        room: Room = self.rooms.get(room_id)
        if room:
            trace = room.trace_generator.change_temperature_trace(temperature)
            await self._async_send_trace(trace, repeat=2)

    async def set_state(self, *, room_id, state: State):
        """Set the state (ON/OFF) for the room."""
        room: Room = self.rooms.get(room_id)
        if room:
            trace = room.trace_generator.switch_trace(state)
            await self._async_send_trace(trace, repeat=2)

    async def set_operation_mode(self, *, room_id, operation_state: OperationState):
        """Set the operation mode (HEAT/COOL) for the room."""
        room: Room = self.rooms.get(room_id)
        if room:
            trace = room.trace_generator.switch_operation_mode(operation_state)
            await self._async_send_trace(trace, repeat=2)
