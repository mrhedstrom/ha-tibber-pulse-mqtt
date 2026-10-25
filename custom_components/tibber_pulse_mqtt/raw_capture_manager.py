from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.loader import async_get_integration
from homeassistant.helpers.network import get_url

from .const import DOMAIN, CONF_SAVE_RAW_DATA
from .raw_capture import RawMessageCapture

MIN_DECODE_SAMPLES = 5
MAX_FAILED_RATIO = 0.50

DOCS_URL = (
    "https://github.com/mrhedstrom/ha-tibber-pulse-mqtt"
    "#sharing-raw-device-data"
)

ISSUE_URL = (
    "https://github.com/mrhedstrom/ha-tibber-pulse-mqtt/issues/new"
)

RAW_CAPTURE_DIR = "www/tibber_pulse_mqtt/raw"
RAW_CAPTURE_API_URL = "/api/tibber_pulse_mqtt/raw_capture"


class RawCaptureManager:
    """Manage automatic raw captures and Repairs."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry,
        devices: dict,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._devices = devices
        self._cfg = {**entry.data, **entry.options}

        self._raw_capture: RawMessageCapture | None = None

        self._decode_stats = defaultdict(
            lambda: {"ok": 0, "failed": 0}
        )

        self._automatic_capture_devices: set[str] = set()
        self._created_issue_ids: set[str] = set()
        self._integration_version = "Unknown"

        if self._cfg.get(CONF_SAVE_RAW_DATA, False):
            self._ensure_raw_capture()

    async def async_initialize(self) -> None:
        integration = await async_get_integration(self.hass, DOMAIN)
        self._integration_version = integration.version or "Unknown"

    def enable_manual_capture(self) -> None:
        self._ensure_raw_capture()

    def should_capture(self, device_key: str) -> bool:
        """Return whether messages for this device should be captured."""
        return bool(
            self._cfg.get(CONF_SAVE_RAW_DATA, False)
            or device_key in self._automatic_capture_devices
        )

    def record_decode_result(
        self,
        dev_id: str,
        success: bool,
    ) -> None:
        stats = self._decode_stats[dev_id]
        stats["ok" if success else "failed"] += 1

        total = stats["ok"] + stats["failed"]

        if (
            total >= MIN_DECODE_SAMPLES
            and stats["failed"] / total
            > MAX_FAILED_RATIO
        ):
            self.hass.loop.call_soon_threadsafe(
                self._register_automatic_capture,
                dev_id,
            )

    @callback
    def _register_automatic_capture(
        self,
        dev_id: str,
    ) -> None:
        if dev_id in self._automatic_capture_devices:
            return

        self._automatic_capture_devices.add(dev_id)
        self._ensure_raw_capture()

    def _ensure_raw_capture(self) -> None:
        if self._raw_capture is not None:
            return

        self._raw_capture = RawMessageCapture(
            self.hass.config.path(RAW_CAPTURE_DIR),
            on_capture_saved=self._on_capture_saved,
        )

    def capture(self) -> RawMessageCapture | None:
        return self._raw_capture

    def _on_capture_saved(
        self,
        device_key: str,
        path: Path,
    ) -> None:
        """Schedule capture reporting safely from an executor thread."""
        self.hass.add_job(
            self._async_report_capture,
            device_key,
            path,
        )

    async def _async_report_capture(
        self,
        device_key: str,
        path: Path,
    ) -> None:
        issue_id = (
            f"raw_capture_{self.entry.entry_id}_"
            f"{path.stem}"
        )

        if issue_id in self._created_issue_ids:
            return

        self._created_issue_ids.add(issue_id)

        unsupported_meter = device_key in self._automatic_capture_devices

        placeholders = {
            "device": device_key,
            "download_url":
                f"{get_url(self.hass)}{RAW_CAPTURE_API_URL}/{path.name}",
            "documentation_url": DOCS_URL,
            "issue_url": self._build_github_issue_url(
                device_key,
                path,
                unsupported_meter=unsupported_meter,
            ),
        }

        if device_key in self._automatic_capture_devices:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"unsupported_meter_{path.stem}",
                is_fixable=False,
                is_persistent=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="unsupported_meter",
                translation_placeholders=placeholders,
            )
            return

        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"raw_capture_saved_{path.stem}",
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="raw_capture_saved",
            translation_placeholders=placeholders,
        )

    def _build_github_issue_url(
        self,
        device_key: str,
        path: Path,
        *,
        unsupported_meter: bool,
    ) -> str:
        status = (
            self._devices
            .get(device_key, {})
            .get("status", {})
        )

        device_model = str(
            status.get("hwmodel")
            or device_key
        )

        firmware = str(
            status.get("Build")
            or status.get("build")
            or "<!-- Please provide the firmware version -->"
        )

        if unsupported_meter:
            brief = "Unsupported meter"
            behaviour = (
                "More than 50% of received payloads could not be decoded "
                "by the integration. This capture was collected automatically "
                "to help add support for the meter."
            )
        else:
            brief = "Working Ok"
            behaviour = (
                "Working meter capture provided for compatibility verification."
            )

        title = f"Raw capture: {device_model} - {brief}"

        body = f"""Device type/model (Pulse P1, HAN, or KM):
{device_model}

Meter manufacturer and model, if known:
<!-- Please provide the manufacturer and model of the meter if known. -->

Firmware/build version, if known:
{firmware}

Integration version:
{self._integration_version}

Raw capture file:
{path.name}

<!-- Don't forget to upload the file to the issue. -->

What behavior or unsupported data the capture demonstrates:
{behaviour}
"""

        return (
            ISSUE_URL
            + "?"
            + urlencode(
                {
                    "labels": "raw capture",
                    "template": "raw_capture.md",
                    "title": title,
                    "body": body,
                }
            )
        )