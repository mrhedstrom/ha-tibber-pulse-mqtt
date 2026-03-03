from __future__ import annotations

import os
import re
import ssl
import threading
import logging
from typing import Callable, Optional, Pattern
from homeassistant.core import HomeAssistant
from homeassistant.components import mqtt as ha_mqtt
import paho.mqtt.client as paho

_LOGGER = logging.getLogger(__name__)

MessageCallback = Callable[[str, bytes], None]  # topic, payload


# ------------------------------
# Helpers for extended '+' logic
# ------------------------------

def _has_extended_plus(topic: str) -> bool:
    """Return True if any topic level contains '+' not equal to '+' (i.e., embedded '+')."""
    for level in topic.split('/'):
        if '+' in level and level != '+':
            return True
    return False


def _derive_subscribe_topic(pattern: str) -> str:
    """
    Transform an extended '+' pattern into a valid MQTT subscribe topic.

    Rule:
    - If a level contains '+' and that level != '+', replace that entire level with '+'.
      e.g., 'tibber-pulse-+/update' -> '+/update'
    - Otherwise, keep each level as-is.
    """
    levels = pattern.split('/')
    sub_levels = []
    for lvl in levels:
        if '+' in lvl and lvl != '+':
            sub_levels.append('+')
        else:
            sub_levels.append(lvl)
    return '/'.join(sub_levels)


def _compile_topic_regex(pattern: str) -> Pattern:
    """
    Compile a regex from a topic pattern that may contain:
    - Standard MQTT '+' level wildcard (match any single level).
    - Extended embedded '+' inside a level (treated as [^/]+ at each '+' position).
    - Standard MQTT '#' (if present as a full level, treated as '.*' in regex).

    The resulting regex matches full topics (anchored).
    """
    regex_parts = []
    levels = pattern.split('/')
    has_hash = False

    for i, lvl in enumerate(levels):
        if lvl == '+':
            # Standard single-level wildcard
            regex_parts.append(r'[^/]+')
        elif lvl == '#':
            # Multi-level wildcard; normally only valid as the last level
            regex_parts.append(r'.*')
            has_hash = True
            # If '#' is not the last level, broker would reject, but our regex still tolerates it.
        elif '+' in lvl:
            # Extended embedded '+': treat each '+' as [^/]+ inside this level
            # Build by escaping the literal chunks and joining with [^/]+
            chunks = lvl.split('+')
            escaped = [re.escape(c) for c in chunks]
            # If pattern starts/ends with '+', we still require non-empty for each '+'
            part = r'[^/]+'.join(escaped)
            regex_parts.append(part)
        else:
            # Literal level
            regex_parts.append(re.escape(lvl))

    # Anchor regex; if '#' present, '.*' will consume tail anyway
    regex_str = '^' + '/'.join(regex_parts) + '$'
    return re.compile(regex_str)


def _payload_to_bytes(payload) -> bytes:
    """Normalize payload to bytes."""
    return payload if isinstance(payload, (bytes, bytearray)) else str(payload).encode()


def _debug_log_rx(prefix: str, topic: str, payload: bytes, enabled: bool):
    """Log a compact RX line if debug is enabled."""
    if enabled:
        head = payload[:16].hex()
        _LOGGER.debug("[%s] RX topic=%s len=%d head=%s", prefix, topic, len(payload), head)


class HAMQTTBridge:
    """Subscribe via Home Assistant's MQTT integration with extended '+' support."""

    def __init__(self, hass: HomeAssistant):
        self.hass = hass
        self._unsubs = []
        self._debug = _LOGGER.isEnabledFor(logging.DEBUG)

    async def async_subscribe(self, topic: str, cb: MessageCallback):
        """
        Subscribe with support for embedded '+':
        - If the topic contains an embedded '+', subscribe to a derived topic with '+' as the whole level.
        - Filter messages locally to ensure they match the original pattern.
        """
        await ha_mqtt.async_wait_for_mqtt_client(self.hass)

        needs_filter = _has_extended_plus(topic)
        subscribe_topic = _derive_subscribe_topic(topic) if needs_filter else topic
        filter_regex = _compile_topic_regex(topic) if needs_filter else None

        async def _wrapped(msg):
            if filter_regex and not filter_regex.match(msg.topic):
                if self._debug:
                    _LOGGER.debug("[HA MQTT] Filtered topic (no match): wanted=%s got=%s", topic, msg.topic)
                return

            payload = _payload_to_bytes(msg.payload)
            _debug_log_rx("HA MQTT", msg.topic, payload, self._debug)

            try:
                cb(msg.topic, payload)
            except Exception as e:
                _LOGGER.exception("Callback error: %s", e)

        unsub = await ha_mqtt.async_subscribe(self.hass, subscribe_topic, _wrapped, qos=1, encoding=None)
        self._unsubs.append(unsub)

    async def async_stop(self):
        for u in self._unsubs:
            u()
        self._unsubs.clear()


class ExternalMQTTClient:
    """Own paho client in a background thread with extended '+' support."""

    def __init__(
        self,
        host: str,
        port: int,
        topic: str,
        cb: MessageCallback,
        username: Optional[str] = None,
        password: Optional[str] = None,
        client_id: Optional[str] = None,
        tls: bool = False,
        cafile: Optional[str] = None,
        certfile: Optional[str] = None,
        keyfile: Optional[str] = None,
        tls_insecure: bool = False,
        tls_version: str = "tlsv1.2",
    ):
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

        # Prepare extended '+' handling for paho client:
        # - If topic contains embedded '+', subscribe to a broadened topic and filter locally.
        self._needs_filter = _has_extended_plus(self.topic)
        self._subscribe_topic = _derive_subscribe_topic(self.topic) if self._needs_filter else self.topic
        self._filter_regex = _compile_topic_regex(self.topic) if self._needs_filter else None

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
        _LOGGER.info("[Ext MQTT] Connected rc=%s, subscribing to %s (derived=%s)",
                     rc, self.topic, self._subscribe_topic)
        client.subscribe(self._subscribe_topic, qos=1)

    def _on_message(self, client, userdata, msg):
        if self._filter_regex and not self._filter_regex.match(msg.topic):
            if self._debug:
                _LOGGER.debug("[Ext MQTT] Filtered topic (no match): wanted=%s got=%s", self.topic, msg.topic)
            return

        payload = _payload_to_bytes(msg.payload)
        _debug_log_rx("Ext MQTT", msg.topic, payload, self._debug)

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