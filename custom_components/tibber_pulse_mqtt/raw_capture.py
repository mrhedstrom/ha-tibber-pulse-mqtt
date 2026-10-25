from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import threading
from pathlib import Path
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

MIN_DATA_MESSAGES = 4
MIN_DATA_MESSAGES_WITHOUT_STATUS = 15
MAX_CAPTURE_MESSAGES = 100
_SAFE_STATUS_KEYS = {
    "hwmodel", "build", "hw", "dsmr", "baud", "ntc", "rssi", "uptime", "heap"
}
_SENSITIVE_OBIS = {"0-0:1.0.0", "0-0:96.1.0", "0-0:96.1.1", "0-0:42.0.0", "0-0:96.13.0", "0-0:96.13.1"}
_OBIS_VALUE = re.compile(r"(\d+-\d+:\d+\.\d+\.\d+\()([^)]*)(\))")
_OBIS_CODE = re.compile(r"\d+-\d+:\d+\.\d+\.\d+")


class RawMessageCapture:
    """Save raw MQTT messages after a device has supplied enough useful data."""

    def __init__(
        self,
        directory: str | Path,
        on_capture_saved: Callable[[str, Path], None] | None = None,
    ):
        self._directory = Path(directory)
        self._on_capture_saved = on_capture_saved
        self._lock = threading.Lock()
        self._messages: dict[str, list[dict[str, Any]]] = {}
        self._statuses: dict[str, dict[str, Any]] = {}
        self._status_messages: dict[str, dict[str, Any]] = {}
        self._saved_paths: dict[str, Path] = {}
        self._data_message_counts: dict[str, int] = {}
        self._saved_message_counts: dict[str, int] = {}
        self._synthetic_ids: dict[str, str] = {}

    def record(self, device_key: str, topic: str, payload: bytes, *, is_status: bool = False) -> None:
        """Buffer a raw message or append it if the device is already eligible."""
        with self._lock:
            synthetic_id = self._synthetic_ids.setdefault(device_key, f"test-{secrets.token_hex(8)}")
            message = self._sanitize_message(topic, payload, synthetic_id, is_status=is_status)
            saved_path = self._saved_paths.get(device_key)
            if saved_path:
                if self._saved_message_counts[device_key] >= MAX_CAPTURE_MESSAGES:
                    return
                try:
                    self._append(saved_path, [message])
                    self._saved_message_counts[device_key] += 1
                except OSError:
                    _LOGGER.exception("Unable to append raw MQTT capture for %s", saved_path.name)
                return

            messages = self._messages.setdefault(device_key, [])
            messages.append(message)
            if not is_status:
                self._data_message_counts[device_key] = self._data_message_counts.get(device_key, 0) + 1
            if len(messages) > MAX_CAPTURE_MESSAGES:
                del messages[0]
            self._save_if_ready(device_key)

    def set_status(self, device_key: str, status: dict[str, Any], topic: str, payload: bytes) -> None:
        """Record device metadata from a parsed STATUS message."""
        if not isinstance(status, dict):
            return
        with self._lock:
            synthetic_id = self._synthetic_ids.setdefault(device_key, f"test-{secrets.token_hex(8)}")
            self._statuses[device_key] = status
            self._status_messages[device_key] = self._sanitize_message(topic, payload, synthetic_id, is_status=True)
            self._save_if_ready(device_key)

    def _save_if_ready(self, device_key: str) -> None:
        """Create a capture file once enough data messages have been collected."""
        if device_key in self._saved_paths:
            return

        status = self._statuses.get(device_key, {})

        required_messages = (
            MIN_DATA_MESSAGES
            if status
            else MIN_DATA_MESSAGES_WITHOUT_STATUS
        )

        if self._data_message_counts.get(device_key, 0) < required_messages:
            return

        hwmodel = re.sub(
            r"[^A-Za-z0-9._-]+",
            "_",
            str(status.get("hwmodel") or "unknown_model"),
        ).strip("._-")
        hwmodel = hwmodel or "unknown_model"

        suffix = secrets.token_hex(5)
        path = self._directory / f"{hwmodel[:80]}-{suffix}.jsonl"

        messages = list(self._messages.get(device_key, []))

        status_message = self._status_messages.get(device_key)
        if status_message is not None and status_message not in messages:
            messages.append(status_message)

        if len(messages) > MAX_CAPTURE_MESSAGES:
            messages = messages[-MAX_CAPTURE_MESSAGES:]

        try:
            self._append(path, messages)
        except OSError:
            _LOGGER.exception(
                "Unable to save raw MQTT capture for %s",
                hwmodel,
            )
            return

        self._saved_paths[device_key] = path
        self._saved_message_counts[device_key] = len(messages)
        self._messages.pop(device_key, None)

        if self._on_capture_saved is not None:
            try:
                self._on_capture_saved(device_key, path)
            except Exception:
                _LOGGER.exception(
                    "Raw capture saved callback failed for %s",
                    device_key,
                )

    @classmethod
    def _sanitize_message(cls, topic: str, payload: bytes, synthetic_id: str, *, is_status: bool) -> dict[str, Any]:
        safe_topic = "/".join(
            f"tibber-pulse-{synthetic_id}" if part.casefold().startswith("tibber-pulse-") else
            part if part.casefold() in {"tibber-pulse", "publish", "status", "data"} else "[redacted]"
            for part in topic.split("/")
        )
        safe_payload, redacted, opaque = cls._sanitize_payload(payload, synthetic_id)
        return {
            "topic": safe_topic,
            "message_type": "status" if is_status else "data",
            "topic_redacted": safe_topic != topic,
            "payload": base64.b64encode(safe_payload).decode("ascii"),
            "payload_redacted": redacted,
            "payload_opaque": opaque,
        }

    @classmethod
    def _sanitize_payload(cls, payload: bytes, synthetic_id: str) -> tuple[bytes, bool, bool]:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            return payload, False, True

        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            if not _OBIS_CODE.search(text):
                return payload, False, True
            redacted_text = _OBIS_VALUE.sub(
                lambda match: f"{match.group(1)}[redacted]{match.group(3)}"
                if match.group(1)[:-1] in _SENSITIVE_OBIS
                else match.group(0),
                text,
            )
            return redacted_text.encode("utf-8"), redacted_text != text, False

        if not isinstance(value, dict) or not isinstance(value.get("status"), dict):
            return payload, False, True
        sanitized = cls._sanitize_json(value, synthetic_id=synthetic_id)
        encoded = json.dumps(sanitized, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return encoded, encoded != payload, False

    @classmethod
    def _sanitize_json(cls, value: Any, key: str = "", *, synthetic_id: str) -> Any:
        key_lower = key.casefold()
        if key_lower == "id":
            return synthetic_id
        if key_lower in _SAFE_STATUS_KEYS:
            return value
        if isinstance(value, dict):
            sanitized = {}
            for child_key, child_value in value.items():
                if str(child_key).casefold() == "status":
                    sanitized[child_key] = {
                        nested_key: cls._sanitize_json(nested_value, str(nested_key), synthetic_id=synthetic_id)
                        if str(nested_key).casefold() in _SAFE_STATUS_KEYS | {"id"}
                        else "[redacted]"
                        for nested_key, nested_value in child_value.items()
                    } if isinstance(child_value, dict) else "[redacted]"
                else:
                    sanitized[child_key] = "[redacted]"
            return sanitized
        if isinstance(value, list):
            return [cls._sanitize_json(item, synthetic_id=synthetic_id) for item in value]
        return "[redacted]"

    @staticmethod
    def _append(path: Path, messages: list[dict[str, Any]]) -> None:
        if not messages:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as capture_file:
            for message in messages:
                capture_file.write(json.dumps(message, separators=(",", ":")) + "\n")
