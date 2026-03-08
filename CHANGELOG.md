# Changelog

## [?.?.?] Next Release
### Added
- **Model‑agnostic multi‑frame handling** in `dispatcher`: split MQTT payloads on Tibber’s internal delimiter and feed **every** inner frame (blob preferred, otherwise raw frame) to the stream manager. The quick deflate probe is now diagnostic‑only.
- **Deflate sniffing** via `extract_zlib_payload_if_any(...)` to gate stream feeding and avoid false MISS from status/metadata-only frames.
- **Buffer safety**: conservative cap with smart trimming to prevent unbounded memory growth on malformed telegrams.

### Changed
- **Header-aware probing** in `obis/streaming.py`: use zlib wrapper (wbits=15) when a zlib header is detected at candidate offsets; otherwise fall back to raw deflate (-15).
- **OBIS text parser** now uses a single `finditer`-regex to capture multiple OBIS codes per line and consistently return `_units`.

### Improved
- **First-chunk behavior after priming**: when a stream is just primed and no complete `/...!` frame is available yet, we mark `skip_bump=True` to avoid counting a MISS for that publish.
- **Resilience across Pulse models (P1/HAN/KM)** by avoiding assumptions about protobuf field names and treating every length-delimited candidate generically.
- **Sanity checks for OBIS frames**: empty or clearly invalid `/...!` fragments (e.g., “/!” or frames yielding no codes) are ignored and never write state nor mark OK. Publishes are only marked OK if at least one inner frame yields actual OBIS.
- **Skip‑MISS behavior preserved**: if a publish advances a zlib stream but no complete OBIS is ready yet, `skip_bump` avoids false MISS. This improves robustness across Pulse P1/HAN/KM and firmware variants.


### Fixed
- Eliminated intermittent “invalid distance too far back” loops by dropping the active decompressor on continuation errors and waiting for a fresh header.
- Prevented false MISS increments on publishes that only carry status or the first part of a new stream.
- **Rare missed candidates in multi‑frame publishes**: raw inner frames without `blob` are no longer dropped when the quick probe fails; the stream manager (header‑aware) now gets every frame and decides when to start or ignore a stream.

### Compatibility
- No breaking changes to configuration.
- Works with previously created entities and options.
- Recommended: keep DEBUG logging enabled when testing new meter/firmware variants to benefit from the enhanced diagnostics.

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