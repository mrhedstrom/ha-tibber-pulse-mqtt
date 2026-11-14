from __future__ import annotations

import asyncio
import threading
from collections import deque
from types import SimpleNamespace

import paho.mqtt.client as paho
import pytest

from custom_components.tibber_pulse_mqtt import aws_forwarder as aws_forwarder_module
from custom_components.tibber_pulse_mqtt.aws_forwarder import AWSForwarder
from custom_components.tibber_pulse_mqtt.mqtt_client import ExternalMQTTClient
from custom_components.tibber_pulse_mqtt.const import (
    CONF_AWS_ENDPOINT,
    CONF_AWS_PUBLISH_TOPIC_OVERRIDE,
    CONF_AWS_RECEIVE_TOPIC_OVERRIDE,
    CONF_DEVICE_ID,
    CONF_PUBLISH_TOPIC,
    CONF_RECEIVE_TOPIC,
)


def _forwarder() -> AWSForwarder:
    forwarder = AWSForwarder.__new__(AWSForwarder)
    forwarder.cfg = {}
    forwarder.device_id = "ABC123"
    forwarder.publish_topic = "tibber-pulse-ABC123/publish"
    forwarder.receive_topic = "tibber-pulse-ABC123/receive"
    forwarder.aws_publish_topic = forwarder.publish_topic
    forwarder.aws_receive_topic = forwarder.receive_topic
    forwarder._client = None
    forwarder._connected = threading.Event()
    forwarder._connection_result = None
    forwarder._pending_upstream = deque(maxlen=1000)
    forwarder._pending_lock = threading.Lock()
    forwarder._connection_test_lock = asyncio.Lock()
    forwarder._stopping = False
    return forwarder


def _configured_forwarder(options=None) -> AWSForwarder:
    entry = SimpleNamespace(
        data={
            CONF_DEVICE_ID: "ABC123",
            CONF_PUBLISH_TOPIC: "site/tibber-pulse-ABC123/publish",
            CONF_RECEIVE_TOPIC: "site/tibber-pulse-ABC123/receive",
            CONF_AWS_ENDPOINT: "example.iot.test:8883",
        },
        options=options or {},
    )
    return AWSForwarder(object(), entry)


def test_forwarder_defaults_to_exact_discovered_topics():
    forwarder = _configured_forwarder()

    assert forwarder.publish_topic == "site/tibber-pulse-ABC123/publish"
    assert forwarder.receive_topic == "site/tibber-pulse-ABC123/receive"
    assert forwarder.aws_publish_topic == forwarder.publish_topic
    assert forwarder.aws_receive_topic == forwarder.receive_topic


def test_forwarder_overrides_only_aws_side_topics():
    forwarder = _configured_forwarder(
        {
            CONF_AWS_PUBLISH_TOPIC_OVERRIDE: "aws/ABC123/publish",
            CONF_AWS_RECEIVE_TOPIC_OVERRIDE: "aws/ABC123/receive",
        }
    )

    assert forwarder.publish_topic == "site/tibber-pulse-ABC123/publish"
    assert forwarder.receive_topic == "site/tibber-pulse-ABC123/receive"
    assert forwarder.aws_publish_topic == "aws/ABC123/publish"
    assert forwarder.aws_receive_topic == "aws/ABC123/receive"


def test_external_local_client_routes_and_unsubscribes_device_callback():
    client = ExternalMQTTClient(
        hass=SimpleNamespace(loop=None),
        host="localhost",
        port=1883,
        topic="tibber-pulse-+/publish",
        cb=lambda _topic, _payload: None,
    )
    received = []
    unsubscribe = client.async_subscribe(
        "tibber-pulse-ABC123/publish",
        lambda topic, payload: received.append((topic, payload)),
    )

    client._dispatch("tibber-pulse-ABC123/publish", b"message")
    client._dispatch("tibber-pulse-OTHER/publish", b"other")
    unsubscribe()
    client._dispatch("tibber-pulse-ABC123/publish", b"after unsubscribe")

    assert received == [("tibber-pulse-ABC123/publish", b"message")]


def test_external_local_client_preserves_qos_and_retain_for_forwarding():
    client = ExternalMQTTClient(
        hass=SimpleNamespace(loop=None),
        host="localhost",
        port=1883,
        topic="tibber-pulse-+/publish",
        cb=lambda _topic, _payload: None,
    )
    received = []
    client.async_subscribe(
        "tibber-pulse-ABC123/publish",
        lambda *args: received.append(args),
        include_metadata=True,
    )

    client._dispatch("tibber-pulse-ABC123/publish", b"message", qos=2, retain=True)

    assert received == [("tibber-pulse-ABC123/publish", b"message", 2, True)]


def test_connection_test_stops_and_restarts_the_same_client_serially(monkeypatch):
    forwarder = _forwarder()
    forwarder.entry = SimpleNamespace(
        data={
            CONF_DEVICE_ID: "ABC123",
            "aws_cert_path": "cert.pem",
            "aws_key_path": "key.pem",
            "aws_ca_path": "ca.pem",
        },
        options={},
    )
    forwarder.device_id = "ABC123"
    forwarder._client = object()
    calls = []

    def _run(fn, *args):
        calls.append((fn, args))
        return fn(*args)

    async def _executor(fn, *args):
        return _run(fn, *args)

    forwarder.hass = SimpleNamespace(async_add_executor_job=_executor)

    monkeypatch.setattr(
        aws_forwarder_module,
        "test_aws_connection",
        lambda *args: calls.append(("test", args)),
    )
    forwarder._stop_client = lambda: calls.append(("stop", ()))
    forwarder._start_client = lambda *args: calls.append(("start", args))

    asyncio.run(
        forwarder.async_test_connection(
            "test.iot:8883",
            "new-cert.pem",
            "new-key.pem",
            None,
        )
    )

    assert [call[0] for call in calls if isinstance(call[0], str)] == [
        "stop",
        "test",
        "start",
    ]


def test_forwarder_derives_aws_receive_override_from_publish_override():
    forwarder = _configured_forwarder(
        {CONF_AWS_PUBLISH_TOPIC_OVERRIDE: "aws/ABC123/publish"}
    )

    assert forwarder.aws_receive_topic == "aws/ABC123/receive"


class _Client:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, *, qos, retain):
        self.published.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=paho.MQTT_ERR_SUCCESS)


def test_local_publish_is_forwarded_to_exact_aws_topic_with_metadata():
    forwarder = _forwarder()
    forwarder._client = _Client()
    forwarder._connected.set()
    forwarder._connection_result = 0
    message = SimpleNamespace(
        topic="tibber-pulse-ABC123/publish",
        payload=b"\x00binary",
        qos=1,
        retain=True,
    )

    asyncio.run(forwarder._async_on_local_message(message))

    assert forwarder._client.published == [
        ("tibber-pulse-ABC123/publish", b"\x00binary", 1, True)
    ]


def test_unrelated_local_topic_is_not_forwarded():
    forwarder = _forwarder()
    forwarder._client = _Client()
    message = SimpleNamespace(
        topic="tibber-pulse-OTHER/publish",
        payload=b"not this device",
        qos=1,
        retain=False,
    )

    asyncio.run(forwarder._async_on_local_message(message))

    assert forwarder._client.published == []


def test_local_messages_are_queued_until_aws_reconnects():
    forwarder = _forwarder()
    forwarder._client = _Client()
    message = SimpleNamespace(
        topic="tibber-pulse-ABC123/publish",
        payload=b"queued",
        qos=1,
        retain=False,
    )

    asyncio.run(forwarder._async_on_local_message(message))

    assert list(forwarder._pending_upstream) == [
        ("tibber-pulse-ABC123/publish", b"queued", 1, False)
    ]


def test_aws_message_is_forwarded_to_matching_local_receive_topic(monkeypatch):
    forwarder = _forwarder()
    published = []

    async def _publish(hass, topic, payload, **kwargs):
        published.append((topic, payload, kwargs))

    monkeypatch.setattr(aws_forwarder_module.ha_mqtt, "async_publish", _publish)
    forwarder.hass = SimpleNamespace(data={})

    asyncio.run(
        forwarder._async_publish_to_local(
            "tibber-pulse-ABC123/receive",
            b"\x00command",
            1,
            True,
        )
    )

    assert len(published) == 1
    assert published[0][0:2] == ("tibber-pulse-ABC123/receive", b"\x00command")
    assert published[0][2]["qos"] == 1
    assert published[0][2]["retain"] is True
    assert published[0][2]["encoding"] is None


def test_aws_downlink_uses_configured_external_broker():
    forwarder = _forwarder()
    published = []

    async def _publish(topic, payload, **kwargs):
        published.append((topic, payload, kwargs))

    external_client = SimpleNamespace(async_publish=_publish)
    source_hub = SimpleNamespace(_ext_mqtt=external_client)
    forwarder.cfg = {"source_entry_id": "local-entry"}
    forwarder.hass = SimpleNamespace(
        data={"tibber_pulse_mqtt": {"local-entry": source_hub}}
    )

    asyncio.run(
        forwarder._async_publish_to_local(
            "tibber-pulse-ABC123/receive",
            b"command",
            1,
            False,
        )
    )

    assert published[0][0:2] == ("tibber-pulse-ABC123/receive", b"command")
    assert published[0][2] == {"qos": 1, "retain": False}


def test_message_for_other_aws_topic_is_not_forwarded(monkeypatch):
    forwarder = _forwarder()
    published = []

    async def _publish(*args, **kwargs):
        published.append((args, kwargs))

    monkeypatch.setattr(aws_forwarder_module.ha_mqtt, "async_publish", _publish)
    forwarder.hass = object()

    asyncio.run(
        forwarder._async_publish_to_local(
            "tibber-pulse-OTHER/receive",
            b"wrong device",
            1,
            False,
        )
    )

    assert published == []


def test_aws_callback_filters_topics_before_scheduling():
    forwarder = _forwarder()
    scheduled = []
    forwarder.hass = SimpleNamespace(add_job=lambda *args: scheduled.append(args))
    valid_message = SimpleNamespace(
        topic="tibber-pulse-ABC123/receive",
        payload=b"cmd",
        qos=1,
        retain=False,
    )
    invalid_message = SimpleNamespace(
        topic="tibber-pulse-OTHER/receive",
        payload=b"cmd",
        qos=1,
        retain=False,
    )

    forwarder._on_message(None, None, invalid_message)
    forwarder._on_message(None, None, valid_message)

    assert len(scheduled) == 1
    assert scheduled[0][1:] == (
        "tibber-pulse-ABC123/receive",
        b"cmd",
        1,
        False,
    )
