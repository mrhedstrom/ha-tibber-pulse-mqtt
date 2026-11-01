from __future__ import annotations

import logging

from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.loader import async_get_integration
from homeassistant.helpers.network import get_url

from .const import (
    DOMAIN,
    CONF_SAVE_RAW_DATA,
    RAW_CAPTURE_DIR,
    RAW_CAPTURE_API_URL,
    CAPTURE_RETENTION_DAYS,
    CAPTURE_WARNING_FILES,
    CAPTURE_WARNING_SIZE_MB,
)
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

_LOGGER = logging.getLogger(__name__)

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
        self._resolved_devices: set[str] = set()
        self._created_issue_ids: set[str] = set()
        self._integration_version = "Unknown"

        if self._cfg.get(CONF_SAVE_RAW_DATA, False):
            self._ensure_raw_capture()

    async def async_initialize(self) -> None:
        integration = await async_get_integration(self.hass, DOMAIN)
        self._integration_version = integration.version or "Unknown"
        await self.async_check_old_captures()

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
            dev_id not in self._resolved_devices
            and total >= MIN_DECODE_SAMPLES
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
        _LOGGER.info("Automatic raw capture enabled for unsupported device %s", dev_id)

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
                is_fixable=True,
                is_persistent=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key="unsupported_meter",
                translation_placeholders=placeholders,
                data={
                    "entry_id": self.entry.entry_id,
                    "device_key": device_key,
                    "capture_path": str(path),
                },
            )
            return

        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"raw_capture_saved_{path.stem}",
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="raw_capture_saved",
            translation_placeholders=placeholders,
            data={
                "entry_id": self.entry.entry_id,
                "device_key": device_key,
                "capture_path": str(path),
            },
        )

        await self.async_check_old_captures()

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

    async def async_resolve_captured_meter(
        self,
        issue_id: str,
        device_key: str,
        path_str: str,
    ) -> None:
        """Stop capture and delete the capture file."""

        if path_str:
            path = Path(path_str)
            try:
                await self.hass.async_add_executor_job(
                    self._delete_capture_file,
                    path,
                )
            except OSError:
                _LOGGER.warning(
                    "Unable to delete capture %s",
                    path,
                )

        self._resolved_devices.add(device_key)
        if device_key in self._automatic_capture_devices:
            self._automatic_capture_devices.discard(device_key)

        if (
            not self._automatic_capture_devices
        ):
            self._raw_capture = None

        if (
            not self._automatic_capture_devices
            and self.entry.options.get(CONF_SAVE_RAW_DATA, False)
        ):
            options = {
                **self.entry.options,
                CONF_SAVE_RAW_DATA: False,
            }

            self.hass.config_entries.async_update_entry(
                self.entry,
                options=options,
            )

        ir.async_delete_issue(
            self.hass,
            DOMAIN,
            issue_id,
        )

    def _delete_capture_file(
        self,
        path: Path,
    ) -> None:
        path.unlink(missing_ok=True)

    async def async_check_old_captures(self) -> None:
        """Create a repair if old captures consume disk space."""

        count, total_size_mb, old_files = (
            await self.hass.async_add_executor_job(
                self._analyze_capture_files
            )
        )

        if (
            not old_files
            or (
                len(old_files) < CAPTURE_WARNING_FILES
                and total_size_mb < CAPTURE_WARNING_SIZE_MB
            )
        ):
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                "old_raw_captures",
            )
            return

        ir.async_create_issue(
            self.hass,
            DOMAIN,
            "old_raw_captures",
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="old_raw_captures",
            translation_placeholders={
                "count": str(len(old_files)),
                "size": f"{total_size_mb:.1f}",
                "days": str(CAPTURE_RETENTION_DAYS),
            },
        )

    def _analyze_capture_files(
        self,
    ) -> tuple[int, float, list[Path]]:

        capture_dir = Path(
            self.hass.config.path(RAW_CAPTURE_DIR)
        )

        if not capture_dir.exists():
            return 0, 0.0, []

        files = list(
            capture_dir.glob("*.jsonl")
        )

        total_size_mb = (
            sum(f.stat().st_size for f in files)
            / 1024
            / 1024
        )

        cutoff = datetime.now() - timedelta(
            days=CAPTURE_RETENTION_DAYS
        )

        old_files = [
            f
            for f in files
            if datetime.fromtimestamp(
                f.stat().st_mtime
            ) < cutoff
        ]

        return (
            len(files),
            total_size_mb,
            old_files,
        )