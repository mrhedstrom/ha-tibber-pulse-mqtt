import re
from typing import Dict, Any

_OBIS_ANY = re.compile(
    r"(?P<code>\d+-\d+:\d+\.\d+\.\d+)\("
    r"(?P<val>[0-9]+(?:[.,][0-9]+)?)"
    r"(?:\*(?P<unit>[^)]+))?"
    r"\)",
    re.MULTILINE
)

def parse_obis_text(text: str) -> Dict[str, Any]:
    """
    Parse OBIS telegram text into { code: value } with strict numeric parsing.
    Returns a "_units": { code: unit } map when units are present.

    Notes:
    - Decimal comma is converted to dot.
    - If a value cannot be parsed as float (rare with this regex), the raw string
      is returned for diagnostics, but downstream sanity filters in sensor.py will
      prevent spikes from reaching entity state.
    """
    values: Dict[str, Any] = {}
    units: Dict[str, str] = {}

    for m in _OBIS_ANY.finditer(text):
        code = m.group("code")
        raw = (m.group("val") or "").strip()
        unit = (m.group("unit") or "").strip() or None

        # fix number format
        if "," in raw and "." not in raw:
            raw = raw.replace(",", ".")

        try:
            values[code] = float(raw)
        except ValueError:
            values[code] = raw

        if unit:
            units[code] = unit

    if units:
        values["_units"] = units

    return values