import re
from typing import Dict, Any

# Matches e.g. 1-0:1.8.0(12345.678*kWh) or 1-0:1.7.0(1234.5)
_OBIS_ANY = re.compile(
    r"(?P<code>\d+-\d+:\d+\.\d+\.\d+)\((?P<val>[^\)*]+)(?:\*(?P<unit>[^)]+))?\)",
    re.MULTILINE
)

def parse_obis_text(text: str) -> Dict[str, Any]:
    """
    Parse OBIS text into { code: value }, where value is float if possible, else string.
    Also returns a "_units": { code: unit } map when units are present.
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