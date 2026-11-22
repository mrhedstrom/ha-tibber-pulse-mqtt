"""Switch that turns Tibber Cloud forwarding of a Pulse on or off."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .cloud_entity import TibberCloudEntity, async_remove_stale_cloud_entities
from .const import DOMAIN, SUBENTRY_TYPE_CLOUD_FORWARDING

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add one forwarding switch per Tibber Cloud forwarding."""
    hub = hass.data[DOMAIN][entry.entry_id]
    # Attached to the Pulse device, not to the subentry. See async_remove_stale_cloud_entities.
    async_remove_stale_cloud_entities(hass, entry, "switch")
    for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_CLOUD_FORWARDING):
        async_add_entities([TibberCloudForwardingSwitch(hub, subentry)])


class TibberCloudForwardingSwitch(TibberCloudEntity, SwitchEntity):
    """On: data is forwarded to Tibber Cloud. Off: the connection is closed.

    The choice is kept by the hub, not in the subentry, because changing the subentry
    would reload the whole integration.
    """

    _attr_translation_key = "cloud_forwarding"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hub, subentry) -> None:
        super().__init__(hub, subentry)
        self._attr_unique_id = f"tibber_{self._pulse_id}_cloud_forwarding"

    @property
    def is_on(self) -> bool:
        return self._hub.cloud_enabled(self._pulse_id)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)

    async def _async_set(self, enabled: bool) -> None:
        try:
            await self._hub.async_set_cloud_enabled(self._pulse_id, enabled)
        except (KeyError, TypeError, ValueError) as err:
            raise HomeAssistantError(
                f"Could not change Tibber Cloud forwarding for Pulse {self._pulse_id}: {err}"
            ) from err
