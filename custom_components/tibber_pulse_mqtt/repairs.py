from __future__ import annotations

import logging

from datetime import datetime, timedelta
from pathlib import Path
from fnmatch import fnmatch

from types import MappingProxyType

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigSubentry
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import selector
from homeassistant.exceptions import HomeAssistantError


from .cloud_certs import commit_staged_credentials, credentials_dir
from .cloud_forwarder import (
    CloudForwardingConfig,
    async_replace_credentials,
    async_stage_and_test,
)
from .const import (
    DOMAIN,
    RAW_CAPTURE_DIR,
    CAPTURE_RETENTION_DAYS,
    CONF_LOCAL_TOPIC,
    CONF_PULSE_ID,
    SUBENTRY_TYPE_CLOUD_FORWARDING,
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


def _credentials_schema() -> vol.Schema:
    """Certificate and private key, as pasted from the Pulse setup page."""
    multiline = selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT, multiline=True)
    )
    return vol.Schema(
        {
            vol.Required("certificate"): multiline,
            vol.Required("private_key"): multiline,
        }
    )


class _CloudRepairFlow(RepairsFlow):
    """Shared plumbing of the Tibber Cloud forwarding repair flows."""

    def __init__(self, issue_id: str, data: dict | None) -> None:
        self.issue_id = issue_id
        self.data = data or {}

    def _hub(self):
        return self.hass.data.get(DOMAIN, {}).get(self.data.get("entry_id"))

    def _entry(self):
        return self.hass.config_entries.async_get_entry(self.data.get("entry_id"))

    def _subentry(self):
        entry = self._entry()
        return entry.subentries.get(self.data.get("subentry_id")) if entry else None

    def _placeholders(self) -> dict[str, str]:
        """The issue's placeholders; Home Assistant only adds them to the first form."""
        issue = ir.async_get(self.hass).async_get_issue(DOMAIN, self.issue_id)
        return dict(issue.translation_placeholders or {}) if issue else {}

    def _finish(self):
        ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
        return self.async_create_entry(title="", data={})


class CloudCertificateRepairFlow(_CloudRepairFlow):
    """Replace the certificate and private key of a Pulse's Tibber Cloud forwarding.

    Used for certificates that are about to expire, have expired, cannot be read, or are
    rejected by Tibber Cloud. The new credentials are tested before they replace the old.
    """

    async def async_step_init(self, user_input=None) -> FlowResult:
        # Home Assistant starts a fix flow with the issue's data as input. That is not
        # form input, so it must not reach the credentials step.
        return await self.async_step_credentials()

    async def async_step_credentials(self, user_input=None) -> FlowResult:
        hub, subentry = self._hub(), self._subentry()
        if hub is None or subentry is None:
            return self.async_abort(reason="entry_not_loaded")

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                error = await async_replace_credentials(
                    self.hass,
                    hub,
                    subentry,
                    user_input["certificate"].strip(),
                    user_input["private_key"].strip(),
                )
            except (KeyError, TypeError, ValueError):
                error = "invalid_settings"
            if error is None:
                return self._finish()
            errors["base"] = error

        schema = _credentials_schema()
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="credentials",
            data_schema=schema,
            errors=errors,
            description_placeholders=self._placeholders(),
        )


class CloudConnectionRepairFlow(_CloudRepairFlow):
    """Restart a forwarding that keeps dropping, after an old bridge has been removed."""

    async def async_step_init(self, user_input=None) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input=None) -> FlowResult:
        if user_input is None:
            return self.async_show_form(step_id="confirm")

        hub = self._hub()
        if hub is None:
            return self.async_abort(reason="entry_not_loaded")
        await hub.async_reset_cloud_connection(self.data["pulse_id"])
        return self._finish()


class CloudSetupRepairFlow(_CloudRepairFlow):
    """Set up Tibber Cloud forwarding for a newly discovered Pulse."""

    async def async_step_init(self, user_input=None) -> FlowResult:
        # See CloudCertificateRepairFlow: the input of the first call is the issue data.
        return await self.async_step_credentials()

    async def async_step_credentials(self, user_input=None) -> FlowResult:
        entry, hub = self._entry(), self._hub()
        if entry is None or hub is None:
            return self.async_abort(reason="entry_not_loaded")
        pulse_id, topic = self.data["pulse_id"], self.data["topic"]
        if any(
            subentry.unique_id == pulse_id
            for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_CLOUD_FORWARDING)
        ):
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            return self.async_abort(reason="already_configured")

        errors: dict[str, str] = {}
        if user_input is not None:
            error = await self._async_create(entry, pulse_id, topic, user_input)
            if error is None:
                return self._finish()
            errors["base"] = error

        schema = _credentials_schema()
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="credentials",
            data_schema=schema,
            errors=errors,
            description_placeholders=self._placeholders(),
        )

    async def _async_create(self, entry, pulse_id: str, topic: str, user_input) -> str | None:
        """Test the credentials and add the forwarding. Returns an error key or None."""
        directory = credentials_dir(self.hass.config.path, pulse_id)
        try:
            template = CloudForwardingConfig(
                pulse_id=pulse_id,
                local_publish_topic=topic,
                cert_path=directory / "cert.pem",
                key_path=directory / "key.pem",
            )
        except ValueError:
            return "invalid_settings"

        stage, error = await async_stage_and_test(
            self.hass,
            template,
            user_input["certificate"].strip(),
            user_input["private_key"].strip(),
        )
        if error is not None:
            return error

        await self.hass.async_add_executor_job(commit_staged_credentials, stage, directory)
        # Adding the subentry reloads the integration, which starts the forwarding.
        self.hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=MappingProxyType({CONF_PULSE_ID: pulse_id, CONF_LOCAL_TOPIC: topic}),
                subentry_type=SUBENTRY_TYPE_CLOUD_FORWARDING,
                title=f"Pulse {pulse_id}",
                unique_id=pulse_id,
            ),
        )
        return None


REPAIR_FLOWS = {
    "old_raw_captures": OldCaptureCleanupRepairFlow,
    "unsupported_meter_*": CapturedMeterRepairFlow,
    "raw_capture_saved_*": CapturedMeterRepairFlow,
    "cloud_certificate_*": CloudCertificateRepairFlow,
    "cloud_auth_rejected_*": CloudCertificateRepairFlow,
    "cloud_connection_unstable_*": CloudConnectionRepairFlow,
    "cloud_pulse_discovered_*": CloudSetupRepairFlow,
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