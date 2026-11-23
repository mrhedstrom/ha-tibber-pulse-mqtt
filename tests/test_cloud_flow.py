"""Tests for the Tibber Cloud forwarding subentry flow."""
from __future__ import annotations

import ssl
from types import SimpleNamespace

import pytest
import voluptuous as vol
from homeassistant.data_entry_flow import FlowResultType, section

from custom_components.tibber_pulse_mqtt import cloud_forwarder
from custom_components.tibber_pulse_mqtt.cloud_certs import (
    commit_staged_credentials,
    stage_credentials,
)
from custom_components.tibber_pulse_mqtt.cloud_forwarder import (
    CloudAuthError,
    receive_topic_for,
)
from custom_components.tibber_pulse_mqtt.config_flow import (
    CloudForwardingSubentryFlow,
    TibberLocalConfigFlow,
)
from custom_components.tibber_pulse_mqtt.const import (
    DEFAULT_CLOUD_HOST,
    DOMAIN,
    SUBENTRY_TYPE_CLOUD_FORWARDING,
)
from tests.cloud_helpers import PULSE_ID, TOPIC, FakeHass, make_pems, run

ENTRY_ID = "entry1"
RECEIVE = receive_topic_for(TOPIC)
PULSES = {PULSE_ID: {"topic": TOPIC, "status": {"hwmodel": "P1"}}}


# --------------------------------- helpers ------------------------------------


class FakeHub:
    def __init__(self, pulses=PULSES, running=()):
        self.dispatcher = SimpleNamespace(discovered_pulses=pulses)
        self._running = set(running)
        self.removed: list[str] = []
        self.added: list = []

    def has_forwarder(self, pulse_id):
        return pulse_id in self._running

    async def async_remove_forwarder(self, pulse_id):
        self.removed.append(pulse_id)
        self._running.discard(pulse_id)

    async def async_add_forwarder(self, config, on_state=None):
        self.added.append(config)
        self._running.add(config.pulse_id)


class Updates:
    """Records async_update_subentry calls on the fake config entries."""

    def __init__(self, changed=True):
        self.changed = changed
        self.calls: list[dict] = []

    def __call__(self, entry, subentry, *, title, data):
        self.calls.append({"title": title, "data": dict(data)})
        return self.changed


def make_flow(tmp_path, hub, subentries=(), reconfigure=None, changed=True):
    subentries = list(subentries)
    entry = SimpleNamespace(
        entry_id=ENTRY_ID,
        subentries={s.subentry_id: s for s in subentries},
        get_subentries_of_type=lambda subentry_type: subentries,
    )
    updates = Updates(changed)
    hass = FakeHass(tmp_path)
    hass.data = {DOMAIN: {ENTRY_ID: hub}} if hub is not None else {}
    hass.config_entries = SimpleNamespace(
        async_get_known_entry=lambda entry_id: entry,
        async_update_subentry=updates,
    )
    flow = CloudForwardingSubentryFlow()
    flow.hass = hass
    flow.handler = (ENTRY_ID, SUBENTRY_TYPE_CLOUD_FORWARDING)
    flow.flow_id = "flow1"
    flow.context = (
        {"source": "reconfigure", "subentry_id": reconfigure.subentry_id}
        if reconfigure
        else {"source": "user"}
    )
    flow.updates = updates
    return flow


def credentials_dir_of(tmp_path):
    return tmp_path / ".storage" / "tibber_pulse_mqtt" / "certs" / PULSE_ID


def form_input(certificate=None, private_key=None, **advanced):
    default_certificate, default_key = make_pems()
    return {
        "certificate": default_certificate if certificate is None else certificate,
        "private_key": default_key if private_key is None else private_key,
        "advanced": {
            "mqtt_topic": TOPIC,
            "mqtt_topic_sub": RECEIVE,
            "mqtt_url": DEFAULT_CLOUD_HOST,
            "mqtt_port": 8883,
            "tls_version": "tlsv1.2",
            **advanced,
        },
    }


@pytest.fixture
def cloud(monkeypatch):
    """Replace the network connection test; ``cloud.outcome`` is raised when set."""

    class Cloud:
        outcome = None
        configs: list = []

    state = Cloud()
    state.configs = []

    def fake_test_connection(config, timeout=None):
        state.configs.append(config)
        if state.outcome is not None:
            raise state.outcome

    monkeypatch.setattr(cloud_forwarder, "test_connection", fake_test_connection)
    return state


def schema_defaults(schema):
    defaults = {}
    for key, value in schema.schema.items():
        if isinstance(value, section):
            defaults[str(key)] = schema_defaults(value.schema)
        elif callable(getattr(key, "default", None)):
            defaults[str(key)] = key.default()
    return defaults


def add_subentry(tmp_path, **data):
    """A stored forwarding with real credentials on disk."""
    directory = credentials_dir_of(tmp_path)
    certificate, private_key = make_pems()
    commit_staged_credentials(stage_credentials(directory, certificate, private_key), directory)
    return SimpleNamespace(
        subentry_id="sub1",
        unique_id=PULSE_ID,
        title=f"Pulse {PULSE_ID}",
        data={"pulse_id": PULSE_ID, "local_topic": TOPIC, **data},
    )


# ------------------------------ choosing a Pulse ------------------------------


def test_the_integration_offers_cloud_forwarding_as_a_subentry():
    types = TibberLocalConfigFlow.async_get_supported_subentry_types(SimpleNamespace())
    assert types == {SUBENTRY_TYPE_CLOUD_FORWARDING: CloudForwardingSubentryFlow}


def test_flow_aborts_when_the_integration_is_not_loaded(tmp_path):
    flow = make_flow(tmp_path, hub=None)
    result = run(flow.async_step_user())
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "entry_not_loaded")


def test_flow_aborts_when_no_pulse_has_been_discovered(tmp_path):
    result = run(make_flow(tmp_path, FakeHub(pulses={})).async_step_user())
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "no_pulse_available")


def test_only_pulses_without_forwarding_and_with_usable_topics_are_offered(tmp_path):
    pulses = {
        "free1": {"topic": "tibber-pulse-a/publish", "status": {"hwmodel": "P1"}},
        "taken": {"topic": "tibber-pulse-b/publish", "status": {}},
        "badtopic": {"topic": "weird", "status": {}},
        "bad/id": {"topic": "tibber-pulse-c/publish", "status": {}},
        "model": {"topic": "tibber-pulse-d/publish", "status": {"Hw": "F"}},
    }
    existing = [SimpleNamespace(subentry_id="s1", unique_id="taken", data={}, title="")]
    result = run(make_flow(tmp_path, FakeHub(pulses), existing).async_step_user())
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "user"
    selector_config = list(result["data_schema"].schema.values())[0].config
    assert [(o["value"], o["label"]) for o in selector_config["options"]] == [
        ("free1", "P1 (free1)"),
        ("model", "F (model)"),
    ]


def test_all_pulses_already_forwarded_aborts(tmp_path):
    existing = [SimpleNamespace(subentry_id="s1", unique_id=PULSE_ID, data={}, title="")]
    result = run(make_flow(tmp_path, FakeHub(), existing).async_step_user())
    assert result["reason"] == "no_pulse_available"


def test_choosing_a_pulse_leads_to_the_credentials_form_with_defaults(tmp_path):
    flow = make_flow(tmp_path, FakeHub())
    result = run(flow.async_step_user({"pulse_id": PULSE_ID}))
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "credentials"
    assert result["description_placeholders"] == {"pulse": PULSE_ID}
    assert schema_defaults(result["data_schema"]) == {
        "advanced": {
            "mqtt_topic": TOPIC,
            "mqtt_topic_sub": RECEIVE,
            "mqtt_url": DEFAULT_CLOUD_HOST,
            "mqtt_port": 8883,
            "tls_version": "tlsv1.2",
        }
    }


def test_credentials_step_aborts_if_the_pulse_disappeared(tmp_path):
    flow = make_flow(tmp_path, FakeHub())
    run(flow.async_step_user({"pulse_id": PULSE_ID}))
    flow.hass.data[DOMAIN][ENTRY_ID] = FakeHub(pulses={})
    assert run(flow.async_step_credentials())["reason"] == "no_pulse_available"


# --------------------------------- the form -----------------------------------


def test_add_form_requires_certificate_and_key_and_hides_the_rest_in_a_collapsed_section():
    schema = CloudForwardingSubentryFlow._credentials_schema(TOPIC, {}, optional=False)
    keys = {str(key): key for key in schema.schema}
    assert isinstance(keys["certificate"], vol.Required)
    assert isinstance(keys["private_key"], vol.Required)
    advanced = schema.schema[keys["advanced"]]
    assert isinstance(advanced, section) and advanced.options["collapsed"] is True
    assert {str(key) for key in advanced.schema.schema} == {
        "ca_cert", "mqtt_topic", "mqtt_topic_sub", "mqtt_url", "mqtt_port", "tls_version",
    }


def test_reconfigure_form_makes_certificate_and_key_optional():
    schema = CloudForwardingSubentryFlow._credentials_schema(TOPIC, {}, optional=True)
    keys = {str(key): key for key in schema.schema}
    assert isinstance(keys["certificate"], vol.Optional)
    assert isinstance(keys["private_key"], vol.Optional)


def test_reconfigure_form_defaults_to_the_stored_settings():
    current = {"mqtt_url": "example.test", "mqtt_port": 443, "tls_version": "tlsv1.3",
               "mqtt_topic": "out", "mqtt_topic_sub": "in"}
    schema = CloudForwardingSubentryFlow._credentials_schema(TOPIC, current, optional=True)
    assert schema_defaults(schema)["advanced"] == {
        "mqtt_topic": "out", "mqtt_topic_sub": "in", "mqtt_url": "example.test",
        "mqtt_port": 443, "tls_version": "tlsv1.3",
    }


# ------------------------------- adding a Pulse --------------------------------


def submit(flow, user_input):
    run(flow.async_step_user({"pulse_id": PULSE_ID}))
    return run(flow.async_step_credentials(user_input))


def test_adding_stores_credentials_and_only_the_necessary_settings(tmp_path, cloud):
    flow = make_flow(tmp_path, FakeHub())
    user_input = form_input()
    result = submit(flow, user_input)

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["unique_id"] == PULSE_ID
    assert result["title"] == f"Pulse {PULSE_ID}"
    assert result["data"] == {"pulse_id": PULSE_ID, "local_topic": TOPIC}, (
        "defaults must not be stored, so a changed default reaches everyone"
    )

    directory = credentials_dir_of(tmp_path)
    assert sorted(p.name for p in directory.iterdir()) == ["cert.pem", "key.pem"]
    assert (directory / "cert.pem").read_text(encoding="ascii") == user_input["certificate"]

    # The connection was tested with the staged files, before anything was activated.
    (tested,) = cloud.configs
    assert tested.cert_path.parent.name.startswith(".staged-")
    assert tested.ca_path is None


def test_adding_with_advanced_settings_stores_the_deviations_and_a_custom_ca(tmp_path, cloud):
    certificate, _ = make_pems()
    flow = make_flow(tmp_path, FakeHub())
    result = submit(
        flow,
        form_input(
            ca_cert=certificate,
            mqtt_url="example.test",
            mqtt_port=443,
            tls_version="tlsv1.3",
            mqtt_topic="cloud/out",
            mqtt_topic_sub="cloud/in",
        ),
    )
    assert result["data"] == {
        "pulse_id": PULSE_ID,
        "local_topic": TOPIC,
        "mqtt_url": "example.test",
        "mqtt_port": 443,
        "tls_version": "tlsv1.3",
        "mqtt_topic": "cloud/out",
        "mqtt_topic_sub": "cloud/in",
    }
    (tested,) = cloud.configs
    assert tested.ca_path is not None and tested.ca_path.name == "ca.pem"
    assert (credentials_dir_of(tmp_path) / "ca.pem").read_text(encoding="ascii") == certificate


def test_number_selector_floats_are_accepted_as_port(tmp_path, cloud):
    result = submit(make_flow(tmp_path, FakeHub()), form_input(mqtt_port=8883.0))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert "mqtt_port" not in result["data"]


def test_missing_advanced_section_falls_back_to_defaults(tmp_path, cloud):
    user_input = form_input()
    del user_input["advanced"]
    result = submit(make_flow(tmp_path, FakeHub()), user_input)
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {"pulse_id": PULSE_ID, "local_topic": TOPIC}


# ------------------------------ validation errors ------------------------------


def invalid_cases():
    good_cert, good_key = make_pems()
    expired_cert, expired_key = make_pems(not_before_days=-10, not_after_days=-1)
    future_cert, future_key = make_pems(not_before_days=1)
    mismatch_cert, mismatch_key = make_pems(key_index=0, cert_key_index=1)
    return [
        ("only-cert", form_input(certificate=good_cert, private_key=""), "credentials_incomplete"),
        ("only-key", form_input(certificate="", private_key=good_key), "credentials_incomplete"),
        ("neither", form_input(certificate="", private_key=""), "credentials_incomplete"),
        ("bad-cert", form_input(certificate="garbage"), "certificate_invalid"),
        ("bad-key", form_input(private_key="garbage"), "key_invalid"),
        ("mismatch", form_input(certificate=mismatch_cert, private_key=mismatch_key), "key_mismatch"),
        ("expired", form_input(certificate=expired_cert, private_key=expired_key), "certificate_expired"),
        ("future", form_input(certificate=future_cert, private_key=future_key), "certificate_not_yet_valid"),
        ("bad-ca", form_input(ca_cert="garbage"), "ca_invalid"),
        ("bad-host", form_input(mqtt_url="not a host"), "invalid_settings"),
        ("bad-port", form_input(mqtt_port="abc"), "invalid_settings"),
        ("bad-topic", form_input(mqtt_topic="a/+/b"), "invalid_settings"),
        ("bad-tls", form_input(tls_version="tlsv1.0"), "invalid_settings"),
    ]


@pytest.mark.parametrize(
    "user_input, expected",
    [pytest.param(i, e, id=name) for name, i, e in invalid_cases()],
)
def test_invalid_input_shows_the_reason_and_stores_nothing(tmp_path, cloud, user_input, expected):
    flow = make_flow(tmp_path, FakeHub())
    result = submit(flow, user_input)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "credentials"
    assert result["errors"] == {"base": expected}
    assert cloud.configs == [], "the cloud is never contacted with unusable input"
    assert not credentials_dir_of(tmp_path).exists(), "nothing may be left behind"


def test_the_form_is_redisplayed_with_what_the_user_typed(tmp_path, cloud):
    user_input = form_input(certificate="garbage", mqtt_url="example.test")
    result = submit(make_flow(tmp_path, FakeHub()), user_input)
    suggested = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if getattr(key, "description", None)
    }
    assert suggested["certificate"] == "garbage"


@pytest.mark.parametrize(
    "outcome, expected",
    [
        (CloudAuthError("rejected"), "auth_failed"),
        (ConnectionError("down"), "cannot_connect"),
        (TimeoutError("slow"), "cannot_connect"),
        (ssl.SSLError("bad"), "certificate_invalid"),
        (FileNotFoundError("gone"), "certificate_invalid"),
    ],
    ids=["rejected", "no-connection", "timeout", "tls-error", "file-gone"],
)
def test_connection_failures_are_reported_and_nothing_is_stored(tmp_path, cloud, outcome, expected):
    cloud.outcome = outcome
    result = submit(make_flow(tmp_path, FakeHub()), form_input())
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": expected}
    assert not credentials_dir_of(tmp_path).exists()


def test_a_failed_add_can_be_retried_after_fixing_the_problem(tmp_path, cloud):
    flow = make_flow(tmp_path, FakeHub())
    cloud.outcome = ConnectionError("down")
    assert submit(flow, form_input())["errors"] == {"base": "cannot_connect"}
    cloud.outcome = None
    result = run(flow.async_step_credentials(form_input()))
    assert result["type"] == FlowResultType.CREATE_ENTRY


# -------------------------------- reconfiguring --------------------------------


def reconfigure_flow(tmp_path, hub, changed=True, **data):
    subentry = add_subentry(tmp_path, **data)
    return make_flow(tmp_path, hub, [subentry], reconfigure=subentry, changed=changed), subentry


def test_reconfigure_form_shows_the_stored_settings(tmp_path):
    flow, _ = reconfigure_flow(tmp_path, FakeHub(), mqtt_port=443)
    result = run(flow.async_step_reconfigure())
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "reconfigure"
    assert schema_defaults(result["data_schema"])["advanced"]["mqtt_port"] == 443


def test_settings_can_change_without_pasting_the_credentials_again(tmp_path, cloud):
    hub = FakeHub(running={PULSE_ID})
    flow, _ = reconfigure_flow(tmp_path, hub, mqtt_port=443)
    before = (credentials_dir_of(tmp_path) / "cert.pem").read_text(encoding="ascii")

    result = run(flow.async_step_reconfigure(form_input(certificate="", private_key="")))

    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "reconfigure_successful")
    assert flow.updates.calls == [
        {"title": f"Pulse {PULSE_ID}", "data": {"pulse_id": PULSE_ID, "local_topic": TOPIC}}
    ], "the port is back at its default, so it is no longer stored"
    (tested,) = cloud.configs
    assert tested.cert_path == credentials_dir_of(tmp_path) / "cert.pem", "tested with the stored files"
    assert (credentials_dir_of(tmp_path) / "cert.pem").read_text(encoding="ascii") == before
    assert hub.removed == [PULSE_ID], "the running forwarder steps aside during the test"
    assert hub.added == [], "the changed entry is reloaded, which starts forwarding again"


def test_forwarding_is_restarted_when_a_reconfigure_changes_nothing_that_reloads(tmp_path, cloud):
    hub = FakeHub(running={PULSE_ID})
    flow, _ = reconfigure_flow(tmp_path, hub, changed=False)
    run(flow.async_step_reconfigure(form_input(certificate="", private_key="")))
    assert hub.removed == [PULSE_ID]
    (restarted,) = hub.added
    assert restarted.pulse_id == PULSE_ID
    assert restarted.cert_path == credentials_dir_of(tmp_path) / "cert.pem"


def test_a_pulse_that_is_not_forwarding_is_not_touched_while_testing(tmp_path, cloud):
    hub = FakeHub()
    flow, _ = reconfigure_flow(tmp_path, hub, changed=False)
    run(flow.async_step_reconfigure(form_input(certificate="", private_key="")))
    assert hub.removed == [] and hub.added == []


def test_replacing_the_credentials_activates_them_only_after_a_successful_test(tmp_path, cloud):
    hub = FakeHub(running={PULSE_ID})
    flow, _ = reconfigure_flow(tmp_path, hub)
    new_certificate, new_key = make_pems(key_index=1)

    result = run(flow.async_step_reconfigure(form_input(new_certificate, new_key)))

    assert result["reason"] == "reconfigure_successful"
    directory = credentials_dir_of(tmp_path)
    assert (directory / "cert.pem").read_text(encoding="ascii") == new_certificate
    assert (directory / "key.pem").read_text(encoding="ascii") == new_key
    assert sorted(p.name for p in directory.iterdir()) == ["cert.pem", "key.pem"]
    assert cloud.configs[0].cert_path.parent.name.startswith(".staged-")


def test_replacing_credentials_without_a_ca_goes_back_to_the_bundled_ca(tmp_path, cloud):
    flow, subentry = reconfigure_flow(tmp_path, FakeHub())
    directory = credentials_dir_of(tmp_path)
    (directory / "ca.pem").write_text("old custom ca")
    run(flow.async_step_reconfigure(form_input(*make_pems(key_index=1))))
    assert not (directory / "ca.pem").exists()


def test_a_failed_test_keeps_the_old_credentials_and_restarts_forwarding(tmp_path, cloud):
    hub = FakeHub(running={PULSE_ID})
    flow, _ = reconfigure_flow(tmp_path, hub)
    directory = credentials_dir_of(tmp_path)
    before = (directory / "cert.pem").read_text(encoding="ascii")
    cloud.outcome = CloudAuthError("rejected")

    result = run(flow.async_step_reconfigure(form_input(*make_pems(key_index=1))))

    assert result["type"] == FlowResultType.FORM and result["errors"] == {"base": "auth_failed"}
    assert (directory / "cert.pem").read_text(encoding="ascii") == before
    assert sorted(p.name for p in directory.iterdir()) == ["cert.pem", "key.pem"]
    assert flow.updates.calls == []
    (restarted,) = hub.added
    assert restarted.cert_path == directory / "cert.pem", "the previous forwarder is back"


def test_reconfigure_requires_both_credentials_or_neither(tmp_path, cloud):
    flow, _ = reconfigure_flow(tmp_path, FakeHub())
    certificate, _ = make_pems()
    result = run(flow.async_step_reconfigure(form_input(certificate=certificate, private_key="")))
    assert result["errors"] == {"base": "credentials_incomplete"}
    assert cloud.configs == []


def test_reconfigure_aborts_when_the_integration_is_not_loaded(tmp_path):
    flow, _ = reconfigure_flow(tmp_path, FakeHub())
    flow.hass.data = {}
    assert run(flow.async_step_reconfigure())["reason"] == "entry_not_loaded"
