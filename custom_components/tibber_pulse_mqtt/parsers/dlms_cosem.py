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

# ---------------------------------------------------------------------------
# Positional (list-id) DLMS lists — meters that send a flat STRUCTURE of bare
# values WITHOUT embedded OBIS codes or scaler/unit (e.g. Norwegian Kamstrup).
#
# Registry format:
#   { list_id_prefix: { member_count: [ (index, obis_or_"skip", kind, scale?), ... ] } }
#
# kind is one of:
#   "str"                    -> emit the octet-string value as a string
#   "<unit>" with a scale    -> emit numeric value * scale, unit "<unit>"
#   "skip"                   -> ignore this member (e.g. the list id itself)
#
# Add new meters by extending this registry; no parser changes required.
# ---------------------------------------------------------------------------
KAMSTRUP_LISTS: Dict[str, Dict[int, list]] = {
    # Kamstrup (Norwegian HAN, list id "KFM_001")
    "KFM": {
        # Short list: active power only
        1: [
            (0, "1-0:1.7.0", "W", 1.0),
        ],
        # Full list (13 members)
        13: [
            (0, None, "skip"),                 # list id "KFM_001"
            (1, "0-0:96.1.0", "str"),          # meter GS1 id
            (2, "0-0:96.1.7", "str"),          # meter type
            (3, "1-0:1.7.0", "W", 1.0),        # active power +
            (4, "1-0:2.7.0", "W", 1.0),        # active power -
            (5, "1-0:3.7.0", "VAr", 1.0),      # reactive power +
            (6, "1-0:4.7.0", "VAr", 1.0),      # reactive power -
            (7, "1-0:31.7.0", "A", 0.001),     # current L1 (mA)
            (8, "1-0:51.7.0", "A", 0.001),     # current L2 (mA)
            (9, "1-0:71.7.0", "A", 0.001),     # current L3 (mA)
            (10, "1-0:32.7.0", "V", 0.1),      # voltage L1
            (11, "1-0:52.7.0", "V", 0.1),      # voltage L2
            (12, "1-0:72.7.0", "V", 0.1),      # voltage L3
        ],
    },
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


def _dlms_app_start(blob: bytes) -> Optional[int]:
    """
    Validate HDLC + LLC and return the position of the first byte AFTER the
    invoke-id-and-priority and optional datetime, i.e. the container tag.
    Shared by the Aidon (ARRAY) and Kamstrup (STRUCTURE) decoders.
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
    else:
        pos += 1  # skip 0x00 absent marker

    if pos >= len(blob):
        return None
    return pos


def parse_dlms_kamstrup(blob: bytes) -> Optional[Dict[str, Any]]:
    """
    Parse a flat positional DLMS list (no embedded OBIS codes / scaler-unit),
    such as the Norwegian Kamstrup HAN list. Mapping is resolved from
    KAMSTRUP_LISTS using the leading list-id octet-string and member count.

    Returns {obis_code: value, "_units": {obis_code: unit_str}} or None.
    """
    pos = _dlms_app_start(blob)
    if pos is None:
        return None

    # Kamstrup uses a top-level STRUCTURE (not ARRAY)
    if blob[pos] != _TAG_STRUCTURE:
        return None
    pos += 1
    if pos >= len(blob):
        return None
    count = blob[pos]
    pos += 1

    # The first member is the list-id octet-string only on the long lists.
    # Short lists (e.g. Kamstrup count=1) carry no list-id, so we cannot key
    # off it — resolve those purely by member count instead.
    first = _read_octet_string(blob, pos)
    list_id = None
    if first is not None:
        try:
            list_id = first[0].decode("ascii")
        except Exception:
            list_id = None

    # Resolve mapping by list-id prefix + member count.
    mapping = None
    if list_id is not None:
        for prefix, by_count in KAMSTRUP_LISTS.items():
            if list_id.startswith(prefix):
                mapping = by_count.get(count)
                break
    else:
        # No list-id present: only accept an unambiguous count whose mapping
        # contains no octet-string ("str"/"skip") members.
        for by_count in KAMSTRUP_LISTS.values():
            candidate = by_count.get(count)
            if candidate and all(e[2] not in ("str", "skip") for e in candidate):
                mapping = candidate
                break
    if mapping is None:
        return None

    # Read exactly `count` members (never touch the trailing HDLC FCS + 0x7e)
    members: list = []
    p = pos
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

    for entry in mapping:
        idx = entry[0]
        obis = entry[1]
        kind = entry[2]
        if kind == "skip" or obis is None:
            continue
        if idx >= len(members):
            continue
        mtype, mval = members[idx]
        if kind == "str":
            if mtype == "str":
                try:
                    result[obis] = mval.decode("ascii", errors="ignore")
                except Exception:
                    result[obis] = mval.hex()
            continue
        # numeric with unit + scale
        if mtype != "num":
            continue
        scale = entry[3] if len(entry) > 3 else 1.0
        result[obis] = mval * scale if scale != 1.0 else mval
        if kind:
            units[obis] = kind

    if not result:
        return None

    result["_units"] = units
    return result


def parse_dlms(blob: bytes) -> Optional[Dict[str, Any]]:
    """
    Top-level DLMS dispatcher. Tries the Aidon-style embedded-OBIS ARRAY format
    first, then the positional (Kamstrup) STRUCTURE format.
    """
    obis = parse_dlms_cosem(blob)
    if obis:
        return obis
    return parse_dlms_kamstrup(blob)


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
            if val_tag == _TAG_OCTET_STRING:
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
