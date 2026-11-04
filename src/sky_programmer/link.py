"""The USB link. This is the only module that writes to the serial port."""
from __future__ import annotations

import glob
import time
from collections import deque

from . import protocol as p
from .errors import CommsError, SafetyRefusal
from .safety import check_packet


def list_ports() -> list[str]:
    return sorted(glob.glob("/dev/ttyACM*"))


def find_port() -> str:
    ports = list_ports()
    if not ports:
        raise CommsError("no VESC found (no /dev/ttyACM* device). Plug it in, or pass --port.")
    if len(ports) > 1:
        raise SafetyRefusal(f"more than one possible board ({', '.join(ports)}). Pick one with --port.")
    return ports[0]


def open_serial(port: str):
    import serial

    try:
        return serial.Serial(port, 115200, timeout=0.05, exclusive=True)
    except (OSError, ValueError, serial.SerialException) as e:
        raise CommsError(f"cannot open {port}: {e}. Is VESC Tool still connected?") from e


class Link:
    def __init__(self, ser, clock=time.monotonic, sleep=time.sleep):
        self.ser, self.clock, self.sleep = ser, clock, sleep
        self.prints = deque(maxlen=2000)
        self._buf = b""

    def close(self) -> None:
        self.ser.close()

    def send(self, payload: bytes, *, config_write: bool = False, repl_allow_motor: bool = False) -> None:
        """Send one packet, after check_packet() has allowed it."""
        check_packet(payload, config_write=config_write, repl_allow_motor=repl_allow_motor)
        try:
            self.ser.write(p.frame(payload))
        except OSError as e:
            raise CommsError(f"USB write failed: {e}") from e

    def _pump(self):
        """Read what is available; return complete non-print packets. Prints go to self.prints."""
        try:
            self._buf += self.ser.read(4096)
        except OSError as e:
            raise CommsError(f"USB read failed: {e}") from e
        replies, i = [], 0
        while i < len(self._buf):
            found = p.frame_at(self._buf, i)
            if found is None:
                i += 1
                continue
            pl, end = found
            if pl and pl[0] in p.PRINT_COMMANDS:
                self.prints.append(pl[1:].decode(errors="replace"))
            elif pl:
                replies.append(pl)
            self._buf, i = self._buf[end:], 0
        if len(self._buf) > 262144:  # never grow without bound on a noisy line
            self._buf = self._buf[-131072:]
        return replies

    def drain(self) -> None:
        """Drop stale replies, keeping prints, so a late reply cannot answer a new request."""
        while self._pump():
            pass

    def request(self, payload: bytes, *, timeout: float = 3.0, config_write: bool = False) -> bytes:
        """Send one packet and return the first reply with the same command id."""
        self.drain()
        self.send(payload, config_write=config_write)
        t0 = self.clock()
        while self.clock() - t0 < timeout:
            for pl in self._pump():
                if pl[0] == payload[0]:
                    return pl
        raise CommsError(f"no reply to command {payload[0]} within {timeout:g} s")

    def collect_prints(self, seconds: float) -> list[str]:
        """Listen for this long and return the print messages that arrived."""
        t0 = self.clock()
        while self.clock() - t0 < seconds:
            self._pump()
        return self.take_prints()

    def take_prints(self) -> list[str]:
        """Return the print messages received so far, and forget them."""
        out = list(self.prints)
        self.prints.clear()
        return out
