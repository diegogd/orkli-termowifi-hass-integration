"""Find TermoWifi devices on the local network.

The TermoWifi does not announce itself (no mDNS, no reply to the Ingenium
``find_ETHBUS`` UDP broadcast), so devices are found by connecting to the TCP
port and checking that the device answers with valid frames.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
import contextlib
import ipaddress
import logging

from .termowifi_connector import (
    ADDR_APP,
    ADDR_TERMOWIFI,
    CMD_WRITE,
    FRAME_LENGTH,
    FRAME_START,
    REG_STATUS_DUMP,
    build_frame,
    checksum,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 12345

# Too many parallel connections or short timeouts miss WiFi devices: with 256
# connections and 1 s the TermoWifi was not found, with 64 and 2 s it was.
SCAN_CONCURRENCY = 64
CONNECT_TIMEOUT = 2.0
ANSWER_TIMEOUT = 3.0

# Larger networks are reduced to the /24 around the Home Assistant address
MIN_SCAN_PREFIX = 22
FALLBACK_SCAN_PREFIX = 24


def is_termowifi_frame(frame: bytes) -> bool:
    """Return whether a frame is valid and sent by the TermoWifi."""
    return (
        len(frame) == FRAME_LENGTH
        and frame[0] == FRAME_START
        and frame[6] == checksum(frame[1:6])
        and frame[2] == ADDR_TERMOWIFI
    )


def contains_termowifi_frame(data: bytes) -> bool:
    """Return whether the data contains a frame sent by the TermoWifi.

    The device echoes the frames written by clients (origin ``FE``), so those
    do not count.
    """
    start = data.find(FRAME_START)
    while start != -1:
        if is_termowifi_frame(data[start : start + FRAME_LENGTH]):
            return True
        start = data.find(FRAME_START, start + 1)
    return False


async def async_probe(
    host: str,
    port: int = DEFAULT_PORT,
    connect_timeout: float = CONNECT_TIMEOUT,
    answer_timeout: float = ANSWER_TIMEOUT,
) -> bool:
    """Return whether a TermoWifi answers on host and port.

    Sends the status dump request (read only, the app sends it when the
    thermostat screen opens) and waits for a frame from the TermoWifi.
    Raises OSError or TimeoutError when the connection fails.
    """
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=connect_timeout
    )
    try:
        writer.write(
            build_frame(ADDR_TERMOWIFI, ADDR_APP, CMD_WRITE, REG_STATUS_DUMP, 0)
        )
        await writer.drain()

        data = b""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + answer_timeout
        while (remaining := deadline - loop.time()) > 0:
            try:
                chunk = await asyncio.wait_for(reader.read(256), timeout=remaining)
            except TimeoutError:
                break
            if not chunk:
                break
            data += chunk
            if contains_termowifi_frame(data):
                return True
        _LOGGER.debug("No TermoWifi answer from %s:%s: %s", host, port, data.hex(" "))
        return False
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


def scan_hosts(
    networks: Iterable[ipaddress.IPv4Interface],
) -> list[ipaddress.IPv4Address]:
    """Return the addresses to scan for the interfaces of Home Assistant."""
    hosts: dict[ipaddress.IPv4Address, None] = {}
    for interface in networks:
        network = interface.network
        if network.is_loopback or network.is_link_local:
            continue
        if network.prefixlen < MIN_SCAN_PREFIX:
            network = ipaddress.IPv4Network(
                f"{interface.ip}/{FALLBACK_SCAN_PREFIX}", strict=False
            )
        for host in network.hosts():
            if host != interface.ip:
                hosts[host] = None
    return list(hosts)


async def async_scan(
    hosts: Iterable[ipaddress.IPv4Address | str],
    port: int = DEFAULT_PORT,
) -> list[str]:
    """Return the hosts where a TermoWifi answers, in scan order."""
    semaphore = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def _probe(host: str) -> bool:
        async with semaphore:
            try:
                return await async_probe(host, port)
            except (OSError, TimeoutError):
                return False

    hosts = [str(host) for host in hosts]
    results = await asyncio.gather(*(_probe(host) for host in hosts))
    found = [host for host, ok in zip(hosts, results, strict=True) if ok]
    _LOGGER.debug("Scanned %d hosts, TermoWifi found at %s", len(hosts), found)
    return found
