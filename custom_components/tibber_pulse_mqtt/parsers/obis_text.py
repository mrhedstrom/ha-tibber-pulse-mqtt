import re
from typing import Dict, Any

_OBIS_LINE = re.compile(r"(\d+-\d+:\d+\.\d+\.\d+)\(([^)]+)\)")

def parse_obis_text(text: str) -> Dict[str, Any]:
    """
    Parsing OBIS-text to { code: value } where value is float if possible, else string.
    """
    values: Dict[str, Any] = {}
    units: Dict[str, str] = {}

    for line in text.splitlines():
        m = _OBIS_LINE.search(line)
        if not m:
            continue

        code, raw = m.groups()

        if "*" in raw:
            val, unit = raw.split("*", 1)
            unit = unit.strip()
            if unit:
                units[code] = unit
        else:
            val, unit = raw, None

        val = val.strip()

        # fix number format
        if "," in val and "." not in val:
            val = val.replace(",", ".")

        try:
            f = float(val)
            values[code] = f
        except ValueError:
            values[code] = val

    if units:
        values["_units"] = units

    return values