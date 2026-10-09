from __future__ import annotations

import math
import pytest
from homeassistant.components.sensor import SensorStateClass
from custom_components.tibber_pulse_mqtt.sensor import (
    TibberSensor,
    SensorManager,
    convert_unit_value,
)
from custom_components.tibber_pulse_mqtt.obis.full_db import obis_meta


class TestTibberSensor:
    def test_is_cumulative_identification(self):
        """Verify cumulative energy registers vs instantaneous measurement registers."""
        # 1.8.0 active energy import
        sensor_import = TibberSensor(
            "test_1_8_0", "pulse1", "1-0:1.8.0", obis_meta["1-0:1.8.0"], {}
        )
        assert sensor_import.is_cumulative is True

        # 2.8.0 active energy export
        sensor_export = TibberSensor(
            "test_2_8_0", "pulse1", "1-0:2.8.0", obis_meta["1-0:2.8.0"], {}
        )
        assert sensor_export.is_cumulative is True

        # 3.8.0 reactive energy
        sensor_reactive = TibberSensor(
            "test_3_8_0", "pulse1", "1-0:3.8.0", obis_meta["1-0:3.8.0"], {}
        )
        assert sensor_reactive.is_cumulative is True

        # Unmapped register without TOTAL_INCREASING metadata is not cumulative
        sensor_unmapped = TibberSensor(
            "test_unmapped", "pulse1", "1-0:1.8.3", {}, {}
        )
        assert sensor_unmapped.is_cumulative is False

        # Register explicitly configured with TOTAL_INCREASING is cumulative
        sensor_custom = TibberSensor(
            "test_custom", "pulse1", "1-0:1.8.3", {"state_class": SensorStateClass.TOTAL_INCREASING}, {}
        )
        assert sensor_custom.is_cumulative is True

        # 1.7.0 active power (measurement, not cumulative)
        sensor_power = TibberSensor(
            "test_1_7_0", "pulse1", "1-0:1.7.0", obis_meta["1-0:1.7.0"], {}
        )
        assert sensor_power.is_cumulative is False

        # 32.7.0 voltage (measurement, not cumulative)
        sensor_voltage = TibberSensor(
            "test_32_7_0", "pulse1", "1-0:32.7.0", obis_meta["1-0:32.7.0"], {}
        )
        assert sensor_voltage.is_cumulative is False

    def test_cumulative_rejects_zero_and_negative(self):
        """Verify cumulative sensor ignores <= 0, None, NaN, and invalid readings."""
        sensor = TibberSensor(
            "test_1_8_0", "pulse1", "1-0:1.8.0", obis_meta["1-0:1.8.0"], {}
        )
        sensor.set_state(66413774.0)
        assert sensor.native_value == 66413774.0

        # Drop to 0.0 is ignored
        sensor.set_state(0.0)
        assert sensor.native_value == 66413774.0

        # Drop to negative is ignored
        sensor.set_state(-10.0)
        assert sensor.native_value == 66413774.0

        # Drop to None is ignored
        sensor.set_state(None)
        assert sensor.native_value == 66413774.0

        # Non-numeric string is ignored
        sensor.set_state("corrupt_value")
        assert sensor.native_value == 66413774.0

        # NaN is ignored
        sensor.set_state(float("nan"))
        assert sensor.native_value == 66413774.0
        sensor.set_state("nan")
        assert sensor.native_value == 66413774.0

        # Inf / -Inf is ignored
        sensor.set_state(float("inf"))
        assert sensor.native_value == 66413774.0
        sensor.set_state(float("-inf"))
        assert sensor.native_value == 66413774.0

    def test_cumulative_allows_decrease(self):
        """Verify cumulative sensor allows decreases (Option 1: prevents lockup on prior high reading)."""
        sensor = TibberSensor(
            "test_1_8_0", "pulse1", "1-0:1.8.0", obis_meta["1-0:1.8.0"], {}
        )
        sensor.set_state(66413774.0)
        assert sensor.native_value == 66413774.0

        # Decrease is allowed so that an earlier erroneously high reading does not permanently block updates
        sensor.set_state(66413.774)
        assert sensor.native_value == 66413.774

    def test_cumulative_initial_invalid_ignored(self):
        """Verify cumulative sensor initialized with 0.0, None, or NaN retains None initially."""
        sensor = TibberSensor(
            "test_1_8_0", "pulse1", "1-0:1.8.0", obis_meta["1-0:1.8.0"], {}
        )
        sensor.set_state(0.0)
        assert sensor.native_value is None

        sensor.set_state(float("nan"))
        assert sensor.native_value is None

        sensor.set_state(None)
        assert sensor.native_value is None

        sensor.set_state(100.0)
        assert sensor.native_value == 100.0

    def test_cumulative_allows_valid_increase(self):
        """Verify cumulative sensor accepts monotonically increasing values."""
        sensor = TibberSensor(
            "test_1_8_0", "pulse1", "1-0:1.8.0", obis_meta["1-0:1.8.0"], {}
        )
        sensor.set_state(66413774.0)
        sensor.set_state(66413785.0)
        assert sensor.native_value == 66413785.0

    def test_non_cumulative_allows_zero_and_decrease(self):
        """Verify instantaneous sensors (e.g. power) can legitimately drop to 0 and decrease."""
        sensor = TibberSensor(
            "test_1_7_0", "pulse1", "1-0:1.7.0", obis_meta["1-0:1.7.0"], {}
        )
        sensor.set_state(500.0)
        assert sensor.native_value == 500.0

        # Active power drops to 0 W (e.g. solar exports all consumption)
        sensor.set_state(0.0)
        assert sensor.native_value == 0.0

        # Active power drops from higher to lower
        sensor.set_state(350.0)
        assert sensor.native_value == 350.0
        sensor.set_state(120.0)
        assert sensor.native_value == 120.0


class TestUnitConversion:
    def test_scale_factors(self):
        """Verify unit scaling for common energy and power conversions."""
        assert convert_unit_value(12.5, "kWh", "Wh") == 12500.0
        assert convert_unit_value(12500.0, "Wh", "kWh") == 12.5
        assert convert_unit_value(3.2, "kW", "W") == 3200.0
        assert convert_unit_value(3200.0, "W", "kW") == 3.2
        assert convert_unit_value(500.0, "Wh", "Wh") == 500.0
        assert convert_unit_value(500.0, None, "Wh") == 500.0
        assert convert_unit_value("text", "kWh", "Wh") == "text"


class TestSensorManager:
    def test_set_obis_units_preserves_cache(self):
        """Verify unit mapping is updated and preserved rather than wiped."""
        manager = SensorManager(None, None, None)
        assert manager._obis_units == {}

        # Frame 1 delivers 1-0:1.8.0 unit
        manager.set_obis_units({"1-0:1.8.0": "kWh"})
        assert manager._obis_units["1-0:1.8.0"] == "kWh"

        # Frame 2 delivers empty unit map (partial frame)
        manager.set_obis_units({})
        assert manager._obis_units["1-0:1.8.0"] == "kWh"

        # Frame 3 delivers another register's unit
        manager.set_obis_units({"1-0:2.8.0": "kWh"})
        assert manager._obis_units["1-0:1.8.0"] == "kWh"
        assert manager._obis_units["1-0:2.8.0"] == "kWh"
