"""Minimal Home Assistant stand-ins so sensor.py can be imported without HA installed."""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    import homeassistant.components.sensor  # noqa: F401
except ImportError:

    def _module(name: str, **attrs) -> types.ModuleType:
        mod = types.ModuleType(name)
        mod.__path__ = []
        mod.__dict__.update(attrs)
        sys.modules[name] = mod
        return mod

    _module("homeassistant")
    _module("homeassistant.core", HomeAssistant=type("HomeAssistant", (), {}), callback=lambda f: f)
    _module("homeassistant.config_entries", ConfigEntry=type("ConfigEntry", (), {}))
    _module("homeassistant.components")
    _module(
        "homeassistant.components.sensor",
        SensorEntity=type(
            "SensorEntity", (), {"enabled": True, "async_write_ha_state": lambda self: None}
        ),
        SensorDeviceClass=types.SimpleNamespace(
            ENERGY="energy", POWER="power", VOLTAGE="voltage", CURRENT="current"
        ),
        SensorStateClass=types.SimpleNamespace(
            TOTAL_INCREASING="total_increasing", MEASUREMENT="measurement", TOTAL="total"
        ),
    )
    _module("homeassistant.helpers")
    _module(
        "homeassistant.helpers.entity",
        DeviceInfo=dict,
        async_generate_entity_id=lambda fmt, obj_id, hass: f"sensor.{obj_id}",
    )
    _module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
    _module(
        "homeassistant.helpers.entity_registry",
        async_get=lambda hass: types.SimpleNamespace(async_get_entity_id=lambda *args: None),
    )
