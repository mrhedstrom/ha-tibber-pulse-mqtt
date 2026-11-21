## Updating fixture metadata

When adding a new `.jsonl` capture to this directory, run the metadata generator from the repository root:

```text
python tests/generate_raw_fixture_metadata.py
```

The command replays every raw capture and creates or updates the companion `.json` file with the decoded `obis_codes` and normalized `obis_units`. To generate metadata for another fixture directory, pass it as an argument:

```text
python tests/generate_raw_fixture_metadata.py path/to/fixture-directory
```

Review the generated JSON file and run `pytest tests/test_raw_fixtures.py` before committing the new capture.

Captures of unsupported meters cannot be sanitized automatically. Before committing, replace meter IDs (such as the GS1 id `0-0:96.1.0`), serial numbers and similar identifiers with dummy values by hand.

## Files

File: F-55bc424cac.jsonl

- Device type/model (Pulse P1, HAN, or KM): Pulse HAN
- Meter manufacturer and model, if known: Kaifa MA105H2E, single-phase (Norway)
- Firmware/build version, if known: 1.2.5 (Hw: F)
- Home Assistant and integration versions: Integration 0.4.7
- What behavior or unsupported data the capture demonstrates: Five data messages with four List 1 frames and one nine-member `KFM_001` List 2 frame per protobuf envelope. List 2 carries active/reactive power, current, and voltage. No hourly List 3 frame is present; the proposed 14-member mapping is not verified by this capture.
- Confirmation that the capture was reviewed and is safe to share: Embedded meter identifier replaced with zeros; MQTT topic and STATUS fields redacted.

File: F-62856487d9.jsonl

- Device type/model (Pulse P1, HAN, or KM): Pulse HAN
- Meter manufacturer and model, if known: Kaifa MA105H2E, single-phase (Norway)
- Firmware/build version, if known: 1.2.5 (Hw: F)
- Home Assistant and integration versions: Integration 0.4.7
- What behavior or unsupported data the capture demonstrates: Ninety-two data messages with four List 1 frames and one nine-member `KFM_001` List 2 frame per protobuf envelope. List 2 carries active/reactive power, current, and voltage. No hourly List 3 frame is present; the proposed 14-member mapping is not verified by this capture.
- Confirmation that the capture was reviewed and is safe to share: Embedded meter identifier replaced with zeros; MQTT topic and STATUS fields redacted.

File: TLC01-8294d970bc.jsonl

- Device type/model (Pulse P1, HAN, or KM): P1
- Meter manufacturer and model, if known: Landis+Gyr E360
- Firmware/build version, if known: 1.0.8
- Home Assistant and integration versions: 0.4.3
- What behavior or unsupported data the capture demonstrates: Ok, this one is with compressed protobuf OBIS values. It is a good example of the OBIS values that are available from this meter.
- Confirmation that the capture was reviewed and is safe to share: Yes

File: TLC01-7ebae9b8f5.jsonl

- Device type/model (Pulse P1, HAN, or KM): P1
- Meter manufacturer and model, if known: Unknown (Netherlands)
- Firmware/build version, if known: Pulse build 1.0.8; meter firmware unknown
- Home Assistant and integration versions: Unknown
- What behavior or unsupported data the capture demonstrates: Compressed protobuf P1 data decoded through the dispatcher, with 19 OBIS codes and 10 unit mappings. The meter model is not known, so the OBIS set is recorded from the capture without adding meter-specific assumptions.
- Confirmation that the capture was reviewed and is safe to share: Not confirmed; Yes

File: TLC01-3ac2ccfb54.jsonl

- Device type/model (Pulse P1, HAN, or KM): P1
- Meter manufacturer and model, if known: Landis+Gyr E360
- Firmware/build version, if known: 1.0.8
- Home Assistant and integration versions: 0.4.3
- What behavior or unsupported data the capture demonstrates: Ok - This one is with plain text OBIS values, no compression or protobuf. It is a good example of the OBIS values that are available from this meter.
- Confirmation that the capture was reviewed and is safe to share: Yes

File:HAN-F-28dc17ea75.jsonl

- Device type/model (Pulse P1, HAN, or KM): HAN
- Meter manufacturer and model, if known: Aidon 6534
- Firmware/build version, if known: 1.2.5
- Home Assistant and integration versions: 0.4.5
- What behavior or unsupported data the capture demonstrates: The protobuf envelope ("HAN" tag) carries a single HDLC type-3 frame (7E A2 43, 579 bytes, not segmented). LLC is E6 E7 00. DataNotification with invoke-id 40 00 00 00 and no date-time (00).
- Confirmation that the capture was reviewed and is safe to share: Yes
- Issue #43

File: HAN-MA304H3E-89100c9157.jsonl

- Device type/model (Pulse P1, HAN, or KM): HAN
- Meter manufacturer and model, if known: Kaifa MA304H3E (3-phase, Norway)
- Firmware/build version, if known: 1.2.5
- Home Assistant and integration versions: 2026.10.1, 0.4.4
- What behavior or unsupported data the capture demonstrates: Kaifa positional lists (1 and 13 members), plus the hourly 18-member list with energy registers at 22:00
- Confirmation that the capture was reviewed and is safe to share: Yes (meter GS1 id replaced with a dummy)

File: HAN-Kamstrup_V0001-e38ac249cd.jsonl

- Device type/model (Pulse P1, HAN, or KM): HAN
- Meter manufacturer and model, if known: Kamstrup V0001
- Firmware/build version, if known: 1.2.5
- Home Assistant and integration versions: 0.4.4
- What behavior or unsupported data the capture demonstrates: flat DLMS structure
- Confirmation that the capture was reviewed and is safe to share: Yes

File: HAN-Kamstrup_V0001-8367cb955d.jsonl

- Device type/model (Pulse P1, HAN, or KM): HAN
- Meter manufacturer and model, if known: Kamstrup V0001
- Firmware/build version, if known: 1.2.5
- Home Assistant and integration versions: 0.4.4
- What behavior or unsupported data the capture demonstrates: flat DLMS structure
- Confirmation that the capture was reviewed and is safe to share: Yes