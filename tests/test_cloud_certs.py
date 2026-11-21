"""Tests for validation and storage of the Tibber Cloud credentials."""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization

from custom_components.tibber_pulse_mqtt.cloud_certs import (
    CA_FILE,
    CERT_FILE,
    KEY_FILE,
    ORPHAN_MIN_AGE_SECONDS,
    CertificateError,
    commit_staged_credentials,
    credentials_dir,
    credentials_root,
    discard_staged_credentials,
    read_certificate_info,
    remove_orphaned_credentials,
    stage_credentials,
    validate_credentials,
)
from tests.cloud_helpers import _rsa_key, make_pems


def reason(exc_info) -> str:
    return exc_info.value.reason


# ------------------------------- validation -----------------------------------


@pytest.mark.parametrize("key_format", ["pkcs1", "pkcs8"])
def test_valid_credentials_are_accepted_in_both_key_formats(key_format):
    certificate, key = make_pems(key_format=key_format)
    info = validate_credentials(certificate, key)
    remaining = info.not_after - datetime.now(timezone.utc)
    assert timedelta(days=29) < remaining <= timedelta(days=30)


def test_the_certificate_can_be_used_as_ca():
    certificate, key = make_pems()
    validate_credentials(certificate, key, ca_pem=certificate)


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"not_before_days": -10, "not_after_days": -1}, "certificate_expired"),
        ({"not_before_days": 1, "not_after_days": 30}, "certificate_not_yet_valid"),
        ({"key_index": 0, "cert_key_index": 1}, "key_mismatch"),
    ],
    ids=["expired", "not-yet-valid", "mismatch"],
)
def test_unusable_credentials_report_why(kwargs, expected):
    certificate, key = make_pems(**kwargs)
    with pytest.raises(CertificateError) as exc_info:
        validate_credentials(certificate, key)
    assert reason(exc_info) == expected


@pytest.mark.parametrize("garbage", ["", "not a certificate", "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----", "åäö"])
def test_garbage_certificate_is_invalid(garbage):
    _, key = make_pems()
    with pytest.raises(CertificateError) as exc_info:
        validate_credentials(garbage, key)
    assert reason(exc_info) == "certificate_invalid"


@pytest.mark.parametrize("garbage", ["", "not a key", "åäö"])
def test_garbage_key_is_invalid(garbage):
    certificate, _ = make_pems()
    with pytest.raises(CertificateError) as exc_info:
        validate_credentials(certificate, garbage)
    assert reason(exc_info) == "key_invalid"


def test_password_protected_key_is_invalid():
    certificate, _ = make_pems()
    encrypted = _rsa_key(0).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"secret"),
    ).decode("ascii")
    with pytest.raises(CertificateError) as exc_info:
        validate_credentials(certificate, encrypted)
    assert reason(exc_info) == "key_invalid"


def test_swapping_certificate_and_key_is_detected():
    certificate, key = make_pems()
    with pytest.raises(CertificateError) as exc_info:
        validate_credentials(key, certificate)
    assert reason(exc_info) == "certificate_invalid"


def test_invalid_ca_is_rejected():
    certificate, key = make_pems()
    with pytest.raises(CertificateError) as exc_info:
        validate_credentials(certificate, key, ca_pem="nope")
    assert reason(exc_info) == "ca_invalid"


# ------------------------------- paths ----------------------------------------


def test_credentials_live_under_storage(tmp_path):
    def path(*parts):
        return str(tmp_path.joinpath(*parts))

    assert credentials_root(path) == tmp_path / ".storage" / "tibber_pulse_mqtt" / "certs"
    assert credentials_dir(path, "aabbccddeeff") == credentials_root(path) / "aabbccddeeff"


@pytest.mark.parametrize("pulse_id", ["", "..", "../x", "a/b", "a\\b", "a b", "a.b"])
def test_pulse_ids_cannot_escape_the_credentials_directory(tmp_path, pulse_id):
    with pytest.raises(ValueError):
        credentials_dir(lambda *parts: str(tmp_path.joinpath(*parts)), pulse_id)


# -------------------------- staging and committing ----------------------------


def test_staged_credentials_are_normalized_and_do_not_touch_active_files(tmp_path):
    directory = tmp_path / "pulse"
    certificate, key = make_pems()
    stage = stage_credentials(directory, certificate.replace("\n", "\r\n"), "  " + key + "\n\n")
    assert stage.parent == directory
    assert (stage / CERT_FILE).read_text(encoding="ascii") == certificate
    assert (stage / KEY_FILE).read_text(encoding="ascii") == key
    assert not (stage / CA_FILE).exists()
    assert not (directory / CERT_FILE).exists()


def test_invalid_credentials_leave_nothing_behind(tmp_path):
    directory = tmp_path / "pulse"
    with pytest.raises(CertificateError):
        stage_credentials(directory, "garbage", "garbage")
    assert not directory.exists()


def test_commit_replaces_active_files_and_removes_the_staging_directory(tmp_path):
    directory = tmp_path / "pulse"
    first_cert, first_key = make_pems()
    commit_staged_credentials(stage_credentials(directory, first_cert, first_key, first_cert), directory)
    assert (directory / CA_FILE).read_text(encoding="ascii") == first_cert

    second_cert, second_key = make_pems(key_index=1)
    stage = stage_credentials(directory, second_cert, second_key)
    commit_staged_credentials(stage, directory)

    assert (directory / CERT_FILE).read_text(encoding="ascii") == second_cert
    assert (directory / KEY_FILE).read_text(encoding="ascii") == second_key
    assert not (directory / CA_FILE).exists(), "no CA staged: the bundled CA is used again"
    assert not stage.exists()
    assert sorted(p.name for p in directory.iterdir()) == [CERT_FILE, KEY_FILE]


def test_discarding_a_stage_keeps_active_credentials(tmp_path):
    directory = tmp_path / "pulse"
    certificate, key = make_pems()
    commit_staged_credentials(stage_credentials(directory, certificate, key), directory)

    stage = stage_credentials(directory, *make_pems(key_index=1))
    discard_staged_credentials(stage)
    assert not stage.exists()
    assert (directory / CERT_FILE).read_text(encoding="ascii") == certificate


def test_discarding_the_only_stage_removes_the_empty_pulse_directory(tmp_path):
    directory = tmp_path / "pulse"
    stage = stage_credentials(directory, *make_pems())
    discard_staged_credentials(stage)
    assert not directory.exists()


def test_certificate_expiry_can_be_read_from_the_stored_file(tmp_path):
    directory = tmp_path / "pulse"
    certificate, key = make_pems(not_after_days=10)
    commit_staged_credentials(stage_credentials(directory, certificate, key), directory)
    remaining = read_certificate_info(directory / CERT_FILE).not_after - datetime.now(timezone.utc)
    assert timedelta(days=9) < remaining <= timedelta(days=10)


# ------------------------------ orphan cleanup --------------------------------


def _make_pulse_dir(root, name, age_seconds):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / CERT_FILE).write_text("x")
    old = time.time() - age_seconds
    os.utime(directory, (old, old))
    return directory


def test_orphaned_credentials_are_removed_but_kept_and_recent_ones_stay(tmp_path):
    root = tmp_path / "certs"
    old = ORPHAN_MIN_AGE_SECONDS + 60
    kept = _make_pulse_dir(root, "kept", old)
    orphan = _make_pulse_dir(root, "orphan", old)
    recent = _make_pulse_dir(root, "recent", 5)

    remove_orphaned_credentials(root, {"kept"})

    assert kept.exists()
    assert not orphan.exists()
    assert recent.exists(), "a directory being committed right now must not be cleaned up"


def test_cleanup_without_a_credentials_directory_is_a_no_op(tmp_path):
    remove_orphaned_credentials(tmp_path / "missing", set())
