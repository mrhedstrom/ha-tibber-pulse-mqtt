---
paths:
  - "custom_components/tibber_pulse_mqtt/parsers/dlms_cosem.py"
---

# DLMS/COSEM parser rules

HAN Tibber devices commonly send DLMS/COSEM. Payloads differ between meter manufacturers and firmware versions.

- Never assume a specific meter manufacturer. Prefer capability detection over meter-specific logic.
- Validate every change against multiple real raw captures in `tests/fixtures/raw/`.
- Preserve unknown OBIS values. Do not discard, filter or remove unsupported OBIS values from parsed data, diagnostics or raw captures, except when sanitizing sensitive information or handling invalid/corrupted data.
- Responsibilities: decode DLMS/COSEM structures, detect payload formats, extract OBIS codes and values, return parsed measurements.
- The parser must not contain meter-specific OBIS mappings, positional register definitions, unit definitions, format-specific scaling metadata or manufacturer registries. These belong in `obis/full_db.py`. When supporting a new meter format, prefer adding metadata over parser logic.
