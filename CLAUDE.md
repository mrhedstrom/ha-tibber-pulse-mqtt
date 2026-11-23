# Claude Project Guide (CLAUDE.md)

## Project Overview

Home Assistant custom integration (`tibber_pulse_mqtt`, "Tibber Pulse MQTT", repo `ha-tibber-pulse-mqtt`) for Tibber Pulse devices in MQTT mode: Pulse P1, HAN and KM. It receives MQTT payloads, extracts OBIS data and creates Home Assistant sensors dynamically. It is independent of Tibber Cloud, Pulse Bridge and Pulse IR, and works fully locally. Users may optionally forward a Pulse's MQTT traffic to Tibber Cloud with the built-in forwarding (see Tibber Cloud Forwarding). The old external broker bridge only remains documented in `docs/legacy-mosquitto-bridge.md`.

Targets Home Assistant Core, Home Assistant OS and HACS installations.

## Commands

- Run all tests: `pytest`
- Run one file: `pytest tests/test_file.py`
- Show stdout: `pytest -s`

## Critical Rules

- Pulse ID from the STATUS payload is the canonical identifier for devices and entities. Never base permanent identifiers on MQTT topics or topic suffixes. The only use of a topic suffix is the Tibber Cloud connection setting described under Tibber Cloud Forwarding; it is never an identifier.
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

Message flow: MQTT message → `TibberLocalHub` (`coordinator.py`, hands it to the Tibber Cloud forwarders first) → `dispatcher.py` → parser selection → `ParsedMessage` → `SensorManager` (`sensor.py`) → HA entities.

All paths below are under `custom_components/tibber_pulse_mqtt/`:

- `mqtt_client.py` – subscriptions and `async_publish` for both the HA MQTT and the external broker client
- `dispatcher.py` – routes incoming messages and records discovered Pulse devices (`discovered_pulses`, `on_pulse_discovered`)
- `parsers/` – protocol parsers:
  - `pulse_envelope.py` – P1, compressed protobuf envelopes
  - `obis_text.py` – plain text OBIS messages (some P1)
  - `dlms_cosem.py` – HAN, DLMS/COSEM
  - `obis_binary_fallback.py` – fallback
- `obis/full_db.py` – all OBIS metadata and format-specific mappings; `obis/streaming.py` – OBIS stream handling
- `sensor.py` – `SensorManager` creates and updates entities; also the Tibber Cloud connection sensor
- `repairs.py`, `views.py`, `util/diagnostics.py` – repairs, HTTP views, diagnostics export
- `raw_capture.py` – collects payloads to share with developers; `raw_capture_manager.py` – user-friendly sharing workflow
- Tibber Cloud forwarding:
  - `cloud_forwarder.py` – `CloudForwarder` (one paho client per Pulse), connection test, `CloudForwardingConfig`, shared helpers for staging and replacing credentials
  - `cloud_certs.py` – credential validation and storage
  - `cloud_issues.py` – Repairs issues for forwarding (`CloudIssueReporter`)
  - `cloud_entity.py`, `switch.py`, `sensor.py` – connection sensor and on/off switch
  - `config_flow.py` – also holds the forwarding subentry flow (`CloudForwardingSubentryFlow`)
  - `ca/verisign_class3_g5.pem` – bundled root CA of the Tibber endpoint
- `docs/legacy-mosquitto-bridge.md` – the old broker bridge, kept as reference only

All devices periodically send a STATUS message (plain JSON). Data message formats vary by Tibber device, meter manufacturer and firmware. Before adding functionality, check whether an existing parser, dispatcher or entity flow can be extended.

## Tibber Cloud Forwarding

Optional, built-in replacement for an external broker bridge. It forwards what a Pulse publishes locally to Tibber Cloud (AWS IoT) and delivers cloud messages (firmware updates, remote control) back to the Pulse. Extend these pieces rather than adding parallel ones.

### Identity and configuration
- One config subentry (`cloud_forwarding`) per Pulse under the existing hub entry, not one config entry per Pulse. Its `unique_id` is the Pulse ID. It stores only `pulse_id`, `local_topic` and deviations from the defaults, never certificate data.
- The AWS client ID and topics come from the Pulse's MQTT topic (`tibber-pulse-<thing>/publish`, the AWS thing name). The thing name is **not** the Pulse ID from the STATUS message. Never derive one from the other. The thing name is connection configuration only, never an identifier.
- Settings use the field names of the Pulse setup page: `certificate`, `private_key`, `ca_cert`, `mqtt_url` (host only), `mqtt_port`, `mqtt_topic`, `mqtt_topic_sub`.
- Only store deviations from the defaults, so a changed default reaches existing users.
- Use fake IDs and generated certificates in tests, docs and logs. Never commit a real thing name, Pulse ID or certificate.

### Forwarding behavior
- Forwarding is a byte-for-byte pass-through of the local message, STATUS included, handed over by the hub before parsing. Never decode, filter or change it.
- The forwarder must never block setup or the local sensors: connect in the background, build the TLS context in the executor, and keep a bounded queue. paho callbacks run on its own thread and must move to the event loop with `call_soon_threadsafe` before touching Home Assistant.
- Mirror the documented bridge: MQTT 3.1.1, TLS 1.2, clean session, QoS 1 both ways.
- The cloud allows one connection per client ID. A running forwarder must step aside while a connection is tested, and an old bridge with the same client ID makes both fight. Documentation, CHANGELOG, setup dialogs and Repairs must keep warning that an existing bridge is removed first.
- The CA the endpoint chains to is not in certifi, so it is bundled and used unless the user pastes another.
- The daily certificate expiry check uses `async_track_time_interval`. That is the one allowed timer; do not add polling loops.

### Credentials
- Stored in `<config>/.storage/tibber_pulse_mqtt/certs/<pulse_id>/`, not under `custom_components` (a HACS update would delete them), so they survive updates and are included in backups.
- Validate, stage, test against the cloud, then commit. Never replace working credentials with untested ones. Certificate and private key are always replaced together. A custom CA is kept when only the certificate is replaced.
- Credentials of a removed forwarding are deleted at the next start, except directories younger than ten minutes.

### Entities and devices
- The connection sensor and the switch are attached to the Pulse device with its `identifiers` only (model and name are added only once STATUS is known, so the OBIS sensors' values are never overwritten). They are **not** linked to the subentry with `config_subentry_id`: a device linked to both an entry and a subentry moves to another place in the UI once the OBIS sensors appear, and the subentry's entities seem to vanish.
- Because they are not linked to the subentry, Home Assistant does not remove them with it; `async_remove_stale_cloud_entities` does at each setup.
- The switch state is kept in a `Store` in the hub, not in the subentry, since changing the subentry reloads the integration. Remove the store in `async_remove_entry`.

### Repairs
- Issue IDs are `<kind>_<pulse_id>` and stay the same across restarts. Escalating a certificate warning uses a different ID, which is new information. Home Assistant keeps an issue dismissed when it is created again with the same ID, which is what keeps an ignored "Pulse discovered" offer from coming back.
- Home Assistant starts a fix flow by calling `async_step_init` with the **issue data** as `user_input`. Never treat it as form input. `async_step_init` must only delegate to a named step, as `CloudCertificateRepairFlow` does with `credentials`.
- Fix flows that replace credentials use `async_replace_credentials` and `async_stage_and_test` from `cloud_forwarder.py`.

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
- Validate every translation JSON file after editing. `tests/test_translations.py` checks that all locales have the same keys as `strings.json`, that every flow error, abort, entity state and Repairs text exists, and that placeholders are valid.
- Labels of fields the user copies from the Pulse setup page end with that page's field name in parentheses, for example "Client certificate (certificate)". The parenthesized name is identical in every language.
- Subentry flows need `config_subentries` strings, form sections need `sections`, and every step id a fix flow shows needs its own `issues.<key>.fix_flow.step.<step_id>` entry.
- When changing configuration help text, verify `strings.json` and every locale are in sync and contain no stale threshold values; do not only update README/CHANGELOG.

## Testing

- Use `pytest`; prefer fixtures over setup/teardown.
- Raw captures in `tests/fixtures/raw/` (base64 payloads from real devices, described in the README.md in that folder) are the primary test source. Test them through the dispatcher whenever applicable, to preserve support across device types, meters and firmware versions.
- Create and reuse helpers that replay all raw fixtures and simulate message ordering; do not duplicate fixture loading and dispatch logic. For startup/message-order behavior, test both STATUS-before-data and data-before-STATUS when relevant.
- Tibber Cloud forwarding is tested without a network and without the Home Assistant test harness: `tests/cloud_helpers.py` has the shared fakes (`FakeClient` for paho, `FakeHass`, `FakeStore`, `FakeIssues`) and `make_pems` for generated certificates (RSA PKCS#1, as the Pulse shows them). Reuse them. Forwarder tests replay every raw capture in both message orders and check that the exact bytes reach the cloud topic.
- Test flows the way Home Assistant calls them (a fix flow's first call carries the issue data), and test the real hub rather than a stub where practical.
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
