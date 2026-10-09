from __future__ import annotations

import pytest
from custom_components.tibber_pulse_mqtt.parsers.obis_text import parse_obis_text
from custom_components.tibber_pulse_mqtt.parsers.dlms_cosem import find_dlms_frame_in_blob


class TestObisTextParser:
    def test_parse_obis_standard_lines(self):
        """Verify OBIS ASCII telegram line parsing with units."""
        raw = """
/ISk5\\2MT382-1000

1-0:1.8.0(066413.774*kWh)
1-0:2.8.0(000000.000*kWh)
1-0:1.7.0(0000.485*kW)
0-0:96.1.1(4B414D0001020304)
!
"""
        result = parse_obis_text(raw)
        assert result["1-0:1.8.0"] == 66413.774
        assert result["1-0:2.8.0"] == 0.0
        assert result["1-0:1.7.0"] == 0.485
        assert "_units" in result
        assert result["_units"]["1-0:1.8.0"] == "kWh"
        assert result["_units"]["1-0:2.8.0"] == "kWh"
        assert result["_units"]["1-0:1.7.0"] == "kW"

    def test_parse_obis_comma_decimal(self):
        """Verify OBIS ASCII with comma as decimal separator."""
        raw = "1-0:1.8.0(012345,678*kWh)"
        result = parse_obis_text(raw)
        assert result["1-0:1.8.0"] == 12345.678
        assert result["_units"]["1-0:1.8.0"] == "kWh"


class TestDlmsCosemParser:
    def test_find_dlms_frame_not_found(self):
        """Verify find_dlms_frame_in_blob returns None for non-HDLC blob."""
        assert find_dlms_frame_in_blob(b"") is None
        assert find_dlms_frame_in_blob(b"arbitrary_non_proto_data") is None
