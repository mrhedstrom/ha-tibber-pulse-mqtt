# Changelog

## [0.4.6] - Future release
<!-- Add sections for new features, fixes, and improvements -->

### Breaking Changes

- The following OBIS values are now exposed as device attributes instead of sensor entities:
  - `0-0:1.0.0` (Date/Time)
  - `0-0:96.1.0` (Serial Number)
  - `0-0:96.1.1` (Meter ID)
  - `0-0:96.1.7` (Meter Type)
  - `0-0:42.0.0` (Alternative ID)

- Existing entities for these OBIS values will become unavailable after upgrading. Users with dashboards, automations, templates, or other references to these entities must update their configuration accordingly.

### Changed

- Identifier and time-related OBIS values are now exposed as device attributes instead of sensor entities.
- Reduced the number of entities created per device.
- Reduced unnecessary history and device activity updates caused by metadata-only values.

> [!IMPORTANT]
> **Help keep your meter supported.**
> Reviewed raw captures are added to the automated test suite and replayed in future releases, helping detect regressions before they affect your device. Please consider sharing captures using the [capture sharing instructions](https://github.com/mrhedstrom/ha-tibber-pulse-mqtt#sharing-raw-device-data).

## [0.4.5]
### Added
- Decode the hourly Kaifa HAN list (18 members), adding cumulative import/export energy sensors for 3-phase Kaifa meters. Thanks @sailedev for the PR. (#38)
- Added a raw capture fixture from a Kaifa MA304H3E (Pulse HAN), including an hourly list. Thanks @sailedev for the PR. (#38)

## [0.4.4]
### Fixed
- Distribute releases as HACS zip asset. Thanks @tingdahl for the fix. (#29)

## [0.4.3]
### Fixed
- Sanitize and append subsequent STATUS messages in the same raw capture file, consistently pseudonymizing device IDs and redacting sensitive STATUS fields.
- Limit each raw capture to 100 MQTT messages total, counting STATUS and data messages together and preserving their arrival order.

### Added
- Test both status first and data first messages scenarios with all fixtures.

## [0.4.2]
### Fixed
- Fix sensor creation failures when decoded OBIS updates are scheduled from worker threads; route meter ID, unit, and sensor updates through the thread-safe event-loop helper. (#23)
- Fix thread safety on shutdown and reload to prevent race conditions and exceptions during Home Assistant stop/unload in DLSM/COSEM parser. Thanks @kornelione for the fix. (#18)
- Fix energy spikes due to a unit race condition on boot. The very first OBIS frame was being processed before the raw units map was populated in some cases. Thanks @tingdahl for the fix. (#21)

### Added
- Parse raw HDLC/DLMS frames with positional mapping for Kamstrup HAN meters. Thanks @omBratteng for the PR.
- Added automated pytest coverage for replaying raw MQTT captures through the dispatcher without running Home Assistant.
- Added a GitHub Actions workflow to run the test suite on pushes and pull requests.
- Raw capture fixtures can now be used to verify support for real-world meter and device payloads, including devices not yet supported by the integration.
- Raw capture begins after STATUS and four data messages and continues collecting up to 100 data messages; fixture tests validate decoding across the complete captured sample.

### Raw capture fixtures
- We welcome as many reviewed raw MQTT examples as users can safely share across all supported devices, meters, and firmware versions. These fixtures help expand device support and catch regressions. See the README's [**Sharing raw device data**](https://github.com/mrhedstrom/ha-tibber-pulse-mqtt#sharing-raw-device-data) section for capture, review, and issue-submission instructions.


## [0.4.1]
### Fixed
- Skip state writes for disabled Home Assistant entities.
  - Prevents warnings caused by updates being sent to entities that are disabled in Home Assistant.
  - Adds an early guard in `_schedule_state_write`, covering updates triggered by `set_state`, `set_status`, and `set_meter_id`.
- Added support for Tibber Pulse P1 devices that split a single Zlib stream across multiple protobuf Field-2 entries.
  - Reconstructs and decompresses multi-chunk streams before OBIS decoding.
  - Resolves decode failures and OBIS parsing errors on affected Pulse P1 devices.
  - Automatically activates only when multiple compressed fragments are detected, preserving existing behavior for single-chunk devices.

### Thanks
- Thanks to @db-EV for the multi-chunk implementation.

## [0.4.0]
### Added
- Initial DLMS/COSEM (HAN) support.
  - Supports Tibber Pulse HAN meters (e.g. Aidon V2).
  - Adds a new decoding path for DLMS/COSEM DataNotification messages.
  - Enables extraction of OBIS values including scaler and unit handling.

### Changed
- Decoder pipeline extended to support multiple data formats (P1 and HAN).

### Thanks
- Thanks to @JBerts for the DLMS/COSEM implementation.

### Testing
- Verified with P1 devices without regressions.

## [0.3.3]
### Fixed
- Fixed External MQTT connection crashes with
  `OSError: Int or String expected` caused by invalid or improperly saved
  host/port values. (#9)
- Fixed crash when receiving MQTT messages due to callbacks being handled
  from the MQTT thread instead of the Home Assistant event loop.

### Improved
- MQTT host is now validated and normalized before connecting.
- MQTT port is now safely converted to an integer (handles values like `1883.0`).
- ExternalMQTTClient now correctly uses the Home Assistant event loop when
  dispatching incoming messages.

## [0.3.2]
### Changed
- Documentation updates only
  - Updated HACS installation instructions now that the integration is an official HACS repository
  - Added detailed, self-contained instructions for configuring Tibber Pulse and bridging MQTT traffic to Tibber Cloud

> Note: This release contains documentation changes only. No code changes.

## [0.3.1]
### Fixed
- HACS country removed.

## [0.3.0]
### Added
- New Tibber Pulse icons for Home Assistant Brands integration.
- Extended MQTT `+` wildcard support with regex-based filtering for embedded wildcard patterns.
- Added full Finnish (Suomi) translation.

### Changed
- UI precision settings migrated to OBIS metadata database.
- Translations refactored to use Home Assistant’s native translation system.
  - Removed custom language selection from config flow.
- Updated `requirements.txt` to align with HA 2026.3.0 Brand icon requirements.

### Improved
- mqtt_client: Improved error handling and added shared helper utilities.
- Deterministic sensor registry retrieval order for stable entity creation/update.
- Converted sensor creation/update pipeline to full async to align with HA best practices.
  - Introduced `asyncio.Lock` for thread-safe entity updates.
  - All external-thread calls now properly scheduled via `loop.call_soon_threadsafe`.
- **Async lifecycle & shutdown:**
  - `coordinator.py`: Robust HA stop handling using `async_listen` with self-unsubscribe on first fire; unload callback now a proper async function; idempotent `async_stop`.
  - `dispatcher.py`: Added `async_start/async_stop`, cancel-safe workers with periodic wake-ups, proper `CancelledError` handling, and `task.add_done_callback` to capture worker exceptions.
  - `dispatcher.py`: Introduced `_threadsafe_call_sm` (executor-loop) and `_loop_call_sm` (on-loop) to safely invoke `SensorManager` methods regardless of sync/async implementation.
  - `sensor.py`: Safer `async_write_ha_state` scheduling (immediate on loop; thread-safe scheduling off loop).

### Fixed
- Eliminated race conditions and non-deterministic behavior during sensor creation.
- Resolved warnings such as:
  - “coroutine was never awaited”
  - “hass.async_create_task called from a thread other than the event loop”
- **Resolved shutdown/reload errors:**
  - “Task was destroyed but it is pending!”
  - “Task exception was never retrieved”
  - “Unable to remove unknown job listener … ValueError: list.remove(x): x not in list”
  - “TypeError: a coroutine was expected, got <Task finished …>”

### Compatibility
- Compatible with all previous configurations.
- Home Assistant **2026.3.0+** recommended for complete Brand icon support.
