from __future__ import annotations

import os
import re
import shutil
import ssl
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as paho

from .const import DOMAIN

DEFAULT_AWS_CLIENT_ID_PREFIX = "tibber-pulse-"
CERTIFICATE_TIMEOUT = 10

_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def validate_device_id(device_id: str) -> str:
    """Validate a Pulse ID before using it in paths, topics, or client IDs."""
    normalized = device_id.strip()
    if not normalized or not _DEVICE_ID_RE.fullmatch(normalized):
        raise ValueError("Invalid Pulse device ID")
    return normalized


def receive_topic_for_publish(publish_topic: str) -> str:
    """Replace only the final publish topic segment with receive."""
    validate_mqtt_topic(publish_topic)
    if not publish_topic.endswith("/publish"):
        raise ValueError("Publish topic must end with /publish")
    return f"{publish_topic[:-len('/publish')]}/receive"


def validate_mqtt_topic(topic: str) -> str:
    """Validate an exact MQTT topic without wildcards or null bytes."""
    if (
        not topic
        or topic.startswith("/")
        or topic.endswith("/")
        or "//" in topic
        or "\x00" in topic
        or "+" in topic
        or "#" in topic
    ):
        raise ValueError("MQTT topic must be a non-empty exact topic")
    return topic


def client_id_for_device(device_id: str) -> str:
    """Return the documented Tibber Pulse AWS client ID."""
    return f"{DEFAULT_AWS_CLIENT_ID_PREFIX}{validate_device_id(device_id)}"


def parse_aws_endpoint(endpoint: str) -> tuple[str, int]:
    """Parse an AWS MQTT endpoint in host:port form."""
    host, separator, port_text = endpoint.strip().rpartition(":")
    if not separator or not host or not port_text:
        raise ValueError("AWS endpoint must use host:port format")
    try:
        port = int(port_text)
    except ValueError as err:
        raise ValueError("AWS endpoint port must be an integer") from err
    if not 1 <= port <= 65535:
        raise ValueError("AWS endpoint port must be between 1 and 65535")
    if "://" in host or "/" in host:
        raise ValueError("AWS endpoint must not include a scheme or path")
    if any(char.isspace() for char in host):
        raise ValueError("AWS endpoint host must not contain whitespace")
    return host, port


def certificate_directory(hass, device_id: str) -> Path:
    """Return the persistent per-device certificate directory."""
    return Path(hass.config.path(DOMAIN, "certs", validate_device_id(device_id)))


def validate_certificate_material(
    certificate_pem: str,
    private_key_pem: str,
    ca_pem: str | None = None,
) -> None:
    """Validate PEM structure, validity dates, CA material, and certificate/key match."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    try:
        certificate = x509.load_pem_x509_certificate(certificate_pem.encode())
        private_key = serialization.load_pem_private_key(
            private_key_pem.encode(), password=None
        )
    except (ValueError, TypeError) as err:
        raise ValueError("Certificate or private key is not valid PEM") from err

    now = datetime.now(timezone.utc)
    not_before = getattr(certificate, "not_valid_before_utc", None)
    not_after = getattr(certificate, "not_valid_after_utc", None)
    if not_before is None:
        not_before = certificate.not_valid_before.replace(tzinfo=timezone.utc)
    if not_after is None:
        not_after = certificate.not_valid_after.replace(tzinfo=timezone.utc)
    if now < not_before:
        raise ValueError("Client certificate is not yet valid")
    if now > not_after:
        raise ValueError("Client certificate has expired")

    cert_public_key = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    key_public_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if cert_public_key != key_public_key:
        raise ValueError("Client certificate does not match the private key")

    if ca_pem:
        try:
            certs = x509.load_pem_x509_certificates(ca_pem.encode())
        except AttributeError:
            certs = _load_pem_certificates_compat(ca_pem, x509)
        except ValueError as err:
            raise ValueError("CA certificate is not valid PEM") from err
        if not certs:
            raise ValueError("CA certificate is not valid PEM")


def validate_certificate_files(
    certificate_path: str | Path,
    private_key_path: str | Path,
    ca_path: str | Path | None = None,
) -> None:
    """Validate persisted certificate files before starting the native client."""
    certificate = Path(certificate_path).read_text(encoding="ascii")
    private_key = Path(private_key_path).read_text(encoding="ascii")
    ca = Path(ca_path).read_text(encoding="ascii") if ca_path and Path(ca_path).is_file() else None
    validate_certificate_material(certificate, private_key, ca)


def _load_pem_certificates_compat(ca_pem: str, x509) -> list:
    """Load a PEM bundle when using a cryptography version without bundle parsing."""
    blocks = re.findall(
        r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
        ca_pem,
        re.DOTALL,
    )
    if not blocks:
        raise ValueError("CA certificate is not valid PEM")
    try:
        return [x509.load_pem_x509_certificate(block.encode()) for block in blocks]
    except ValueError as err:
        raise ValueError("CA certificate is not valid PEM") from err


def stage_certificate_material(
    certificate_dir: str | Path,
    certificate_pem: str,
    private_key_pem: str,
    ca_pem: str | None = None,
) -> Path:
    """Validate and stage PEM files without replacing the active credentials."""
    validate_certificate_material(certificate_pem, private_key_pem, ca_pem)
    root = Path(certificate_dir)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    stage_dir = Path(tempfile.mkdtemp(prefix=".staged-", dir=root))
    files = {
        "cert.pem": certificate_pem,
        "key.pem": private_key_pem,
    }
    if ca_pem:
        files["ca.pem"] = ca_pem
    try:
        for filename, contents in files.items():
            path = stage_dir / filename
            path.write_text(contents, encoding="ascii")
            try:
                path.chmod(0o600)
            except OSError:
                pass
    except OSError:
        shutil.rmtree(stage_dir, ignore_errors=True)
        raise
    return stage_dir


def commit_staged_certificate_material(
    stage_dir: str | Path,
    certificate_dir: str | Path,
) -> None:
    """Atomically replace active certificate files with previously tested files."""
    stage = Path(stage_dir)
    target = Path(certificate_dir)
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    for filename in ("cert.pem", "key.pem", "ca.pem"):
        staged = stage / filename
        destination = target / filename
        if staged.exists():
            os.replace(staged, destination)
        elif filename == "ca.pem":
            destination.unlink(missing_ok=True)
    shutil.rmtree(stage, ignore_errors=True)


def discard_staged_certificate_material(stage_dir: str | Path) -> None:
    """Remove a failed or abandoned temporary certificate staging directory."""
    shutil.rmtree(stage_dir, ignore_errors=True)


def remove_certificate_material(certificate_dir: str | Path) -> None:
    """Remove only integration-owned certificate files and empty staging folders."""
    root = Path(certificate_dir)
    for filename in ("cert.pem", "key.pem", "ca.pem"):
        (root / filename).unlink(missing_ok=True)
    if root.exists():
        for staged in root.glob(".staged-*"):
            shutil.rmtree(staged, ignore_errors=True)
        try:
            root.rmdir()
        except OSError:
            pass


async def async_remove_certificate_files(hass, device_id: str) -> None:
    """Remove the device's certificate files when its AWS entry is removed."""
    directory = certificate_directory(hass, device_id)
    await hass.async_add_executor_job(remove_certificate_material, directory)


def test_aws_connection(
    endpoint: str,
    device_id: str,
    certificate_path: str | Path,
    private_key_path: str | Path,
    ca_path: str | Path | None = None,
    timeout: int = CERTIFICATE_TIMEOUT,
) -> None:
    """Perform a bounded TLS-authenticated MQTT connection test."""
    host, port = parse_aws_endpoint(endpoint)
    connected = threading.Event()
    outcome: dict[str, object] = {}
    client = paho.Client(client_id=client_id_for_device(device_id))
    context = ssl.create_default_context(cafile=str(ca_path) if ca_path else None)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(certificate_path), str(private_key_path))
    client.tls_set_context(context)

    def _on_connect(_client, _userdata, _flags, reason_code, *_args):
        outcome["reason_code"] = reason_code
        connected.set()

    client.on_connect = _on_connect
    try:
        client.connect(host, port, keepalive=30)
        client.loop_start()
        if not connected.wait(timeout):
            raise TimeoutError("AWS MQTT connection test timed out")
        reason_code = outcome.get("reason_code")
        if int(reason_code) != 0:
            raise ConnectionError(f"AWS MQTT connection rejected ({reason_code})")
    finally:
        try:
            client.disconnect()
        finally:
            client.loop_stop()
