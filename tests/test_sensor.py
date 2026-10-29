"""Regression tests for cumulative energy spikes after a Home Assistant restart.

Incident (2026-10-10 09:22:37, device cb8158ec4600, sensor 1-0:1.8.0):
the first OBIS frame after a restart was handled before the raw unit map was
populated. Without a unit, 66487.305 kWh was stored as 66487.305 Wh. When the
unit (kWh) arrived 38 ms later, the value jumped to 66487306 Wh, and the HA
recorder logged the whole lifetime reading (~66.4 MWh) as consumption.

Fixes covered here:
  1. Unit readiness guard: updates that need unit conversion are skipped until
     the raw unit is known (SensorManager.add_or_update).
  2. Unit cache persistence: partial frames no longer wipe known units
     (SensorManager.set_obis_units).
  3. Cumulative sanitising: TOTAL_INCREASING sensors ignore None, non-numeric,
     NaN, Inf and negative readings (TibberSensor.set_state).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.components.sensor import SensorStateClass
from homeassistant.helpers.entity import EntityCategory

from custom_components.tibber_pulse_mqtt import sensor as sensor_module
from custom_components.tibber_pulse_mqtt.obis.full_db import obis_meta
from custom_components.tibber_pulse_mqtt.sensor import SensorManager, TibberSensor

DEV = "cb8158ec4600"
IMPORT_UID = f"tibber_{DEV}_1-0_1_8_0"


def _sensor(obis: str, meta: dict | None = None) -> TibberSensor:
    return TibberSensor(f"test_{obis}", DEV, obis, obis_meta[obis] if meta is None else meta, {})


def _manager() -> SensorManager:
    hass = SimpleNamespace(
        data={},
        states=SimpleNamespace(async_available=lambda entity_id: True),
    )
    return SensorManager(hass, object(), lambda ents: None)


# ---------------------------------------------------------------------------
# 1. Unit readiness guard
# ---------------------------------------------------------------------------


class TestUnitGuard:
    @pytest.fixture(autouse=True)
    def _mock_entity_registry(self, monkeypatch):
        registry = SimpleNamespace(async_get_entity_id=lambda *args: None)
        monkeypatch.setattr(sensor_module.er, "async_get", lambda hass: registry)
        monkeypatch.setattr(
            sensor_module,
            "async_generate_entity_id",
            lambda entity_id_format, name, hass: entity_id_format.format(name),
        )

    def test_restart_unit_race_produces_no_spike(self):
        """Replay the incident with the values stored in the HA recorder database.

          09:22:37.714  frame handled before set_obis_units: 66487.305 (no unit)
          09:22:37.752  unit map known (kWh), next frame:     66487.306 kWh
          09:23:03      steady state:                         66487.307 kWh

        On the original code this fails with
        "unscaled value exposed: [66487.305, 66487306.0, 66487307.0]".
        """

        async def _run():
            manager = _manager()
            observed = []

            async def _frame(value):
                await manager.add_or_update(DEV, "1-0:1.8.0", value, {})
                ent = manager._entities.get(IMPORT_UID)
                if ent is not None and ent.native_value is not None:
                    observed.append(ent.native_value)

            await _frame(66487.305)
            manager.set_obis_units({"1-0:1.8.0": "kWh"})
            await _frame(66487.306)
            await _frame(66487.307)
            return observed

        observed = asyncio.run(_run())

        assert observed, "entity never received a value"
        assert all(v >= 66_000_000 for v in observed), f"unscaled value exposed: {observed}"
        steps = [b - a for a, b in zip(observed, observed[1:])]
        assert all(abs(s) < 1000 for s in steps), f"spike detected: {steps}"
        assert observed[-1] == pytest.approx(66487307.0)

    def test_new_entity_waits_for_unit(self):
        """No entity is created from a reading whose unit is still unknown."""

        async def _run():
            added = []
            manager = SensorManager(_manager().hass, object(), added.extend)

            await manager.add_or_update(DEV, "1-0:1.8.0", 66487.309, {})
            assert manager._entities == {} and added == []

            manager.set_obis_units({"1-0:1.8.0": "kWh"})
            await manager.add_or_update(DEV, "1-0:1.8.0", 66487.309, {})
            assert len(added) == 1
            assert manager._entities[IMPORT_UID].native_value == pytest.approx(66487309.0)

        asyncio.run(_run())

    def test_existing_entity_keeps_value_when_unit_missing(self):
        """A reading without a unit must not overwrite a correctly scaled value."""

        async def _run():
            manager = _manager()
            manager.set_obis_units({"1-0:1.8.0": "kWh"})
            await manager.add_or_update(DEV, "1-0:1.8.0", 66487.309, {})

            manager._obis_units.clear()
            await manager.add_or_update(DEV, "1-0:1.8.0", 66487.315, {})
            assert manager._entities[IMPORT_UID].native_value == pytest.approx(66487309.0)

        asyncio.run(_run())

    def test_registers_without_target_unit_are_not_blocked(self):
        """OBIS codes with no unit in full_db (e.g. meter id 0-0:96.1.1) update normally."""

        async def _run():
            manager = _manager()
            await manager.add_or_update(DEV, "0-0:96.13.0", "12345678", {})
            assert manager._entities[f"tibber_{DEV}_0-0_96_13_0"].native_value == "12345678"

        asyncio.run(_run())

    def test_1_1_channel_reuses_1_0_name_and_metadata(self):
        """Kamstrup 1-1 readings use the translated name and metadata for 1-0."""

        async def _run():
            manager = _manager()
            manager.set_obis_units({"1-1:1.7.0": "W"})
            await manager.add_or_update(DEV, "1-1:1.7.0", 3574.0, {})

            ent = manager._entities[f"tibber_{DEV}_1-1_1_7_0"]
            assert ent._attr_translation_key == "1-0_1_7_0"
            assert ent.meta == obis_meta["1-0:1.7.0"]
            assert ent.native_value == pytest.approx(3574.0)

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# 2. Unit cache persistence
# ---------------------------------------------------------------------------


class TestUnitCache:
    def test_partial_frames_do_not_wipe_known_units(self):
        """Previously set_obis_units replaced the map, so an empty frame dropped all units."""
        manager = SensorManager(None, None, None)

        manager.set_obis_units({"1-0:1.8.0": "kWh"})
        manager.set_obis_units({})
        manager.set_obis_units({"1-0:2.8.0": "kWh"})

        assert manager._obis_units == {"1-0:1.8.0": "kWh", "1-0:2.8.0": "kWh"}


# ---------------------------------------------------------------------------
# 3. Cumulative sanitising
# ---------------------------------------------------------------------------


class TestCumulativeSensor:
    def test_cumulative_is_defined_by_state_class_metadata(self):
        """Cumulative detection uses state_class from full_db, not OBIS code patterns."""
        assert _sensor("1-0:1.8.0").is_cumulative
        assert _sensor("1-0:2.8.0").is_cumulative
        assert _sensor("1-0:3.8.0").is_cumulative
        assert not _sensor("1-0:1.7.0").is_cumulative
        assert not _sensor("1-0:32.7.0").is_cumulative
        # Same OBIS code, decided only by metadata
        assert not _sensor("1-0:1.8.3", {}).is_cumulative
        assert _sensor("1-0:1.8.3", {"state_class": SensorStateClass.TOTAL_INCREASING}).is_cumulative

    @pytest.mark.parametrize(
        "bad", [-10.0, None, "corrupt_value", float("nan"), "nan", float("inf"), float("-inf")]
    )
    def test_invalid_readings_keep_last_value(self, bad):
        sensor = _sensor("1-0:1.8.0")
        sensor.set_state(66413774.0)
        sensor.set_state(bad)
        assert sensor.native_value == 66413774.0

    def test_invalid_first_reading_is_ignored(self):
        sensor = _sensor("1-0:1.8.0")
        for bad in (-5.0, float("nan"), None):
            sensor.set_state(bad)
            assert sensor.native_value is None
        sensor.set_state(100.0)
        assert sensor.native_value == 100.0

    def test_zero_is_valid(self):
        """Export counter 2.8.0 is legitimately 0 in homes without solar."""
        sensor = _sensor("1-0:2.8.0")
        sensor.set_state(0.0)
        assert sensor.native_value == 0.0

    def test_decrease_is_allowed(self):
        """Agreed in review: rejecting decreases would lock the sensor after one bad high reading."""
        sensor = _sensor("1-0:1.8.0")
        sensor.set_state(50000.0)
        sensor.set_state(45000.0)
        assert sensor.native_value == 45000.0

    def test_measurement_sensors_are_not_sanitised(self):
        """Power can drop to 0 and decrease freely; cumulative rules must not apply."""
        sensor = _sensor("1-0:1.7.0")
        for value in (500.0, 0.0, 350.0, 120.0):
            sensor.set_state(value)
            assert sensor.native_value == value

# ---------------------------------------------------------------------------
# 4. Attribute-only OBIS values
# ---------------------------------------------------------------------------

class TestAttributeOnlyObis:
    @pytest.fixture(autouse=True)
    def _mock_entity_registry(self, monkeypatch):
        registry = SimpleNamespace(async_get_entity_id=lambda *args: None)
        monkeypatch.setattr(sensor_module.er, "async_get", lambda hass: registry)
        monkeypatch.setattr(
            sensor_module,
            "async_generate_entity_id",
            lambda entity_id_format, name, hass: entity_id_format.format(name),
        )

    def test_attribute_only_value_creates_no_entity(self):
        """attribute_only OBIS values should be stored as attributes, not entities."""

        async def _run():
            manager = _manager()

            await manager.add_or_update(
                DEV,
                "0-0:1.0.0",
                "261023201220W",
                {},
            )

            assert manager._entities == {}
            assert manager._device_attributes[DEV]["meter_datetime"] == "261023201220W"

        asyncio.run(_run())

    def test_attribute_only_values_are_exposed_on_sensor_attributes(self):
        """Device attributes should be exposed on all entities."""

        async def _run():
            manager = _manager()

            manager.set_obis_units({"1-0:1.8.0": "kWh"})

            await manager.add_or_update(
                DEV,
                "1-0:1.8.0",
                100.0,
                {},
            )

            await manager.add_or_update(
                DEV,
                "0-0:1.0.0",
                "261023201220W",
                {},
            )

            sensor = manager._entities[IMPORT_UID]

            assert (
                sensor.extra_state_attributes["meter_datetime"]
                == "261023201220W"
            )

        asyncio.run(_run())

    def test_multiple_attribute_only_values_are_retained(self):
        """Multiple device attributes should coexist."""

        async def _run():
            manager = _manager()

            await manager.add_or_update(
                DEV,
                "0-0:96.1.1",
                "735999999999999999",
                {},
            )

            await manager.add_or_update(
                DEV,
                "0-0:96.1.7",
                "Kamstrup MA304H3E",
                {},
            )

            attrs = manager._device_attributes[DEV]

            assert attrs["meter_id"] == "735999999999999999"
            assert attrs["meter_type"] == "Kamstrup MA304H3E"

        asyncio.run(_run())