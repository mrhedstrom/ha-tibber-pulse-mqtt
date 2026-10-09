from __future__ import annotations

import sys
import types
import os

# Add root directory to sys.path so custom_components can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# If homeassistant is not installed in the current environment, provide lightweight shims
try:
    import homeassistant
    import homeassistant.components.sensor
except ImportError:
    ha = types.ModuleType("homeassistant")
    ha.__path__ = []

    ha_const = types.ModuleType("homeassistant.const")
    ha_const.EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"

    ha_core = types.ModuleType("homeassistant.core")
    ha_core.HomeAssistant = type("HomeAssistant", (), {})
    ha_core.callback = lambda f: f

    ha_config = types.ModuleType("homeassistant.config_entries")
    ha_config.ConfigEntry = type("ConfigEntry", (), {})

    ha_components = types.ModuleType("homeassistant.components")
    ha_components.__path__ = []
    ha_components.mqtt = types.ModuleType("homeassistant.components.mqtt")

    ha_sensor = types.ModuleType("homeassistant.components.sensor")
    ha_sensor.SensorEntity = type(
        "SensorEntity",
        (),
        {
            "enabled": True,
            "async_write_ha_state": lambda self: None,
        },
    )
    ha_sensor.SensorDeviceClass = types.SimpleNamespace(
        ENERGY="energy",
        POWER="power",
        VOLTAGE="voltage",
        CURRENT="current",
    )
    ha_sensor.SensorStateClass = types.SimpleNamespace(
        TOTAL_INCREASING="total_increasing",
        MEASUREMENT="measurement",
        TOTAL="total",
    )

    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_helpers.__path__ = []
    ha_helpers.config_validation = types.ModuleType("homeassistant.helpers.config_validation")
    ha_helpers.config_validation.platform_only_config_schema = lambda d: None
    ha_helpers.typing = types.ModuleType("homeassistant.helpers.typing")
    ha_helpers.typing.ConfigType = dict

    ha_helpers_entity = types.ModuleType("homeassistant.helpers.entity")
    ha_helpers_entity.DeviceInfo = dict
    ha_helpers_entity.async_generate_entity_id = lambda fmt, obj_id, hass: f"sensor.{obj_id}"

    ha_helpers_plat = types.ModuleType("homeassistant.helpers.entity_platform")
    ha_helpers_plat.AddEntitiesCallback = object

    ha_helpers_er = types.ModuleType("homeassistant.helpers.entity_registry")
    ha_helpers_er.async_get = lambda hass: types.SimpleNamespace(
        async_get_entity_id=lambda *args: None
    )

    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.const"] = ha_const
    sys.modules["homeassistant.core"] = ha_core
    sys.modules["homeassistant.config_entries"] = ha_config
    sys.modules["homeassistant.components"] = ha_components
    sys.modules["homeassistant.components.mqtt"] = ha_components.mqtt
    sys.modules["homeassistant.components.sensor"] = ha_sensor
    sys.modules["homeassistant.helpers"] = ha_helpers
    sys.modules["homeassistant.helpers.config_validation"] = ha_helpers.config_validation
    sys.modules["homeassistant.helpers.typing"] = ha_helpers.typing
    sys.modules["homeassistant.helpers.entity"] = ha_helpers_entity
    sys.modules["homeassistant.helpers.entity_platform"] = ha_helpers_plat
    sys.modules["homeassistant.helpers.entity_registry"] = ha_helpers_er

# Provide mock for paho.mqtt if missing
try:
    import paho.mqtt.client
except ImportError:
    paho = types.ModuleType("paho")
    paho.__path__ = []
    paho_mqtt = types.ModuleType("paho.mqtt")
    paho_mqtt.__path__ = []
    paho_client = types.ModuleType("paho.mqtt.client")
    paho_client.Client = type("Client", (), {})

    sys.modules["paho"] = paho
    sys.modules["paho.mqtt"] = paho_mqtt
    sys.modules["paho.mqtt.client"] = paho_client
