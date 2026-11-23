"""Tests for the entities of a Tibber Cloud forwarding: connection sensor and switch."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory

from custom_components.tibber_pulse_mqtt import cloud_entity
from custom_components.tibber_pulse_mqtt import sensor as sensor_platform
from custom_components.tibber_pulse_mqtt import switch as switch_platform
from custom_components.tibber_pulse_mqtt.const import (
    CLOUD_STATES,
    DOMAIN,
    SUBENTRY_TYPE_CLOUD_FORWARDING,
)
from custom_components.tibber_pulse_mqtt.sensor import TibberCloudConnectionSensor
from custom_components.tibber_pulse_mqtt.switch import TibberCloudForwardingSwitch
from tests.cloud_helpers import PULSE_ID, TOPIC, run

SUBENTRY = SimpleNamespace(subentry_id="sub1", data={"pulse_id": PULSE_ID, "local_topic": TOPIC})


class FakeCloudHub:
    def __init__(self, status=None, states=None, disabled=()):
        self.dispatcher = SimpleNamespace(
            discovered_pulses={PULSE_ID: {"topic": TOPIC, "status": status}} if status is not None else {}
        )
        self.states = states or {}
        self.disabled = set(disabled)
        self.listeners = []
        self.calls = []
        self.error = None

    def cloud_state(self, pulse_id):
        return self.states.get(pulse_id, "disabled")

    def cloud_enabled(self, pulse_id):
        return pulse_id not in self.disabled

    def async_add_cloud_listener(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    async def async_set_cloud_enabled(self, pulse_id, enabled):
        self.calls.append((pulse_id, enabled))
        if self.error:
            raise self.error


def device_fields(info) -> dict:
    """DeviceInfo is a plain mapping in older Home Assistant versions and an object in newer."""
    return info.as_dict() if hasattr(info, "as_dict") else dict(info)


def with_state_writes(entity):
    entity.writes = 0

    def write():
        entity.writes += 1

    entity.async_write_ha_state = write
    return entity


# ------------------------------- connection sensor ------------------------------


def test_sensor_shows_the_state_of_the_forwarding():
    hub = FakeCloudHub(states={PULSE_ID: "connected"})
    sensor = TibberCloudConnectionSensor(hub, SUBENTRY)
    assert sensor.native_value == "connected"
    hub.states[PULSE_ID] = "auth_failed"
    assert sensor.native_value == "auth_failed"


def test_sensor_describes_itself_as_a_translated_diagnostic_enum():
    sensor = TibberCloudConnectionSensor(FakeCloudHub(), SUBENTRY)
    assert sensor.device_class == SensorDeviceClass.ENUM
    assert sensor.options == list(CLOUD_STATES)
    assert sensor.entity_category == EntityCategory.DIAGNOSTIC
    assert sensor.translation_key == "cloud_connection"
    assert sensor.has_entity_name is True
    assert sensor.should_poll is False
    assert sensor.unique_id == f"tibber_{PULSE_ID}_cloud_connection"


def test_every_state_the_hub_can_report_is_a_sensor_option():
    for state in ("connected", "connecting", "disconnected", "auth_failed", "invalid_certificate", "disabled"):
        assert state in CLOUD_STATES


# ----------------------------------- switch -------------------------------------


def test_switch_reflects_whether_forwarding_is_enabled():
    hub = FakeCloudHub()
    switch = TibberCloudForwardingSwitch(hub, SUBENTRY)
    assert switch.is_on is True
    hub.disabled.add(PULSE_ID)
    assert switch.is_on is False


def test_switch_describes_itself_as_a_translated_config_entity():
    switch = TibberCloudForwardingSwitch(FakeCloudHub(), SUBENTRY)
    assert switch.entity_category == EntityCategory.CONFIG
    assert switch.translation_key == "cloud_forwarding"
    assert switch.has_entity_name is True
    assert switch.unique_id == f"tibber_{PULSE_ID}_cloud_forwarding"


def test_switch_asks_the_hub_to_turn_forwarding_on_and_off():
    hub = FakeCloudHub()
    switch = TibberCloudForwardingSwitch(hub, SUBENTRY)
    run(switch.async_turn_off())
    run(switch.async_turn_on())
    assert hub.calls == [(PULSE_ID, False), (PULSE_ID, True)]


@pytest.mark.parametrize("error", [ValueError("no forwarding"), KeyError("pulse_id"), TypeError("bad")])
def test_switch_reports_a_forwarding_that_cannot_be_changed(error):
    hub = FakeCloudHub()
    hub.error = error
    with pytest.raises(HomeAssistantError):
        run(TibberCloudForwardingSwitch(hub, SUBENTRY).async_turn_on())


# ------------------------------- shared behaviour -------------------------------


@pytest.mark.parametrize("entity_class", [TibberCloudConnectionSensor, TibberCloudForwardingSwitch])
def test_entities_attach_to_the_pulse_device_without_overwriting_it_before_status_is_known(entity_class):
    info = device_fields(entity_class(FakeCloudHub(), SUBENTRY).device_info)
    assert info == {"identifiers": {(f"tibber_pulse_{PULSE_ID}",)}}


@pytest.mark.parametrize("entity_class", [TibberCloudConnectionSensor, TibberCloudForwardingSwitch])
def test_entities_use_the_same_device_description_as_the_obis_sensors(entity_class):
    hub = FakeCloudHub(status={"hwmodel": "P1", "Build": "1.2.3", "ID": PULSE_ID})
    info = device_fields(entity_class(hub, SUBENTRY).device_info)
    assert info == {
        "identifiers": {(f"tibber_pulse_{PULSE_ID}",)},
        "manufacturer": "Tibber",
        "model": "P1",
        "name": f"Tibber P1 ({PULSE_ID})",
        "sw_version": "1.2.3",
    }


@pytest.mark.parametrize("entity_class", [TibberCloudConnectionSensor, TibberCloudForwardingSwitch])
def test_entities_update_only_for_their_own_pulse(entity_class):
    hub = FakeCloudHub()
    entity = with_state_writes(entity_class(hub, SUBENTRY))
    run(entity.async_added_to_hass())
    assert len(hub.listeners) == 1

    hub.listeners[0]("someone-else")
    assert entity.writes == 0
    hub.listeners[0](PULSE_ID)
    assert entity.writes == 1


@pytest.mark.parametrize("entity_class", [TibberCloudConnectionSensor, TibberCloudForwardingSwitch])
def test_entities_stop_listening_when_removed(entity_class):
    hub = FakeCloudHub()
    entity = entity_class(hub, SUBENTRY)
    run(entity.async_added_to_hass())
    for unsubscribe in entity._on_remove:
        unsubscribe()
    assert hub.listeners == []


# ------------------------------- platform setup ---------------------------------


class FakeEntityRegistry:
    def __init__(self, entries=()):
        self.entries = list(entries)
        self.removed: list[str] = []

    def async_remove(self, entity_id):
        self.removed.append(entity_id)


def registered(domain, unique_id, config_subentry_id=None):
    return SimpleNamespace(
        domain=domain,
        unique_id=unique_id,
        entity_id=f"{domain}.{unique_id}",
        config_subentry_id=config_subentry_id,
    )


@pytest.fixture
def entity_registry(monkeypatch):
    fake = FakeEntityRegistry()
    monkeypatch.setattr(cloud_entity.er, "async_get", lambda hass: fake)
    monkeypatch.setattr(
        cloud_entity.er, "async_entries_for_config_entry", lambda registry, entry_id: registry.entries
    )
    return fake


def make_hass(hub, subentries):
    entry = SimpleNamespace(
        entry_id="entry1",
        get_subentries_of_type=lambda subentry_type: subentries if subentry_type == SUBENTRY_TYPE_CLOUD_FORWARDING else [],
    )
    return SimpleNamespace(data={DOMAIN: {"entry1": hub}}), entry


def test_switch_platform_adds_one_switch_per_forwarding_to_the_pulse_device(entity_registry):
    second = SimpleNamespace(subentry_id="sub2", data={"pulse_id": "112233445566", "local_topic": TOPIC})
    hass, entry = make_hass(FakeCloudHub(), [SUBENTRY, second])
    added = []
    run(switch_platform.async_setup_entry(hass, entry, lambda entities, **kwargs: added.append((entities, kwargs))))
    assert [type(entities[0]) for entities, _ in added] == [TibberCloudForwardingSwitch] * 2
    assert [entities[0].unique_id for entities, _ in added] == [
        f"tibber_{PULSE_ID}_cloud_forwarding",
        "tibber_112233445566_cloud_forwarding",
    ]
    # Not linked to the subentry: a device linked to both an entry and a subentry is shown
    # under the subentry only until the OBIS sensors appear, then jumps elsewhere.
    assert all(kwargs == {} for _entities, kwargs in added)


def test_switch_platform_adds_nothing_without_forwarding(entity_registry):
    hass, entry = make_hass(FakeCloudHub(), [])
    added = []
    run(switch_platform.async_setup_entry(hass, entry, lambda *args, **kwargs: added.append(args)))
    assert added == []


def test_sensor_platform_adds_the_connection_sensor_to_the_pulse_device(entity_registry):
    hass, entry = make_hass(FakeCloudHub(), [SUBENTRY])
    hass.data[DOMAIN]["entry1"].sensor_manager = None
    added = []
    run(sensor_platform.async_setup_entry(hass, entry, lambda entities, **kwargs: added.append((entities, kwargs))))
    cloud = [(entities, kwargs) for entities, kwargs in added if isinstance(entities[0], TibberCloudConnectionSensor)]
    assert len(cloud) == 1
    assert cloud[0][1] == {}, "not linked to the subentry"


# ---- cleanup of registry entries


def test_entities_of_a_removed_forwarding_are_deleted(entity_registry):
    entity_registry.entries = [
        registered("switch", f"tibber_{PULSE_ID}_cloud_forwarding"),
        registered("switch", "tibber_112233445566_cloud_forwarding"),
    ]
    _hass, entry = make_hass(FakeCloudHub(), [SUBENTRY])
    cloud_entity.async_remove_stale_cloud_entities(None, entry, "switch")
    assert entity_registry.removed == ["switch.tibber_112233445566_cloud_forwarding"]


def test_entities_an_earlier_version_registered_under_the_subentry_are_recreated(entity_registry):
    entity_registry.entries = [
        registered("sensor", f"tibber_{PULSE_ID}_cloud_connection", config_subentry_id="sub1"),
    ]
    _hass, entry = make_hass(FakeCloudHub(), [SUBENTRY])
    cloud_entity.async_remove_stale_cloud_entities(None, entry, "sensor")
    assert entity_registry.removed == [f"sensor.tibber_{PULSE_ID}_cloud_connection"]


def test_cleanup_never_touches_other_entities(entity_registry):
    entity_registry.entries = [
        registered("sensor", f"tibber_{PULSE_ID}_1-0_1_8_0"),  # an OBIS sensor of the same Pulse
        registered("sensor", f"tibber_{PULSE_ID}_cloud_connection"),  # wanted
        registered("switch", "tibber_gone_cloud_forwarding"),  # another platform's job
        registered("sensor", "something_else"),
        registered("sensor", None),
    ]
    _hass, entry = make_hass(FakeCloudHub(), [SUBENTRY])
    cloud_entity.async_remove_stale_cloud_entities(None, entry, "sensor")
    assert entity_registry.removed == []


def test_cleanup_without_any_forwarding_removes_all_forwarding_entities(entity_registry):
    entity_registry.entries = [
        registered("sensor", f"tibber_{PULSE_ID}_cloud_connection"),
        registered("sensor", f"tibber_{PULSE_ID}_1-0_1_8_0"),
    ]
    _hass, entry = make_hass(FakeCloudHub(), [])
    cloud_entity.async_remove_stale_cloud_entities(None, entry, "sensor")
    assert entity_registry.removed == [f"sensor.tibber_{PULSE_ID}_cloud_connection"]
