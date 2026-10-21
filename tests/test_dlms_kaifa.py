"""Kaifa (Norwegian HAN, list id "KFM_001") positional list decoding."""
import struct

import pytest

from custom_components.tibber_pulse_mqtt.parsers.dlms_cosem import parse_dlms

_CLOCK = bytes.fromhex("07e60a150512000000ff8000")


def _octet(value: bytes) -> bytes:
    return bytes([0x09, len(value)]) + value


def _u32(value: int) -> bytes:
    return b"\x06" + struct.pack(">I", value)


def _kaifa_frame(members: list[bytes]) -> bytes:
    """Wrap positional members in a Kaifa HDLC DataNotification frame (FCS not checked)."""
    apdu = (
        b"\xe6\xe7\x00\x0f" + b"\x40\x00\x00\x00" + _octet(_CLOCK)
        + bytes([0x02, len(members)]) + b"".join(members)
    )
    body = b"\x01\x02\x01\x10" + b"\x00\x00" + apdu + b"\x00\x00"
    return b"\x7e" + bytes([0xA0, len(body) + 2]) + body + b"\x7e"


def test_kaifa_hourly_list_decodes_energy_registers():
    frame = _kaifa_frame([
        _octet(b"KFM_001"),
        _octet(b"6970631401753985"),
        _octet(b"MA304H3E"),
        _u32(1703),        # P+ W
        _u32(0),           # P- W
        _u32(0),           # Q+ VAr
        _u32(860),         # Q- VAr
        _u32(3116),        # I L1 mA
        _u32(2020),        # I L2 mA
        _u32(2975),        # I L3 mA
        _u32(2332),        # U L1 0.1 V
        _u32(0),           # U L2 0.1 V
        _u32(2334),        # U L3 0.1 V
        _octet(_CLOCK),
        _u32(163728),      # A+ Wh
        _u32(0),           # A- Wh
        _u32(14909),       # R+ VArh
        _u32(0),           # R- VArh
    ])

    result = parse_dlms(frame)

    assert result is not None
    assert result["0-0:96.1.7"] == "MA304H3E"
    assert result["1-0:1.7.0"] == 1703
    assert result["1-0:31.7.0"] == pytest.approx(3.116)
    assert result["1-0:32.7.0"] == pytest.approx(233.2)
    assert result["1-0:1.8.0"] == 163728
    assert result["1-0:2.8.0"] == 0
    assert result["1-0:3.8.0"] == 14909
    assert result["1-0:4.8.0"] == 0
    assert "0-0:1.0.0" not in result
    assert result["_units"]["1-0:1.8.0"] == "Wh"
    assert result["_units"]["1-0:3.8.0"] == "VArh"
