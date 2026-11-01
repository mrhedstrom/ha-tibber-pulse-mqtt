import pytest

from custom_components.tibber_pulse_mqtt.parsers.dlms_cosem import (
    iter_dlms_frames_in_blob,
    obis_measurement_code,
    parse_dlms,
    parse_dlms_cosem,
)


def _octet_string(value):
    return bytes((0x09, len(value))) + value


def _obis(a, b, c, d, e):
    return _octet_string(bytes((a, b, c, d, e, 0xFF)))


def _visible_string(value):
    encoded = value.encode("ascii")
    return bytes((0x0A, len(encoded))) + encoded


def _uint32(value):
    return bytes((0x06,)) + value.to_bytes(4, "big")


def _uint16(value):
    return bytes((0x12,)) + value.to_bytes(2, "big")


def _frame(members):
    data = b"\x02" + bytes((len(members),)) + b"".join(members)
    return (
        b"\x7e\x00\xe6\xe7\x00\x0f\x00\x00\x00\x00"
        + b"\x0c"
        + b"\x00" * 12
        + data
    )


def _array_frame(obis, value):
    data = b"\x01\x01\x02\x02" + _obis(*obis) + _uint32(value)
    return (
        b"\x7e\x00\xe6\xe7\x00\x0f\x00\x00\x00\x00"
        + b"\x0c"
        + b"\x00" * 12
        + data
    )


def test_parse_kamstrup_v0001_flat_structure():
    members = [
        _octet_string(b"Kamstrup_V0001"),
        _obis(1, 1, 0, 0, 5),
        _visible_string("sample-id"),
        _obis(1, 1, 96, 1, 1),
        _visible_string("sample-meter"),
        _obis(1, 1, 1, 7, 0),
        _uint32(3574),
        _obis(1, 1, 31, 7, 0),
        _uint32(540),
        _obis(1, 1, 32, 7, 0),
        _uint16(230),
        _obis(1, 1, 1, 8, 0),
        _uint32(132915000),
        _obis(1, 1, 2, 8, 0),
        _uint32(250),
        _obis(1, 1, 3, 8, 0),
        _uint32(300),
        _obis(1, 1, 4, 8, 0),
        _uint32(40),
    ]

    frame = _frame(members)
    result = parse_dlms(frame)
    direct_result = parse_dlms_cosem(frame)

    expected = {
        "1-1:1.7.0": 3574.0,
        "1-1:31.7.0": 5.4,
        "1-1:32.7.0": 230.0,
        "1-1:1.8.0": 132915000.0,
        "1-1:2.8.0": 250.0,
        "1-1:3.8.0": 300.0,
        "1-1:4.8.0": 40.0,
        "_units": {
            "1-1:1.7.0": "W",
            "1-1:31.7.0": "A",
            "1-1:32.7.0": "V",
            "1-1:1.8.0": "Wh",
            "1-1:2.8.0": "Wh",
            "1-1:3.8.0": "VArh",
            "1-1:4.8.0": "VArh",
        },
    }
    assert result == expected
    assert direct_result == expected


@pytest.mark.parametrize("channel", [0, 1, 2])
def test_parse_two_member_array_numeric_value_uses_channel_independent_units(channel):
    obis_code = f"1-{channel}:1.7.0"
    result = parse_dlms_cosem(_array_frame((1, channel, 1, 7, 0), 3574))

    assert result == {
        obis_code: 3574.0,
        "_units": {obis_code: "W"},
    }


@pytest.mark.parametrize("channel", [0, 1, 2])
def test_flat_unit_and_implicit_scale_lookups_are_channel_independent(channel):
    current_code = f"1-{channel}:31.7.0"
    energy_code = f"1-{channel}:1.8.0"
    frame = _frame([
        _visible_string("Kamstrup_V0001"),
        _obis(1, channel, 31, 7, 0),
        _uint32(540),
        _obis(1, channel, 1, 8, 0),
        _uint32(1234),
    ])

    result = parse_dlms(frame)

    assert result[current_code] == 5.4
    assert result[energy_code] == 1234.0
    assert result["_units"][current_code] == "A"
    assert result["_units"][energy_code] == "Wh"
    assert obis_measurement_code(current_code) == "31.7.0"
    assert obis_measurement_code(energy_code) == "1.8.0"


def test_iter_dlms_frames_in_blob_finds_all_frames():
    first = _frame([
        _octet_string(b"Kamstrup_V0001"),
        _obis(1, 1, 1, 7, 0),
        _uint32(3574),
    ])
    second = _frame([
        _octet_string(b"Kamstrup_V0001"),
        _obis(1, 1, 1, 7, 0),
        _uint32(3590),
    ])
    blob = b"\x12" + bytes((len(first),)) + first
    blob += b"\x12" + bytes((len(second),)) + second

    assert list(iter_dlms_frames_in_blob(blob)) == [first, second]