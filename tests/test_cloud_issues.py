"""Tests for the Repairs issues of Tibber Cloud forwarding."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.tibber_pulse_mqtt import cloud_issues
from custom_components.tibber_pulse_mqtt.cloud_issues import (
    CloudIssueReporter,
    async_clear_pulse_discovered,
    async_remove_stale_issues,
    async_report_pulse_discovered,
)
from custom_components.tibber_pulse_mqtt.const import (
    CLOUD_STATE_AUTH_FAILED,
    CLOUD_STATE_CONNECTED,
    CLOUD_STATE_CONNECTING,
    CLOUD_STATE_DISABLED,
    CLOUD_STATE_DISCONNECTED,
    CLOUD_STATE_INVALID_CERTIFICATE,
    DOMAIN,
)
from tests.cloud_helpers import PULSE_ID, TOPIC, FakeHass, FakeIssues, make_pems, run

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def issues(monkeypatch):
    fake = FakeIssues()
    monkeypatch.setattr(cloud_issues, "ir", fake)
    return fake


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_reporter(clock=None):
    return CloudIssueReporter(
        FakeHass(), "entry1", "sub1", PULSE_ID, clock=clock or Clock(), now=lambda: NOW
    )


def drop(reporter, times=1):
    for _ in range(times):
        reporter.on_state(CLOUD_STATE_CONNECTED)
        reporter.on_state(CLOUD_STATE_DISCONNECTED)


# ------------------------------- connection state ------------------------------


def test_rejected_credentials_raise_a_fixable_persistent_issue(issues):
    make_reporter().on_state(CLOUD_STATE_AUTH_FAILED)
    assert issues.ids == [f"cloud_auth_rejected_{PULSE_ID}"]
    issue = issues.issues[(DOMAIN, f"cloud_auth_rejected_{PULSE_ID}")]
    assert issue["is_fixable"] is True and issue["is_persistent"] is True
    assert issue["severity"] == "error"
    assert issue["translation_key"] == "cloud_auth_rejected"
    assert issue["translation_placeholders"] == {"device": PULSE_ID}
    assert issue["data"] == {"entry_id": "entry1", "subentry_id": "sub1", "pulse_id": PULSE_ID}


def test_an_unreadable_certificate_raises_an_issue(issues):
    make_reporter().on_state(CLOUD_STATE_INVALID_CERTIFICATE)
    assert issues.ids == [f"cloud_certificate_invalid_{PULSE_ID}"]


def test_a_working_connection_clears_the_issues_it_caused(issues):
    reporter = make_reporter()
    reporter.on_state(CLOUD_STATE_AUTH_FAILED)
    reporter.on_state(CLOUD_STATE_CONNECTED)
    assert issues.ids == []

    reporter.on_state(CLOUD_STATE_INVALID_CERTIFICATE)
    reporter.on_state(CLOUD_STATE_CONNECTED)
    assert issues.ids == []


@pytest.mark.parametrize("state", [CLOUD_STATE_CONNECTING, CLOUD_STATE_DISCONNECTED, CLOUD_STATE_DISABLED])
def test_other_states_do_not_raise_issues(issues, state):
    make_reporter().on_state(state)
    assert issues.ids == []


def test_repeated_drops_point_at_a_second_client_using_the_same_id(issues):
    reporter = make_reporter()
    drop(reporter, 4)
    assert issues.ids == []
    drop(reporter, 1)
    assert issues.ids == [f"cloud_connection_unstable_{PULSE_ID}"]
    assert issues.issues[(DOMAIN, f"cloud_connection_unstable_{PULSE_ID}")]["severity"] == "warning"


def test_drops_far_apart_do_not_count(issues):
    clock = Clock()
    reporter = make_reporter(clock)
    for _ in range(10):
        drop(reporter)
        clock.now += 400  # more than two minutes of margin each time the window slides
    assert issues.ids == []


def test_failing_to_connect_is_not_a_drop(issues):
    reporter = make_reporter()
    for _ in range(10):
        reporter.on_state(CLOUD_STATE_CONNECTING)
        reporter.on_state(CLOUD_STATE_DISCONNECTED)
    assert issues.ids == []


def test_clearing_the_unstable_issue_forgets_earlier_drops(issues):
    reporter = make_reporter()
    drop(reporter, 5)
    reporter.clear_unstable()
    assert issues.ids == []
    drop(reporter, 4)
    assert issues.ids == []


# ------------------------------ certificate expiry ------------------------------


def expiring_in(days, seconds=0):
    return NOW + timedelta(days=days, seconds=seconds)


@pytest.mark.parametrize(
    "not_after, expected",
    [
        (expiring_in(90), None),
        (expiring_in(31), None),
        (expiring_in(30, 60), "cloud_certificate_expiring"),
        (expiring_in(8, 60), "cloud_certificate_expiring"),
        (expiring_in(7, 60), "cloud_certificate_expiring_soon"),
        (expiring_in(0, 60), "cloud_certificate_expiring_soon"),
        (expiring_in(0, -60), "cloud_certificate_expired"),
        (expiring_in(-30), "cloud_certificate_expired"),
    ],
    ids=["90d", "31d", "30d", "8d", "7d", "today", "just-expired", "long-expired"],
)
def test_expiry_thresholds(issues, not_after, expected):
    make_reporter().apply_expiry(not_after)
    assert issues.ids == ([f"{expected}_{PULSE_ID}"] if expected else [])


def test_expiry_issue_describes_the_certificate(issues):
    make_reporter().apply_expiry(expiring_in(20, 60))
    issue = issues.issues[(DOMAIN, f"cloud_certificate_expiring_{PULSE_ID}")]
    assert issue["translation_placeholders"] == {
        "device": PULSE_ID, "days": "20", "date": (NOW + timedelta(days=20, seconds=60)).date().isoformat(),
    }
    assert issue["severity"] == "warning"


def test_warning_escalates_with_a_new_issue_id_so_a_dismissed_one_stays_dismissed(issues):
    reporter = make_reporter()
    reporter.apply_expiry(expiring_in(20, 60))
    assert issues.ids == [f"cloud_certificate_expiring_{PULSE_ID}"]
    reporter.apply_expiry(expiring_in(5, 60))
    assert issues.ids == [f"cloud_certificate_expiring_soon_{PULSE_ID}"]
    reporter.apply_expiry(expiring_in(-1))
    assert issues.ids == [f"cloud_certificate_expired_{PULSE_ID}"]


def test_a_readable_certificate_is_no_longer_invalid_and_a_new_one_clears_everything(issues):
    reporter = make_reporter()
    reporter.on_state(CLOUD_STATE_INVALID_CERTIFICATE)
    reporter.apply_expiry(expiring_in(3, 60))
    assert issues.ids == [f"cloud_certificate_expiring_soon_{PULSE_ID}"]
    reporter.apply_expiry(expiring_in(365))
    assert issues.ids == []


def test_stored_certificate_is_checked_from_its_file(issues, tmp_path):
    cert = tmp_path / "cert.pem"
    cert.write_text(make_pems(not_after_days=5)[0], encoding="ascii")
    # The real clock is used for the file, so give the reporter the real clock too.
    reporter = CloudIssueReporter(FakeHass(), "entry1", "sub1", PULSE_ID)
    run(reporter.async_check_certificate(cert))
    assert issues.ids == [f"cloud_certificate_expiring_soon_{PULSE_ID}"]


@pytest.mark.parametrize("content", [None, "garbage"], ids=["missing", "garbage"])
def test_unreadable_certificate_file_raises_an_issue(issues, tmp_path, content):
    cert = tmp_path / "cert.pem"
    if content is not None:
        cert.write_text(content)
    run(make_reporter().async_check_certificate(cert))
    assert issues.ids == [f"cloud_certificate_invalid_{PULSE_ID}"]


# ------------------------------- discovered Pulse -------------------------------


def test_a_discovered_pulse_gets_one_offer_with_what_the_fix_flow_needs(issues):
    async_report_pulse_discovered(None, "entry1", PULSE_ID, TOPIC)
    async_report_pulse_discovered(None, "entry1", PULSE_ID, TOPIC)
    assert issues.ids == [f"cloud_pulse_discovered_{PULSE_ID}"]
    issue = issues.issues[(DOMAIN, f"cloud_pulse_discovered_{PULSE_ID}")]
    assert issue["is_fixable"] is True and issue["is_persistent"] is True
    assert issue["data"] == {"entry_id": "entry1", "pulse_id": PULSE_ID, "topic": TOPIC}
    assert issue["translation_placeholders"] == {"device": PULSE_ID}


def test_the_offer_is_removed_when_forwarding_is_set_up(issues):
    async_report_pulse_discovered(None, "entry1", PULSE_ID, TOPIC)
    async_clear_pulse_discovered(None, PULSE_ID)
    assert issues.ids == []


# --------------------------------- stale issues ---------------------------------


def test_issues_of_removed_forwardings_are_deleted_but_nothing_else(issues):
    for entry_id, subentry_id, pulse in [("entry1", "keep", "a"), ("entry1", "gone", "b"), ("entry2", "gone", "c")]:
        CloudIssueReporter(FakeHass(), entry_id, subentry_id, pulse).on_state(CLOUD_STATE_AUTH_FAILED)
    async_report_pulse_discovered(None, "entry1", "d", TOPIC)
    issues.async_create_issue(None, DOMAIN, "old_raw_captures", data=None)

    async_remove_stale_issues(None, SimpleNamespace(entry_id="entry1", subentries={"keep": object()}))

    assert issues.ids == [
        "cloud_auth_rejected_a",  # still has its subentry
        "cloud_auth_rejected_c",  # belongs to another entry
        "cloud_pulse_discovered_d",  # offers are not tied to a subentry
        "old_raw_captures",
    ]
