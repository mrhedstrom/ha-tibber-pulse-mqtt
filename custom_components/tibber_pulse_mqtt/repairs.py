from __future__ import annotations

import logging

from datetime import datetime, timedelta
from pathlib import Path
from fnmatch import fnmatch

from homeassistant.components.repairs import RepairsFlow
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import issue_registry as ir
from homeassistant.exceptions import HomeAssistantError


from .const import (
    DOMAIN,
    RAW_CAPTURE_DIR,
    CAPTURE_RETENTION_DAYS,
)

_LOGGER = logging.getLogger(__name__)

class OldCaptureCleanupRepairFlow(RepairsFlow):
    """Repair flow for cleaning up old raw capture files."""

    def __init__(
        self,
        issue_id: str,
        data: dict | None,
    ) -> None:
        self.issue_id = issue_id
        self.data = data or {}

    async def async_step_init(
        self,
        user_input=None,
    ):
        return await self.async_step_confirm()

    async def async_step_confirm(
        self,
        user_input=None,
    ):
        """Delete raw capture files older than the retention period."""
        if user_input is None:
            return self.async_show_form(
            step_id="confirm",
        )

        deleted = await self.hass.async_add_executor_job(
            self._delete_old_captures,
        )

        ir.async_delete_issue(
            self.hass,
            DOMAIN,
            "old_raw_captures",
        )

        return self.async_create_entry(
            title="",
            data={
                "deleted_files": deleted,
            },
        )

    def _delete_old_captures(self):
        """Delete old raw capture files."""
        capture_dir = Path(
            self.hass.config.path(RAW_CAPTURE_DIR)
        )

        cutoff = datetime.now() - timedelta(
            days=CAPTURE_RETENTION_DAYS
        )

        deleted = 0

        if capture_dir.exists():
            for file in capture_dir.glob("*.jsonl"):
                try:
                    if (
                        datetime.fromtimestamp(
                            file.stat().st_mtime
                        )
                        < cutoff
                    ):
                        file.unlink()
                        deleted += 1
                except OSError:
                    continue

        return deleted

class CapturedMeterRepairFlow(RepairsFlow):
    """Repair flow for unsupported meters."""

    def __init__(
        self,
        issue_id: str,
        data: dict | None,
    ) -> None:
        self.issue_id = issue_id
        self.data = data or {}

    async def async_step_init(
        self,
        user_input=None,
    ):
        return await self.async_step_confirm()

    async def async_step_confirm(
        self,
        user_input=None,
    ):
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
            )

        entry_id = self.data.get("entry_id")

        if not entry_id:
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                self.issue_id,
            )

            return self.async_create_entry(
                title="",
                data={},
            )

        hub = self.hass.data[DOMAIN].get(entry_id)

        if hub is None:
            raise HomeAssistantError(
                "Integration is not loaded"
            )

        await hub.dispatcher.capture_manager.async_resolve_captured_meter(
            self.issue_id,
            self.data.get("device_key"),
            self.data.get("capture_path"),
        )

        return self.async_create_entry(
            title="",
            data={},
        )


REPAIR_FLOWS = {
    "old_raw_captures": OldCaptureCleanupRepairFlow,
    "unsupported_meter_*": CapturedMeterRepairFlow,
    "raw_capture_saved_*": CapturedMeterRepairFlow,
}

async def async_create_fix_flow(
    hass,
    issue_id: str,
    data: dict | None,
) -> RepairsFlow:
    """Create repair flow."""

    for pattern, flow_cls in REPAIR_FLOWS.items():
        if fnmatch(issue_id, pattern):
            return flow_cls(issue_id, data)

    raise HomeAssistantError(
        f"Unsupported repair issue: {issue_id}"
    )