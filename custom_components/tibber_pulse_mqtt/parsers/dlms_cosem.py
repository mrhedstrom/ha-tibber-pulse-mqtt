from __future__ import annotations

import struct
from typing import Dict, Any, Optional

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


def parse_dlms_cosem(blob: bytes) -> Optional[Dict[str, Any]]:
    """
    Parse an HDLC-framed DLMS/COSEM DataNotification (Aidon V2 and similar HAN meters).
    Returns {obis_code: float, "_units": {obis_code: unit_str}} or None.
    """
    if not blob or blob[0] != _HDLC_FLAG:
        return None

    # Locate LLC header (E6 E7 00) — always present in DLMS meters, within first 30 bytes
    llc_pos = blob.find(_LLC_HEADER, 1, 30)
    if llc_pos < 0:
        return None

    # DLMS application layer starts after LLC (3 bytes)
    pos = llc_pos + 3

    if pos >= len(blob) or blob[pos] != _TAG_DATANOTIFICATION:
        return None
    pos += 1

    # 4-byte Long-Invoke-Id-And-Priority
    pos += 4
    if pos >= len(blob):
        return None

    # Optional date-time: 0x09 = octet-string follows, 0x00 = absent
    if blob[pos] == _TAG_OCTET_STRING:
        pos += 1
        if pos >= len(blob):
            return None
        dt_len = blob[pos]
        pos += 1 + dt_len
    else:
        pos += 1  # skip 0x00 absent marker

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
    for *_, field_bytes in iter_len_delimited(blob, 0, 4):
        if field_bytes and field_bytes[0] == _HDLC_FLAG:
            return field_bytes
    return None
