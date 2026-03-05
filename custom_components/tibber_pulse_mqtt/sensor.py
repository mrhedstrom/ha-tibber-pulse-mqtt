from __future__ import annotations

import logging
import asyncio
from typing import Any, Dict

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.entity import DeviceInfo, async_generate_entity_id
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .obis.full_db import obis_meta

_LOGGER = logging.getLogger(__name__)

# --- Unit conversion: OBIS -> target (full_db.py) ---
UNIT_SCALE = {
    ("kWh",  "Wh"):    1000.0,
    ("Wh",   "kWh"):   0.001,
    ("kW",   "W"):     1000.0,
    ("W",    "kW"):    0.001,
    ("kVArh","VArh"):  1000.0,
    ("VArh", "kVArh"): 0.001,
    ("kVAr", "VAr"):   1000.0,
    ("VAr",  "kVAr"):  0.001,
}

def convert_unit_value(value, raw_unit: str | None, target_unit: str | None):
    """Convert a numeric value from its raw OBIS unit into the desired target unit if known."""
    if not isinstance(value, (int, float)):
        return value
    if not raw_unit or not target_unit or raw_unit == target_unit:
        return value
    factor = UNIT_SCALE.get((raw_unit, target_unit))
    return value * factor if factor is not None else value


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
):
    """Home Assistant platform bootstrap for Tibber Local sensors."""
    hub = hass.data[DOMAIN][entry.entry_id]
    manager = SensorManager(hass, entry, async_add_entities)
    hub.sensor_manager = manager

    _LOGGER.info("Tibber Local sensor platform initialized")


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry
):
    """Platform-level unload hook (optional). Entities are handled by HA."""
    hub = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if hub and hasattr(hub, "sensor_manager"):
        # Nothing to explicitly unload in SensorManager right now
        pass
    return True


class SensorManager:
    """Creates and updates Tibber sensors in a race-safe and idempotent way."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
        self.hass = hass
        self.entry = entry
        self.async_add_entities = async_add_entities

        # Local cache of entity instances by unique_id
        self._entities: dict[str, TibberSensor] = {}

        # Translation and unit caches
        self._obis_units: dict[str, str] = {}  # OBIS code -> last-seen raw unit

        # Meter serial (0-0:96.1.1) per device (pulse_id)
        self._meter_ids: Dict[str, str] = {}

        # Creation guard (prevents duplicate creation in races)
        self._creating: set[str] = set()
        self._create_lock = asyncio.Lock()

    def set_obis_units(self, units_map: dict[str, str] | None):
        """Set last-seen raw unit mapping per OBIS code (used for conversion)."""
        self._obis_units = units_map or {}

    async def add_or_update(
        self,
        dev_id: str,           # pulse_id
        obis_code: str,
        value: Any,
        status: Dict[str, Any] | None
    ):
        """
        Add or update one OBIS sensor bound to a canonical device (pulse_id).
        Robust against user-renamed entity_ids.
        """
        unique_id = f"tibber_{dev_id}_{obis_code.replace(':','_').replace('.','_')}"
        raw_unit = self._obis_units.get(obis_code) if hasattr(self, "_obis_units") else None

        # Entity Registry lookup
        reg = er.async_get(self.hass)
        er_entry_entity_id = reg.async_get_entity_id("sensor", DOMAIN, unique_id)

        # Runtime instance lookup
        ent = self._entities.get(unique_id)

        # If entity exists in registry but not in runtime, recreate it
        if ent is None and er_entry_entity_id:
            meta = obis_meta.get(obis_code, {})
            ent = TibberSensor(
                unique_id=unique_id,
                dev_id=dev_id,
                obis_code=obis_code,
                meta=meta,
                status=status or {}
            )

            self._entities[unique_id] = ent
            # HA will automatically attach entity_id via registry
            self.async_add_entities([ent])

        # Compute scaled value (needed for both new + existing entities)
        if ent:
            target_unit = ent.meta.get("unit")
        else:
            target_unit = obis_meta.get(obis_code, {}).get("unit")

        scaled_value = convert_unit_value(value, raw_unit, target_unit)

        # Create brand-new entity
        if ent is None:
            async with self._create_lock:
                # Double-check after locking
                if unique_id in self._entities:
                    ent = self._entities[unique_id]
                else:
                    meta = obis_meta.get(obis_code, {})

                    ent = TibberSensor(
                        unique_id=unique_id,
                        dev_id=dev_id,
                        obis_code=obis_code,
                        meta=meta,
                        status=status or {}
                    )
                    ent._state = scaled_value

                    self._entities[unique_id] = ent

                    if er_entry_entity_id:
                        # HA will reconnect by unique_id
                        self.async_add_entities([ent])
                        return

                    # First-time creation, generate suggested entity_id
                    suggested_object_id = unique_id
                    ent.entity_id = async_generate_entity_id(
                        "sensor.{}",
                        suggested_object_id,
                        hass=self.hass
                    )

                    self.async_add_entities([ent])
                # After creation, fall through and update its state below

        # Update entity
        ent.set_status(status or {})
        ent.set_state(scaled_value)

    def update_status_for_device(self, dev_id: str, status: Dict[str, Any]):
        """Propagate status attributes to all entities of the device (dev_id = pulse_id)."""
        for ent in self._entities.values():
            if getattr(ent, "_dev_id", None) == dev_id:
                ent.set_status(status)

    def update_meter_id_for_device(self, pulse_id: str, meter_id: str):
        """
        Set the meter serial (OBIS 0-0:96.1.1) for the device and propagate to its entities.
        """
        self._meter_ids[pulse_id] = meter_id
        for ent in self._entities.values():
            if getattr(ent, "_dev_id", None) == pulse_id:
                ent.set_meter_id(meter_id)


class TibberSensor(SensorEntity):
    _attr_should_poll = False

    def __init__(
        self,
        unique_id: str,
        dev_id: str,     # pulse_id
        obis_code: str,
        meta: Dict[str, Any],
        status: Dict[str, Any]
    ):
        self._attr_unique_id = unique_id
        self.has_entity_name = True
        self._attr_translation_key = obis_code.replace(':','_').replace('.','_')

        self._dev_id = dev_id          # Canonical device key = pulse_id
        self._obis = obis_code
        self._status = status or {}

        self.meta = meta or {}

        self._state = None
        self._meter_id = None
        self._added_to_hass = False

        # Target unit from OBIS metadata
        unit = self.meta.get("unit")
        if unit:
            self._attr_native_unit_of_measurement = unit

        # UI precision
        display_precision = self.meta.get("display_precision")
        if display_precision is not None:
            self._attr_suggested_display_precision = display_precision

        if "device_class" in self.meta:
            self._attr_device_class = self.meta["device_class"]
        if "state_class" in self.meta:
            self._attr_state_class = self.meta["state_class"]

    @property
    def device_info(self) -> DeviceInfo:
        """Return info for the parent device (keyed by pulse_id)."""
        identifiers = {(f"tibber_pulse_{self._dev_id}",)}

        model = (self._status or {}).get("hwmodel") or "Pulse"
        sw = (self._status or {}).get("Build")
        name = f"Tibber {model} ({self._dev_id})"

        di: Dict[str, Any] = {
            "identifiers": identifiers,
            "manufacturer": "Tibber",
            "model": model,
            "name": name,
            "sw_version": sw,
        }

        # Optional: expose meter serial (0-0:96.1.1) as a "connection"
        if self._meter_id:
            di["connections"] = {("meter_serial", str(self._meter_id))}

        return DeviceInfo(**di)

    @property
    def native_value(self):
        return self._state

    @property
    def extra_state_attributes(self):
        """Only expose a constrained set of status fields as entity attributes."""
        if not self._status:
            return None
        allowed = [
            "hwmodel", "rssi", "ssid", "Build", "Hw", "ID", "IP", "Uptime",
            "baud", "ntc", "dsmr", "heap"
        ]
        return {k: v for k, v in self._status.items() if k in allowed}

    def _schedule_state_write(self):
        """Schedule async_write_ha_state on the HA event loop thread-safely."""
        if getattr(self, "hass", None):
            loop = self.hass.loop
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            # If already on the HA loop, write immediately; else schedule thread-safe
            if running_loop is loop:
                try:
                    self.async_write_ha_state()
                except Exception:
                    pass
            else:
                try:
                    loop.call_soon_threadsafe(self.async_write_ha_state)
                except Exception:
                    # As a last resort, ignore; HA will refresh soon anyway
                    pass

    def set_state(self, value: Any):
        """Set internal state; write only after entity is added to HA, on the HA loop."""
        self._state = value
        if getattr(self, "_added_to_hass", False):
            self._schedule_state_write()

    def set_status(self, status: Dict[str, Any]):
        """Set internal status; write only after entity is added to HA, on the HA loop."""
        self._status = status or {}
        if getattr(self, "_added_to_hass", False):
            self._schedule_state_write()

    def set_meter_id(self, meter_id: str):
        """Set meter serial (0-0:96.1.1); write only after entity is added to HA, on the HA loop."""
        self._meter_id = meter_id
        if getattr(self, "_added_to_hass", False):
            self._schedule_state_write()

    async def async_added_to_hass(self):
        """Mark entity as added and perform the first write if state is buffered."""
        self._added_to_hass = True
        if self._state is not None:
            # We are on the HA loop here; safe to call directly
            try:
                self.async_write_ha_state()
            except Exception:
                pass