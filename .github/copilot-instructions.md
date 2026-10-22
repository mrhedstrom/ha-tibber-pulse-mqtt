# Copilot Instructions - Tibber Pulse MQTT

## Project Overview

This project is a Home Assistant custom integration for Tibber Pulse devices operating in MQTT mode.

Supported devices include:

- Tibber Pulse P1
- Tibber Pulse HAN
- Tibber Pulse KM

The integration receives compressed protobuf envelopes via MQTT, extracts OBIS data, and creates Home Assistant sensors dynamically.

This integration is intentionally independent of Tibber Cloud and can operate fully locally. Users may optionally bridge MQTT traffic externally to Tibber Cloud.

---

## Naming

### Repository

GitHub repository:

ha-tibber-pulse-mqtt

### Home Assistant domain

```python
DOMAIN = "tibber_pulse_mqtt"
```

### Integration Name

Tibber Pulse MQTT

### Avoid

Do not use:

- tibber_local
- tibber_pulse_p1_mqtt
- names containing "HA" in the Home Assistant domain

Reason:

- The integration supports Pulse P1, HAN and KM.
- Home Assistant domains should not contain "ha", "hass", or "homeassistant".

---

## Supported Architecture

Pulse Device  
↓  
MQTT  
↓  
mqtt_client.py  
↓  
dispatcher.py  
↓  
SensorManager  
↓  
Home Assistant Sensors

Important:

- Topic identifiers are NOT stable device identifiers.
- The Pulse ID from STATUS messages is the canonical device identifier.
- Device registry entries must use Pulse ID.

---

## MQTT Startup Requirements

The integration must not subscribe before the Home Assistant MQTT integration is ready.

manifest.json must contain:

```json
"after_dependencies": ["mqtt"]
```

Before subscribing:

```python
await mqtt.async_wait_for_mqtt_client(hass)
```

Never assume MQTT is ready during Home Assistant startup.

---

## MQTT Subscription Rules

Only one subscription may exist per hub instance.

Required pattern:

```python
if self._mqtt_unsub:
    return
```

When unloading:

```python
if self._mqtt_unsub:
    self._mqtt_unsub()
    self._mqtt_unsub = None
```

Never create duplicate subscriptions.

---

## Config Flow Reloading

Update listeners must only reload the config entry.

Correct:

```python
async def _update_listener(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)
```

Incorrect:

```python
await hub.async_stop()
await hass.config_entries.async_reload(...)
await hub.async_start()
```

Reason:

Home Assistant already performs unload + setup during reload.

---

## Translation Maintenance

Keep every supported locale in `custom_components/tibber_pulse_mqtt/translations/` updated whenever configuration options, labels, descriptions, or help text are added or changed. Update `strings.json` and all locale files together, including both config-flow and options-flow entries where applicable. Validate every translation JSON file after editing. When changing configuration help text, verify the source strings.json and every supported locale translation—not only README/changelog—are synchronized and contain no stale threshold values.

---

## Thread Safety

Home Assistant APIs must only be called from the event loop.

Never call:

```python
hass.async_create_task(...)
```

from worker threads.

Instead:

```python
hass.loop.call_soon_threadsafe(
    lambda: hass.async_create_task(...)
)
```
---

## State Updates

async_write_ha_state() must only execute on the Home Assistant event loop.

Preferred pattern:

```python
self.hass.loop.call_soon_threadsafe(
    self.async_write_ha_state
)
```

Never write state directly from executor threads.

---

## Sensor Creation

Entity creation must be race-safe.

Required protections:

### Creation lock

```python
self._create_lock = asyncio.Lock()
```

### Creation tracking

```python
self._creating = set()
```

### Double-check

After acquiring the lock:

```python
if unique_id in self._entities:
    return
```

---

## Entity Registry

Before creating a new entity:

```python
reg = er.async_get(hass)

entity_id = reg.async_get_entity_id(
    "sensor",
    DOMAIN,
    unique_id
)
```

If already present:

- Recreate local entity object
- Do not create a new unique ID
- Allow Home Assistant to reattach

---

## Device Identification

The STATUS payload field:

```json
{
  "status": {
    "ID": "..."
  }
}
```

is the canonical Pulse ID.

Use it for:

- Device registry identifiers
- Entity unique IDs
- DeviceInfo

Never use:

- MQTT topic IDs
- Topic suffixes

as permanent identifiers.

---

## DeviceInfo

Required:

```python
identifiers = {
    (f"tibber_pulse_{pulse_id}",)
}
```

Optional:

```python
connections = {
    ("meter_serial", meter_id)
}
```

when OBIS:

```text
0-0:96.1.1
```

is available.

---

## Equipment Model Information

STATUS fields such as:

- hwmodel
- Build
- Hw

must populate:

```python
DeviceInfo(
    model=...,
    sw_version=...,
)
```

These values should be stored under the Pulse ID device object.

---

## Status Attributes

Expose selected STATUS fields as entity attributes:

```python
[
    "hwmodel",
    "rssi",
    "ssid",
    "Build",
    "Hw",
    "ID",
    "IP",
    "Uptime",
    "baud",
    "ntc",
    "dsmr",
    "heap"
]
```

Do not expose all raw fields automatically.

---

## Comments

All source code comments must be written in English.

Avoid Swedish comments.

Docstrings shall also be written in English.

---

## Logging

All component logging must use:

```python
_LOGGER = logging.getLogger(__name__)
```

Dynamic log level configuration should be supported via options flow.

Never leave debug print statements in production code.

---

## OBIS Handling

The integration must:

- Preserve unknown OBIS codes.
- Create sensors dynamically.
- Support future Pulse variants without code changes.

Do not hardcode assumptions specific to Pulse P1.

Never hard code OBIS codes. All OBIS related settings should be stored in full_db.py.

---

## Unit Conversion

Perform unit conversion before entities receive values.

Support:

- Wh ↔ kWh
- W ↔ kW
- VArh ↔ kVArh
- VAr ↔ kVAr

Sensor entities must always show normalized units from full_db.py.

---

## Home Assistant Compatibility

Target:

- Home Assistant Core
- Home Assistant OS
- HACS installations

No dependencies on:

- Tibber Cloud
- Pulse Bridge
- Pulse IR

---

## README Guidelines

README should clearly state:

- Supports Pulse P1, HAN and KM.
- Operates locally over MQTT.
- Decodes compressed protobuf envelopes.
- Parses OBIS data.
- Optional forwarding to Tibber Cloud via external MQTT bridge.

Repository description:

Local MQTT integration for Tibber Pulse devices (P1, HAN, KM). Decodes compressed protobuf envelopes, parses OBIS data, and exposes real-time sensors in Home Assistant. Can also be forwarded to Tibber Cloud via an external MQTT bridge to keep data in both HA and Tibber.

---

## Testing Guidelines

- Prefer testing against every raw capture fixture so changes preserve support across device types, meters, and firmware versions.
- Use hard-coded synthetic input only when the behavior under test is independent of the incoming payload data.
- For startup and message-order behavior, test both STATUS-before-data and data-before-STATUS when relevant.
- Create reusable test helpers to replay all raw fixtures and simulate message ordering, rather than duplicating fixture-loading and dispatch logic across tests.

---

## Release Notes

For every upcoming release note, include a very short “Raw capture fixtures” section reminding users to upload meter readings and read the README so all meters can remain supported; link to the README.

---

## Preferred Development Style

Prefer:

- explicit code
- simple control flow
- thread-safe Home Assistant patterns
- defensive programming

Avoid:

- hidden side effects
- implicit reloads
- duplicate subscriptions
- direct Home Assistant API calls from worker threads

When multiple solutions exist, prefer the solution most aligned with Home Assistant core development patterns.
