"""Module provides classes and methods to interact with the Termowifi system."""

import enum
import logging
import socket

from homeassistant.core import HomeAssistant
import asyncio

from .termowifi_tools import (
    temperature_from_value,
    value_to_ambient,
    value_to_humidity,
    value_from_temperature,
)

VALID_ANSWER_PREFIX = bytes([0x3B, 0x01, 0x01, 0x04])
SEND_COMMAND_PREFIX = bytes([0x3B, 0x01, 0xFE, 0x04])


class State(enum.Enum):
    """Enum representing the state of the system."""

    ON = 1
    OFF = 0


class OperationState(enum.Enum):
    """Enum representing the operation state of the system."""

    HEAT = 0
    COLD = 1


class SocketConnector:
    """Handle socket connections and communication."""

    def __init__(self, host, port, hass: HomeAssistant) -> None:
        """Initialize the SocketConnector with host and port.

        Args:
            host (str): The host address.
            port (int): The port number.

        """
        self.host = host
        self.port = port
        self.hass = hass
        self.reader = None
        self.writer = None

    async def connect(self):
        """Establish a connection to the server."""
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)

    async def send_trace(self, message, show_return_traces=10):
        """Send a message to the server and trace the response.

        Args:
            message (str): The message to send.
            show_return_traces (int): Number of response traces to show.

        """
        if self.writer is None:
            await self.connect()

        message_bytes = bytes(message)
        self.writer.write(message_bytes)
        await self.writer.drain()

        previous_message = message_bytes

        while show_return_traces > 0:
            try:
                response = await asyncio.wait_for(self.reader.read(1024), timeout=0.4)
                if response != previous_message:
                    show_return_traces -= 1
                else:
                    continue

                self._parse_response(response)

                previous_message = response
            except asyncio.TimeoutError:
                show_return_traces -= 1
                continue

    def valid_response(self, response):
        return response[:4] == VALID_ANSWER_PREFIX

    def _parse_response(self, response):
        print("Not implemented")


class Room(SocketConnector):
    """Representing a room in the Termowifi system."""

    def __init__(self, id, host, port, hass: HomeAssistant) -> None:
        """Initialize the Room with id, host, and port.

        Args:
            id (int): The room id.
            host (str): The host address.
            port (int): The port number.

        """
        super().__init__(host, port, hass)
        self.name = f"Room {id}"
        self.id = id
        self.state = None
        self.operation_state = None
        self.temperature = None
        self.conf_temperature = None
        self.humidity = None

    def _switch(self, state: State):
        base_command = 0x00
        cid = base_command + (self.id * 4)
        if state == State.ON:
            data1 = 0x03
        else:
            data1 = 0x02
        checksum = cid + data1 + 0x03

        return SEND_COMMAND_PREFIX + bytes([cid, data1, checksum])

    def _change_temperature(self, temperature):
        base_command = 0x02
        cid = base_command + (self.id * 4)
        data1 = value_from_temperature(temperature)
        checksum = cid + data1 + 0x03

        return SEND_COMMAND_PREFIX + bytes([cid, data1, checksum])

    def _get_info(self):
        base_command = 0x03
        cid = base_command + (self.id * 4)
        data1 = 0x00
        checksum = cid + data1 + 0x03
        # concat write header with id, data1, checksum
        return SEND_COMMAND_PREFIX + bytes([cid, data1, checksum])

    async def update(self):
        await self.hass.async_add_executor_job(
            lambda: super(Room, self).send_trace(self._get_info(), 10)
        )

    async def set_temperature(self, temperature):
        trace = self._change_temperature(temperature)
        await self.hass.async_add_executor_job(
            lambda: super(Room, self).send_trace(trace, 2)
        )

    def _parse_response(self, response):
        # valid prefix 3B 01 01 04
        if (
            response[0] == 0x3B
            and response[1] == 0x01
            and response[2] == 0x01
            and response[3] == 0x04
        ):
            command = response[4]
            value = response[5]
            checksum = response[6]

            # Info response
            base_info = self.id * 4
            # Status ON/OFF
            if command == base_info and checksum == (command + value + 0x06):
                if value == 0x03:
                    self.state = State.ON
                elif value == 0x02:
                    self.state = State.OFF
            # Heat/Cold status
            elif command == base_info + 1 and checksum == (command + value + 0x06):
                if value == 0x02:
                    self.operation_state = OperationState.HEAT
                elif value == 0x03:
                    self.operation_state = OperationState.COLD
            # Configured temperature
            elif command == base_info + 2 and checksum == (command + value + 0x06):
                self.conf_temperature = temperature_from_value(value)
            # Room temperature
            elif command == base_info + 3 and checksum == (command + value + 0x06):
                # External temperature start at 110
                self.temperature = value_to_ambient(value)
            # Humidity
            elif command == self.id + 0x64 and checksum == (command + value + 0x06):
                # External temperature start at 110
                self.humidity = value_to_humidity(value)

    def print_room_details(self) -> None:
        """Print the details of the room."""

        logging.info("%s is %s at %sºC", self.name, self.state, self.temperature)
        logging.info("Configured temperature is %sºC", self.conf_temperature)
        logging.info("Operation state is %s", self.operation_state)
        logging.info("Humidity is %s%%", self.humidity)


class TermowifiConnector(SocketConnector):
    """Handle the connection and interaction with the Termowifi system."""

    def __init__(
        self,
        host: str,
        port: int,
        hass: HomeAssistant,
    ) -> None:
        super().__init__(host, port, hass)
        self.rooms = []

    def get_rooms(self):
        return self.rooms

    async def async_initialize(self):
        await self._init_rooms()

    async def _init_rooms(self):
        # 3B 01 FE 04 23 00 26
        request = SEND_COMMAND_PREFIX + bytes([0x23, 0x00, 0x26])
        await self.hass.async_add_executor_job(
            lambda: super(TermowifiConnector, self).send_trace(request, 5)
        )

    def _parse_response(self, response):
        if self.valid_response(response):
            cid = response[4]
            data = response[5]
            checksum = response[6]
            if data == 0x00 and checksum == cid + data + 0x06:
                room_id = cid - 0x32
                logging.debug("Room found with id: %s", room_id)
                self.rooms.append(Room(room_id, self.host, self.port, self.hass))
