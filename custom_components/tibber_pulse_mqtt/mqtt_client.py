from __future__ import annotations

import os
import ssl
import threading
import logging
from typing import Callable, Optional
from homeassistant.core import HomeAssistant
from homeassistant.components import mqtt as ha_mqtt
import paho.mqtt.client as paho

_LOGGER = logging.getLogger(__name__)

MessageCallback = Callable[[str, bytes], None]  # topic, payload

class HAMQTTBridge:
    """Subscribe via Home Assistant's MQTT integration."""
    def __init__(self, hass: HomeAssistant):
        self.hass = hass
        self._unsubs = []
        self._debug = _LOGGER.isEnabledFor(logging.DEBUG)

    async def async_subscribe(self, topic: str, cb: MessageCallback):
        await ha_mqtt.async_wait_for_mqtt_client(self.hass)
        async def _wrapped(msg):
            payload = msg.payload if isinstance(msg.payload, (bytes, bytearray)) else str(msg.payload).encode()
            if self._debug:
                head = payload[:16].hex()
                _LOGGER.debug("[HA MQTT] RX topic=%s len=%d head=%s", msg.topic, len(payload), head)
            cb(msg.topic, payload)
        unsub = await ha_mqtt.async_subscribe(self.hass, topic, _wrapped, qos=1, encoding=None)
        self._unsubs.append(unsub)

    async def async_stop(self):
        for u in self._unsubs:
            u()
        self._unsubs.clear()

class ExternalMQTTClient:
    """Own paho client in a background thread."""
    def __init__(self, host: str, port: int, topic: str, cb: MessageCallback,
                username: Optional[str]=None, password: Optional[str]=None,
                client_id: Optional[str]=None,
                tls: bool=False, cafile: Optional[str]=None, certfile: Optional[str]=None,
                keyfile: Optional[str]=None, tls_insecure: bool=False, tls_version: str="tlsv1.2"):
        self.host, self.port = host, port
        self.topic = topic
        self.cb = cb
        self.username, self.password = username, password
        self.client_id = client_id or f"tibber-pulse-mqtt-{os.getpid()}"
        self.tls = tls
        self.cafile, self.certfile, self.keyfile = cafile, certfile, keyfile
        self.tls_insecure = tls_insecure
        self.tls_version = tls_version
        self._debug = _LOGGER.isEnabledFor(logging.DEBUG)

        self._client = paho.Client(client_id=self.client_id)
        if self.username:
            self._client.username_pw_set(self.username, self.password)
        if self.tls:
            ctx = ssl.create_default_context(cafile=self.cafile) if self.cafile else ssl.create_default_context()
            if self.certfile and self.keyfile:
                ctx.load_cert_chain(certfile=self.certfile, keyfile=self.keyfile)
            if self.tls_version == "tlsv1.2":
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            elif self.tls_version == "tlsv1.3":
                ctx.minimum_version = ssl.TLSVersion.TLSv1_3
            if self.tls_insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            self._client.tls_set_context(ctx)

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._thread: Optional[threading.Thread] = None

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        _LOGGER.info("[Ext MQTT] Connected rc=%s, subscribing to %s", rc, self.topic)
        client.subscribe(self.topic, qos=1)

    def _on_message(self, client, userdata, msg):
        payload = msg.payload
        if self._debug:
            head = payload[:16].hex()
            _LOGGER.debug("[Ext MQTT] RX topic=%s len=%d head=%s", msg.topic, len(payload), head)
        try:
            self.cb(msg.topic, payload)
        except Exception as e:
            _LOGGER.exception("Callback error: %s", e)

    def start(self):
        self._client.connect(self.host, self.port, keepalive=60)
        self._thread = threading.Thread(target=self._client.loop_forever, daemon=True)
        self._thread.start()

    def stop(self):
        try:
            self._client.disconnect()
        finally:
            if self._thread:
                self._thread.join(timeout=5)