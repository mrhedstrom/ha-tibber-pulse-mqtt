"""Tests for the built-in Tibber Cloud forwarder (replacement for a broker bridge)."""
from __future__ import annotations

import base64
import logging
import os
import ssl
import time
from pathlib import Path
from types import SimpleNamespace

import paho.mqtt.client as paho
import pytest

from custom_components.tibber_pulse_mqtt import cloud_forwarder, mqtt_client
from custom_components.tibber_pulse_mqtt.cloud_forwarder import (
    BUNDLED_CA_PATH,
    CloudForwarder,
    CloudForwardingConfig,
    build_ssl_context,
    client_id_for,
    receive_topic_for,
)
from custom_components.tibber_pulse_mqtt.const import (
    CLOUD_STATE_AUTH_FAILED,
    CLOUD_STATE_CONNECTED,
    CLOUD_STATE_CONNECTING,
    CLOUD_STATE_DISABLED,
    CLOUD_STATE_DISCONNECTED,
    CLOUD_STATE_INVALID_CERTIFICATE,
    DEFAULT_CLOUD_HOST,
)
from custom_components.tibber_pulse_mqtt.coordinator import TibberLocalHub
from tests.cloud_helpers import (
    PULSE_ID,
    TOPIC,
    FakeClient,
    FakeHass,
    make_pems,
    run,
    settle,
    write_credentials,
)
from tests.test_raw_fixtures import (
    RAW_FIXTURES,
    _is_status_record,
    _load_fixture_records,
    _order_fixture_records,
)


# --------------------------------- helpers ------------------------------------


@pytest.fixture(autouse=True)
def fake_paho(monkeypatch):
    FakeClient.instances = []
    FakeClient.auto_event = None
    monkeypatch.setattr(cloud_forwarder.paho, "Client", FakeClient)
    return FakeClient


@pytest.fixture(scope="module")
def cert_files(tmp_path_factory) -> tuple[Path, Path]:
    """Self-signed certificate plus an RSA PKCS#1 key, the format the Pulse shows."""
    return write_credentials(tmp_path_factory.mktemp("certs"))


def make_config(cert_files, topic: str = TOPIC, **overrides) -> CloudForwardingConfig:
    cert_path, key_path = cert_files
    return CloudForwardingConfig(
        pulse_id=PULSE_ID,
        local_publish_topic=topic,
        cert_path=cert_path,
        key_path=key_path,
        **overrides,
    )


async def start_forwarder(cert_files, topic: str = TOPIC, on_state=None, publish_local=None,
                          **overrides):
    forwarder = CloudForwarder(
        FakeHass(),
        make_config(cert_files, topic, **overrides),
        publish_local or _noop_publish,
        on_state,
    )
    await forwarder.async_start()
    return forwarder, FakeClient.instances[-1]


async def _noop_publish(topic: str, payload: bytes) -> None:
    return None


# ------------------------------ topics and config -----------------------------


def test_receive_topic_and_client_id_follow_the_publish_topic():
    assert receive_topic_for(TOPIC) == "tibber-pulse-0123456789abcdef0123456789abcdef/receive"
    assert client_id_for(TOPIC) == "tibber-pulse-0123456789abcdef0123456789abcdef"


def test_client_id_ignores_a_topic_prefix():
    topic = "$aws/rules/push_to_kinesis/tibber-pulse-abc/publish"
    assert client_id_for(topic) == "tibber-pulse-abc"


@pytest.mark.parametrize(
    "topic",
    ["", "tibber-pulse-abc", "tibber-pulse-abc/receive", "tibber-pulse-+/publish",
     "tibber-pulse-#/publish", "/tibber-pulse-abc/publish", "/publish", "a//b/publish"],
)
def test_invalid_publish_topics_are_rejected(topic):
    with pytest.raises(ValueError):
        receive_topic_for(topic)


def test_config_defaults_mirror_the_documented_bridge(cert_files):
    config = make_config(cert_files)
    assert config.host == DEFAULT_CLOUD_HOST
    assert config.port == 8883
    assert config.tls_version == "tlsv1.2"
    assert config.effective_aws_publish_topic == TOPIC
    assert config.effective_aws_receive_topic == config.local_receive_topic


def test_config_topic_overrides_only_change_the_cloud_side(cert_files):
    prefixed = "$aws/rules/push_to_kinesis/" + TOPIC
    config = make_config(cert_files, aws_publish_topic=prefixed)
    assert config.effective_aws_publish_topic == prefixed
    assert config.local_publish_topic == TOPIC
    assert config.client_id == "tibber-pulse-0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize(
    "overrides",
    [{"port": 0}, {"port": 70000}, {"host": ""}, {"host": "a b"}, {"host": "https://x/y"},
     {"tls_version": "tlsv1.0"}, {"aws_publish_topic": "a/+/b"}, {"aws_receive_topic": "#"}],
)
def test_config_rejects_invalid_values(cert_files, overrides):
    with pytest.raises(ValueError):
        make_config(cert_files, **overrides)


# --------------------------------- TLS context --------------------------------


def test_bundled_ca_is_the_verisign_root_and_is_the_only_trust_anchor(cert_files):
    context = build_ssl_context(make_config(cert_files))
    subjects = [dict(item[0] for item in ca["subject"]) for ca in context.get_ca_certs()]
    assert len(subjects) == 1
    assert "VeriSign Class 3 Public Primary Certification Authority - G5" in subjects[0]["commonName"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_tls_version_is_pinned(cert_files):
    context = build_ssl_context(make_config(cert_files, tls_version="tlsv1.3"))
    assert context.minimum_version == context.maximum_version == ssl.TLSVersion.TLSv1_3
    context = build_ssl_context(make_config(cert_files))
    assert context.minimum_version == context.maximum_version == ssl.TLSVersion.TLSv1_2


def test_custom_ca_replaces_the_bundled_ca(cert_files):
    cert_path, _ = cert_files
    context = build_ssl_context(make_config(cert_files, ca_path=cert_path))
    subjects = [dict(item[0] for item in ca["subject"]) for ca in context.get_ca_certs()]
    assert [s["commonName"] for s in subjects] == ["test-pulse"]


def test_mismatching_key_is_rejected(cert_files, tmp_path):
    bad_key = tmp_path / "other.pem"
    bad_key.write_text(make_pems(key_index=1)[1], encoding="ascii")
    config = CloudForwardingConfig(
        pulse_id=PULSE_ID, local_publish_topic=TOPIC, cert_path=cert_files[0], key_path=bad_key
    )
    with pytest.raises(ssl.SSLError):
        build_ssl_context(config)


# -------------------------------- lifecycle -----------------------------------


def test_start_connects_in_the_background_with_bridge_equivalent_settings(cert_files):
    async def scenario():
        states = []
        forwarder, client = await start_forwarder(
            cert_files, on_state=lambda pulse_id, state: states.append((pulse_id, state))
        )
        assert forwarder.state == CLOUD_STATE_CONNECTING
        assert states == [(PULSE_ID, CLOUD_STATE_CONNECTING)]
        assert client.client_id == "tibber-pulse-0123456789abcdef0123456789abcdef"
        assert client.protocol == paho.MQTTv311
        assert client.clean_session is True
        assert client.callback_api_version == paho.CallbackAPIVersion.VERSION2
        assert client.connect_args == (DEFAULT_CLOUD_HOST, 8883, 60)
        assert client.reconnect_delay == (5, 120)
        assert client.max_queued == 200
        assert client.tls_context.minimum_version == ssl.TLSVersion.TLSv1_2
        assert client.loop_started

    run(scenario())


def test_start_with_unreadable_certificate_does_not_create_a_client(cert_files, tmp_path):
    async def scenario():
        garbage = tmp_path / "garbage.pem"
        garbage.write_text("not a certificate")
        states = []
        forwarder = CloudForwarder(
            FakeHass(),
            CloudForwardingConfig(
                pulse_id=PULSE_ID, local_publish_topic=TOPIC, cert_path=garbage, key_path=garbage
            ),
            _noop_publish,
            lambda pulse_id, state: states.append(state),
        )
        await forwarder.async_start()
        assert forwarder.state == CLOUD_STATE_INVALID_CERTIFICATE
        assert states == [CLOUD_STATE_CONNECTING, CLOUD_STATE_INVALID_CERTIFICATE]
        assert FakeClient.instances == []
        forwarder.forward_upstream(TOPIC, b"x")  # no client: silently ignored

    run(scenario())


def test_stop_disconnects_and_ignores_late_events(cert_files):
    async def scenario():
        forwarder, client = await start_forwarder(cert_files)
        client.simulate_connected()
        await settle()
        assert forwarder.state == CLOUD_STATE_CONNECTED

        await forwarder.async_stop()
        assert client.disconnected and client.loop_stopped
        assert forwarder.state == CLOUD_STATE_DISABLED

        client.simulate_disconnected()  # late callback from the network thread
        client.simulate_connected()
        await settle()
        assert forwarder.state == CLOUD_STATE_DISABLED

        forwarder.forward_upstream(TOPIC, b"after stop")
        assert client.published == []

    run(scenario())


def test_start_twice_keeps_a_single_client(cert_files):
    async def scenario():
        forwarder, _ = await start_forwarder(cert_files)
        await forwarder.async_start()
        assert len(FakeClient.instances) == 1

    run(scenario())


# ------------------------------- state handling -------------------------------


def test_connection_states_follow_the_client(cert_files):
    async def scenario():
        states = []
        forwarder, client = await start_forwarder(
            cert_files, on_state=lambda pulse_id, state: states.append(state)
        )
        client.simulate_connected()
        await settle()
        client.simulate_disconnected()
        await settle()
        client.simulate_connect_failed()  # repeated retries must not spam state changes
        await settle()
        client.simulate_connected()
        await settle()
        assert states == [
            CLOUD_STATE_CONNECTING,
            CLOUD_STATE_CONNECTED,
            CLOUD_STATE_DISCONNECTED,
            CLOUD_STATE_CONNECTED,
        ]

    run(scenario())


@pytest.mark.parametrize("code", [134, 135])
def test_rejected_credentials_are_reported_and_not_overwritten_by_retries(cert_files, code):
    async def scenario():
        forwarder, client = await start_forwarder(cert_files)
        client.simulate_refused(code)
        await settle()
        assert forwarder.state == CLOUD_STATE_AUTH_FAILED
        client.simulate_disconnected()
        client.simulate_connect_failed()
        await settle()
        assert forwarder.state == CLOUD_STATE_AUTH_FAILED
        client.simulate_connected()
        await settle()
        assert forwarder.state == CLOUD_STATE_CONNECTED

    run(scenario())


def test_other_connack_refusals_are_plain_disconnects(cert_files):
    async def scenario():
        forwarder, client = await start_forwarder(cert_files)
        client.simulate_refused(136)  # server unavailable
        await settle()
        assert forwarder.state == CLOUD_STATE_DISCONNECTED

    run(scenario())


def test_failing_state_listener_does_not_break_the_forwarder(cert_files):
    async def scenario():
        def broken(pulse_id, state):
            raise RuntimeError("boom")

        forwarder, client = await start_forwarder(cert_files, on_state=broken)
        client.simulate_connected()
        await settle()
        assert forwarder.state == CLOUD_STATE_CONNECTED

    run(scenario())


# ----------------------------- Pulse -> cloud ---------------------------------


def test_only_the_pulses_publish_topic_is_forwarded(cert_files):
    async def scenario():
        forwarder, client = await start_forwarder(cert_files)
        forwarder.forward_upstream(TOPIC, b"\x00\x01binary\xff")
        forwarder.forward_upstream("tibber-pulse-other/publish", b"other pulse")
        forwarder.forward_upstream(receive_topic_for(TOPIC), b"receive topic")
        assert client.published == [(TOPIC, b"\x00\x01binary\xff", 1, False)]

    run(scenario())


def test_cloud_publish_topic_override_is_used(cert_files):
    async def scenario():
        prefixed = "$aws/rules/push_to_kinesis/" + TOPIC
        forwarder, client = await start_forwarder(cert_files, aws_publish_topic=prefixed)
        forwarder.forward_upstream(TOPIC, b"data")
        assert client.published == [(prefixed, b"data", 1, False)]

    run(scenario())


def test_rejected_publish_does_not_raise(cert_files):
    async def scenario():
        forwarder, client = await start_forwarder(cert_files)
        client.publish_rc = paho.MQTT_ERR_QUEUE_SIZE
        forwarder.forward_upstream(TOPIC, b"data")

    run(scenario())


@pytest.mark.parametrize("raw_file", RAW_FIXTURES, ids=lambda path: path.stem)
@pytest.mark.parametrize("status_first", [True, False], ids=["status-first", "data-first"])
def test_raw_captures_are_forwarded_byte_for_byte(raw_file, status_first, cert_files):
    records = _order_fixture_records(_load_fixture_records(raw_file), status_first)
    topic = records[0]["topic"]
    mine = [r for r in records if r["topic"] == topic]

    async def scenario():
        forwarder, client = await start_forwarder(cert_files, topic=topic)
        for record in records:
            forwarder.forward_upstream(record["topic"], base64.b64decode(record["payload"]))
            # Traffic from another Pulse on the same local broker must never leak through.
            forwarder.forward_upstream("tibber-pulse-someone-else/publish", b"not mine")
        return client

    client = run(scenario())
    assert client.published == [
        (topic, base64.b64decode(r["payload"]), 1, False) for r in mine
    ]
    assert any(_is_status_record(r) for r in mine), "STATUS messages must be forwarded too"


# ----------------------------- cloud -> Pulse ---------------------------------


def test_connect_subscribes_to_the_receive_topic_on_every_connection(cert_files):
    async def scenario():
        forwarder, client = await start_forwarder(cert_files)
        client.simulate_connected()
        client.simulate_disconnected()
        client.simulate_connected()
        receive = receive_topic_for(TOPIC)
        assert client.subscribed == [(receive, 1), (receive, 1)]

    run(scenario())


def test_cloud_messages_are_published_locally_unchanged(cert_files):
    async def scenario():
        published = []

        async def publish_local(topic, payload):
            published.append((topic, payload))

        forwarder, client = await start_forwarder(cert_files, publish_local=publish_local)
        receive = receive_topic_for(TOPIC)
        client.simulate_message(receive, b'{"cmd":"update"}')
        client.simulate_message(TOPIC, b"wrong topic")
        await settle()
        assert published == [(receive, b'{"cmd":"update"}')]

    run(scenario())


def test_cloud_receive_topic_override_maps_back_to_the_local_receive_topic(cert_files):
    async def scenario():
        published = []

        async def publish_local(topic, payload):
            published.append((topic, payload))

        forwarder, client = await start_forwarder(
            cert_files, publish_local=publish_local, aws_receive_topic="cloud/special/receive"
        )
        client.simulate_connected()
        client.simulate_message("cloud/special/receive", b"cmd")
        client.simulate_message(receive_topic_for(TOPIC), b"ignored")
        await settle()
        assert client.subscribed == [("cloud/special/receive", 1)]
        assert published == [(receive_topic_for(TOPIC), b"cmd")]

    run(scenario())


def test_failed_local_publish_is_logged_and_does_not_stop_forwarding(cert_files, caplog):
    async def scenario():
        async def publish_local(topic, payload):
            raise ConnectionError("broker down")

        forwarder, client = await start_forwarder(cert_files, publish_local=publish_local)
        client.simulate_message(receive_topic_for(TOPIC), b"cmd")
        await settle()
        forwarder.forward_upstream(TOPIC, b"still works")
        assert client.published == [(TOPIC, b"still works", 1, False)]

    with caplog.at_level(logging.WARNING, logger=cloud_forwarder.__name__):
        run(scenario())
    assert "broker down" in caplog.text


# ---------------------------------- hub ---------------------------------------


class RecordingDispatcher:
    def __init__(self):
        self.messages = []

    def on_mqtt_message(self, topic, payload):
        self.messages.append((topic, payload))


async def _noop_stop() -> None:
    return None


def make_hub(config_dir=None):
    hub = object.__new__(TibberLocalHub)
    hub.hass = FakeHass(config_dir)
    hub.dispatcher = RecordingDispatcher()
    hub.dispatcher.async_stop = _noop_stop
    hub._unsub_ha_stop = None
    hub._ha_mqtt = None
    hub._ext_mqtt = None
    hub._forwarders = {}
    return hub


def test_hub_forwards_before_dispatching_and_survives_a_failing_forwarder(cert_files):
    async def scenario():
        hub = make_hub()
        good = await hub.async_add_forwarder(make_config(cert_files))
        client = FakeClient.instances[-1]

        bad = SimpleNamespace(forward_upstream=lambda t, p: (_ for _ in ()).throw(RuntimeError("x")))
        hub._forwarders["bad"] = bad

        hub._on_mqtt_message(TOPIC, b"payload")
        assert client.published == [(TOPIC, b"payload", 1, False)]
        assert hub.dispatcher.messages == [(TOPIC, b"payload")]
        assert good.state == CLOUD_STATE_CONNECTING

    run(scenario())


def test_hub_starts_forwarding_for_stored_subentries_and_removes_orphaned_credentials(tmp_path, caplog):
    root = tmp_path / ".storage" / "tibber_pulse_mqtt" / "certs"
    write_credentials(root / PULSE_ID)
    orphan = root / "orphan"
    orphan.mkdir(parents=True)
    old = time.time() - 3600
    os.utime(orphan, (old, old))

    subentries = [
        SimpleNamespace(title="ok", data={"pulse_id": PULSE_ID, "local_topic": TOPIC}),
        SimpleNamespace(title="broken", data={"pulse_id": "other"}),  # no topic stored
    ]

    async def scenario():
        hub = make_hub(tmp_path)
        hub.entry = SimpleNamespace(get_subentries_of_type=lambda subentry_type: subentries)
        hub.hass.config_entries = SimpleNamespace(async_entries=lambda domain: [hub.entry])
        await hub._async_start_cloud_forwarders()
        return hub

    with caplog.at_level(logging.WARNING, logger="custom_components.tibber_pulse_mqtt.coordinator"):
        hub = run(scenario())

    assert list(hub._forwarders) == [PULSE_ID]
    assert FakeClient.instances[-1].client_id == "tibber-pulse-0123456789abcdef0123456789abcdef"
    assert not orphan.exists()
    assert (root / PULSE_ID / "cert.pem").exists()
    assert "Skipping Tibber Cloud forwarding broken" in caplog.text


def test_hub_without_forwarders_only_dispatches():
    hub = make_hub()
    hub._on_mqtt_message(TOPIC, b"payload")
    assert hub.dispatcher.messages == [(TOPIC, b"payload")]


def test_hub_replaces_removes_and_stops_forwarders(cert_files):
    async def scenario():
        hub = make_hub()
        await hub.async_add_forwarder(make_config(cert_files))
        first = FakeClient.instances[-1]
        await hub.async_add_forwarder(make_config(cert_files))  # same Pulse: replaced
        assert first.disconnected and len(hub._forwarders) == 1

        second = FakeClient.instances[-1]
        await hub.async_remove_forwarder(PULSE_ID)
        assert second.disconnected and hub._forwarders == {}

        await hub.async_add_forwarder(make_config(cert_files))
        third = FakeClient.instances[-1]
        await hub.async_stop()
        assert third.disconnected and hub._forwarders == {}

    run(scenario())


def test_hub_publishes_cloud_messages_through_ha_mqtt(cert_files):
    async def scenario():
        published = []

        class HaBridge:
            async def async_publish(self, topic, payload):
                published.append((topic, payload))

        hub = make_hub()
        hub._ha_mqtt = HaBridge()
        await hub.async_add_forwarder(make_config(cert_files))
        FakeClient.instances[-1].simulate_message(receive_topic_for(TOPIC), b"cmd")
        await settle()
        assert published == [(receive_topic_for(TOPIC), b"cmd")]

    run(scenario())


def test_hub_publishes_cloud_messages_through_the_external_client(cert_files):
    async def scenario():
        published = []

        class ExternalClient:
            async def async_publish(self, topic, payload):
                published.append((topic, payload))

        hub = make_hub()
        hub._ext_mqtt = ExternalClient()
        await hub.async_add_forwarder(make_config(cert_files))
        FakeClient.instances[-1].simulate_message(receive_topic_for(TOPIC), b"cmd")
        await settle()
        assert published == [(receive_topic_for(TOPIC), b"cmd")]

    run(scenario())


def test_hub_cannot_publish_locally_when_mqtt_is_not_running():
    async def scenario():
        with pytest.raises(ConnectionError):
            await make_hub()._async_publish_local("t", b"x")

    run(scenario())


# ------------------------------ MQTT clients ----------------------------------


def test_ha_mqtt_bridge_publishes_raw_bytes(monkeypatch):
    calls = []

    async def fake_publish(hass, topic, payload, qos, retain, encoding):
        calls.append((hass, topic, payload, qos, retain, encoding))

    monkeypatch.setattr(mqtt_client.ha_mqtt, "async_publish", fake_publish)
    hass = object()
    run(mqtt_client.HAMQTTBridge(hass).async_publish("t/receive", b"\x00\xff"))
    assert calls == [(hass, "t/receive", b"\x00\xff", 1, False, None)]


def test_external_client_publishes_through_paho_and_reports_failures():
    client = object.__new__(mqtt_client.ExternalMQTTClient)
    client._client = FakeClient()
    run(client.async_publish("t/receive", b"\x00\xff"))
    assert client._client.published == [("t/receive", b"\x00\xff", 1, False)]

    client._client.publish_rc = paho.MQTT_ERR_NO_CONN
    with pytest.raises(ConnectionError):
        run(client.async_publish("t/receive", b"x"))


def test_bundled_ca_file_exists():
    assert BUNDLED_CA_PATH.is_file()


# ---------------------------- connection test ---------------------------------


def test_connection_test_succeeds_when_the_cloud_accepts_the_client(cert_files):
    FakeClient.auto_event = "connected"
    cloud_forwarder.test_connection(make_config(cert_files), timeout=1)
    client = FakeClient.instances[-1]
    assert client.client_id == "tibber-pulse-0123456789abcdef0123456789abcdef"
    assert client.connect_args == (DEFAULT_CLOUD_HOST, 8883, 30)
    assert client.kwargs == {"reconnect_on_failure": False}, "a test must not keep retrying"
    assert client.disconnected and client.loop_stopped


@pytest.mark.parametrize("code", [134, 135])
def test_connection_test_reports_rejected_credentials(cert_files, code):
    FakeClient.auto_event = f"refused:{code}"
    with pytest.raises(cloud_forwarder.CloudAuthError):
        cloud_forwarder.test_connection(make_config(cert_files), timeout=1)
    assert FakeClient.instances[-1].loop_stopped


def test_connection_test_reports_other_refusals_as_connection_errors(cert_files):
    FakeClient.auto_event = "refused:136"
    with pytest.raises(ConnectionError) as exc_info:
        cloud_forwarder.test_connection(make_config(cert_files), timeout=1)
    assert not isinstance(exc_info.value, cloud_forwarder.CloudAuthError)


def test_connection_test_reports_failed_connections(cert_files):
    FakeClient.auto_event = "fail"
    with pytest.raises(ConnectionError):
        cloud_forwarder.test_connection(make_config(cert_files), timeout=1)
    assert FakeClient.instances[-1].loop_stopped


def test_connection_test_times_out_and_still_cleans_up(cert_files):
    FakeClient.auto_event = None
    with pytest.raises(TimeoutError):
        cloud_forwarder.test_connection(make_config(cert_files), timeout=0.05)
    client = FakeClient.instances[-1]
    assert client.disconnected and client.loop_stopped


def test_connection_test_with_unreadable_credentials_never_connects(tmp_path):
    garbage = tmp_path / "garbage.pem"
    garbage.write_text("nope")
    config = CloudForwardingConfig(
        pulse_id=PULSE_ID, local_publish_topic=TOPIC, cert_path=garbage, key_path=garbage
    )
    with pytest.raises((ssl.SSLError, OSError, ValueError)):
        cloud_forwarder.test_connection(config, timeout=1)
    assert FakeClient.instances == []


# ------------------------------ stored settings -------------------------------


def _subentry(**data):
    return SimpleNamespace(data={"pulse_id": PULSE_ID, "local_topic": TOPIC, **data})


def _config_path(tmp_path):
    return lambda *parts: str(tmp_path.joinpath(*parts))


def test_config_from_subentry_uses_defaults_when_nothing_deviates(tmp_path):
    config = cloud_forwarder.config_from_subentry(_config_path(tmp_path), _subentry())
    directory = tmp_path / ".storage" / "tibber_pulse_mqtt" / "certs" / PULSE_ID
    assert config.cert_path == directory / "cert.pem"
    assert config.key_path == directory / "key.pem"
    assert config.ca_path is None
    assert (config.host, config.port, config.tls_version) == (DEFAULT_CLOUD_HOST, 8883, "tlsv1.2")
    assert config.effective_aws_publish_topic == TOPIC
    assert config.effective_aws_receive_topic == receive_topic_for(TOPIC)


def test_config_from_subentry_applies_stored_deviations_and_a_custom_ca(tmp_path):
    directory = tmp_path / ".storage" / "tibber_pulse_mqtt" / "certs" / PULSE_ID
    directory.mkdir(parents=True)
    (directory / "ca.pem").write_text("ca")
    config = cloud_forwarder.config_from_subentry(
        _config_path(tmp_path),
        _subentry(
            mqtt_url="example.test",
            mqtt_port=443,
            tls_version="tlsv1.3",
            mqtt_topic="cloud/out",
            mqtt_topic_sub="cloud/in",
        ),
    )
    assert config.ca_path == directory / "ca.pem"
    assert (config.host, config.port, config.tls_version) == ("example.test", 443, "tlsv1.3")
    assert config.effective_aws_publish_topic == "cloud/out"
    assert config.effective_aws_receive_topic == "cloud/in"


@pytest.mark.parametrize(
    "subentry",
    [
        SimpleNamespace(data={"local_topic": TOPIC}),
        SimpleNamespace(data={"pulse_id": PULSE_ID}),
        SimpleNamespace(data={"pulse_id": "../evil", "local_topic": TOPIC}),
        SimpleNamespace(data={"pulse_id": PULSE_ID, "local_topic": "not-a-publish-topic"}),
        _subentry(mqtt_port="abc"),
    ],
    ids=["no-pulse", "no-topic", "bad-pulse-id", "bad-topic", "bad-port"],
)
def test_config_from_subentry_rejects_unusable_stored_data(tmp_path, subentry):
    with pytest.raises((KeyError, TypeError, ValueError)):
        cloud_forwarder.config_from_subentry(_config_path(tmp_path), subentry)
