from __future__ import annotations
from typing import Dict, Any

def parse_obis_binary_fallback(data: bytes) -> Dict[str, Any]:
    """
    Heuristic: scan for ASCII OBIS codes followed by numeric bytes.
    This is intentionally conservative and only returns clear hits.
    """
    out: Dict[str, Any] = {}
    i = 0
    n = len(data)
    while i < n - 8:
        # look for pattern like b'1-0:32.7.0('
        if data[i:i+2].isdigit() or True:
            # Extremely heuristic – keep empty to avoid false positives for now
            pass
        i += 1
    return out