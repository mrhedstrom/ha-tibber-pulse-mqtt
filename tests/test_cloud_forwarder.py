"""Tests for the built-in Tibber Cloud forwarder (replacement for a broker bridge)."""
from __future__ import annotations

import asyncio
import base64
import logging
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import paho.mqtt.client as paho
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

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
from tests.test_raw_fixtures import (
    RAW_FIXTURES,
    _is_status_record,
    _load_fixture_records,
    _order_fixture_records,
)

TOPIC = "tibber-pulse-0123456789abcdef0123456789abcdef/publish"
PULSE_ID = "aabbccddeeff"


# --------------------------------- helpers ------------------------------------


def run(coro):
    return asyncio.run(coro)


async def settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


class FakeHass:
    """Just enough of HomeAssistant for the forwarder."""

    @property
    def loop(self):
        return asyncio.get_running_loop()

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def async_create_task(self, coro):
        return asyncio.get_running_loop().create_task(coro)


class FakeClient:
    """Stand-in for paho.Client that records everything the forwarder does."""

    instances: list["FakeClient"] = []

    def __init__(self, callback_api_version=None, client_id="", clean_session=None,
                 protocol=None, **kwargs):
        self.callback_api_version = callback_api_version
        self.client_id = client_id
        self.clean_session = clean_session
        self.protocol = protocol
        self.tls_context = None
        self.reconnect_delay = None
        self.max_queued = None
        self.connect_args = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.published: list[tuple[str, bytes, int, bool]] = []
        self.subscribed: list[tuple[str, int]] = []
        self.publish_rc = paho.MQTT_ERR_SUCCESS
        self.on_connect = self.on_connect_fail = self.on_disconnect = self.on_message = None
        FakeClient.instances.append(self)

    def tls_set_context(self, context):
        self.tls_context = context

    def reconnect_delay_set(self, min_delay, max_delay):
        self.reconnect_delay = (min_delay, max_delay)

    def max_queued_messages_set(self, value):
        self.max_queued = value

    def connect_async(self, host, port, keepalive=60):
        self.connect_args = (host, port, keepalive)

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_stopped = True

    def disconnect(self):
        self.disconnected = True

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=self.publish_rc)

    # Helpers simulating paho's network thread
    def simulate_connected(self):
        self.on_connect(self, None, {}, ReasonCode(PacketTypes.CONNACK, identifier=0), None)

    def simulate_refused(self, code):
        self.on_connect(self, None, {}, ReasonCode(PacketTypes.CONNACK, identifier=code), None)

    def simulate_connect_failed(self):
        self.on_connect_fail(self, None)

    def simulate_disconnected(self):
        self.on_disconnect(self, None, None, ReasonCode(PacketTypes.DISCONNECT, identifier=0), None)

    def simulate_message(self, topic, payload):
        self.on_message(self, None, SimpleNamespace(topic=topic, payload=payload))


@pytest.fixture(autouse=True)
def fake_paho(monkeypatch):
    FakeClient.instances = []
    monkeypatch.setattr(cloud_forwarder.paho, "Client", FakeClient)
    return FakeClient


@pytest.fixture(scope="module")
def cert_files(tmp_path_factory) -> tuple[Path, Path]:
    """Self-signed certificate plus an RSA PKCS#1 key, the format the Pulse shows."""
    directory = tmp_path_factory.mktemp("certs")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-pulse")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


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
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    bad_key = tmp_path / "other.pem"
    bad_key.write_bytes(
        other_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
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


def make_hub():
    hub = object.__new__(TibberLocalHub)
    hub.hass = FakeHass()
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
