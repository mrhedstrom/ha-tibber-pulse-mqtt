"""Built-in forwarding of one Tibber Pulse's MQTT traffic to Tibber Cloud (AWS IoT).

Replaces an external broker bridge. The forwarder mirrors what the documented
Mosquitto bridge does:

* Pulse -> cloud: messages published locally on the Pulse's publish topic are
  forwarded unchanged to the cloud (QoS 1).
* cloud -> Pulse: messages arriving from the cloud on the receive topic are
  published locally so the Pulse sees firmware updates and remote commands.

The forwarder owns its own paho client (one per Pulse) and never touches the
local MQTT subscriptions; the hub hands it every local message. All callbacks
coming from paho's network thread are moved onto the event loop before they
touch Home Assistant.
"""
from __future__ import annotations

import dataclasses
import logging
import ssl
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

import paho.mqtt.client as paho

from .cloud_certs import (
    CA_FILE,
    CERT_FILE,
    KEY_FILE,
    CertificateError,
    commit_staged_credentials,
    credentials_dir,
    discard_staged_credentials,
    stage_credentials,
)
from .const import (
    CLOUD_STATE_AUTH_FAILED,
    CLOUD_STATE_CONNECTED,
    CLOUD_STATE_CONNECTING,
    CLOUD_STATE_DISABLED,
    CLOUD_STATE_DISCONNECTED,
    CLOUD_STATE_INVALID_CERTIFICATE,
    DEFAULT_CLOUD_HOST,
    DEFAULT_CLOUD_PORT,
    DEFAULT_CLOUD_TLS_VERSION,
    CONF_CLOUD_HOST,
    CONF_CLOUD_PORT,
    CONF_CLOUD_PUBLISH_TOPIC,
    CONF_CLOUD_RECEIVE_TOPIC,
    CONF_CLOUD_TLS_VERSION,
    CONF_LOCAL_TOPIC,
    CONF_PULSE_ID,
)

_LOGGER = logging.getLogger(__name__)

PUBLISH_SUFFIX = "/publish"
RECEIVE_SUFFIX = "/receive"

# VeriSign Class 3 Public Primary CA - G5. The Tibber endpoint chains to it and it is
# not part of the certifi bundle, so it is shipped as the default CA.
BUNDLED_CA_PATH = Path(__file__).parent / "ca" / "verisign_class3_g5.pem"

# CONNACK results that mean "the cloud rejected our credentials" (MQTT 3.1.1 return
# codes 4/5 and their MQTT 5 reason code equivalents 134/135).
_AUTH_REJECTED_CODES = frozenset({4, 5, 134, 135})

_TLS_VERSIONS = {
    "tlsv1.2": ssl.TLSVersion.TLSv1_2,
    "tlsv1.3": ssl.TLSVersion.TLSv1_3,
}

CONNECTION_TEST_TIMEOUT = 15
MAX_QUEUED_MESSAGES = 200
RECONNECT_MIN_DELAY = 5
RECONNECT_MAX_DELAY = 120
KEEPALIVE = 60

PublishLocal = Callable[[str, bytes], Awaitable[None]]
StateCallback = Callable[[str, str], None]  # (pulse_id, state)


def validate_exact_topic(topic: str) -> str:
    """Return topic if it is a non-empty, exact (wildcard-free) MQTT topic."""
    if (
        not topic
        or topic.startswith("/")
        or topic.endswith("/")
        or "//" in topic
        or "\x00" in topic
        or "+" in topic
        or "#" in topic
    ):
        raise ValueError(f"Not an exact MQTT topic: {topic!r}")
    return topic


def _publish_base(publish_topic: str) -> str:
    validate_exact_topic(publish_topic)
    if not publish_topic.endswith(PUBLISH_SUFFIX):
        raise ValueError(f"Publish topic must end with {PUBLISH_SUFFIX}: {publish_topic!r}")
    return publish_topic[: -len(PUBLISH_SUFFIX)]


def receive_topic_for(publish_topic: str) -> str:
    """Return the receive topic that belongs to a publish topic."""
    return f"{_publish_base(publish_topic)}{RECEIVE_SUFFIX}"


def client_id_for(publish_topic: str) -> str:
    """Return the cloud client ID: the last topic level before ``/publish``.

    The Tibber cloud identifies a Pulse by its AWS thing name, which is the topic
    prefix (``tibber-pulse-<thing>``). It is not the Pulse ID from the STATUS message.
    """
    return _publish_base(publish_topic).rsplit("/", 1)[-1]


@dataclass(frozen=True)
class CloudForwardingConfig:
    """Connection settings for forwarding one Pulse to Tibber Cloud."""

    pulse_id: str
    local_publish_topic: str
    cert_path: Path
    key_path: Path
    ca_path: Optional[Path] = None  # None -> bundled CA
    host: str = DEFAULT_CLOUD_HOST
    port: int = DEFAULT_CLOUD_PORT
    tls_version: str = DEFAULT_CLOUD_TLS_VERSION
    aws_publish_topic: Optional[str] = None  # None -> same as the local publish topic
    aws_receive_topic: Optional[str] = None  # None -> same as the local receive topic

    def __post_init__(self) -> None:
        if not self.pulse_id:
            raise ValueError("pulse_id is required")
        validate_exact_topic(self.local_publish_topic)
        receive_topic_for(self.local_publish_topic)
        if self.aws_publish_topic:
            validate_exact_topic(self.aws_publish_topic)
        if self.aws_receive_topic:
            validate_exact_topic(self.aws_receive_topic)
        if not self.host or any(c.isspace() for c in self.host) or "/" in self.host:
            raise ValueError(f"Invalid host: {self.host!r}")
        if not 1 <= int(self.port) <= 65535:
            raise ValueError(f"Invalid port: {self.port!r}")
        if self.tls_version not in _TLS_VERSIONS:
            raise ValueError(f"Unsupported TLS version: {self.tls_version!r}")

    @property
    def client_id(self) -> str:
        return client_id_for(self.local_publish_topic)

    @property
    def local_receive_topic(self) -> str:
        return receive_topic_for(self.local_publish_topic)

    @property
    def effective_aws_publish_topic(self) -> str:
        return self.aws_publish_topic or self.local_publish_topic

    @property
    def effective_aws_receive_topic(self) -> str:
        return self.aws_receive_topic or self.local_receive_topic


def build_ssl_context(config: CloudForwardingConfig) -> ssl.SSLContext:
    """Build the TLS context. Blocking (reads files); run it in the executor."""
    ca_path = config.ca_path or BUNDLED_CA_PATH
    context = ssl.create_default_context(cafile=str(ca_path))
    version = _TLS_VERSIONS[config.tls_version]
    context.minimum_version = version
    context.maximum_version = version
    context.load_cert_chain(str(config.cert_path), str(config.key_path))
    return context


def config_from_subentry(config_path, subentry) -> CloudForwardingConfig:
    """Build the forwarder configuration from a stored subentry.

    ``config_path`` is ``hass.config.path``. Only deviations from the defaults are
    stored in the subentry; the credentials are read from their own directory.
    Raises ValueError/KeyError when the stored data is unusable.
    """
    data = subentry.data
    directory = credentials_dir(config_path, data[CONF_PULSE_ID])
    ca_path = directory / CA_FILE
    return CloudForwardingConfig(
        pulse_id=data[CONF_PULSE_ID],
        local_publish_topic=data[CONF_LOCAL_TOPIC],
        cert_path=directory / CERT_FILE,
        key_path=directory / KEY_FILE,
        ca_path=ca_path if ca_path.is_file() else None,
        host=data.get(CONF_CLOUD_HOST, DEFAULT_CLOUD_HOST),
        port=int(data.get(CONF_CLOUD_PORT, DEFAULT_CLOUD_PORT)),
        tls_version=data.get(CONF_CLOUD_TLS_VERSION, DEFAULT_CLOUD_TLS_VERSION),
        aws_publish_topic=data.get(CONF_CLOUD_PUBLISH_TOPIC) or None,
        aws_receive_topic=data.get(CONF_CLOUD_RECEIVE_TOPIC) or None,
    )


class CloudAuthError(ConnectionError):
    """The cloud accepted the TLS connection but rejected the client."""


def test_connection(config: CloudForwardingConfig, timeout: float = CONNECTION_TEST_TIMEOUT) -> None:
    """Connect once to the cloud and return when it accepts the client. Blocking.

    Raises CloudAuthError when the client is rejected, ConnectionError when no
    connection could be made, TimeoutError when the cloud does not answer in time and
    OSError/ssl.SSLError/ValueError when the credentials cannot be loaded.
    """
    context = build_ssl_context(config)
    finished = threading.Event()
    outcome: dict = {}

    def _on_connect(client, userdata, flags, reason_code, properties=None):
        outcome["reason_code"] = reason_code
        finished.set()

    def _on_connect_fail(client, userdata):
        outcome["failed"] = True
        finished.set()

    client = paho.Client(
        paho.CallbackAPIVersion.VERSION2,
        client_id=config.client_id,
        protocol=paho.MQTTv311,
        clean_session=True,
        reconnect_on_failure=False,
    )
    client.tls_set_context(context)
    client.on_connect = _on_connect
    client.on_connect_fail = _on_connect_fail
    client.connect_async(config.host, config.port, keepalive=30)
    client.loop_start()
    try:
        if not finished.wait(timeout):
            raise TimeoutError("The cloud did not answer in time")
        if outcome.get("failed"):
            raise ConnectionError("Could not connect to the cloud")
        reason_code = outcome["reason_code"]
        if getattr(reason_code, "is_failure", reason_code != 0):
            code = getattr(reason_code, "value", reason_code)
            if code in _AUTH_REJECTED_CODES:
                raise CloudAuthError("The cloud rejected the certificate")
            raise ConnectionError(f"The cloud refused the connection ({code})")
    finally:
        try:
            client.disconnect()
        finally:
            client.loop_stop()


class CloudForwarder:
    """Forward one Pulse's traffic between the local broker and Tibber Cloud."""

    def __init__(
        self,
        hass,
        config: CloudForwardingConfig,
        publish_local: PublishLocal,
        on_state: Optional[StateCallback] = None,
    ) -> None:
        self._hass = hass
        self._config = config
        self._publish_local = publish_local
        self._on_state = on_state
        self._client: Optional[paho.Client] = None
        self._state = CLOUD_STATE_DISABLED
        self._stopping = False

    @property
    def config(self) -> CloudForwardingConfig:
        return self._config

    @property
    def state(self) -> str:
        return self._state

    # ------------------------------ Lifecycle ---------------------------------

    async def async_start(self) -> None:
        """Start connecting in the background. Never blocks on the network."""
        if self._client is not None:
            return
        self._stopping = False
        self._set_state(CLOUD_STATE_CONNECTING)

        try:
            context = await self._hass.async_add_executor_job(
                build_ssl_context, self._config
            )
        except (OSError, ssl.SSLError, ValueError) as err:
            _LOGGER.warning(
                "Tibber Cloud forwarding for Pulse %s cannot start, the certificate "
                "or private key could not be loaded (%s: %s)",
                self._config.pulse_id,
                type(err).__name__,
                err,
            )
            self._set_state(CLOUD_STATE_INVALID_CERTIFICATE)
            return

        client = paho.Client(
            paho.CallbackAPIVersion.VERSION2,
            client_id=self._config.client_id,
            protocol=paho.MQTTv311,
            clean_session=True,
        )
        client.tls_set_context(context)
        client.reconnect_delay_set(RECONNECT_MIN_DELAY, RECONNECT_MAX_DELAY)
        client.max_queued_messages_set(MAX_QUEUED_MESSAGES)
        client.on_connect = self._on_connect
        client.on_connect_fail = self._on_connect_fail
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message

        self._client = client
        client.connect_async(self._config.host, self._config.port, keepalive=KEEPALIVE)
        client.loop_start()

    async def async_stop(self) -> None:
        """Disconnect and stop the network thread."""
        self._stopping = True
        client, self._client = self._client, None
        if client is not None:
            await self._hass.async_add_executor_job(self._stop_client, client)
        self._set_state(CLOUD_STATE_DISABLED)

    @staticmethod
    def _stop_client(client: paho.Client) -> None:
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001 - shutting down, nothing useful to do
            _LOGGER.debug("Cloud MQTT disconnect failed", exc_info=True)
        client.loop_stop()

    # --------------------------- Pulse -> cloud -------------------------------

    def forward_upstream(self, topic: str, payload: bytes) -> None:
        """Forward a local message to the cloud if it is this Pulse's publish topic.

        Must be called on the event loop. paho queues QoS 1 messages while the
        connection is down (bounded by MAX_QUEUED_MESSAGES) and sends them after
        it reconnects.
        """
        client = self._client
        if client is None or topic != self._config.local_publish_topic:
            return
        result = client.publish(
            self._config.effective_aws_publish_topic, payload, qos=1, retain=False
        )
        if result.rc != paho.MQTT_ERR_SUCCESS:
            _LOGGER.debug(
                "Tibber Cloud publish for Pulse %s was not accepted (rc=%s)",
                self._config.pulse_id,
                result.rc,
            )

    # --------------------------- Cloud -> Pulse -------------------------------

    def _handle_downstream(self, payload: bytes) -> None:
        """Publish a cloud message locally (runs on the event loop)."""
        self._hass.async_create_task(self._async_publish_local(payload))

    async def _async_publish_local(self, payload: bytes) -> None:
        try:
            await self._publish_local(self._config.local_receive_topic, payload)
        except Exception as err:  # noqa: BLE001 - keep the connection alive
            _LOGGER.warning(
                "Could not publish a Tibber Cloud message to the local broker for "
                "Pulse %s (%s: %s)",
                self._config.pulse_id,
                type(err).__name__,
                err,
            )

    # ----------------------- paho callbacks (network thread) -------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if getattr(reason_code, "is_failure", reason_code != 0):
            code = getattr(reason_code, "value", reason_code)
            state = (
                CLOUD_STATE_AUTH_FAILED
                if code in _AUTH_REJECTED_CODES
                else CLOUD_STATE_DISCONNECTED
            )
            self._set_state_threadsafe(state)
            return
        # Clean session: the subscription must be renewed on every (re)connect.
        client.subscribe(self._config.effective_aws_receive_topic, qos=1)
        self._set_state_threadsafe(CLOUD_STATE_CONNECTED)

    def _on_connect_fail(self, client, userdata) -> None:
        self._set_state_threadsafe(CLOUD_STATE_DISCONNECTED, keep_auth_failed=True)

    def _on_disconnect(
        self, client, userdata, disconnect_flags, reason_code, properties=None
    ) -> None:
        if self._stopping:
            return
        self._set_state_threadsafe(CLOUD_STATE_DISCONNECTED, keep_auth_failed=True)

    def _on_message(self, client, userdata, message) -> None:
        if message.topic != self._config.effective_aws_receive_topic:
            return
        self._hass.loop.call_soon_threadsafe(
            self._handle_downstream, bytes(message.payload)
        )

    # ------------------------------- State ------------------------------------

    def _set_state_threadsafe(self, state: str, keep_auth_failed: bool = False) -> None:
        self._hass.loop.call_soon_threadsafe(self._set_state, state, keep_auth_failed)

    def _set_state(self, state: str, keep_auth_failed: bool = False) -> None:
        if self._stopping and state != CLOUD_STATE_DISABLED:
            return
        if keep_auth_failed and self._state == CLOUD_STATE_AUTH_FAILED:
            return
        if state == self._state:
            return
        self._state = state
        level = (
            logging.INFO
            if state in (CLOUD_STATE_CONNECTED, CLOUD_STATE_CONNECTING, CLOUD_STATE_DISABLED)
            else logging.WARNING
        )
        _LOGGER.log(
            level, "Tibber Cloud forwarding for Pulse %s: %s", self._config.pulse_id, state
        )
        if self._on_state is not None:
            try:
                self._on_state(self._config.pulse_id, state)
            except Exception:  # noqa: BLE001 - a listener must not break forwarding
                _LOGGER.exception("Tibber Cloud state listener failed")


# ------------------------- shared by the config and repair flows -------------------------


async def async_test_connection(hass, config: CloudForwardingConfig) -> Optional[str]:
    """Test the connection off the event loop. Returns a flow error key, or None if it works."""
    try:
        await hass.async_add_executor_job(test_connection, config)
    except CloudAuthError:
        return "auth_failed"
    except (TimeoutError, ConnectionError):
        return "cannot_connect"
    except (ssl.SSLError, OSError, ValueError):
        # The credentials could not be loaded, e.g. the stored files are gone.
        return "certificate_invalid"
    return None


async def async_stage_and_test(
    hass,
    template: CloudForwardingConfig,
    certificate: str,
    private_key: str,
    ca_pem: Optional[str] = None,
) -> tuple[Optional[Path], Optional[str]]:
    """Validate new credentials, stage them and test them against the cloud.

    ``template`` supplies every other setting. Returns ``(stage, None)`` when the cloud
    accepts the credentials; the caller then commits the stage. Otherwise returns
    ``(None, error_key)`` and nothing is left behind.
    """
    directory = credentials_dir(hass.config.path, template.pulse_id)
    try:
        stage = await hass.async_add_executor_job(
            stage_credentials, directory, certificate, private_key, ca_pem
        )
    except CertificateError as err:
        return None, err.reason
    except OSError:
        return None, "storage_failed"

    ca_path = stage / CA_FILE
    config = dataclasses.replace(
        template,
        cert_path=stage / CERT_FILE,
        key_path=stage / KEY_FILE,
        ca_path=ca_path if ca_path.is_file() else None,
    )
    error = await async_test_connection(hass, config)
    if error is not None:
        await hass.async_add_executor_job(discard_staged_credentials, stage)
        return None, error
    return stage, None


def _read_text_if_exists(path: Path) -> Optional[str]:
    return path.read_text(encoding="ascii") if path.is_file() else None


async def async_replace_credentials(
    hass, hub, subentry, certificate: str, private_key: str
) -> Optional[str]:
    """Replace the certificate and key of an existing forwarding. Returns an error key or None.

    A custom CA is kept. The current credentials stay active unless the cloud accepts the
    new ones.
    """
    template = config_from_subentry(hass.config.path, subentry)
    directory = credentials_dir(hass.config.path, template.pulse_id)
    ca_pem = await hass.async_add_executor_job(_read_text_if_exists, directory / CA_FILE)

    # The cloud allows one connection per client ID, so a running forwarder steps aside.
    was_running = hub.has_forwarder(template.pulse_id)
    if was_running:
        await hub.async_remove_forwarder(template.pulse_id)

    stage, error = await async_stage_and_test(hass, template, certificate, private_key, ca_pem)
    if error is not None:
        if was_running:
            await hub.async_add_forwarder(template)
        return error

    await hass.async_add_executor_job(commit_staged_credentials, stage, directory)
    if hub.cloud_enabled(template.pulse_id):
        await hub.async_add_forwarder(template)
    await hub.async_check_certificates()
    return None
