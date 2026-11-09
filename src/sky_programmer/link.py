"""The USB link. This is the only module that writes to the serial port."""
from __future__ import annotations

import glob
import os
import re
import sys
import time
from collections import deque

try:
    import fcntl
    import termios
except ImportError:  # Windows, where every COM port is exclusive anyway
    fcntl = termios = None

from . import protocol as p
from .errors import CommsError, SafetyRefusal
from .safety import check_packet

WRITE_TIMEOUT_S = 2.0  # a wedged USB link must become an error, never a hang

# pyserial raises the same error when its read finds nothing for two different reasons: the board was unplugged or
# reset, or another program read the bytes first. The bench showed both, so the message has to tell them apart.
_EMPTY_READ_SIGNS = ("multiple access", "returned no data")
SHARED_PORT_HINT = ("Another program is reading this port at the same time, so replies go missing. Close it and "
                    "connect again.")  # fmt: skip
UNSEEN_READER_HINT = ("The board stopped answering, but its port is still there. Either it reset or lost power, or "
                      "another program is reading the port: on Ubuntu, ModemManager tests a newly plugged-in board for "
                      "a few seconds. Wait a moment and connect again, and see packaging/linux/99-sky-programmer.rules "
                      "in the README to stop ModemManager for good.")  # fmt: skip


def _empty_read_reason(port: str) -> str:
    """Why a read found nothing, from what can be seen now."""
    if port and not os.path.exists(port):
        return "the board's USB connection was lost: it was unplugged, or the board lost power or reset."
    users = port_users(port) if port else []
    if users:
        return f"{SHARED_PORT_HINT} Open in: {'; '.join(users)}."
    return UNSEEN_READER_HINT


def _describe_process(pid: str) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            args = f.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        args = "?"
    return f"{args[:90]} (process {pid})"


def port_users(port: str) -> list[str]:
    """Other programs of this user that have the port open. Linux only, and processes of other users can't be seen."""
    if not sys.platform.startswith("linux"):
        return []
    try:
        target = os.path.realpath(port)
        pids = [pid for pid in os.listdir("/proc") if pid.isdigit() and int(pid) != os.getpid()]
    except OSError:
        return []
    users = []
    for pid in pids:
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                if os.path.realpath(f"/proc/{pid}/fd/{fd}") == target:
                    users.append(_describe_process(pid))
                    break
            except OSError:
                continue
    return users


def _port_number(name: str) -> int:
    found = re.search(r"\d+$", name)
    return int(found.group()) if found else -1


def list_ports() -> list[str]:
    """Ports that could be a board. On Windows that's USB COM ports only, so Bluetooth ones are never offered."""
    if sys.platform == "win32":
        from serial.tools import list_ports as ports

        return sorted((port.device for port in ports.comports() if port.vid is not None), key=_port_number)
    return sorted(glob.glob("/dev/ttyACM*"))


def find_port() -> str:
    ports = list_ports()
    if not ports:
        raise CommsError("no VESC found. Plug it in, or pass --port.")
    if len(ports) > 1:
        raise SafetyRefusal(f"more than one possible board ({', '.join(ports)}). Pick one with --port.")
    return ports[0]


def open_serial(port: str):
    import serial

    class ExclusiveSerial(serial.Serial):
        def close(self) -> None:
            # The kernel keeps this flag while anything else holds the port, which would block the next connection.
            if fcntl and self.is_open:
                try:
                    fcntl.ioctl(self.fileno(), termios.TIOCNXCL)
                except OSError:
                    pass
            super().close()

    # The lock below only stops programs that also lock, so look for any that already have the port open.
    users = port_users(port)
    if users:
        raise CommsError(f"{port} is already open in another program: {'; '.join(users)}. Close it, then connect "
                         "again: two programs reading one board each lose some of its replies.")  # fmt: skip
    try:
        ser = ExclusiveSerial(port, 115200, timeout=0.05, write_timeout=WRITE_TIMEOUT_S, exclusive=True)
        # exclusive= is only an advisory lock, so a program opening the port after us could still take the replies.
        if fcntl:
            fcntl.ioctl(ser.fileno(), termios.TIOCEXCL)
        return ser
    except (OSError, ValueError, serial.SerialException) as e:
        raise CommsError(f"cannot open {port}: {e}. Is VESC Tool still connected?") from e


class Link:
    def __init__(self, ser, clock=time.monotonic, sleep=time.sleep):
        self.ser, self.clock, self.sleep = ser, clock, sleep
        self.prints = deque(maxlen=2000)
        self._buf = b""

    def close(self) -> None:
        self.ser.close()

    def send(self, payload: bytes, *, config_write: bool = False, repl_allow_motor: bool = False,
             code_write: bool = False, code_stream: bool = False) -> None:
        """Send one packet, after check_packet() has allowed it."""
        check_packet(payload, config_write=config_write, repl_allow_motor=repl_allow_motor, code_write=code_write,
                     code_stream=code_stream)
        try:
            self.ser.write(p.frame(payload))
        except OSError as e:
            raise CommsError(f"USB write failed: {e}") from e

    def _pump(self):
        """Read what is available; return complete non-print packets. Prints go to self.prints."""
        try:
            self._buf += self.ser.read(4096)
        except OSError as e:
            if any(sign in str(e) for sign in _EMPTY_READ_SIGNS):
                raise CommsError(f"USB read failed: {_empty_read_reason(getattr(self.ser, 'port', None) or '')}") from e
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

    def request(self, payload: bytes, *, timeout: float = 3.0, config_write: bool = False, code_write: bool = False,
                code_stream: bool = False) -> bytes:
        """Send one packet and return the first reply with the same command id."""
        self.drain()
        self.send(payload, config_write=config_write, code_write=code_write, code_stream=code_stream)
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
