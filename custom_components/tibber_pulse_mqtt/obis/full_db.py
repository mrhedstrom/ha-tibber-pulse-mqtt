from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

# Common DSMR/OBIS subset. Extend as needed.
obis_meta = {
    # Energy (kWh)
    "1-0:1.8.0": {"unit": "Wh", "device_class": SensorDeviceClass.ENERGY, "state_class": SensorStateClass.TOTAL_INCREASING, "display_precision": 0, "sanity": {"monotonic": True}},
    "1-0:1.8.1": {"unit": "Wh", "device_class": SensorDeviceClass.ENERGY, "state_class": SensorStateClass.TOTAL_INCREASING, "display_precision": 0, "sanity": {"monotonic": True}},
    "1-0:1.8.2": {"unit": "Wh", "device_class": SensorDeviceClass.ENERGY, "state_class": SensorStateClass.TOTAL_INCREASING, "display_precision": 0, "sanity": {"monotonic": True}},
    "1-0:2.8.0": {"unit": "Wh", "device_class": SensorDeviceClass.ENERGY, "state_class": SensorStateClass.TOTAL_INCREASING, "display_precision": 0, "sanity": {"monotonic": True}},
    "1-0:2.8.1": {"unit": "Wh", "device_class": SensorDeviceClass.ENERGY, "state_class": SensorStateClass.TOTAL_INCREASING, "display_precision": 0, "sanity": {"monotonic": True}},
    "1-0:2.8.2": {"unit": "Wh", "device_class": SensorDeviceClass.ENERGY, "state_class": SensorStateClass.TOTAL_INCREASING, "display_precision": 0, "sanity": {"monotonic": True}},

    # Active power (kW)
    "1-0:1.7.0": {"unit": "W", "device_class": SensorDeviceClass.POWER, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0, "sanity": {"min": 0, "max": 35000, "drop_below": 5}},
    "1-0:2.7.0": {"unit": "W", "device_class": SensorDeviceClass.POWER, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0, "sanity": {"min": 0, "max": 35000, "drop_below": 5}},
    "1-0:21.7.0": {"unit": "W", "device_class": SensorDeviceClass.POWER, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0, "sanity": {"min": 0, "max": 15000, "drop_below": 5}},
    "1-0:22.7.0": {"unit": "W", "device_class": SensorDeviceClass.POWER, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0, "sanity": {"min": 0, "max": 15000, "drop_below": 5}},
    "1-0:41.7.0": {"unit": "W", "device_class": SensorDeviceClass.POWER, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0, "sanity": {"min": 0, "max": 15000, "drop_below": 5}},
    "1-0:42.7.0": {"unit": "W", "device_class": SensorDeviceClass.POWER, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0, "sanity": {"min": 0, "max": 15000, "drop_below": 5}},
    "1-0:61.7.0": {"unit": "W", "device_class": SensorDeviceClass.POWER, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0, "sanity": {"min": 0, "max": 15000, "drop_below": 5}},
    "1-0:62.7.0": {"unit": "W", "device_class": SensorDeviceClass.POWER, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0, "sanity": {"min": 0, "max": 15000, "drop_below": 5}},
    
    # Total reactive energi (kVArh) – kumulativa
    "1-0:3.8.0": {"unit": "VArh", "state_class": SensorStateClass.TOTAL_INCREASING, "display_precision": 0, "sanity": {"monotonic": True}},
    "1-0:4.8.0": {"unit": "VArh", "state_class": SensorStateClass.TOTAL_INCREASING, "display_precision": 0, "sanity": {"monotonic": True}},

    # Reactive power (kVAr)
    "1-0:3.7.0": {"unit": "VAr", "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0, "sanity": {"min": 0, "max": 35000, "drop_below": 5}},
    "1-0:4.7.0": {"unit": "VAr", "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0, "sanity": {"min": 0, "max": 35000, "drop_below": 5}},
    "1-0:23.7.0": {"unit": "VAr", "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0, "sanity": {"min": 0, "max": 15000, "drop_below": 5}},
    "1-0:24.7.0": {"unit": "VAr", "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0, "sanity": {"min": 0, "max": 15000, "drop_below": 5}},
    "1-0:43.7.0": {"unit": "VAr", "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0, "sanity": {"min": 0, "max": 15000, "drop_below": 5}},
    "1-0:44.7.0": {"unit": "VAr", "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0, "sanity": {"min": 0, "max": 15000, "drop_below": 5}},
    "1-0:63.7.0": {"unit": "VAr", "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0, "sanity": {"min": 0, "max": 15000, "drop_below": 5}},
    "1-0:64.7.0": {"unit": "VAr", "state_class": SensorStateClass.MEASUREMENT, "display_precision": 0, "sanity": {"min": 0, "max": 15000, "drop_below": 5}},

    # Voltage (V)
    "1-0:32.7.0": {"unit": "V", "device_class": SensorDeviceClass.VOLTAGE, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 1, "sanity": {"min": 180, "max": 260}},
    "1-0:52.7.0": {"unit": "V", "device_class": SensorDeviceClass.VOLTAGE, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 1, "sanity": {"min": 180, "max": 260}},
    "1-0:72.7.0": {"unit": "V", "device_class": SensorDeviceClass.VOLTAGE, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 1, "sanity": {"min": 180, "max": 260}},

    # Current (A)
    "1-0:31.7.0": {"unit": "A", "device_class": SensorDeviceClass.CURRENT, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 1, "sanity": {"min": 0, "max": 100, "drop_below": 0.05}},
    "1-0:51.7.0": {"unit": "A", "device_class": SensorDeviceClass.CURRENT, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 1, "sanity": {"min": 0, "max": 100, "drop_below": 0.05}},
    "1-0:71.7.0": {"unit": "A", "device_class": SensorDeviceClass.CURRENT, "state_class": SensorStateClass.MEASUREMENT, "display_precision": 1, "sanity": {"min": 0, "max": 100, "drop_below": 0.05}},

    # Frequency (Hz)
    "1-0:14.7.0": {"unit": "Hz", "state_class": SensorDeviceClass.FREQUENCY, "display_precision": 0, "sanity": {"min": 45.0, "max": 65.0}},

    # Identifiers & time
    "0-0:1.0.0": {},         # Date/time
    "0-0:96.1.0": {},        # Serial
    "0-0:96.1.1": {},        # Meter ID
    "0-0:42.0.0": {},        # Alt ID
    "0-0:96.13.0": {},       # Message text
    "0-0:96.13.1": {},       # Message code
}