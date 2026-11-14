from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from custom_components.tibber_pulse_mqtt.aws import (
    client_id_for_device,
    commit_staged_certificate_material,
    parse_aws_endpoint,
    receive_topic_for_publish,
    remove_certificate_material,
    stage_certificate_material,
    validate_certificate_material,
)
from custom_components.tibber_pulse_mqtt.const import DEFAULT_AWS_ENDPOINT
from custom_components.tibber_pulse_mqtt.config_flow import TibberLocalConfigFlow
from custom_components.tibber_pulse_mqtt.const import CONF_DEVICE_ID, CONF_PUBLISH_TOPIC


def _certificate_pair(*, expired: bool = False) -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "test-pulse")]
    )
    now = datetime.now(timezone.utc)
    not_before = now - timedelta(days=3)
    not_after = now - timedelta(days=1) if expired else now + timedelta(days=30)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    return cert_pem, key_pem


def test_aws_defaults_and_device_identity():
    assert parse_aws_endpoint(DEFAULT_AWS_ENDPOINT) == (
        "a1zhmn1192zl1a.iot.eu-west-1.amazonaws.com",
        8883,
    )
    assert client_id_for_device("ABC123") == "tibber-pulse-ABC123"


def test_receive_topic_replaces_only_the_terminal_publish_segment():
    assert receive_topic_for_publish("tibber-pulse-ABC123/publish") == (
        "tibber-pulse-ABC123/receive"
    )
    assert receive_topic_for_publish("prefix/publish/device/publish") == (
        "prefix/publish/device/receive"
    )


def test_receive_topic_rejects_non_publish_suffix():
    with pytest.raises(ValueError):
        receive_topic_for_publish("tibber-pulse-ABC123/data")


@pytest.mark.parametrize("topic", ["", "/publish", "pulse//publish", "pulse/publish/", "pulse/#/publish"])
def test_receive_topic_rejects_malformed_or_non_exact_topics(topic):
    with pytest.raises(ValueError):
        receive_topic_for_publish(topic)


@pytest.mark.parametrize("device_id", ["", "../ABC", "ABC/DEF", "x+#"])
def test_client_id_rejects_unsafe_device_id(device_id):
    with pytest.raises(ValueError):
        client_id_for_device(device_id)


def test_endpoint_rejects_invalid_values():
    for endpoint in (
        "",
        "mqtts://host:8883",
        "host:0",
        "host:65536",
        "host:abc",
        "host name:8883",
    ):
        with pytest.raises(ValueError):
            parse_aws_endpoint(endpoint)


def test_integration_discovery_creates_device_scoped_forwarding_entry(monkeypatch, tmp_path):
    flow = TibberLocalConfigFlow()
    flow.context = {
        CONF_DEVICE_ID: "ABC123",
        CONF_PUBLISH_TOPIC: "site/tibber-pulse-ABC123/publish",
    }
    flow.hass = type(
        "Hass",
        (),
        {"config": type("Config", (), {"path": lambda self, *parts: str(tmp_path.joinpath(*parts))})()},
    )()
    async def _set_unique_id(_unique_id):
        return None

    flow.async_set_unique_id = _set_unique_id
    flow._abort_if_unique_id_configured = lambda: None
    flow.async_create_entry = lambda **kwargs: kwargs

    result = asyncio.run(flow.async_step_integration_discovery())

    assert result["title"] == "Tibber Pulse ABC123 AWS forwarding"
    assert result["data"][CONF_PUBLISH_TOPIC] == "site/tibber-pulse-ABC123/publish"
    assert result["data"]["receive_topic"] == "site/tibber-pulse-ABC123/receive"
    assert result["data"]["device_id"] == "ABC123"


def test_certificate_validation_accepts_matching_unexpired_key():
    cert, key = _certificate_pair()
    validate_certificate_material(cert, key)


def test_certificate_validation_rejects_mismatched_key():
    cert, _key = _certificate_pair()
    _other_cert, other_key = _certificate_pair()
    with pytest.raises(ValueError, match="does not match"):
        validate_certificate_material(cert, other_key)


def test_certificate_validation_rejects_expired_certificate():
    cert, key = _certificate_pair(expired=True)
    with pytest.raises(ValueError, match="expired"):
        validate_certificate_material(cert, key)


def test_staged_certificate_commit_and_cleanup_preserve_unowned_files(tmp_path: Path):
    cert, key = _certificate_pair()
    cert_dir = tmp_path / "certs" / "ABC123"
    staged = stage_certificate_material(cert_dir, cert, key)

    commit_staged_certificate_material(staged, cert_dir)

    assert (cert_dir / "cert.pem").read_text() == cert
    assert (cert_dir / "key.pem").read_text() == key
    (cert_dir / "user-data.txt").write_text("preserve")
    remove_certificate_material(cert_dir)
    assert not (cert_dir / "cert.pem").exists()
    assert not (cert_dir / "key.pem").exists()
    assert (cert_dir / "user-data.txt").read_text() == "preserve"
