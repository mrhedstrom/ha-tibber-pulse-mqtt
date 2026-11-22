"""Tests for the Tibber Cloud forwarding repair flows."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError

from custom_components.tibber_pulse_mqtt import cloud_forwarder, repairs
from custom_components.tibber_pulse_mqtt.cloud_certs import (
    commit_staged_credentials,
    stage_credentials,
)
from custom_components.tibber_pulse_mqtt.cloud_forwarder import CloudAuthError
from custom_components.tibber_pulse_mqtt.const import DOMAIN, SUBENTRY_TYPE_CLOUD_FORWARDING
from custom_components.tibber_pulse_mqtt.repairs import (
    CloudCertificateRepairFlow,
    CloudConnectionRepairFlow,
    CloudSetupRepairFlow,
    async_create_fix_flow,
)
from tests.cloud_helpers import PULSE_ID, TOPIC, FakeHass, FakeIssues, make_pems, run

ENTRY_ID = "entry1"
CERT_ISSUE = f"cloud_certificate_expiring_{PULSE_ID}"


# --------------------------------- helpers ------------------------------------


class RepairHub:
    def __init__(self, running=True, enabled=True):
        self.running = running
        self.enabled = enabled
        self.removed: list[str] = []
        self.added: list = []
        self.resets: list[str] = []
        self.certificate_checks = 0

    def has_forwarder(self, pulse_id):
        return self.running

    def cloud_enabled(self, pulse_id):
        return self.enabled

    async def async_remove_forwarder(self, pulse_id):
        self.removed.append(pulse_id)
        self.running = False

    async def async_add_forwarder(self, config):
        self.added.append(config)
        self.running = True

    async def async_check_certificates(self):
        self.certificate_checks += 1

    async def async_reset_cloud_connection(self, pulse_id):
        self.resets.append(pulse_id)


@pytest.fixture
def env(monkeypatch):
    """Fake issue registry and connection test; ``env.outcome`` is raised when set."""
    issues = FakeIssues()
    monkeypatch.setattr(repairs, "ir", issues)

    class Env:
        pass

    state = Env()
    state.issues = issues
    state.outcome = None
    state.tested = []

    def fake_test_connection(config, timeout=None):
        state.tested.append(config)
        if state.outcome is not None:
            raise state.outcome

    monkeypatch.setattr(cloud_forwarder, "test_connection", fake_test_connection)
    return state


def credentials_dir_of(tmp_path):
    return tmp_path / ".storage" / "tibber_pulse_mqtt" / "certs" / PULSE_ID


def stored_subentry(tmp_path, **data):
    directory = credentials_dir_of(tmp_path)
    certificate, key = make_pems()
    commit_staged_credentials(stage_credentials(directory, certificate, key), directory)
    return SimpleNamespace(
        subentry_id="sub1",
        unique_id=PULSE_ID,
        title=f"Pulse {PULSE_ID}",
        data={"pulse_id": PULSE_ID, "local_topic": TOPIC, **data},
    )


def make_flow(flow_class, tmp_path, env, hub, subentries=(), data=None, issue_id=CERT_ISSUE, with_entry=True):
    subentries = list(subentries)
    entry = SimpleNamespace(
        entry_id=ENTRY_ID,
        subentries={s.subentry_id: s for s in subentries},
        get_subentries_of_type=lambda subentry_type: subentries,
    )
    hass = FakeHass(tmp_path)
    hass.data = {DOMAIN: {ENTRY_ID: hub}} if hub is not None else {}
    added = []
    hass.config_entries = SimpleNamespace(
        async_get_entry=lambda entry_id: entry if with_entry and entry_id == ENTRY_ID else None,
        async_add_subentry=lambda entry_, subentry: added.append(subentry),
    )
    flow = flow_class(
        issue_id,
        data if data is not None else {"entry_id": ENTRY_ID, "subentry_id": "sub1", "pulse_id": PULSE_ID},
    )
    flow.hass = hass
    flow.flow_id = "flow1"
    flow.handler = DOMAIN
    flow.context = {}
    flow.added_subentries = added
    return flow


def raise_issue(env, issue_id=CERT_ISSUE):
    env.issues.async_create_issue(
        None,
        DOMAIN,
        issue_id,
        translation_placeholders={"device": PULSE_ID, "days": "20", "date": "2026-12-12"},
    )


def pems(**kwargs):
    certificate, key = make_pems(**kwargs)
    return {"certificate": certificate, "private_key": key}


# --------------------------------- routing ------------------------------------


@pytest.mark.parametrize(
    "issue_id, expected",
    [
        ("cloud_certificate_expiring_x", CloudCertificateRepairFlow),
        ("cloud_certificate_expiring_soon_x", CloudCertificateRepairFlow),
        ("cloud_certificate_expired_x", CloudCertificateRepairFlow),
        ("cloud_certificate_invalid_x", CloudCertificateRepairFlow),
        ("cloud_auth_rejected_x", CloudCertificateRepairFlow),
        ("cloud_connection_unstable_x", CloudConnectionRepairFlow),
        ("cloud_pulse_discovered_x", CloudSetupRepairFlow),
    ],
)
def test_every_cloud_issue_has_a_repair_flow(issue_id, expected):
    flow = run(async_create_fix_flow(None, issue_id, {"entry_id": ENTRY_ID}))
    assert type(flow) is expected


def test_existing_repair_flows_still_route_and_unknown_issues_are_refused():
    assert type(run(async_create_fix_flow(None, "old_raw_captures", None))) is repairs.OldCaptureCleanupRepairFlow
    with pytest.raises(HomeAssistantError):
        run(async_create_fix_flow(None, "cloud_something_else_x", None))


# ----------------------------- replacing a certificate --------------------------


def test_certificate_form_shows_the_issue_placeholders(tmp_path, env):
    raise_issue(env)
    flow = make_flow(CloudCertificateRepairFlow, tmp_path, env, RepairHub(), [stored_subentry(tmp_path)])
    result = run(flow.async_step_init())
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "credentials"
    assert result["description_placeholders"] == {"device": PULSE_ID, "days": "20", "date": "2026-12-12"}
    assert {str(key) for key in result["data_schema"].schema} == {"certificate", "private_key"}


def test_a_new_certificate_replaces_the_old_and_forwarding_restarts(tmp_path, env):
    raise_issue(env)
    hub = RepairHub()
    flow = make_flow(CloudCertificateRepairFlow, tmp_path, env, hub, [stored_subentry(tmp_path)])
    new = pems(key_index=1)

    result = run(flow.async_step_credentials(new))

    assert result["type"] == FlowResultType.CREATE_ENTRY
    directory = credentials_dir_of(tmp_path)
    assert (directory / "cert.pem").read_text(encoding="ascii") == new["certificate"]
    assert (directory / "key.pem").read_text(encoding="ascii") == new["private_key"]
    assert sorted(p.name for p in directory.iterdir()) == ["cert.pem", "key.pem"]
    assert hub.removed == [PULSE_ID], "the old connection steps aside while the new one is tested"
    (restarted,) = hub.added
    assert restarted.cert_path == directory / "cert.pem"
    assert hub.certificate_checks == 1, "expiry issues are re-evaluated for the new certificate"
    assert env.issues.ids == [], "the issue is gone"


def test_the_new_certificate_is_tested_with_the_staged_files(tmp_path, env):
    flow = make_flow(CloudCertificateRepairFlow, tmp_path, env, RepairHub(), [stored_subentry(tmp_path)])
    run(flow.async_step_credentials(pems(key_index=1)))
    (tested,) = env.tested
    assert tested.cert_path.parent.name.startswith(".staged-")


def test_a_custom_ca_is_kept_when_the_certificate_is_replaced(tmp_path, env):
    subentry = stored_subentry(tmp_path)
    custom_ca = make_pems(key_index=2)[0]
    (credentials_dir_of(tmp_path) / "ca.pem").write_text(custom_ca, encoding="ascii")
    flow = make_flow(CloudCertificateRepairFlow, tmp_path, env, RepairHub(), [subentry])

    run(flow.async_step_credentials(pems(key_index=1)))

    assert (credentials_dir_of(tmp_path) / "ca.pem").read_text(encoding="ascii") == custom_ca
    assert env.tested[0].ca_path is not None


def test_a_switched_off_forwarding_stays_off_after_a_certificate_replacement(tmp_path, env):
    hub = RepairHub(running=False, enabled=False)
    flow = make_flow(CloudCertificateRepairFlow, tmp_path, env, hub, [stored_subentry(tmp_path)])
    result = run(flow.async_step_credentials(pems(key_index=1)))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert hub.removed == [] and hub.added == []


@pytest.mark.parametrize(
    "outcome, expected",
    [(CloudAuthError("no"), "auth_failed"), (ConnectionError("down"), "cannot_connect"), (TimeoutError(), "cannot_connect")],
)
def test_a_rejected_replacement_keeps_the_old_certificate_and_the_issue(tmp_path, env, outcome, expected):
    raise_issue(env)
    env.outcome = outcome
    hub = RepairHub()
    flow = make_flow(CloudCertificateRepairFlow, tmp_path, env, hub, [stored_subentry(tmp_path)])
    directory = credentials_dir_of(tmp_path)
    before = (directory / "cert.pem").read_text(encoding="ascii")

    result = run(flow.async_step_credentials(pems(key_index=1)))

    assert result["type"] == FlowResultType.FORM and result["errors"] == {"base": expected}
    assert (directory / "cert.pem").read_text(encoding="ascii") == before
    assert sorted(p.name for p in directory.iterdir()) == ["cert.pem", "key.pem"]
    assert env.issues.ids == [CERT_ISSUE]
    (restarted,) = hub.added
    assert restarted.cert_path == directory / "cert.pem", "the previous forwarding is back"
    assert hub.certificate_checks == 0


@pytest.mark.parametrize(
    "user_input, expected",
    [
        ({"certificate": "garbage", "private_key": "garbage"}, "certificate_invalid"),
        ({**pems(), "private_key": "garbage"}, "key_invalid"),
        (pems(key_index=0, cert_key_index=1), "key_mismatch"),
        (pems(not_before_days=-10, not_after_days=-1), "certificate_expired"),
    ],
    ids=["garbage", "bad-key", "mismatch", "expired"],
)
def test_unusable_credentials_are_explained_and_nothing_changes(tmp_path, env, user_input, expected):
    hub = RepairHub()
    flow = make_flow(CloudCertificateRepairFlow, tmp_path, env, hub, [stored_subentry(tmp_path)])
    before = (credentials_dir_of(tmp_path) / "cert.pem").read_text(encoding="ascii")

    result = run(flow.async_step_credentials(user_input))

    assert result["errors"] == {"base": expected}
    assert env.tested == [], "the cloud is never contacted with unusable credentials"
    assert (credentials_dir_of(tmp_path) / "cert.pem").read_text(encoding="ascii") == before
    assert len(hub.added) == 1, "the running forwarding is restored"


def test_what_the_user_typed_is_kept_when_the_form_is_shown_again(tmp_path, env):
    flow = make_flow(CloudCertificateRepairFlow, tmp_path, env, RepairHub(), [stored_subentry(tmp_path)])
    result = run(flow.async_step_credentials({"certificate": "garbage", "private_key": "also garbage"}))
    suggested = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
        if getattr(key, "description", None)
    }
    assert suggested == {"certificate": "garbage", "private_key": "also garbage"}


@pytest.mark.parametrize("case", ["no-hub", "no-subentry", "no-entry"])
def test_certificate_flow_aborts_when_the_forwarding_is_gone(tmp_path, env, case):
    flow = make_flow(
        CloudCertificateRepairFlow,
        tmp_path,
        env,
        None if case == "no-hub" else RepairHub(),
        [] if case == "no-subentry" else [stored_subentry(tmp_path)],
        with_entry=case != "no-entry",
    )
    result = run(flow.async_step_init())
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "entry_not_loaded")


def test_the_same_flow_handles_rejected_credentials(tmp_path, env):
    issue_id = f"cloud_auth_rejected_{PULSE_ID}"
    raise_issue(env, issue_id)
    hub = RepairHub()
    flow = make_flow(
        CloudCertificateRepairFlow, tmp_path, env, hub, [stored_subentry(tmp_path)], issue_id=issue_id
    )
    assert run(flow.async_step_credentials(pems(key_index=1)))["type"] == FlowResultType.CREATE_ENTRY
    assert env.issues.ids == []


# --------------------------- connection keeps dropping --------------------------


UNSTABLE_ISSUE = f"cloud_connection_unstable_{PULSE_ID}"


def test_unstable_connection_flow_asks_for_confirmation_first(tmp_path, env):
    flow = make_flow(CloudConnectionRepairFlow, tmp_path, env, RepairHub(), issue_id=UNSTABLE_ISSUE)
    result = run(flow.async_step_init())
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "confirm"


def test_confirming_restarts_forwarding_and_removes_the_notice(tmp_path, env):
    raise_issue(env, UNSTABLE_ISSUE)
    hub = RepairHub()
    flow = make_flow(CloudConnectionRepairFlow, tmp_path, env, hub, issue_id=UNSTABLE_ISSUE)
    result = run(flow.async_step_confirm({}))
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert hub.resets == [PULSE_ID]
    assert env.issues.ids == []


def test_unstable_connection_flow_aborts_without_the_integration(tmp_path, env):
    flow = make_flow(CloudConnectionRepairFlow, tmp_path, env, None, issue_id=UNSTABLE_ISSUE)
    assert run(flow.async_step_confirm({}))["reason"] == "entry_not_loaded"


# ------------------------------ discovered Pulse --------------------------------


DISCOVERED_ISSUE = f"cloud_pulse_discovered_{PULSE_ID}"
DISCOVERED_DATA = {"entry_id": ENTRY_ID, "pulse_id": PULSE_ID, "topic": TOPIC}


def setup_flow(tmp_path, env, hub=None, subentries=(), data=None, **kwargs):
    return make_flow(
        CloudSetupRepairFlow,
        tmp_path,
        env,
        hub if hub is not None else RepairHub(),
        subentries,
        data=data or DISCOVERED_DATA,
        issue_id=DISCOVERED_ISSUE,
        **kwargs,
    )


def test_discovered_pulse_flow_asks_for_the_certificate_and_key(tmp_path, env):
    raise_issue(env, DISCOVERED_ISSUE)
    result = run(setup_flow(tmp_path, env).async_step_init())
    assert result["type"] == FlowResultType.FORM and result["step_id"] == "credentials"
    assert {str(key) for key in result["data_schema"].schema} == {"certificate", "private_key"}


def test_forwarding_is_created_from_the_pasted_credentials(tmp_path, env):
    raise_issue(env, DISCOVERED_ISSUE)
    flow = setup_flow(tmp_path, env)
    user_input = pems()

    result = run(flow.async_step_credentials(user_input))

    assert result["type"] == FlowResultType.CREATE_ENTRY
    directory = credentials_dir_of(tmp_path)
    assert (directory / "cert.pem").read_text(encoding="ascii") == user_input["certificate"]
    assert sorted(p.name for p in directory.iterdir()) == ["cert.pem", "key.pem"]

    (subentry,) = flow.added_subentries
    assert dict(subentry.data) == {"pulse_id": PULSE_ID, "local_topic": TOPIC}, "defaults are not stored"
    assert subentry.subentry_type == SUBENTRY_TYPE_CLOUD_FORWARDING
    assert subentry.unique_id == PULSE_ID
    assert subentry.title == f"Pulse {PULSE_ID}"
    assert env.issues.ids == [], "the offer is gone"
    (tested,) = env.tested
    assert tested.cert_path.parent.name.startswith(".staged-")
    assert tested.local_publish_topic == TOPIC


@pytest.mark.parametrize(
    "user_input, outcome, expected",
    [
        ({"certificate": "garbage", "private_key": "garbage"}, None, "certificate_invalid"),
        (pems(key_index=0, cert_key_index=1), None, "key_mismatch"),
        (pems(), CloudAuthError("no"), "auth_failed"),
        (pems(), ConnectionError("down"), "cannot_connect"),
    ],
    ids=["garbage", "mismatch", "rejected", "unreachable"],
)
def test_failed_setup_stores_nothing_and_keeps_the_offer(tmp_path, env, user_input, outcome, expected):
    raise_issue(env, DISCOVERED_ISSUE)
    env.outcome = outcome
    flow = setup_flow(tmp_path, env)

    result = run(flow.async_step_credentials(user_input))

    assert result["type"] == FlowResultType.FORM and result["errors"] == {"base": expected}
    assert flow.added_subentries == []
    assert not credentials_dir_of(tmp_path).exists()
    assert env.issues.ids == [DISCOVERED_ISSUE]


def test_a_topic_that_cannot_be_forwarded_is_reported(tmp_path, env):
    flow = setup_flow(tmp_path, env, data={**DISCOVERED_DATA, "topic": "no-publish-topic"})
    result = run(flow.async_step_credentials(pems()))
    assert result["errors"] == {"base": "invalid_settings"}
    assert flow.added_subentries == []


def test_a_pulse_that_got_forwarding_in_the_meantime_closes_the_offer(tmp_path, env):
    raise_issue(env, DISCOVERED_ISSUE)
    existing = SimpleNamespace(subentry_id="sub9", unique_id=PULSE_ID, data={})
    result = run(setup_flow(tmp_path, env, subentries=[existing]).async_step_init())
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "already_configured")
    assert env.issues.ids == []


@pytest.mark.parametrize("case", ["no-hub", "no-entry"])
def test_discovered_pulse_flow_aborts_without_the_integration(tmp_path, env, case):
    flow = setup_flow(
        tmp_path,
        env,
        hub=RepairHub(),
        with_entry=case != "no-entry",
    )
    if case == "no-hub":
        flow.hass.data = {}
    assert run(flow.async_step_init())["reason"] == "entry_not_loaded"


# ------------------ regression: Home Assistant starts a fix flow with the issue data ------------------


def test_discovered_pulse_flow_shows_its_form_when_home_assistant_starts_it(tmp_path, env):
    """HA calls async_step_credentials(issue data), not async_step_credentials(None). It must not be read as input."""
    raise_issue(env, DISCOVERED_ISSUE)
    flow = setup_flow(tmp_path, env)

    result = run(flow.async_step_init(DISCOVERED_DATA))

    assert result["type"] == FlowResultType.FORM and result["step_id"] == "credentials"
    assert flow.added_subentries == [] and env.tested == []


@pytest.mark.parametrize(
    "issue_id",
    [
        f"cloud_certificate_expiring_{PULSE_ID}",
        f"cloud_certificate_expired_{PULSE_ID}",
        f"cloud_certificate_invalid_{PULSE_ID}",
        f"cloud_auth_rejected_{PULSE_ID}",
    ],
)
def test_certificate_flow_shows_its_form_when_home_assistant_starts_it(tmp_path, env, issue_id):
    raise_issue(env, issue_id)
    hub = RepairHub()
    flow = make_flow(
        CloudCertificateRepairFlow, tmp_path, env, hub, [stored_subentry(tmp_path)], issue_id=issue_id
    )
    issue_data = {"entry_id": ENTRY_ID, "subentry_id": "sub1", "pulse_id": PULSE_ID}

    result = run(flow.async_step_init(issue_data))

    assert result["type"] == FlowResultType.FORM and result["step_id"] == "credentials"
    assert hub.removed == [] and hub.added == [] and env.tested == []
    assert env.issues.ids == [issue_id]
