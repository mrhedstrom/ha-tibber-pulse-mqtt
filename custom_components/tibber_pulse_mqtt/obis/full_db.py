from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.helpers.entity import EntityCategory

# Common DSMR/OBIS subset. Extend as needed.
obis_meta = {
    # Energy (kWh)
    "1-0:1.8.0": {"unit": "Wh", "device_class": SensorDeviceClass.ENERGY, "state_class": SensorStateClass.TOTAL_INCREASING, "display_precision": 0},
    "1-0:1.8.1": {"unit": "Wh", "device_class": SensorDeviceClass.ENERGY, "state_class": SensorStateClass.TOTAL_INCREASING, "display_precision": 0},
    "1-0:1.8.2": {"unit": "Wh", "device_class": SensorDeviceClass.ENERGY, "state_class": SensorStateClass.TOTAL_INCREASING, "display_precision": 0},
    "1-0:2.8.0": {"unit": "Wh", "device_class": SensorDeviceClass.ENERGY, "state_class": SensorStateClass.TOTAL_INCREASING, "display_precision": 0},
    "1-0:2.8.1": {"unit": "Wh", "device_class": SensorDeviceClass.ENERGY, "state_class": SensorStateClass.TOTAL_INCREASING, "display_precision": 0},
    "1-0:2.8.2": {"unit": "Wh", "device_class": SensorDeviceClass.ENERGY, "state_class": SensorStateClass.TOTAL_INCREASING, "display_precision": 0},

    # Active power (kW)
    "1-0:1.7.0": {"unit": "W", "device_class": SensorDeviceClass.POWER, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0},
    "1-0:2.7.0": {"unit": "W", "device_class": SensorDeviceClass.POWER, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0},
    "1-0:21.7.0": {"unit": "W", "device_class": SensorDeviceClass.POWER, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0},
    "1-0:22.7.0": {"unit": "W", "device_class": SensorDeviceClass.POWER, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0},
    "1-0:41.7.0": {"unit": "W", "device_class": SensorDeviceClass.POWER, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0},
    "1-0:42.7.0": {"unit": "W", "device_class": SensorDeviceClass.POWER, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0},
    "1-0:61.7.0": {"unit": "W", "device_class": SensorDeviceClass.POWER, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0},
    "1-0:62.7.0": {"unit": "W", "device_class": SensorDeviceClass.POWER, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0},
    
    # Total reactive energi (kVArh) – kumulativa
    "1-0:3.8.0": {"unit": "VArh", "state_class": SensorStateClass.TOTAL_INCREASING, "display_precision": 0},
    "1-0:4.8.0": {"unit": "VArh", "state_class": SensorStateClass.TOTAL_INCREASING, "display_precision": 0},

    # Reactive power (kVAr)
    "1-0:3.7.0": {"unit": "VAr", "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0},
    "1-0:4.7.0": {"unit": "VAr", "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0},
    "1-0:23.7.0": {"unit": "VAr", "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0},
    "1-0:24.7.0": {"unit": "VAr", "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0},
    "1-0:43.7.0": {"unit": "VAr", "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0},
    "1-0:44.7.0": {"unit": "VAr", "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0},
    "1-0:63.7.0": {"unit": "VAr", "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0},
    "1-0:64.7.0": {"unit": "VAr", "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0},

    # Voltage (V)
    "1-0:32.7.0": {"unit": "V", "device_class": SensorDeviceClass.VOLTAGE, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 1},
    "1-0:52.7.0": {"unit": "V", "device_class": SensorDeviceClass.VOLTAGE, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 1},
    "1-0:72.7.0": {"unit": "V", "device_class": SensorDeviceClass.VOLTAGE, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 1},

    # Current (A)
    "1-0:31.7.0": {"unit": "A", "device_class": SensorDeviceClass.CURRENT, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 1},
    "1-0:51.7.0": {"unit": "A", "device_class": SensorDeviceClass.CURRENT, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 1},
    "1-0:71.7.0": {"unit": "A", "device_class": SensorDeviceClass.CURRENT, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 1},

    # Frequency (Hz)
    "1-0:14.7.0": {"unit": "Hz", "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0},

    # Identifiers & time
    "0-0:1.0.0": {"attribute_only": True, "attribute_name": "meter_datetime",},
    "0-0:96.1.0": {"attribute_only": True, "attribute_name": "meter_serial",},
    "0-0:96.1.1": {"attribute_only": True, "attribute_name": "meter_id",},
    "0-0:96.1.7": {"attribute_only": True, "attribute_name": "meter_type",},
    "0-0:42.0.0": {"attribute_only": True, "attribute_name": "alt_id",},

    # Messages
    "0-0:96.13.0": {},  # Message text
    "0-0:96.13.1": {},  # Message code
}

# DLMS format-specific value scales, keyed by OBIS measurement code.
flat_dlms_scales = {
    "31.7.0": 0.01,
    "51.7.0": 0.01,
    "71.7.0": 0.01,
}

# Positional DLMS list layouts. Entries reference OBIS definitions above; scales
# here are specific to the raw positional values and do not duplicate OBIS units.
positional_dlms_lists = [
    {
        "list_id": "KFM_001",
        "member_count": 13,
        "members": {
            1: "0-0:96.1.0", 2: "0-0:96.1.7", 3: "1-0:1.7.0",
            4: "1-0:2.7.0", 5: "1-0:3.7.0", 6: "1-0:4.7.0",
            7: "1-0:31.7.0", 8: "1-0:51.7.0", 9: "1-0:71.7.0",
            10: "1-0:32.7.0", 11: "1-0:52.7.0", 12: "1-0:72.7.0",
        },
        "scales": {7: 0.001, 8: 0.001, 9: 0.001, 10: 0.1, 11: 0.1, 12: 0.1},
    },
    {
        "list_id": "KFM_001",
        "member_count": 18,
        "members": {
            1: "0-0:96.1.0", 2: "0-0:96.1.7", 3: "1-0:1.7.0",
            4: "1-0:2.7.0", 5: "1-0:3.7.0", 6: "1-0:4.7.0",
            7: "1-0:31.7.0", 8: "1-0:51.7.0", 9: "1-0:71.7.0",
            10: "1-0:32.7.0", 11: "1-0:52.7.0", 12: "1-0:72.7.0",
            14: "1-0:1.8.0", 15: "1-0:2.8.0", 16: "1-0:3.8.0",
            17: "1-0:4.8.0",
        },
        "scales": {7: 0.001, 8: 0.001, 9: 0.001, 10: 0.1, 11: 0.1, 12: 0.1},
    },
    {"list_id": None, "member_count": 1, "members": {0: "1-0:1.7.0"}},
]


def get_positional_dlms_list(
    list_id: str | None, member_count: int
) -> dict[str, Any] | None:
    """Return an exact positional-list definition, if one is registered."""
    return next(
        (
            layout
            for layout in positional_dlms_lists
            if layout["list_id"] == list_id and layout["member_count"] == member_count
        ),
        None,
    )
