# Changelog

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

### Fixed
- Eliminated race conditions and non-deterministic behavior during sensor creation.
- Resolved warnings such as:
  - “coroutine was never awaited”
  - “hass.async_create_task called from a thread other than the event loop”

### Compatibility
- Compatible with all previous configurations.
- Home Assistant **2026.3.0+** recommended for complete Brand icon support.