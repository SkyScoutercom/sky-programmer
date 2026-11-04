import math
import struct

import pytest

from sky_programmer import protocol as p


def test_crc_is_xmodem():
    assert p.crc16(b"123456789") == 0x31C3
    assert p.crc16(b"") == 0


@pytest.mark.parametrize("n", [1, 255, 256, 1000])
def test_frame_round_trip(n):
    pl = bytes(i % 256 for i in range(n))
    f = p.frame(pl)
    assert p.frame_at(f, 0) == (pl, len(f))


def test_corrupted_frame_is_rejected():
    f = bytearray(p.frame(b"\x04hello"))
    f[3] ^= 0xFF
    assert p.frame_at(bytes(f), 0) is None


def test_short_frame_layout():
    f = p.frame(b"\x04")
    assert f[0] == 2 and f[1] == 1 and f[2] == 4 and f[-1] == 3
    assert struct.unpack(">H", f[3:5])[0] == p.crc16(b"\x04")


@pytest.mark.parametrize("x", [0.0, 1.0, -1.0, 0.5, 48.5, 4.97e-6, 0.0018, 140.0, -2.0, 1e-30, 35000.0])
def test_f32auto_round_trip(x):
    back = p.dec_f32auto(struct.unpack(">I", p.enc_f32auto(x))[0])
    assert math.isclose(back, struct.unpack(">f", struct.pack(">f", x))[0], rel_tol=1e-6)
