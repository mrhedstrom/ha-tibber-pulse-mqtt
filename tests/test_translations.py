"""Consistency of strings.json and the translations for the Tibber Cloud forwarding flow."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from custom_components.tibber_pulse_mqtt.config_flow import CloudForwardingSubentryFlow
from custom_components.tibber_pulse_mqtt.const import CLOUD_STATES

COMPONENT = Path(__file__).parent.parent / "custom_components" / "tibber_pulse_mqtt"
LOCALES = ["da", "de", "en", "fi", "nl", "no", "sv"]

# Every error key the flow or the credential validation can return.
FLOW_ERRORS = {
    "credentials_incomplete",
    "certificate_invalid",
    "key_invalid",
    "key_mismatch",
    "certificate_expired",
    "certificate_not_yet_valid",
    "ca_invalid",
    "storage_failed",
    "invalid_settings",
    "cannot_connect",
    "auth_failed",
}
FLOW_ABORTS = {"entry_not_loaded", "no_pulse_available", "reconfigure_successful"}

# Fields that are copied from the Pulse setup page; their label ends with the page's field name.
PULSE_FIELDS = ["certificate", "private_key", "ca_cert", "mqtt_topic", "mqtt_topic_sub", "mqtt_url", "mqtt_port"]


def load(name: str) -> dict:
    return json.loads((COMPONENT / name).read_text(encoding="utf-8"))


def flatten(data, prefix=""):
    if isinstance(data, dict):
        for key, value in data.items():
            yield from flatten(value, f"{prefix}.{key}" if prefix else key)
    else:
        yield prefix, data


def subentry_block(translations: dict) -> dict:
    return translations["config_subentries"]["cloud_forwarding"]


def steps(block: dict):
    return block["step"]["credentials"], block["step"]["reconfigure"]


@pytest.fixture(scope="module", params=["strings.json"] + [f"translations/{lang}.json" for lang in LOCALES])
def translations(request):
    return load(request.param)


def test_every_file_has_exactly_the_same_keys_as_strings_json():
    expected = {path for path, _ in flatten(load("strings.json"))}
    for lang in LOCALES:
        actual = {path for path, _ in flatten(load(f"translations/{lang}.json"))}
        assert actual == expected, (
            f"{lang}.json differs from strings.json: "
            f"missing {sorted(expected - actual)}, extra {sorted(actual - expected)}"
        )


def test_english_translation_equals_strings_json():
    assert load("translations/en.json") == load("strings.json")


def test_no_translation_is_empty(translations):
    empty = [path for path, value in flatten(translations) if isinstance(value, str) and not value.strip()]
    assert empty == []


def test_all_errors_and_aborts_of_the_flow_are_translated(translations):
    block = subentry_block(translations)
    assert FLOW_ERRORS <= set(block["error"])
    assert FLOW_ABORTS <= set(block["abort"])


def test_form_fields_match_the_translated_fields(translations):
    block = subentry_block(translations)
    schema = CloudForwardingSubentryFlow._credentials_schema("tibber-pulse-x/publish", {}, optional=False)
    advanced = next(value for value in schema.schema.values() if hasattr(value, "options"))
    for step in steps(block):
        assert set(step["data"]) == {"certificate", "private_key"}
        assert set(step["data_description"]) == {"certificate", "private_key"}
        section = step["sections"]["advanced"]
        assert set(section["data"]) == {str(key) for key in advanced.schema.schema}
        assert set(section["data_description"]) == set(section["data"])


def test_labels_end_with_the_field_name_shown_on_the_pulse_setup_page(translations):
    """Users match the label with the field they copy from the Pulse, in every language."""
    for step in steps(subentry_block(translations)):
        labels = {**step["data"], **step["sections"]["advanced"]["data"]}
        for field in PULSE_FIELDS:
            assert f"({field})" in labels[field], f"label of {field} must contain ({field})"
        assert "(" not in labels["tls_version"], "TLS version has no field on the Pulse page"


def test_credential_labels_are_identical_when_adding_and_reconfiguring(translations):
    adding, reconfiguring = steps(subentry_block(translations))
    assert adding["data"] == reconfiguring["data"]
    assert adding["sections"] == reconfiguring["sections"]


def test_pulse_placeholder_is_present_and_the_only_one(translations):
    for step in steps(subentry_block(translations)):
        assert set(re.findall(r"\{(\w+)\}", step["description"])) == {"pulse"}
    user = subentry_block(translations)["step"]["user"]
    assert re.findall(r"\{(\w+)\}", user["description"]) == []


def test_every_language_warns_about_an_existing_bridge(translations):
    """The old bridge and the built-in forwarding share a client ID, so the warning is essential."""
    block = subentry_block(translations)
    for text in (block["step"]["user"]["description"], block["step"]["credentials"]["description"]):
        assert "**" in text and "Mosquitto" in text


def test_pulse_field_names_are_identical_in_all_languages():
    def names(lang):
        block = subentry_block(load(f"translations/{lang}.json"))
        step = block["step"]["credentials"]
        labels = {**step["data"], **step["sections"]["advanced"]["data"]}
        return {field: re.findall(r"\(([a-z_]+)\)", labels[field])[0] for field in PULSE_FIELDS}

    reference = names("en")
    assert reference == {field: field for field in PULSE_FIELDS}
    for lang in LOCALES:
        assert names(lang) == reference


# ------------------------- entities and repair flows -------------------------

CERTIFICATE_ISSUES = [
    "cloud_certificate_expiring",
    "cloud_certificate_expiring_soon",
    "cloud_certificate_expired",
    "cloud_certificate_invalid",
    "cloud_auth_rejected",
]
# Errors the repair flows can show: the flow errors minus the ones only the form can cause.
REPAIR_ERRORS = FLOW_ERRORS - {"credentials_incomplete"}
ALL_REPAIR_ISSUES = CERTIFICATE_ISSUES + ["cloud_connection_unstable", "cloud_pulse_discovered"]


def test_entity_names_and_every_connection_state_are_translated(translations):
    sensor = translations["entity"]["sensor"]["cloud_connection"]
    assert sensor["name"]
    assert set(sensor["state"]) == set(CLOUD_STATES), "every sensor option needs a label"
    assert translations["entity"]["switch"]["cloud_forwarding"]["name"]


@pytest.mark.parametrize("key", CERTIFICATE_ISSUES + ["cloud_pulse_discovered"])
def test_repair_flows_that_ask_for_credentials_have_all_their_texts(translations, key):
    fix_flow = translations["issues"][key]["fix_flow"]
    step = fix_flow["step"]["credentials"]
    assert set(step["data"]) == {"certificate", "private_key"}
    assert set(step["data_description"]) == {"certificate", "private_key"}
    assert step["title"] and step["description"]
    assert REPAIR_ERRORS <= set(fix_flow["error"])
    assert "entry_not_loaded" in fix_flow["abort"]


def test_credential_labels_in_repairs_are_the_same_as_in_the_config_flow(translations):
    form = subentry_block(translations)["step"]["credentials"]
    for key in CERTIFICATE_ISSUES + ["cloud_pulse_discovered"]:
        step = translations["issues"][key]["fix_flow"]["step"]["credentials"]
        assert step["data"] == form["data"]


def test_unstable_connection_repair_has_its_texts(translations):
    fix_flow = translations["issues"]["cloud_connection_unstable"]["fix_flow"]
    assert fix_flow["step"]["confirm"]["title"] and fix_flow["step"]["confirm"]["description"]
    assert "entry_not_loaded" in fix_flow["abort"]


def test_discovered_pulse_repair_can_report_an_already_configured_pulse(translations):
    assert "already_configured" in translations["issues"]["cloud_pulse_discovered"]["fix_flow"]["abort"]


@pytest.mark.parametrize("key", ALL_REPAIR_ISSUES)
def test_repair_texts_only_use_placeholders_the_issues_provide(translations, key):
    issue = translations["issues"][key]
    texts = [issue["title"]] + [value for path, value in flatten(issue["fix_flow"]["step"])]
    for text in texts:
        assert set(re.findall(r"\{(\w+)\}", text)) <= {"device", "days", "date"}
    assert "{device}" in issue["title"]


def test_expiry_texts_name_the_days_and_the_date(translations):
    for key in ("cloud_certificate_expiring", "cloud_certificate_expiring_soon"):
        issue = translations["issues"][key]
        assert "{days}" in issue["title"]
        description = issue["fix_flow"]["step"]["credentials"]["description"]
        assert "{days}" in description and "{date}" in description
    expired = translations["issues"]["cloud_certificate_expired"]["fix_flow"]["step"]["credentials"]["description"]
    assert "{date}" in expired


def test_the_unstable_and_discovered_repairs_warn_about_the_old_bridge(translations):
    for key, step in (("cloud_connection_unstable", "confirm"), ("cloud_pulse_discovered", "credentials")):
        description = translations["issues"][key]["fix_flow"]["step"][step]["description"]
        assert "Mosquitto" in description
