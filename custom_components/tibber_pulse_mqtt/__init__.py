from __future__ import annotations
import logging
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, CONF_DEBUG_LOG_COMPONENT
from .coordinator import TibberLocalHub

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor"]

CONFIG_SCHEMA = cv.platform_only_config_schema(DOMAIN)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    # Nothing to do for YAML setup; we use config entries.
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Register update listener (and ensure it is removed on unload).
    unsub_update = entry.add_update_listener(_update_listener)
    entry.async_on_unload(unsub_update)

    # Apply dynamic logging preference from entry data/options.
    cfg = {**entry.data, **entry.options}
    await _apply_component_logging(hass, cfg.get(CONF_DEBUG_LOG_COMPONENT, False))

    # Create and start the hub (exactly once per setup).
    hub = TibberLocalHub(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = hub

    # Start hub BEFORE forwarding platforms so entities can attach to a live backend.
    await hub.async_start()

    # Forward platforms (sensor, …)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options/data updates from the config flow."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload platforms and stop the hub cleanly."""
    hub: TibberLocalHub = hass.data[DOMAIN].get(entry.entry_id)

    # First unload platforms (entities detach).
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Then stop the hub and drop reference (so a fresh setup creates a fresh listener).
    if hub:
        await hub.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return ok


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