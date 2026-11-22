"""Base class for the entities that belong to a Tibber Cloud forwarding subentry."""
from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo, Entity

from .const import CONF_PULSE_ID, SUBENTRY_TYPE_CLOUD_FORWARDING

# unique_id suffix of the entities each platform adds for a forwarding
CLOUD_UNIQUE_ID_SUFFIXES = {"sensor": "_cloud_connection", "switch": "_cloud_forwarding"}


@callback
def async_remove_stale_cloud_entities(hass, entry, domain: str) -> None:
    """Remove forwarding entities that no longer belong to anything.

    The entities are attached to the Pulse device but not to the subentry (a device that
    is linked to both an entry and one of its subentries is shown in a different place
    once the OBIS sensors appear), so Home Assistant does not remove them with the
    subentry. This removes the entities of deleted forwardings, and entities an earlier
    version registered under the subentry, which are then created again.
    """
    suffix = CLOUD_UNIQUE_ID_SUFFIXES[domain]
    keep = {
        f"tibber_{subentry.data[CONF_PULSE_ID]}{suffix}"
        for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_CLOUD_FORWARDING)
        if CONF_PULSE_ID in subentry.data
    }
    registry = er.async_get(hass)
    for registered in er.async_entries_for_config_entry(registry, entry.entry_id):
        unique_id = registered.unique_id or ""
        if registered.domain != domain or not (
            unique_id.startswith("tibber_") and unique_id.endswith(suffix)
        ):
            continue
        if unique_id not in keep or registered.config_subentry_id is not None:
            registry.async_remove(registered.entity_id)


class TibberCloudEntity(Entity):
    """Entity of one Pulse's Tibber Cloud forwarding, attached to the Pulse device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub, subentry) -> None:
        self._hub = hub
        self._pulse_id: str = subentry.data[CONF_PULSE_ID]

    @property
    def device_info(self) -> DeviceInfo:
        """Attach to the Pulse device, which is keyed by Pulse ID like the OBIS sensors.

        Model, name and software version are only sent once the STATUS message is known,
        so this entity never overwrites what the OBIS sensors have set.
        """
        identifiers = {(f"tibber_pulse_{self._pulse_id}",)}
        status = self._hub.dispatcher.discovered_pulses.get(self._pulse_id, {}).get("status") or {}
        model = status.get("hwmodel")
        if not model:
            return DeviceInfo(identifiers=identifiers)
        return DeviceInfo(
            identifiers=identifiers,
            manufacturer="Tibber",
            model=model,
            name=f"Tibber {model} ({self._pulse_id})",
            sw_version=status.get("Build"),
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._hub.async_add_cloud_listener(self._handle_cloud_update))

    @callback
    def _handle_cloud_update(self, pulse_id: str) -> None:
        if pulse_id == self._pulse_id:
            self.async_write_ha_state()
