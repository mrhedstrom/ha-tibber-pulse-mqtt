import base64
import json

from custom_components.tibber_pulse_mqtt.raw_capture import RawMessageCapture
from custom_components.tibber_pulse_mqtt.raw_capture import MAX_CAPTURE_DATA_MESSAGES, MIN_DATA_MESSAGES


def test_capture_waits_for_status_and_minimum_data_messages(tmp_path):
    capture = RawMessageCapture(tmp_path)
    device_key = "topic-device"
    status_payload = b'{"status":{"ID":"pulse-1","hwmodel":"Pulse P1"}}'

    topic = "tibber-pulse-topic-device/publish"
    capture.record(device_key, topic, status_payload, is_status=True)
    capture.set_status(device_key, {"ID": "pulse-1", "hwmodel": "Pulse P1"}, topic, status_payload)
    assert not list(tmp_path.glob("*.jsonl"))

    for reading in range(MIN_DATA_MESSAGES):
        payload = f"reading-{reading}".encode()
        capture.record(device_key, topic, payload)
        if reading < MIN_DATA_MESSAGES - 1:
            assert not list(tmp_path.glob("*.jsonl"))

    files = list(tmp_path.glob("Pulse_P1-*.jsonl"))
    assert len(files) == 1
    records = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert len(records) == MIN_DATA_MESSAGES + 1
    sanitized_status = json.loads(base64.b64decode(records[0]["payload"]))
    synthetic_id = sanitized_status["status"]["ID"]
    assert synthetic_id.startswith("test-")
    assert records[0]["payload_redacted"] is True
    assert records[1]["payload_opaque"] is True
    assert records[0]["topic_redacted"] is True
    assert records[0]["topic"] == f"tibber-pulse-{synthetic_id}/publish"
    assert all(record["topic"] == records[0]["topic"] for record in records)


def test_capture_preserves_data_before_late_status(tmp_path):
    capture = RawMessageCapture(tmp_path)
    device_key = "topic-device"
    topic = "tibber-pulse-topic-device/publish"
    readings = [f"reading-{index}".encode() for index in range(MIN_DATA_MESSAGES)]

    for payload in readings:
        capture.record(device_key, topic, payload)
    assert not list(tmp_path.glob("*.jsonl"))

    status_payload = b'{"status":{"ID":"pulse-late","hwmodel":"Pulse P1"}}'
    capture.record(device_key, topic, status_payload, is_status=True)
    capture.set_status(device_key, {"ID": "pulse-late", "hwmodel": "Pulse P1"}, topic, status_payload)

    capture_file = next(tmp_path.glob("Pulse_P1-*.jsonl"))
    records = [json.loads(line) for line in capture_file.read_text(encoding="utf-8").splitlines()]
    assert [base64.b64decode(record["payload"]) for record in records[:MIN_DATA_MESSAGES]] == readings
    status_record = records[MIN_DATA_MESSAGES]
    assert json.loads(base64.b64decode(status_record["payload"]))["status"]["hwmodel"] == "Pulse P1"


def test_capture_appends_messages_after_threshold(tmp_path):
    capture = RawMessageCapture(tmp_path)
    device_key = "topic-device"
    capture.record(device_key, "pulse/status", b"status", is_status=True)
    capture.set_status(device_key, {"ID": "pulse-1", "hwmodel": "Pulse/P1"}, "pulse/status", b"status")
    for index in range(MIN_DATA_MESSAGES):
        capture.record(device_key, "pulse/data", f"reading-{index}".encode())

    capture.record(device_key, "pulse/data", b"later message")

    files = list(tmp_path.glob("Pulse_P1-*.jsonl"))
    assert len(files) == 1
    records = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert [base64.b64decode(record["payload"]) for record in records] == [
        b"status",
        *(f"reading-{index}".encode() for index in range(MIN_DATA_MESSAGES)),
        b"later message",
    ]


def test_repeated_status_does_not_create_new_capture_file(tmp_path):
    capture = RawMessageCapture(tmp_path)
    device_key = "topic-device"
    topic = "tibber-pulse-topic-device/publish"
    status = {"ID": "pulse-1", "hwmodel": "Pulse P1"}
    status_payload = json.dumps({"status": status}).encode()

    capture.record(device_key, topic, status_payload, is_status=True)
    capture.set_status(device_key, status, topic, status_payload)
    for index in range(MIN_DATA_MESSAGES):
        capture.record(device_key, topic, f"reading-{index}".encode())

    original_file = next(tmp_path.glob("*.jsonl"))
    capture.record(device_key, topic, status_payload, is_status=True)
    capture.set_status(device_key, status, topic, status_payload)
    capture.record(device_key, topic, b"later reading")

    files = list(tmp_path.glob("*.jsonl"))
    assert files == [original_file]
    records = [json.loads(line) for line in original_file.read_text(encoding="utf-8").splitlines()]
    assert base64.b64decode(records[-1]["payload"]) == b"later reading"


def test_capture_preserves_status_when_buffer_evicts_it(tmp_path):
    capture = RawMessageCapture(tmp_path)
    device_key = "topic-device"
    status_payload = b'{"status":{"ID":"pulse-1","hwmodel":"Pulse P1"}}'
    capture.record(device_key, "pulse/status", status_payload, is_status=True)

    for index in range(MAX_CAPTURE_DATA_MESSAGES + 25):
        capture.record(device_key, "pulse/data", f"message-{index}".encode())
    capture.record(device_key, "pulse/status", status_payload, is_status=True)
    capture.set_status(device_key, {"ID": "pulse-1", "hwmodel": "Pulse P1"}, "pulse/status", status_payload)

    capture_file = next(tmp_path.glob("Pulse_P1-*.jsonl"))
    records = [json.loads(line) for line in capture_file.read_text(encoding="utf-8").splitlines()]
    assert sum(record["message_type"] == "data" for record in records) == MAX_CAPTURE_DATA_MESSAGES
    assert any(
        record.get("payload_redacted")
        and json.loads(base64.b64decode(record["payload"])).get("status", {}).get("ID", "").startswith("test-")
        for record in records
    )


def test_capture_redacts_sensitive_status_fields(tmp_path):
    capture = RawMessageCapture(tmp_path)
    status = {
        "ID": "pulse-secret",
        "hwmodel": "Pulse P1",
        "Build": "firmware-secret-build",
        "IP": "192.0.2.42",
        "ssid": "private-network",
        "password": "private-password",
    }
    status_payload = json.dumps({"status": status}).encode()
    capture.record("pulse-secret", "tibber-pulse-pulse-secret/publish", status_payload, is_status=True)
    capture.set_status("pulse-secret", status, "tibber-pulse-pulse-secret/publish", status_payload)
    for index in range(MIN_DATA_MESSAGES):
        capture.record("pulse-secret", "tibber-pulse-pulse-secret/publish", f"reading-{index}".encode())

    capture_file = next(tmp_path.glob("*.jsonl"))
    capture_text = capture_file.read_text(encoding="utf-8")
    records = [json.loads(line) for line in capture_text.splitlines()]
    saved_status = json.loads(base64.b64decode(records[0]["payload"]))["status"]
    synthetic_id = saved_status["ID"]
    assert synthetic_id.startswith("test-")
    assert saved_status == {
        "ID": synthetic_id,
        "hwmodel": "Pulse P1",
        "Build": "firmware-secret-build",
        "IP": "[redacted]",
        "ssid": "[redacted]",
        "password": "[redacted]",
    }
    assert "pulse-secret" not in capture_text
    assert records[0]["topic"] == f"tibber-pulse-{synthetic_id}/publish"


def test_capture_redacts_known_obis_identifiers_and_flags_opaque_data(tmp_path):
    capture = RawMessageCapture(tmp_path)
    status_payload = b'{"status":{"ID":"pulse-1","hwmodel":"Pulse P1"}}'
    capture.record("device", "pulse/status", status_payload, is_status=True)
    capture.set_status("device", {"ID": "pulse-1", "hwmodel": "Pulse P1"}, "pulse/status", status_payload)
    obis_payload = b"/ABC5\\n0-0:96.1.1(123456789)\\n1-0:1.8.0(123.45*kWh)!"
    capture.record("device", "pulse/data", obis_payload)
    for index in range(MIN_DATA_MESSAGES - 1):
        capture.record("device", "pulse/data", bytes([0xFF, index]))

    capture_file = next(tmp_path.glob("*.jsonl"))
    records = [json.loads(line) for line in capture_file.read_text(encoding="utf-8").splitlines()]
    saved_obis = base64.b64decode(records[1]["payload"])
    assert b"123456789" not in saved_obis
    assert b"1-0:1.8.0(123.45*kWh)" in saved_obis
    assert records[1]["payload_redacted"] is True
    assert records[2]["payload_opaque"] is True
    assert base64.b64decode(records[2]["payload"]) == bytes([0xFF, 0])


def test_capture_uses_distinct_synthetic_ids_for_devices(tmp_path):
    capture = RawMessageCapture(tmp_path)
    for device_key, original_id in (("device-one", "pulse-original-one"), ("device-two", "pulse-original-two")):
        topic = f"tibber-pulse-{device_key}/publish"
        payload = json.dumps({"status": {"ID": original_id, "hwmodel": "Pulse P1"}}).encode()
        capture.record(device_key, topic, payload, is_status=True)
        capture.set_status(device_key, {"ID": original_id, "hwmodel": "Pulse P1"}, topic, payload)
        for index in range(MIN_DATA_MESSAGES):
            capture.record(device_key, topic, f"reading-{index}".encode())

    ids = set()
    for capture_file in tmp_path.glob("*.jsonl"):
        first_record = json.loads(capture_file.read_text(encoding="utf-8").splitlines()[0])
        synthetic_id = json.loads(base64.b64decode(first_record["payload"]))["status"]["ID"]
        ids.add(synthetic_id)
        assert first_record["topic"] == f"tibber-pulse-{synthetic_id}/publish"
        assert "pulse-original" not in capture_file.read_text(encoding="utf-8")
    assert len(ids) == 2
