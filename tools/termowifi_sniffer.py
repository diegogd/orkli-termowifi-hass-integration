#!/usr/bin/env python3
"""Log the TermoWifi bus traffic with timestamps.

Passive by default: it only reads from the socket. With --probe it also sends
read-only requests (status dump, C_POLLING and a read of every zone).

Usage:
    python3 tools/termowifi_sniffer.py 192.168.1.50 --out traffic.jsonl
    python3 tools/termowifi_sniffer.py 192.168.1.50 --changes-only
    python3 tools/termowifi_sniffer.py 192.168.1.50 --probe

See PROTOCOL.md for the frame format and registers.
"""

import argparse
import asyncio
import contextlib
import json
import sys
from collections import defaultdict
from datetime import datetime

FRAME_START = 0x3B
FRAME_LENGTH = 7
ADDR_TERMOWIFI = 0x01
ADDR_APP = 0xFE
MAX_ZONES = 8

COMMANDS = {0x01: "ACK", 0x02: "NACK", 0x04: "WRITE", 0x0A: "POLLING", 0x8C: "CONFIG"}
ZONE_REGISTERS = ("state", "mode", "setpoint", "ambient")
GLOBAL_REGISTERS = {0x23: "status_dump", 0x32: "dewpoint", 0x35: "available_modes"}


def checksum(payload) -> int:
    """Calculate the checksum of the frame bytes after the start byte."""
    return sum(payload) & 0xFF


def build_frame(dest, origin, command, data1, data2) -> bytes:
    """Build a frame for the Termowifi bus."""
    payload = bytes([dest, origin, command, data1, data2])
    return bytes([FRAME_START]) + payload + bytes([checksum(payload)])


def decode_value(data1, data2):
    """Describe a register and its value."""
    if data1 < MAX_ZONES * 4:
        zone, register = divmod(data1, 4)
        name = f"zone{zone}.{ZONE_REGISTERS[register]}"
        if register == 0:
            value = "on" if data2 & 0x01 else "off"
        elif register == 1:
            mode = data2 & 0x0F
            kind = "-" if not mode else ("cool" if mode & 0x01 else "heat")
            value = f"mode={mode}({kind}) fan=0x{data2 & 0xF0:02x}"
        elif register == 2:
            value = f"{data2 / 2:.1f}C" if data2 else "no data"
        else:
            value = f"{(162 - data2) / 2:.1f}C" if data2 else "no data / read"
        return name, value
    if 0x64 <= data1 < 0x64 + MAX_ZONES:
        name = f"zone{data1 - 0x64}.humidity"
        return name, "no data" if data2 in (0, 255) else f"{data2 / 2.55:.1f}%"
    return GLOBAL_REGISTERS.get(data1, f"reg_0x{data1:02x}"), f"0x{data2:02x}"


class Sniffer:
    """Read frames from the socket and log them."""

    def __init__(self, out, changes_only) -> None:
        """Initialize the sniffer."""
        self.out = out
        self.changes_only = changes_only
        self.last_values = {}
        self.seen = defaultdict(lambda: {"count": 0, "values": set()})
        self.discarded = 0

    def process_buffer(self, buffer: bytearray):
        """Extract the complete frames in the buffer."""
        while len(buffer) >= FRAME_LENGTH:
            if buffer[0] != FRAME_START:
                start = buffer.find(FRAME_START)
                discarded = len(buffer) if start == -1 else start
                self.log_garbage(bytes(buffer[:discarded]))
                del buffer[:discarded]
                continue
            frame = bytes(buffer[:FRAME_LENGTH])
            if frame[6] != checksum(frame[1:6]):
                self.log_garbage(frame[:1])
                del buffer[:1]
                continue
            del buffer[:FRAME_LENGTH]
            self.log_frame(frame)

    def log_garbage(self, data: bytes):
        """Log bytes that are not part of a valid frame."""
        self.discarded += len(data)
        self.write({"type": "garbage", "raw": data.hex(" ").upper()})

    def log_frame(self, frame: bytes):
        """Log a valid frame."""
        dest, origin, command, data1, data2 = frame[1:6]
        name, value = decode_value(data1, data2)
        key = (dest, origin, command, data1)
        stats = self.seen[key]
        stats["count"] += 1
        stats["values"].add(data2)

        changed = self.last_values.get(key) != data2
        self.last_values[key] = data2
        if self.changes_only and not changed:
            return

        self.write(
            {
                "type": "frame",
                "raw": frame.hex(" ").upper(),
                "dest": dest,
                "origin": origin,
                "cmd": COMMANDS.get(command, f"0x{command:02x}"),
                "d1": data1,
                "d2": data2,
                "register": name,
                "value": value,
                "changed": changed,
            }
        )

    def write(self, record):
        """Print a record and append it to the output file."""
        record = {
            "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            **record,
        }
        if record["type"] == "frame":
            print(
                f"{record['ts']}  {record['raw']}  "
                f"{record['origin']:02X}->{record['dest']:02X} {record['cmd']:<7} "
                f"{record['register']:<22} {record['value']}"
                f"{'' if record['changed'] else '  (=)'}"
            )
        else:
            print(f"{record['ts']}  garbage: {record['raw']}")
        if self.out:
            self.out.write(json.dumps(record) + "\n")
            self.out.flush()

    def print_summary(self):
        """Print the registers seen and their distinct values."""
        print(f"\nSummary ({self.discarded} bytes discarded)", file=sys.stderr)
        for (dest, origin, command, data1), stats in sorted(self.seen.items()):
            name, _ = decode_value(data1, 0)
            values = " ".join(f"{v:02X}" for v in sorted(stats["values"]))
            print(
                f"  {origin:02X}->{dest:02X} {COMMANDS.get(command, hex(command)):<7} "
                f"d1=0x{data1:02x} {name:<22} n={stats['count']:<5} values: {values}",
                file=sys.stderr,
            )


async def probe(writer):
    """Send read-only requests: status dump, polling and every zone."""
    requests = [
        build_frame(ADDR_TERMOWIFI, ADDR_APP, 0x04, 0x23, 0x00),
        build_frame(ADDR_TERMOWIFI, 0xFF, 0x0A, 0x00, 0x00),
        build_frame(0xFF, 0xFF, 0x0A, 0x00, 0x00),
    ]
    requests += [
        build_frame(ADDR_TERMOWIFI, ADDR_APP, 0x04, zone * 4 + 3, 0x00)
        for zone in range(MAX_ZONES)
    ]
    for request in requests:
        print(f"--> sending {request.hex(' ').upper()}", file=sys.stderr)
        writer.write(request)
        await writer.drain()
        await asyncio.sleep(3)


async def run(args, out):
    """Connect to the TermoWifi and log the traffic until interrupted."""
    sniffer = Sniffer(out, args.changes_only)
    reader, writer = await asyncio.open_connection(args.host, args.port)
    print(f"Connected to {args.host}:{args.port}", file=sys.stderr)
    probe_task = asyncio.create_task(probe(writer)) if args.probe else None
    buffer = bytearray()
    try:
        while data := await reader.read(1024):
            buffer += data
            sniffer.process_buffer(buffer)
        print("Connection closed by the TermoWifi", file=sys.stderr)
    finally:
        if probe_task:
            probe_task.cancel()
        sniffer.print_summary()
        writer.close()


def main():
    """Parse the arguments and run the sniffer."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("host", help="TermoWifi IP address")
    parser.add_argument("--port", type=int, default=12345)
    parser.add_argument("--out", help="Append the frames as JSON lines to this file")
    parser.add_argument(
        "--changes-only",
        action="store_true",
        help="Only show frames whose value changed",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Also send read-only requests (status dump, polling, zone reads)",
    )
    args = parser.parse_args()
    with contextlib.ExitStack() as stack:
        out = (
            stack.enter_context(open(args.out, "a", encoding="utf-8"))
            if args.out
            else None
        )
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(run(args, out))


if __name__ == "__main__":
    main()
