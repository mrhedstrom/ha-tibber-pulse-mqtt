"""Shared test helpers for the Tibber Cloud forwarding tests."""
from __future__ import annotations

import asyncio
import functools
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

import paho.mqtt.client as paho
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

# Fake IDs and topics; never use real device identifiers in tests.
THING = "0123456789abcdef0123456789abcdef"
TOPIC = f"tibber-pulse-{THING}/publish"
PULSE_ID = "aabbccddeeff"


def run(coro):
    return asyncio.run(coro)


async def settle() -> None:
    """Let call_soon_threadsafe callbacks and freshly created tasks run."""
    for _ in range(3):
        await asyncio.sleep(0)


class FakeHass:
    """Just enough of HomeAssistant for the forwarder and the subentry flow."""

    def __init__(self, config_dir: Optional[Path] = None) -> None:
        self.config = SimpleNamespace(path=lambda *parts: str(Path(config_dir or ".").joinpath(*parts)))
        self.data: dict = {}
        self.config_entries = SimpleNamespace()

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
    # Event delivered from loop_start(), used by connection tests:
    # "connected", "refused:<code>", "fail" or None (nothing happens).
    auto_event: Optional[str] = None

    def __init__(self, callback_api_version=None, client_id="", clean_session=None,
                 protocol=None, **kwargs):
        self.callback_api_version = callback_api_version
        self.client_id = client_id
        self.clean_session = clean_session
        self.protocol = protocol
        self.kwargs = kwargs
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
        event = FakeClient.auto_event
        if event == "connected":
            self.simulate_connected()
        elif event and event.startswith("refused:"):
            self.simulate_refused(int(event.split(":", 1)[1]))
        elif event == "fail":
            self.simulate_connect_failed()

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


@functools.lru_cache(maxsize=None)
def _rsa_key(index: int):
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_pems(
    *,
    key_index: int = 0,
    cert_key_index: Optional[int] = None,
    not_before_days: int = -1,
    not_after_days: int = 365,
    key_format: str = "pkcs1",
    ca: bool = True,
) -> tuple[str, str]:
    """Return (certificate PEM, private key PEM) of a self-signed test certificate.

    ``cert_key_index`` signs the certificate with a different key than the one returned
    (a mismatching pair). ``key_format`` is "pkcs1" (what the Pulse shows) or "pkcs8".
    """
    cert_key = _rsa_key(key_index if cert_key_index is None else cert_key_index)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-pulse")])
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(cert_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + timedelta(days=not_before_days))
        .not_valid_after(now + timedelta(days=not_after_days))
    )
    if ca:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
    certificate = builder.sign(cert_key, hashes.SHA256())
    private_format = (
        serialization.PrivateFormat.TraditionalOpenSSL
        if key_format == "pkcs1"
        else serialization.PrivateFormat.PKCS8
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        _rsa_key(key_index)
        .private_bytes(serialization.Encoding.PEM, private_format, serialization.NoEncryption())
        .decode("ascii"),
    )


def write_credentials(directory: Path, **kwargs) -> tuple[Path, Path]:
    """Write cert.pem and key.pem to ``directory``; returns their paths."""
    certificate, private_key = make_pems(**kwargs)
    directory.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_text(certificate, encoding="ascii")
    key_path.write_text(private_key, encoding="ascii")
    return cert_path, key_path


class FakeStore:
    """In-memory stand-in for homeassistant.helpers.storage.Store."""

    data: dict = {}

    def __init__(self, hass, version, key):
        self.key = key

    async def async_load(self):
        return FakeStore.data.get(self.key)

    async def async_save(self, data):
        FakeStore.data[self.key] = data

    async def async_remove(self):
        FakeStore.data.pop(self.key, None)


class FakeIssues:
    """Stand-in for homeassistant.helpers.issue_registry that records issues."""

    IssueSeverity = SimpleNamespace(WARNING="warning", ERROR="error", CRITICAL="critical")

    def __init__(self):
        self.issues: dict[tuple[str, str], dict] = {}

    def async_create_issue(self, hass, domain, issue_id, **kwargs):
        self.issues[(domain, issue_id)] = kwargs

    def async_delete_issue(self, hass, domain, issue_id):
        self.issues.pop((domain, issue_id), None)

    def async_get(self, hass):
        def async_get_issue(domain, issue_id):
            kwargs = self.issues.get((domain, issue_id))
            if kwargs is None:
                return None
            return SimpleNamespace(
                data=kwargs.get("data"),
                translation_placeholders=kwargs.get("translation_placeholders"),
            )

        return SimpleNamespace(
            issues={key: SimpleNamespace(data=value.get("data")) for key, value in self.issues.items()},
            async_get_issue=async_get_issue,
        )

    @property
    def ids(self) -> list[str]:
        return sorted(issue_id for _domain, issue_id in self.issues)
