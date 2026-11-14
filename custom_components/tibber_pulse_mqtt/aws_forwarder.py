from __future__ import annotations

import asyncio
import logging
import ssl
import threading
from collections import deque
from pathlib import Path
from types import SimpleNamespace

from homeassistant.components import mqtt as ha_mqtt
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
import paho.mqtt.client as paho

from .aws import (
    client_id_for_device,
    parse_aws_endpoint,
    receive_topic_for_publish,
    validate_mqtt_topic,
    validate_certificate_files,
    test_aws_connection,
)
from .const import (
    CONF_AWS_CA_PATH,
    CONF_AWS_CONFIGURED,
    CONF_AWS_ENDPOINT,
    CONF_AWS_KEY_PATH,
    CONF_AWS_PUBLISH_TOPIC_OVERRIDE,
    CONF_AWS_RECEIVE_TOPIC_OVERRIDE,
    CONF_AWS_CERT_PATH,
    CONF_DEVICE_ID,
    CONF_PUBLISH_TOPIC,
    CONF_RECEIVE_TOPIC,
    CONF_SOURCE_ENTRY_ID,
    DOMAIN,
    DEFAULT_AWS_ENDPOINT,
    AWS_FORWARDING_CONNECTING,
    AWS_FORWARDING_CONNECTED,
    AWS_FORWARDING_ERROR,
    AWS_FORWARDING_NOT_CONFIGURED,
    AWS_INVALID_CERT_ISSUE_PREFIX,
    AWS_CONNECTION_ISSUE_PREFIX,
    AWS_UNCONFIGURED_ISSUE_PREFIX,
)
from homeassistant.helpers import issue_registry as ir

_LOGGER = logging.getLogger(__name__)
CONNECT_TIMEOUT = 15


class AWSForwarder:
    """Own the per-device native AWS forwarding config entry."""

    def __init__(self, hass, entry) -> None:
        self.hass = hass
        self.entry = entry
        self.device_id = entry.data[CONF_DEVICE_ID]
        self.cfg = {**entry.data, **entry.options}
        self.publish_topic = validate_mqtt_topic(entry.data[CONF_PUBLISH_TOPIC])
        self.receive_topic = validate_mqtt_topic(
            entry.data.get(CONF_RECEIVE_TOPIC)
            or receive_topic_for_publish(self.publish_topic)
        )
        publish_override = self.cfg.get(CONF_AWS_PUBLISH_TOPIC_OVERRIDE, "")
        receive_override = self.cfg.get(CONF_AWS_RECEIVE_TOPIC_OVERRIDE, "")
        self.aws_publish_topic = validate_mqtt_topic(
            publish_override or self.publish_topic
        )
        self.aws_receive_topic = validate_mqtt_topic(
            receive_override
            or (
                receive_topic_for_publish(self.aws_publish_topic)
                if publish_override
                else self.receive_topic
            )
        )
        self.endpoint = self.cfg.get(CONF_AWS_ENDPOINT, DEFAULT_AWS_ENDPOINT)
        self._client = None
        self._thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._connection_result: int | None = None
        self._local_unsubscribe = None
        self._local_hub = None
        self._stopping = False
        self._pending_upstream: deque[tuple[str, bytes, int, bool]] = deque(maxlen=1000)
        self._pending_lock = threading.Lock()
        self._connection_test_lock = asyncio.Lock()
        self.state = AWS_FORWARDING_NOT_CONFIGURED

    async def async_start(self) -> None:
        """Associate this config entry with its device and start configured forwarding."""
        status = self._device_status()
        dr.async_get(self.hass).async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(f"tibber_pulse_{self.device_id}",)},
            manufacturer="Tibber",
            model=status.get("hwmodel") or status.get("Hw") or "Pulse",
            name=f"Tibber Pulse ({self.device_id})",
        )
        if not self.cfg.get(CONF_AWS_CONFIGURED):
            await self._async_set_device_status(AWS_FORWARDING_NOT_CONFIGURED)
            return

        cert_path = Path(self.cfg.get(CONF_AWS_CERT_PATH, ""))
        key_path = Path(self.cfg.get(CONF_AWS_KEY_PATH, ""))
        ca_path = Path(self.cfg.get(CONF_AWS_CA_PATH, ""))
        try:
            await self.hass.async_add_executor_job(
                validate_certificate_files,
                cert_path,
                key_path,
                ca_path if ca_path.is_file() else None,
            )
        except (OSError, ValueError):
            await self._async_set_device_status(AWS_FORWARDING_ERROR)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{AWS_INVALID_CERT_ISSUE_PREFIX}{self.device_id}",
                is_fixable=False,
                is_persistent=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key="aws_forwarding_invalid_certificate",
                translation_placeholders={"device": self.device_id},
            )
            return

        try:
            await self._async_subscribe_local_publish()
            await self._async_set_device_status(AWS_FORWARDING_CONNECTING)
            await self.hass.async_add_executor_job(
                self._start_client,
                cert_path,
                key_path,
                ca_path if ca_path.is_file() else None,
            )
        except Exception as err:
            await self.async_stop()
            await self._async_set_device_status(AWS_FORWARDING_ERROR)
            self._create_connection_issue()
            raise ConfigEntryNotReady("Unable to start the AWS MQTT connection") from err

    async def _async_subscribe_local_publish(self) -> None:
        """Subscribe to this Pulse publish topic via its configured local MQTT client."""
        if self._local_unsubscribe:
            return
        source_entry_id = self.cfg.get(CONF_SOURCE_ENTRY_ID)
        source_hub = self.hass.data.get(DOMAIN, {}).get(source_entry_id)
        if source_entry_id and source_hub is None:
            raise ConfigEntryNotReady("The local MQTT config entry is not ready")
        if source_hub and source_hub._ha_mqtt:
            self._local_hub = source_hub
            self._local_unsubscribe = await source_hub._ha_mqtt.async_subscribe(
                self.publish_topic,
                self._async_on_local_message_metadata,
                include_metadata=True,
            )
            return
        if source_hub and source_hub._ext_mqtt:
            self._local_hub = source_hub
            self._local_unsubscribe = source_hub._ext_mqtt.async_subscribe(
                self.publish_topic,
                self._async_on_local_message_metadata,
                include_metadata=True,
            )
            return
        if source_hub:
            raise ConfigEntryNotReady("The local MQTT client is not ready")
        if source_entry_id:
            raise ConfigEntryNotReady("The local MQTT config entry is not ready")
        await ha_mqtt.async_wait_for_mqtt_client(self.hass)
        self._local_unsubscribe = await ha_mqtt.async_subscribe(
            self.hass,
            self.publish_topic,
            self._async_on_local_message,
            qos=1,
            encoding=None,
        )

    async def async_detach_local_subscription(self) -> None:
        """Unsubscribe from a local hub that is stopping or reloading."""
        if self._local_unsubscribe:
            if self._local_hub and self._local_hub._ha_mqtt:
                self._local_hub._ha_mqtt.async_unsubscribe(self._local_unsubscribe)
            else:
                self._local_unsubscribe()
            self._local_unsubscribe = None
        self._local_hub = None

    async def async_attach_local_subscription(self) -> None:
        """Attach to the configured local hub after it starts or reloads."""
        if self.cfg.get(CONF_AWS_CONFIGURED) and self._client:
            await self._async_subscribe_local_publish()

    async def async_test_connection(
        self,
        endpoint: str,
        certificate_path: str | Path,
        private_key_path: str | Path,
        ca_path: str | Path | None,
    ) -> None:
        """Test AWS credentials without competing with this entry's active client ID."""
        async with self._connection_test_lock:
            was_running = self._client is not None
            if was_running:
                await self.hass.async_add_executor_job(self._stop_client)
                self._client = None
                self._thread = None
                self._connected.clear()

            try:
                await self.hass.async_add_executor_job(
                    test_aws_connection,
                    endpoint,
                    self.device_id,
                    certificate_path,
                    private_key_path,
                    ca_path,
                )
            finally:
                if was_running:
                    current = {**self.entry.data, **self.entry.options}
                    cert_path = Path(current[CONF_AWS_CERT_PATH])
                    key_path = Path(current[CONF_AWS_KEY_PATH])
                    current_ca = Path(current[CONF_AWS_CA_PATH])
                    try:
                        await self.hass.async_add_executor_job(
                            self._start_client,
                            cert_path,
                            key_path,
                            current_ca if current_ca.is_file() else None,
                        )
                    except Exception:
                        _LOGGER.exception(
                            "Unable to restart AWS forwarding after connection test for Pulse %s",
                            self.device_id,
                        )
                        self.hass.add_job(self._create_connection_issue)
                        self.hass.add_job(
                            self._async_set_device_status,
                            AWS_FORWARDING_ERROR,
                        )

    def async_refresh_config(self) -> None:
        """Refresh option-backed runtime values after a config entry reload."""
        self.cfg = {**self.entry.data, **self.entry.options}
        self.endpoint = self.cfg.get(CONF_AWS_ENDPOINT, DEFAULT_AWS_ENDPOINT)
        publish_override = self.cfg.get(CONF_AWS_PUBLISH_TOPIC_OVERRIDE, "")
        receive_override = self.cfg.get(CONF_AWS_RECEIVE_TOPIC_OVERRIDE, "")
        self.aws_publish_topic = validate_mqtt_topic(
            publish_override or self.publish_topic
        )
        self.aws_receive_topic = validate_mqtt_topic(
            receive_override
            or (
                receive_topic_for_publish(self.aws_publish_topic)
                if publish_override
                else self.receive_topic
            )
        )

    def _async_on_local_message_metadata(
        self,
        topic: str,
        payload: bytes,
        qos: int,
        retain: bool,
    ) -> None:
        """Forward a source-hub message while retaining MQTT metadata."""
        message = SimpleNamespace(
            topic=topic,
            payload=payload,
            qos=qos,
            retain=retain,
        )
        self.hass.add_job(self._async_on_local_message, message)

    async def async_stop(self) -> None:
        """Stop the AWS client and local MQTT subscription, if configured."""
        self._stopping = True
        await self.async_detach_local_subscription()
        if self._client:
            await self.hass.async_add_executor_job(self._stop_client)
        self._client = None
        self._thread = None

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set() and self._connection_result == 0

    def _start_client(self, cert_path: Path, key_path: Path, ca_path: Path | None) -> None:
        host, port = parse_aws_endpoint(self.endpoint)
        client = paho.Client(client_id=client_id_for_device(self.device_id))
        context = ssl.create_default_context(cafile=str(ca_path) if ca_path else None)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(cert_path), str(key_path))
        client.tls_set_context(context)
        client.reconnect_delay_set(min_delay=1, max_delay=120)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        self._client = client
        self._stopping = False
        self._connected.clear()
        self._connection_result = None
        client.connect(host, port, keepalive=60)
        self._thread = threading.Thread(
            target=client.loop_forever,
            name=f"tibber_aws_{self.device_id}",
            daemon=True,
        )
        self._thread.start()
        if not self._connected.wait(CONNECT_TIMEOUT):
            self._stop_client()
            raise TimeoutError("AWS MQTT connection timed out")
        if self._connection_result != 0:
            self._stop_client()
            raise ConnectionError("AWS MQTT connection was rejected")

    def _stop_client(self) -> None:
        if self._client:
            try:
                self._client.disconnect()
            except Exception:
                _LOGGER.debug("AWS MQTT disconnect failed for %s", self.device_id)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    async def _async_on_local_message(self, message) -> None:
        """Forward only the exact observed local publish topic to AWS."""
        if message.topic != self.publish_topic or not self._client:
            return
        payload = bytes(message.payload)
        qos = min(message.qos, 1)
        retain = message.retain
        if not self.is_connected:
            with self._pending_lock:
                if len(self._pending_upstream) == self._pending_upstream.maxlen:
                    _LOGGER.warning("AWS forwarding queue is full for Pulse %s", self.device_id)
                self._pending_upstream.append(
                    (self.aws_publish_topic, payload, qos, retain)
                )
            return
        result = self._client.publish(
            self.aws_publish_topic,
            payload,
            qos=qos,
            retain=retain,
        )
        if result.rc != paho.MQTT_ERR_SUCCESS:
            _LOGGER.debug("AWS publish was not accepted for Pulse %s", self.device_id)

    def _on_connect(self, client, _userdata, _flags, reason_code, *args) -> None:
        try:
            self._connection_result = int(reason_code)
        except (TypeError, ValueError):
            self._connection_result = 1
        if self._connection_result == 0:
            client.subscribe(self.aws_receive_topic, qos=1)
            self._connected.set()
            self._drain_upstream_queue(client)
            self.hass.add_job(
                ir.async_delete_issue,
                self.hass,
                DOMAIN,
                f"{AWS_INVALID_CERT_ISSUE_PREFIX}{self.device_id}",
            )
            self.hass.add_job(
                ir.async_delete_issue,
                self.hass,
                DOMAIN,
                f"{AWS_CONNECTION_ISSUE_PREFIX}{self.device_id}",
            )
            self.hass.add_job(
                ir.async_delete_issue,
                self.hass,
                DOMAIN,
                f"{AWS_UNCONFIGURED_ISSUE_PREFIX}{self.device_id}",
            )
            self.hass.add_job(self._async_set_device_status, AWS_FORWARDING_CONNECTED)
        else:
            self._connected.set()
            self.hass.add_job(self._async_set_device_status, AWS_FORWARDING_ERROR)
            self.hass.add_job(self._create_connection_issue)

    def _on_disconnect(self, _client, _userdata, reason_code, *args) -> None:
        if self._stopping:
            return
        self._connected.clear()
        if reason_code:
            self.hass.add_job(self._async_set_device_status, AWS_FORWARDING_ERROR)
            self.hass.add_job(self._create_connection_issue)

    def _on_message(self, _client, _userdata, message) -> None:
        if message.topic != self.aws_receive_topic:
            return
        self.hass.add_job(
            self._async_publish_to_local,
            message.topic,
            bytes(message.payload),
            message.qos,
            message.retain,
        )

    def _drain_upstream_queue(self, client) -> None:
        with self._pending_lock:
            queued = list(self._pending_upstream)
            self._pending_upstream.clear()
        for topic, payload, qos, retain in queued:
            result = client.publish(topic, payload, qos=qos, retain=retain)
            if result.rc != paho.MQTT_ERR_SUCCESS:
                with self._pending_lock:
                    self._pending_upstream.appendleft((topic, payload, qos, retain))
                break

    async def _async_publish_to_local(
        self,
        topic: str,
        payload: bytes,
        qos: int,
        retain: bool,
    ) -> None:
        if topic != self.aws_receive_topic:
            return
        source_entry_id = self.cfg.get(CONF_SOURCE_ENTRY_ID)
        source_hub = self.hass.data.get(DOMAIN, {}).get(source_entry_id)
        for attempt in range(5):
            if self._stopping:
                return
            try:
                if source_hub and source_hub._ext_mqtt:
                    await source_hub._ext_mqtt.async_publish(
                        self.receive_topic,
                        payload,
                        qos=qos,
                        retain=retain,
                    )
                else:
                    await ha_mqtt.async_publish(
                        self.hass,
                        self.receive_topic,
                        payload,
                        qos=min(qos, 1),
                        retain=retain,
                        encoding=None,
                    )
                return
            except Exception:
                if attempt == 4:
                    _LOGGER.exception(
                        "Unable to forward AWS message to local MQTT for Pulse %s",
                        self.device_id,
                    )
                    return
                await asyncio.sleep(2**attempt)

    async def _async_set_device_status(self, state: str) -> None:
        self.state = state
        for runtime in self.hass.data.get(DOMAIN, {}).values():
            manager = getattr(runtime, "sensor_manager", None)
            if manager and hasattr(manager, "set_aws_forwarding_status"):
                manager.set_aws_forwarding_status(
                    self.device_id,
                    state,
                    self._device_status(),
                )

    def _create_connection_issue(self) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{AWS_CONNECTION_ISSUE_PREFIX}{self.device_id}",
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="aws_forwarding_connection_failed",
            translation_placeholders={"device": self.device_id},
        )

    def _device_status(self) -> dict:
        for runtime in self.hass.data.get(DOMAIN, {}).values():
            if isinstance(runtime, dict):
                continue
            dispatcher = getattr(runtime, "dispatcher", None)
            if dispatcher:
                status = dispatcher.discovered_pulse_devices.get(self.device_id)
                if status:
                    return status
        return {}
