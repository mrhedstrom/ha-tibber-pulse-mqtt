from __future__ import annotations
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    CONF_DEBUG_LOG_COMPONENT,
    CONF_ENTRY_TYPE,
    ENTRY_TYPE_AWS_FORWARDING,
    CONF_DEVICE_ID,
    AWS_UNCONFIGURED_ISSUE_PREFIX,
    AWS_INVALID_CERT_ISSUE_PREFIX,
    AWS_CONNECTION_ISSUE_PREFIX,
)
from .views import RawCaptureDownloadView

from homeassistant.helpers import config_validation as cv

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor"]

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    # Nothing to do for YAML setup; we use config entries.
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_AWS_FORWARDING:
        from .aws_forwarder import AWSForwarder

        unsub_update = entry.add_update_listener(_update_listener)
        entry.async_on_unload(unsub_update)
        forwarder = AWSForwarder(hass, entry)
        entry.runtime_data = forwarder
        forwarding = hass.data.setdefault(DOMAIN, {}).setdefault("_aws_forwarders", {})
        forwarding[entry.entry_id] = forwarder
        try:
            await forwarder.async_start()
        except Exception:
            await forwarder.async_stop()
            forwarding.pop(entry.entry_id, None)
            entry.runtime_data = None
            raise
        return True

    from .coordinator import TibberLocalHub

    # Register the raw capture download view for serving raw captures.
    hass.http.register_view(
        RawCaptureDownloadView(hass)
    )

    # Register update listener (and ensure it is removed on unload).
    unsub_update = entry.add_update_listener(_update_listener)
    entry.async_on_unload(unsub_update)

    # Apply dynamic logging preference from entry data/options.
    cfg = {**entry.data, **entry.options}
    await _apply_component_logging(hass, cfg.get(CONF_DEBUG_LOG_COMPONENT, False))

    # Create and start the hub (exactly once per setup).
    hub = TibberLocalHub(hass, entry)
    entry.runtime_data = hub
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub

    # Start hub BEFORE forwarding platforms so entities can attach to a live backend.
    await hub.async_start()

    # Forward platforms (sensor, …)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options/data updates from the config flow."""
    forwarder = hass.data.get(DOMAIN, {}).get("_aws_forwarders", {}).get(entry.entry_id)
    if forwarder:
        forwarder.async_refresh_config()
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload platforms and stop the hub cleanly."""
    forwarding = hass.data.get(DOMAIN, {}).get("_aws_forwarders", {})
    forwarder = forwarding.get(entry.entry_id)
    if forwarder:
        await forwarder.async_stop()
        forwarding.pop(entry.entry_id, None)
        entry.runtime_data = None
        return True

    hub: TibberLocalHub = hass.data[DOMAIN].get(entry.entry_id)

    # First unload platforms (entities detach).
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Then stop the hub and drop reference (so a fresh setup creates a fresh listener).
    if hub:
        await hub.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        entry.runtime_data = None

    return ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove only credentials and Repairs owned by a deleted AWS entry."""
    if entry.data.get(CONF_ENTRY_TYPE) != ENTRY_TYPE_AWS_FORWARDING:
        return

    from .aws import async_remove_certificate_files
    from homeassistant.helpers import issue_registry as ir

    pulse_id = entry.data.get(CONF_DEVICE_ID, "")
    for runtime in hass.data.get(DOMAIN, {}).values():
        manager = getattr(runtime, "sensor_manager", None)
        if manager and hasattr(manager, "set_aws_forwarding_status"):
            manager.set_aws_forwarding_status(pulse_id, "not_configured")
    await async_remove_certificate_files(hass, pulse_id)
    for prefix in (
        AWS_UNCONFIGURED_ISSUE_PREFIX,
        AWS_INVALID_CERT_ISSUE_PREFIX,
        AWS_CONNECTION_ISSUE_PREFIX,
    ):
        ir.async_delete_issue(hass, DOMAIN, f"{prefix}{pulse_id}")


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Preserve existing broker entries while advancing the entry schema version."""
    if entry.version < 2:
        hass.config_entries.async_update_entry(entry, version=2)
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Explicit reload helper if you need one in the future."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)


async def _apply_component_logging(hass: HomeAssistant, enable_debug: bool) -> None:
    """Dynamically adjust logger level for this custom component."""
    # Prefer using the logger service if available
    if hass.services.has_service("logger", "set_level"):
        level = "debug" if enable_debug else "info"
        await hass.services.async_call(
            "logger",
            "set_level",
            {f"custom_components.{DOMAIN}": level},
            blocking=False,
        )
    else:
        # Fallback to python logger level on the component logger itself
        level = logging.DEBUG if enable_debug else logging.INFO
        logging.getLogger(f"custom_components.{DOMAIN}").setLevel(level)