import base64
import json
import threading
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.tibber_pulse_mqtt.const import CONF_SAVE_RAW_DATA, DOMAIN
from custom_components.tibber_pulse_mqtt.dispatcher import TibberDispatcher
from custom_components.tibber_pulse_mqtt.raw_capture import MIN_DATA_MESSAGES

RAW_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "raw"
RAW_FIXTURES = sorted(RAW_FIXTURE_DIR.glob("*.jsonl"))


class ImmediateLoop:
    def call_soon_threadsafe(self, callback, *args):
        callback(*args)

    def call_soon(self, callback, *args):
        callback(*args)


class ThreadSafeTestLoop:
    def __init__(self):
        self.callbacks = []
        self.scheduling_threads = []

    def call_soon_threadsafe(self, callback, *args):
        self.scheduling_threads.append(threading.get_ident())
        self.callbacks.append((callback, args))


class RecordingSensorManager:
    def __init__(self):
        self.status = {}
        self.readings = {}
        self.reading_updates = []
        self.units = {}
        self.meter_ids = {}

    def update_status_for_device(self, device_id, status):
        self.status[device_id] = status

    def update_meter_id_for_device(self, device_id, meter_id):
        self.meter_ids[device_id] = meter_id

    def set_obis_units(self, units):
        self.units.update(units)

    def add_or_update(self, device_id, code, value, status):
        self.reading_updates.append((device_id, code, value))
        self.readings.setdefault(device_id, {})[code] = value


def _load_fixture_records(raw_file):
    records = [
        json.loads(line)
        for line in raw_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records, f"{raw_file.name} contains no MQTT messages"
    return records


def _is_status_record(record):
    if record.get("message_type") == "status":
        return True
    if record.get("message_type") is not None:
        return False
    try:
        decoded_json = json.loads(base64.b64decode(record["payload"], validate=True))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(decoded_json, dict) and isinstance(decoded_json.get("status"), dict)


def _order_fixture_records(records, status_first):
    ordered = list(records)
    if status_first:
        target_index = next(
            (index for index, record in enumerate(ordered) if _is_status_record(record)),
            None,
        )
    else:
        target_index = next(
            (index for index, record in enumerate(ordered) if not _is_status_record(record)),
            None,
        )

    if target_index is not None:
        ordered.insert(0, ordered.pop(target_index))
    return ordered


def _replay_fixture(raw_file, tmp_path, records=None):
    sensor_manager = RecordingSensorManager()
    entry = SimpleNamespace(entry_id="fixture", data={}, options={})
    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
        loop=ImmediateLoop(),
        data={DOMAIN: {"fixture": SimpleNamespace(sensor_manager=sensor_manager)}},
    )
    dispatcher = TibberDispatcher(hass, entry)
    records = _load_fixture_records(raw_file) if records is None else records

    per_record_updates = []
    for index, record in enumerate(records, start=1):
        topic = record["topic"]
        payload = base64.b64decode(record["payload"], validate=True)
        is_status = _is_status_record(record)
        device_key = topic.split("tibber-pulse-", 1)[-1].split("/", 1)[0]
        reading_count_before = len(sensor_manager.reading_updates)
        pending_count_before = len(dispatcher._pending_by_topic.get(device_key, []))
        dispatcher._process_payload_sync(device_key, topic, payload)
        if not is_status:
            parsed_updates = len(sensor_manager.reading_updates) - reading_count_before
            pending_updates = len(dispatcher._pending_by_topic.get(device_key, [])) - pending_count_before
            assert parsed_updates > 0 or pending_updates > 0, (
                f"{raw_file.name} data record {index} produced no OBIS updates or queued readings"
            )
            per_record_updates.append(parsed_updates + pending_updates)

    return sensor_manager, records, per_record_updates


def test_dispatcher_captures_unrecognized_data_messages(tmp_path):
    device_key = "topic-device"
    topic = f"tibber-pulse-{device_key}/publish"
    status_payload = b'{"status":{"ID":"pulse-1","hwmodel":"Unknown meter"}}'
    entry = SimpleNamespace(
        entry_id="fixture",
        data={CONF_SAVE_RAW_DATA: True},
        options={},
    )
    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
        loop=ImmediateLoop(),
        data={},
    )
    dispatcher = TibberDispatcher(hass, entry)

    dispatcher._process_payload_sync(device_key, topic, status_payload)
    raw_payloads = [bytes([0x00, 0xFF, index]) for index in range(MIN_DATA_MESSAGES)]
    for payload in raw_payloads:
        dispatcher._process_payload_sync(device_key, topic, payload)

    capture_file = next((tmp_path / "tibber_pulse_mqtt_raw").glob("*.jsonl"))
    records = [json.loads(line) for line in capture_file.read_text(encoding="utf-8").splitlines()]
    synthetic_id = json.loads(base64.b64decode(records[0]["payload"]))["status"]["ID"]
    assert synthetic_id.startswith("test-")
    assert records[0]["topic"] == f"tibber-pulse-{synthetic_id}/publish"
    assert records[0]["payload_redacted"] is True
    assert [base64.b64decode(record["payload"]) for record in records[1:]] == raw_payloads
    assert all(record["payload_opaque"] is True for record in records[1:])


def test_apply_obis_schedules_all_sensor_manager_calls_thread_safely():
    loop = ThreadSafeTestLoop()
    sensor_manager = RecordingSensorManager()
    entry = SimpleNamespace(entry_id="fixture", data={}, options={})
    hass = SimpleNamespace(loop=loop, data={DOMAIN: {"fixture": SimpleNamespace(sensor_manager=sensor_manager)}})
    dispatcher = TibberDispatcher(hass, entry)
    obis = {
        "0-0:96.1.1": "meter-serial",
        "1-0:1.8.0": 123.0,
        "_units": {"1-0:1.8.0": "kWh"},
    }

    worker = threading.Thread(target=dispatcher._apply_obis_with_pulse_id, args=("pulse-id", obis))
    worker.start()
    worker.join()

    assert len(loop.callbacks) == 4
    assert set(loop.scheduling_threads) == {worker.ident}
    for callback, args in loop.callbacks:
        callback(*args)

    assert sensor_manager.meter_ids == {"pulse-id": "meter-serial"}
    assert sensor_manager.units == {"1-0:1.8.0": "kWh"}
    assert {code for _device, code, _value in sensor_manager.reading_updates} == {
        "0-0:96.1.1", "1-0:1.8.0"
    }


@pytest.mark.parametrize(
    "raw_file",
    RAW_FIXTURES or [None],
    ids=lambda path: path.name if path else "no-raw-captures",
)
def test_raw_fixture_replays_through_dispatcher(raw_file, tmp_path):
    if raw_file is None:
        pytest.skip("Place shared MQTT JSONL captures in tests/fixtures/raw")

    sensor_manager, _records, per_record_updates = _replay_fixture(raw_file, tmp_path)

    assert any(status.get("ID") for status in sensor_manager.status.values()), (
        f"{raw_file.name} did not decode a STATUS message"
    )
    assert len(per_record_updates) > 0


@pytest.mark.parametrize("raw_file", RAW_FIXTURES, ids=lambda path: path.name)
def test_raw_fixture_dispatcher_handles_both_status_data_orders(raw_file, tmp_path):
    records = _load_fixture_records(raw_file)
    status_records = [record for record in records if _is_status_record(record)]
    data_records = [record for record in records if not _is_status_record(record)]
    assert status_records, f"{raw_file.name} contains no STATUS messages"
    assert data_records, f"{raw_file.name} contains no data messages"

    data_first_records = _order_fixture_records(records, status_first=False)
    status_first_records = _order_fixture_records(records, status_first=True)
    assert data_first_records[0] in data_records
    assert status_first_records[0] == status_records[0]

    data_first_manager, _records, data_first_updates = _replay_fixture(
        raw_file, tmp_path / "data-first", data_first_records
    )
    status_first_manager, _records, status_first_updates = _replay_fixture(
        raw_file, tmp_path / "status-first", status_first_records
    )

    assert len(data_first_updates) == len(data_records)
    assert len(status_first_updates) == len(data_records)
    assert data_first_manager.reading_updates == status_first_manager.reading_updates


@pytest.mark.parametrize(
    "raw_file",
    RAW_FIXTURES,
    ids=lambda path: path.name,
)
def test_raw_fixture_has_sufficient_decoded_obis_coverage(raw_file, tmp_path):
    sensor_manager, _records, per_record_updates = _replay_fixture(raw_file, tmp_path)

    code_counts = Counter(code for _device, code, _value in sensor_manager.reading_updates)
    # Norwegian Kaifa HAN lists carry 16 distinct OBIS codes in total
    assert len(code_counts) >= 16, f"{raw_file.name} should exercise at least 16 distinct OBIS codes"
    assert all(isinstance(value, (float, int, str)) for _device, _code, value in sensor_manager.reading_updates)
    expected_codes = {
        "1-0:1.8.0",  # imported energy
        "1-0:2.8.0",  # exported energy
        "1-0:1.7.0",  # active power
        "1-0:32.7.0",  # voltage
        "1-0:31.7.0",  # current
    }
    assert expected_codes <= code_counts.keys(), f"{raw_file.name} is missing expected energy/electrical readings"
    assert sensor_manager.units, f"{raw_file.name} did not provide OBIS unit mappings"
    # Pulse HAN firmware reports only the hardware revision (Hw), not hwmodel
    assert all(status.get("hwmodel") or status.get("Hw") for status in sensor_manager.status.values()), (
        f"{raw_file.name} did not decode STATUS hardware model or revision"
    )
    assert len(per_record_updates) == sum(record.get("message_type") != "status" for record in _records)


@pytest.mark.parametrize("raw_file", RAW_FIXTURES, ids=lambda path: path.name)
def test_raw_fixture_normalizes_units_for_known_obis_codes(raw_file, tmp_path):
    sensor_manager, _records, _per_record_updates = _replay_fixture(raw_file, tmp_path)

    # P1 meters report kWh/kW, HAN meters such as Kaifa report Wh/W; sensors convert either
    expected_units = {
        "1-0:1.8.0": {"kWh", "Wh"},
        "1-0:2.8.0": {"kWh", "Wh"},
        "1-0:1.7.0": {"kW", "W"},
        "1-0:32.7.0": {"V"},
        "1-0:31.7.0": {"A"},
    }
    for code, expected_unit in expected_units.items():
        assert sensor_manager.units.get(code) in expected_unit, (
            f"{raw_file.name} has unexpected normalized unit for OBIS code {code}"
        )


@pytest.mark.parametrize("raw_file", RAW_FIXTURES, ids=lambda path: path.name)
def test_raw_fixture_replay_is_repeatable(raw_file, tmp_path):
    first_manager, _first_records, _ = _replay_fixture(raw_file, tmp_path / "first")
    second_manager, _second_records, _ = _replay_fixture(raw_file, tmp_path / "second")

    first_codes = Counter(code for _device, code, _value in first_manager.reading_updates)
    second_codes = Counter(code for _device, code, _value in second_manager.reading_updates)
    assert first_codes == second_codes
    assert first_manager.units == second_manager.units
    assert {key: set(status) for key, status in first_manager.status.items()} == {
        key: set(status) for key, status in second_manager.status.items()
    }

