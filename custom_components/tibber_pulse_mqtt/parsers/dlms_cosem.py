from __future__ import annotations

import struct
from typing import Dict, Any, Iterable, Optional

from ..obis.full_db import flat_dlms_scales, get_positional_dlms_list, obis_meta
from .pulse_envelope import iter_len_delimited

_HDLC_FLAG = 0x7E
_LLC_HEADER = b'\xe6\xe7\x00'

_TAG_INT32 = 0x05
_TAG_UINT32 = 0x06
_TAG_OCTET_STRING = 0x09
_TAG_INT8 = 0x0F
_TAG_INT16 = 0x10
_TAG_UINT8 = 0x11
_TAG_UINT16 = 0x12
_TAG_INT64 = 0x14
_TAG_UINT64 = 0x15
_TAG_ENUM = 0x16
_TAG_FLOAT32 = 0x17
_TAG_FLOAT64 = 0x18

_TAG_ARRAY = 0x01
_TAG_STRUCTURE = 0x02
_TAG_DATANOTIFICATION = 0x0F
_TAG_VISIBLE_STRING = 0x0A

# IEC 62056-62 unit codes → HA unit strings (matches obis_meta in full_db.py)
_DLMS_UNITS: Dict[int, str] = {
    27: "W",
    28: "VA",
    29: "VAr",
    30: "Wh",
    31: "VAh",
    32: "VArh",
    33: "A",
    35: "V",
    36: "V",
    37: "V",
}

def obis_measurement_code(obis: str) -> str:
    """Return the OBIS measurement code without medium or channel fields."""
    return obis.split(":", 1)[1]


def _obis_metadata(obis_code: str) -> dict:
    """Return OBIS metadata, falling back to the channel-zero definition."""
    if obis_code in obis_meta:
        return obis_meta[obis_code]
    medium_channel, measurement = obis_code.split(":", 1)
    medium, _channel = medium_channel.split("-", 1)
    return obis_meta.get(f"{medium}-0:{measurement}", {})


def _scale_flat_value(obis_code: str, value: float) -> float:
    scale = flat_dlms_scales.get(obis_measurement_code(obis_code), 1.0)
    return value * scale


def _read_numeric(data: bytes, pos: int) -> Optional[tuple[float, int]]:
    """Read a typed numeric DLMS value at pos. Returns (value, new_pos) or None."""
    if pos >= len(data):
        return None
    tag = data[pos]
    pos += 1

    if tag == _TAG_INT8:
        if pos + 1 > len(data):
            return None
        return float(struct.unpack_from(">b", data, pos)[0]), pos + 1
    if tag in (_TAG_UINT8, _TAG_ENUM):
        if pos + 1 > len(data):
            return None
        return float(data[pos]), pos + 1
    if tag == _TAG_INT16:
        if pos + 2 > len(data):
            return None
        return float(struct.unpack_from(">h", data, pos)[0]), pos + 2
    if tag == _TAG_UINT16:
        if pos + 2 > len(data):
            return None
        return float(struct.unpack_from(">H", data, pos)[0]), pos + 2
    if tag == _TAG_INT32:
        if pos + 4 > len(data):
            return None
        return float(struct.unpack_from(">i", data, pos)[0]), pos + 4
    if tag == _TAG_UINT32:
        if pos + 4 > len(data):
            return None
        return float(struct.unpack_from(">I", data, pos)[0]), pos + 4
    if tag == _TAG_INT64:
        if pos + 8 > len(data):
            return None
        return float(struct.unpack_from(">q", data, pos)[0]), pos + 8
    if tag == _TAG_UINT64:
        if pos + 8 > len(data):
            return None
        return float(struct.unpack_from(">Q", data, pos)[0]), pos + 8
    if tag == _TAG_FLOAT32:
        if pos + 4 > len(data):
            return None
        return float(struct.unpack_from(">f", data, pos)[0]), pos + 4
    if tag == _TAG_FLOAT64:
        if pos + 8 > len(data):
            return None
        return float(struct.unpack_from(">d", data, pos)[0]), pos + 8
    return None


def _read_octet_string(data: bytes, pos: int) -> Optional[tuple[bytes, int]]:
    """Read a DLMS octet-string (0x09 len bytes...) at pos. Returns (value, new_pos)."""
    if pos >= len(data) or data[pos] != _TAG_OCTET_STRING:
        return None
    pos += 1
    if pos >= len(data):
        return None
    length = data[pos]
    pos += 1
    if pos + length > len(data):
        return None
    return data[pos:pos + length], pos + length


def _skip_dlms_value(data: bytes, pos: int) -> Optional[int]:
    """Skip a single DLMS value (numeric or octet-string) and return new pos."""
    if pos >= len(data):
        return None
    tag = data[pos]
    if tag == _TAG_OCTET_STRING:
        r = _read_octet_string(data, pos)
        return r[1] if r else None
    r = _read_numeric(data, pos)
    return r[1] if r else None


def _skip_element(data: bytes, pos: int) -> Optional[int]:
    """Skip one AXDR element and return the position after it."""
    if pos >= len(data):
        return None
    tag = data[pos]
    pos += 1
    sizes = {
        _TAG_INT8: 1, _TAG_UINT8: 1, _TAG_INT16: 2, _TAG_UINT16: 2,
        _TAG_INT32: 4, _TAG_UINT32: 4, _TAG_INT64: 8, _TAG_UINT64: 8,
        _TAG_ENUM: 1, _TAG_FLOAT32: 4, _TAG_FLOAT64: 8,
        0x0C: 12,
    }
    if tag in sizes:
        end = pos + sizes[tag]
        return end if end <= len(data) else None
    if tag in (_TAG_OCTET_STRING, _TAG_VISIBLE_STRING):
        if pos >= len(data):
            return None
        end = pos + 1 + data[pos]
        return end if end <= len(data) else None
    if tag in (_TAG_ARRAY, _TAG_STRUCTURE):
        if pos >= len(data):
            return None
        count = data[pos]
        pos += 1
        for _ in range(count):
            pos = _skip_element(data, pos)
            if pos is None:
                return None
        return pos
    return None


def _parse_flat_structure(data: bytes, pos: int) -> Optional[tuple[Dict[str, Any], Dict[str, str]]]:
    """Parse flat structures containing OBIS octet-strings and separate values."""
    if pos + 2 > len(data) or data[pos] != _TAG_STRUCTURE:
        return None
    count = data[pos + 1]
    pos += 2
    result: Dict[str, Any] = {}
    units: Dict[str, str] = {}

    members_read = 0
    while members_read < count:
        if pos >= len(data):
            return None
        if data[pos] == _TAG_OCTET_STRING and pos + 8 <= len(data) and data[pos + 1] == 6:
            a, b, c, d, e, _f = data[pos + 2:pos + 8]
            obis_code = f"{a}-{b}:{c}.{d}.{e}"
            pos += 8
            members_read += 1
            numeric = _read_numeric(data, pos)
            if numeric is not None:
                value, pos = numeric
                result[obis_code] = _scale_flat_value(obis_code, value)
                unit = _obis_metadata(obis_code).get("unit")
                if unit:
                    units[obis_code] = unit
                members_read += 1
                continue
        next_pos = _skip_element(data, pos)
        if next_pos is None:
            return None
        pos = next_pos
        members_read += 1

    return (result, units) if result else None


def _dlms_app_start(blob: bytes) -> Optional[int]:
    """
    Validate HDLC + LLC and return the position of the first byte AFTER the
    invoke-id-and-priority and optional datetime, i.e. the container tag.
    Shared by the array, embedded-OBIS structure, and positional-list decoders.
    """
    if not blob or blob[0] != _HDLC_FLAG:
        return None

    llc_pos = blob.find(_LLC_HEADER, 1, 30)
    if llc_pos < 0:
        return None

    pos = llc_pos + 3
    if pos >= len(blob) or blob[pos] != _TAG_DATANOTIFICATION:
        return None
    pos += 1

    # 4-byte Long-Invoke-Id-And-Priority
    pos += 4
    if pos >= len(blob):
        return None

    # Optional date-time
    if blob[pos] == _TAG_OCTET_STRING:
        pos += 1
        if pos >= len(blob):
            return None
        dt_len = blob[pos]
        pos += 1 + dt_len
    elif blob[pos] == 0x0C:
        pos += 1 + 12
    else:
        pos += 1  # skip 0x00 absent marker

    if pos >= len(blob):
        return None
    return pos


def _parse_positional_dlms(blob: bytes) -> Optional[Dict[str, Any]]:
    """Decode a registered positional DLMS layout without inferring unknown layouts."""
    pos = _dlms_app_start(blob)
    if pos is None:
        return None

    if blob[pos] != _TAG_STRUCTURE:
        return None
    if pos + 1 >= len(blob):
        return None
    count = blob[pos + 1]
    members_pos = pos + 2

    first = _read_octet_string(blob, members_pos)
    list_id = None
    if first is not None:
        try:
            list_id = first[0].decode("ascii")
        except Exception:
            return None
    mapping = get_positional_dlms_list(list_id, count)
    if mapping is None:
        return None

    members: list[tuple[str, Any]] = []
    p = members_pos
    for _ in range(count):
        if p >= len(blob):
            return None
        tag = blob[p]
        if tag == _TAG_OCTET_STRING:
            r = _read_octet_string(blob, p)
            if r is None:
                return None
            members.append(("str", r[0]))
            p = r[1]
        else:
            r = _read_numeric(blob, p)
            if r is None:
                return None
            members.append(("num", r[0]))
            p = r[1]

    result: Dict[str, Any] = {}
    units: Dict[str, str] = {}

    for index, obis in mapping["members"].items():
        if index >= len(members):
            continue
        value_type, value = members[index]
        metadata = _obis_metadata(obis)
        if metadata.get("attribute_only"):
            if value_type == "str":
                result[obis] = value.decode("ascii", errors="ignore")
            continue
        if value_type != "num":
            continue
        scale = mapping.get("scales", {}).get(index, 1.0)
        result[obis] = value * scale if scale != 1.0 else value
        if unit := metadata.get("unit"):
            units[obis] = unit

    if not result:
        return None

    result["_units"] = units
    return result


def parse_dlms_kamstrup(blob: bytes) -> Optional[Dict[str, Any]]:
    """Compatibility wrapper for the former positional-list parser."""
    return _parse_positional_dlms(blob)


def parse_dlms(blob: bytes) -> Optional[Dict[str, Any]]:
    """
    Detect the DLMS container format and decode its registered capabilities.
    """
    pos = _dlms_app_start(blob)
    if pos is None:
        return None
    if blob[pos] == _TAG_ARRAY:
        return parse_dlms_cosem(blob)
    if blob[pos] != _TAG_STRUCTURE:
        return None

    flat = _parse_flat_structure(blob, pos)
    if flat:
        result, units = flat
        result["_units"] = units
        return result
    return _parse_positional_dlms(blob)


def parse_dlms_cosem(blob: bytes) -> Optional[Dict[str, Any]]:
    """
    Parse an HDLC-framed DLMS/COSEM DataNotification with OBIS-coded values.
    Returns {obis_code: float, "_units": {obis_code: unit_str}} or None.
    """
    pos = _dlms_app_start(blob)
    if pos is None:
        return None

    if pos < len(blob) and blob[pos] == _TAG_STRUCTURE:
        flat = _parse_flat_structure(blob, pos)
        if flat:
            result, units = flat
            result["_units"] = units
            return result

    if pos >= len(blob) or blob[pos] != _TAG_ARRAY:
        return None
    pos += 1

    if pos >= len(blob):
        return None
    count = blob[pos]
    pos += 1

    result: Dict[str, Any] = {}
    units: Dict[str, str] = {}

    for _ in range(count):
        if pos >= len(blob) or blob[pos] != _TAG_STRUCTURE:
            break
        pos += 1

        if pos >= len(blob):
            break
        num_members = blob[pos]
        pos += 1

        if num_members < 2:
            break

        # Member 1: OBIS code — octet-string of exactly 6 bytes
        if pos >= len(blob) or blob[pos] != _TAG_OCTET_STRING:
            break
        pos += 1
        if pos >= len(blob):
            break
        obis_len = blob[pos]
        pos += 1
        if obis_len != 6 or pos + 6 > len(blob):
            break
        a, b, c, d, e, _f = blob[pos:pos + 6]
        obis_code = f"{a}-{b}:{c}.{d}.{e}"
        pos += 6

        # 2-member structure: second member is a non-numeric value (e.g. datetime)
        if num_members == 2:
            if pos >= len(blob):
                break
            numeric = _read_numeric(blob, pos)
            if numeric is not None:
                raw_val, pos = numeric
                result[obis_code] = raw_val
                unit = _obis_metadata(obis_code).get("unit")
                if unit:
                    units[obis_code] = unit
                continue

            val_tag = blob[pos]
            pos += 1
            if val_tag in (_TAG_OCTET_STRING, _TAG_VISIBLE_STRING):
                if pos >= len(blob):
                    break
                skip_len = blob[pos]
                pos += 1 + skip_len
            # skip and continue — we don't emit non-numeric values
            continue

        # Member 2: numeric value
        numeric = _read_numeric(blob, pos)
        if numeric is None:
            return None  # unrecognised type — give up rather than emit wrong data
        raw_val, pos = numeric

        # Member 3: scaler-unit structure
        if pos >= len(blob) or blob[pos] != _TAG_STRUCTURE:
            return None
        pos += 1
        if pos >= len(blob) or blob[pos] != 2:
            return None
        pos += 1

        # Scaler: signed Int8
        if pos >= len(blob) or blob[pos] != _TAG_INT8:
            return None
        pos += 1
        if pos >= len(blob):
            return None
        scaler = struct.unpack_from(">b", blob, pos)[0]
        pos += 1

        # Unit: Enum (unsigned byte)
        if pos >= len(blob) or blob[pos] != _TAG_ENUM:
            return None
        pos += 1
        if pos >= len(blob):
            return None
        unit_code = blob[pos]
        pos += 1

        actual = raw_val * (10.0 ** scaler) if scaler != 0 else raw_val
        result[obis_code] = actual
        unit_str = _DLMS_UNITS.get(unit_code)
        if unit_str:
            units[obis_code] = unit_str

    if not result:
        return None

    result["_units"] = units
    return result


def find_dlms_frame_in_blob(blob: bytes) -> Optional[bytes]:
    """
    Walk nested protobuf length-delimited fields looking for an HDLC frame (starts with 0x7E).
    Returns the frame bytes, or None if not found. No decompression is attempted.
    """
    return next(iter_dlms_frames_in_blob(blob), None)


def iter_dlms_frames_in_blob(blob: bytes) -> Iterable[bytes]:
    """Yield every unique nested length-delimited field that starts with HDLC."""
    seen: set[bytes] = set()
    for *_, field_bytes in iter_len_delimited(blob, 0, 4):
        if field_bytes and field_bytes[0] == _HDLC_FLAG and field_bytes not in seen:
            seen.add(field_bytes)
            yield field_bytes
