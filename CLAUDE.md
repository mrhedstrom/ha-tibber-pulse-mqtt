# Claude Project Guide (CLAUDE.md)

## Project Overview

Home Assistant custom integration (`tibber_pulse_mqtt`, "Tibber Pulse MQTT", repo `ha-tibber-pulse-mqtt`) for Tibber Pulse devices in MQTT mode: Pulse P1, HAN and KM. It receives MQTT payloads, extracts OBIS data and creates Home Assistant sensors dynamically. It is independent of Tibber Cloud, Pulse Bridge and Pulse IR, and works fully locally. Users may optionally bridge MQTT traffic to Tibber Cloud.

Targets Home Assistant Core, Home Assistant OS and HACS installations.

## Commands

- Run all tests: `pytest`
- Run one file: `pytest tests/test_file.py`
- Show stdout: `pytest -s`

## Critical Rules

- Pulse ID from the STATUS payload is the canonical identifier for devices and entities. Never base permanent identifiers on MQTT topics or topic suffixes.
- Never change an existing `unique_id` format without migration support. Never generate a new `unique_id` for an existing sensor or create duplicate entities for the same OBIS value.
- Never hardcode OBIS codes. All OBIS settings live in `obis/full_db.py`.
- Preserve unknown OBIS values in parsed data, diagnostics and raw captures. Only drop data when sanitizing sensitive information or when data is invalid/corrupted. Unknown OBIS codes still create sensors dynamically.
- Always prefer real raw captures over synthetic data. Never write parser logic from assumptions, and never assume behavior seen in one capture applies to all meters of the same type.
- Do not hardcode assumptions specific to Pulse P1; support future Pulse variants without code changes.
- Preserve backward compatibility: existing entities, unique IDs, MQTT topics, config entries and previously supported meters must keep working. Existing behavior is intentional unless stated otherwise. Add migration logic when behavior must change.
- Follow existing Home Assistant and repository patterns before introducing abstractions. Prefer extending existing code over parallel implementations; avoid new managers, coordinators or helpers without a clear architectural need. Explain why before proposing large refactors.
- Keep changes focused and minimal. Do not refactor unrelated code or modify unrelated files.

## Code Style

- PEP 8, explicit type hints on all function signatures.
- All code, comments and log messages in English regardless of the language of the prompt.
- Prefer `asyncio` for I/O; await all coroutines.
- Logging: `_LOGGER = logging.getLogger(__name__)`. Log level is configurable via the options flow. Never leave debug prints in production code.
- Be defensive with incoming payloads.
- Avoid hidden side effects, duplicate MQTT subscriptions, global state and custom polling loops.

## Architecture

Message flow: MQTT message → `dispatcher.py` → parser selection → `ParsedMessage` → `TibberLocalHub` (`coordinator.py`) → `SensorManager` (`sensor.py`) → HA entities.

All paths below are under `custom_components/tibber_pulse_mqtt/`:

- `mqtt_client.py` – subscriptions
- `dispatcher.py` – routes incoming messages
- `parsers/` – protocol parsers:
  - `pulse_envelope.py` – P1, compressed protobuf envelopes
  - `obis_text.py` – plain text OBIS messages (some P1)
  - `dlms_cosem.py` – HAN, DLMS/COSEM
  - `obis_binary_fallback.py` – fallback
- `obis/full_db.py` – all OBIS metadata and format-specific mappings; `obis/streaming.py` – OBIS stream handling
- `sensor.py` – `SensorManager` creates and updates entities
- `repairs.py`, `views.py`, `util/diagnostics.py` – repairs, HTTP views, diagnostics export
- `raw_capture.py` – collects payloads to share with developers; `raw_capture_manager.py` – user-friendly sharing workflow

All devices periodically send a STATUS message (plain JSON). Data message formats vary by Tibber device, meter manufacturer and firmware. Before adding functionality, check whether an existing parser, dispatcher or entity flow can be extended.

## Metadata Layering

A parser knows how to decode data, not what the data means. Parsers decode payloads, detect formats and extract values; they must not contain meter-specific metadata, OBIS metadata, positional mappings, unit definitions, scaling metadata or manufacturer registries. That belongs in `obis/full_db.py`.

Before adding metadata, ask whether it is a property of the OBIS code, of a payload format, or of a specific meter, and store it in that layer:

- Canonical OBIS metadata (meaning of the code): unit, device class, state class, precision, sensor metadata.
- Format-specific metadata (how a format encodes values): positional mappings, scaling factors, structure definitions, format identifiers. Never store these as canonical OBIS properties.

For a new meter format: first try metadata only; extend parser code only when the payload structure itself is new.

DLMS-specific rules are in `.claude/rules/dlms-parsers.md` and Repairs rules in `.claude/rules/repairs.md`; they load when those files are touched.

## Unit Conversion

Convert units before entities receive values. Support Wh↔kWh, W↔kW, VArh↔kVArh, VAr↔kVAr. Sensors always show the normalized units from `obis/full_db.py`.

## Home Assistant Rules

### Threading and state

- Home Assistant APIs and entity state updates must run on the event loop. Never call HA APIs or update state from worker/executor threads; schedule work onto the loop with thread-safe HA patterns.

### Entity creation and persistence

- Creation must be race-safe: synchronize concurrent attempts, track entities currently being created, and re-check that the entity does not exist immediately before creating it.
- Check the Entity Registry first. If the entity exists, recreate the local object if needed, reuse the existing `unique_id` and let HA reattach it. Reuse existing entities whenever possible.

### Devices

- Pulse ID is the primary `DeviceInfo` identifier. Meter serial from OBIS data may be exposed as a device attribute but never replaces Pulse ID.
- STATUS fields `hwmodel`, `Build` and `Hw` populate device model and software version; when `hwmodel` is empty, use `Hw`.
- Expose only the standard STATUS attributes already defined in the implementation, not every STATUS field.

### Config flow reload

- Update listeners must only trigger a config entry reload. Do not manually stop or start the integration; HA already unloads and sets up on reload.

## Translations

All user-facing text (sensor enteties names, config/options flow labels, descriptions, help text, repairs, issues) must be translated.

- Update `strings.json` and every locale in `translations/` (da, de, en, fi, nl, no, sv) together, including both config-flow and options-flow entries where applicable.
- Validate every translation JSON file after editing.
- When changing configuration help text, verify `strings.json` and every locale are in sync and contain no stale threshold values; do not only update README/CHANGELOG.

## Testing

- Use `pytest`; prefer fixtures over setup/teardown.
- Raw captures in `tests/fixtures/raw/` (base64 payloads from real devices, described in the README.md in that folder) are the primary test source. Test them through the dispatcher whenever applicable, to preserve support across device types, meters and firmware versions.
- Create and reuse helpers that replay all raw fixtures and simulate message ordering; do not duplicate fixture loading and dispatch logic. For startup/message-order behavior, test both STATUS-before-data and data-before-STATUS when relevant.
- Use plain unit tests only when raw-capture testing is not applicable. Use hard-coded synthetic input only when the behavior is independent of payload data.
- New business logic needs high coverage. Every newly supported meter gets a regression test and its capture stored in `tests/fixtures/raw/`.
- For bug fixes, add a regression test that reproduces the issue before implementing the fix.
- When debugging, replay existing raw captures first and add temporary logging before changing parser behavior; remove it before finishing.

## Documentation Priority

When implementing or modifying code, use information sources in this order:

1. Existing implementation
2. Tests and raw captures
3. This CLAUDE.md
4. CHANGELOG.md
5. README.md

README.md primarily contains installation and user documentation and should not be treated as an implementation specification.

## Workflow

Before changing code: read the relevant files completely, search for existing patterns and reuse them, and explain planned changes before editing multiple files.

Before finishing: run the tests when possible, update tests for changed behavior, check translation consistency for user-facing text changes, and update CHANGELOG.md.

## CHANGELOG.md

GitHub Actions copies the current release section (the topmost one, e.g. `## [x.y.z] - future release`) into the GitHub release notes. For every user-visible change add user-friendly text there. Do not mention workflow or test changes unless explicitly requested.
