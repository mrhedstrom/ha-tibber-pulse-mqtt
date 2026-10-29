# Claude Project Guide (CLAUDE.md)

## Critical Rules

- Never change existing unique_id formats without migration support.
- Never hardcode OBIS codes.
- Always prefer real raw captures over synthetic data.
- Preserve unknown OBIS values whenever possible.
- Use Pulse ID as the canonical device identifier.
- Follow existing Home Assistant patterns before introducing new abstractions.
- Prefer extending existing code over creating parallel implementations.
- Update tests and translations when behavior changes.

## Project Overview

This project is a Home Assistant custom integration for Tibber Pulse devices operating in MQTT mode.

Supported devices include:

- Tibber Pulse P1
- Tibber Pulse HAN
- Tibber Pulse KM

The integration receives payloads via MQTT, interprets the message to extract OBIS data and creates Home Assistant sensors dynamically.

This integration is intentionally independent of Tibber Cloud and can operate fully locally. Users may optionally bridge MQTT traffic externally to Tibber Cloud.

## Build and Test Commands
- Run all tests: `pytest`
- Run specific test file: `pytest tests/test_file.py`
- Run tests with stdout enabled: `pytest -s`
- Run linting and code style checks: `flake8 .` or `black --check .`

## Code Style & Conventions
- **Formatting:** Follow PEP 8 standards strictly. Use explicit type hinting for all function signatures.
- **Localization:** If string resources or localization dictionaries are changed, you must also update all corresponding translation files to maintain parity.
- **Asynchronous Code:** Prefer `asyncio` for I/O-bound operations. Ensure all async functions are properly awaited.
- Use thread-safe Home Assistant patterns.
- Use defensive programming.
- When multiple solutions exist, prefer the solution most aligned with Home Assistant core development patterns.

Avoid:

- hidden side effects
- implicit reloads
- duplicate subscriptions
- direct Home Assistant API calls from worker threads

## Testing Guidelines
- **Framework:** Always use `pytest` for testing. Prefer fixtures over setup/teardown methods.
- **Raw Data Capture Tests:** Tests must always be written for raw data captures stored in the `\tests\fixtures\raw` folder through the dispatcher when applicable. This is to preserve support across device types, meters, and firmware versions.
	-For startup and message-order behavior, test both STATUS-before-data and data-before-STATUS when relevant.
	- Create reusable test helpers to replay all raw fixtures and simulate message ordering, rather than duplicating fixture-loading and dispatch logic across tests.
- **Unit Tests:** You may only use unit tests when raw data capture testing is not applicable. Use hard-coded synthetic input only when the behavior under test is independent of the incoming payload data.
- **Coverage:** Aim for high test coverage on all new business logic.
- When fixing a bug, prefer adding a regression test that reproduces the issue before implementing the fix.

### Debugging

When investigating issues:
- Prefer replaying existing raw captures.
- Add temporary logging before changing parser behavior.
- Remove temporary debug logging before finalizing changes.

## Architecture

Message flow:

MQTT Message
    ↓
dispatcher.py
    ↓
parser selection
    ↓
ParsedMessage
    ↓
SensorCoordinator
    ↓
EntityManager
    ↓
Home Assistant entities

Key modules:

- mqtt_client.py handles subscriptions
- dispatcher.py routes incoming messages
- parsers/ contains protocol specific parsers
- full_db.py contains all OBIS metadata and mappings
- entity_manager.py creates and updates entities
- diagnostics.py exports troubleshooting data
- raw_capture.py collects payloads for sharing to developers
- raw_capture_manager.py provides a user-friendly workflow to share the raw captures.

Before creating new functionality, first determine if an existing parser, dispatcher or entity management flow should be extended.

## Claude Code Workflow

Before making changes:

1. Read relevant files completely.
2. Search for existing patterns in the repository.
3. Reuse existing implementations when possible.
4. Explain planned changes before editing multiple files.

When modifying code:

- Keep changes focused.
- Update tests when behavior changes.
- Update translations when user-facing text changes.
- Update CHANGELOG.md for user-visible changes.
- Do not modify files unrelated to the requested change.

Before finishing:

- Run tests when possible.
- Verify linting.
- Check for translation consistency.

## Modification Strategy

When fixing bugs:

- Prefer minimal targeted changes.
- Do not refactor unrelated code unless explicitly asked to do so.
- Do not replace working implementations unless required.
- Preserve backward compatibility.
- Existing behavior is intentional unless explicitly stated otherwise.
- Prefer extending existing classes and workflows over creating new ones.
- Avoid introducing new managers, coordinators, helpers or abstractions unless there is a clear architectural need.

Before proposing large refactors explain why they are needed.

### Backward Compatibility

Changes must not break support for:
- Existing entities
- Existing unique IDs
- Existing MQTT topics
- Existing configuration entries
- Previously supported meters

When behavior changes, add migration logic or maintain backwards compatibility where possible.

## Raw Capture Development

Raw capture files stored in '\tests\fixtures\raw' are base64 encoded raw payloads from different users' devices and meters. The README.md file in the same directory describes the different raw captures.

When implementing support for new meters:

- Always use real raw captures when available.
- Never create parser logic based only on assumptions.
- Verify behavior against actual captured data.
- Store reusable captures in tests/fixtures/raw.
- Add regression tests for every newly supported meter.
- Never assume that behavior observed in a single raw capture applies to all meters of the same type.

## Release Notes and CHANGELOG.md

CHANGELOG.md is used to automate release creation in GitHub Actions.

Everything written in the current release section, usually marked "Future Release", will be copied to the GitHub release details.

When making changes, always add user-friendly information to CHANGELOG.md. Do not include workflow or test changes if not explicit request to do so.

### Documentation Priority

When implementing or modifying code, use information sources in this order:

1. Existing implementation
2. Tests and raw captures
3. This CLAUDE.md
4. CHANGELOG.md
5. README.md

README.md primarily contains installation and user documentation and should not be treated as an implementation specification.

## Naming

- Repository: ha-tibber-pulse-mqtt
- DOMAIN: tibber_pulse_mqtt
- Integration name: Tibber Pulse MQTT

## MQTT messages

Messages are formatted differently depending on Tibber device, Meter manufacturer and firmware. All devices periodically send a status MQTT message containing information about the tibber device in plain json.

For a P1 Tibber device, messages are usually compressed protobuf envelopes. These messages are handled in 'custom_components\tibber_pulse_mqtt\parsers\pulse_envelope.py'. In some cases plain text OBIS messages are received and handled in 'custom_components\tibber_pulse_mqtt\parsers\obis_text.py'.

For a HAN Tibber device, messages are commonly encoded using DLMS/COSEM and are handled in 'custom_components\tibber_pulse_mqtt\parsers\dlms_cosem.py'. DLMS/COSEM payloads may differ between meter manufacturers and firmware versions, therefore parser changes should be validated against real raw captures whenever possible.

DLMS/COSEM payloads often vary between meter vendors.

When modifying dlms_cosem.py:
- Never assume a specific meter manufacturer.
- Prefer capability detection over meter-specific logic.
- Preserve unknown OBIS values whenever possible.
- Do not discard, filter or remove unsupported OBIS values from parsed meter data, diagnostics or raw captures, except when removing sensitive information during sanitization or when handling invalid/corrupted data.
- Verify changes against multiple real captures.

## Config Flow Reloading

Update listeners must only trigger a config entry reload.

Do not manually stop or start the integration during reload handling.
Home Assistant already performs unload and setup as part of the reload process.

## Translation Maintenance

Keep every supported locale in `custom_components/tibber_pulse_mqtt/translations/` updated whenever configuration options, labels, descriptions, repairs, issues, or help text are added or changed. Update `strings.json` and all locale files together, including both config-flow and options-flow entries where applicable. Validate every translation JSON file after editing. When changing configuration help text, verify the source strings.json and every supported locale translation—not only README/changelog—are synchronized and contain no stale threshold values.

## Thread Safety

Home Assistant APIs must only be called from the event loop. Use Home Assistant thread-safe patterns when scheduling work from worker threads. Never call async Home Assistant APIs directly from worker threads.

## HA devices

### State Updates

Entity state updates must always occur on the Home Assistant event loop. Never update entity state directly from worker or executor threads.

### Sensor Creation

Entity creation must be race-safe.

Required protections:
- Synchronize concurrent creation attempts.
- Track entities currently being created.
- Verify that an entity still does not exist immediately before creation.

### Entity Registry

Check the Entity Registry before creating entities.

If an entity already exists:
- Recreate the local entity object when needed.
- Reuse the existing unique_id.
- Allow Home Assistant to reattach the entity.

### Device Identification

Pulse ID from the STATUS payload is the canonical identifier for devices and entities.

Never base permanent identifiers on MQTT topics or topic suffixes.

### DeviceInfo

Use Pulse ID as the primary device identifier.

When meter serial information is available through OBIS data it may be added as a device connection.

### Entity Persistence

Existing entities should be reused whenever possible.

Never:
- Generate a new unique_id for an existing sensor.
- Create duplicate entities for the same OBIS value.
- Change unique_id formats without explicit migration support.

### Equipment Model Information

STATUS fields such as hwmodel, Build and Hw should populate Home Assistant device model and software version information.

When hwmodel is empty, use Hw instead.

### Status Attributes

Expose the standard set of STATUS fields defined in the current implementation. Do not automatically expose all STATUS payload fields.

## Language

All source code comments and code must be written in English regardless of user prompt language.

## Logging

All component logging must use:

```python
_LOGGER = logging.getLogger(__name__)
```

Dynamic log level configuration should be supported via options flow.

Never leave debug print statements in production code.

## OBIS Handling

The integration must:

- Preserve unknown OBIS codes.
- Create sensors dynamically.
- Support future Pulse variants without code changes.

Do not hardcode assumptions specific to Pulse P1.

Never hard code OBIS codes. All OBIS related settings should be stored in full_db.py.

## Unit Conversion

Perform unit conversion before entities receive values.

Support:

- Wh ↔ kWh
- W ↔ kW
- VArh ↔ kVArh
- VAr ↔ kVAr

Sensor entities must always show normalized units from full_db.py.

## Home Assistant Compatibility

Target:

- Home Assistant Core
- Home Assistant OS
- HACS / HA Marketplace installations

No dependencies on:

- Tibber Cloud
- Pulse Bridge
- Pulse IR

## Home Assistant Standards

Always follow Home Assistant core patterns.

Prefer:
- DataUpdateCoordinator
- ConfigEntry runtime data
- DeviceInfo
- EntityDescription
- Repairs framework
- Diagnostics framework

Avoid:
- Global state
- Singleton patterns
- Custom polling loops
- Manual setup/unload lifecycle management

## Repairs

Repairs must:

- Use issue_id that remains stable across restarts.
- Support dismissal persistence.
- Use fix_flow when user action is required.
- Use translations for all titles and descriptions.
- Never delete user data automatically without confirmation.
- Repairs descriptions are informational only.
- Repair issues must survive Home Assistant restarts.
- Repairs should not recreate dismissed issues unless new underlying data exists.

## Dependency Strategy

- Prefer Home Assistant built-in helpers and frameworks over custom implementations.
- Avoid introducing dependencies unless they provide significant value.
- Compatibility with future Home Assistant releases is more important than short-term implementation convenience.