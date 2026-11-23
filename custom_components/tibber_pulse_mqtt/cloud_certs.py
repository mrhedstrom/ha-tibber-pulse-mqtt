"""Validation and storage of the per-Pulse Tibber Cloud credentials.

The credentials (client certificate, private key and an optional CA) are stored as
files below ``<config>/.storage/tibber_pulse_mqtt/certs/<pulse_id>/`` and never inside
the config entry. ``.storage`` survives HACS updates of the integration and is part of
Home Assistant backups.

New credentials are validated and written to a staging directory first, tested against
the cloud, and only then moved over the active files, so a failed attempt can never
replace working credentials.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CERT_FILE = "cert.pem"
KEY_FILE = "key.pem"
CA_FILE = "ca.pem"
_STAGING_PREFIX = ".staged-"
_PULSE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Orphaned directories younger than this are left alone so a flow that is just
# committing credentials is never raced by a reload cleaning up.
ORPHAN_MIN_AGE_SECONDS = 600


class CertificateError(ValueError):
    """Credentials are unusable. ``reason`` is a translation key of the config flow."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class CertificateInfo:
    """What the rest of the integration needs to know about a client certificate."""

    not_after: datetime


def credentials_root(config_dir_path) -> Path:
    """Return the directory holding all credentials; ``config_dir_path`` is hass.config.path."""
    return Path(config_dir_path(".storage", DOMAIN, "certs"))


def credentials_dir(config_dir_path, pulse_id: str) -> Path:
    """Return the credentials directory of one Pulse."""
    if not pulse_id or not _PULSE_ID_RE.fullmatch(pulse_id):
        raise ValueError(f"Invalid Pulse ID: {pulse_id!r}")
    return credentials_root(config_dir_path) / pulse_id


def _normalize_pem(pem: str) -> str:
    """Normalize line endings of pasted PEM text and end it with a newline."""
    return pem.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"


def validate_credentials(
    certificate_pem: str,
    private_key_pem: str,
    ca_pem: Optional[str] = None,
) -> CertificateInfo:
    """Check that the PEM texts are usable together. Raises CertificateError."""
    try:
        certificate = x509.load_pem_x509_certificate(certificate_pem.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as err:
        raise CertificateError("certificate_invalid") from err

    try:
        private_key = serialization.load_pem_private_key(
            private_key_pem.encode("ascii"), password=None
        )
    except (ValueError, TypeError, UnicodeEncodeError) as err:
        # TypeError: the key is password protected, which the Pulse never produces.
        raise CertificateError("key_invalid") from err

    now = datetime.now(timezone.utc)
    if now < certificate.not_valid_before_utc:
        raise CertificateError("certificate_not_yet_valid")
    if now > certificate.not_valid_after_utc:
        raise CertificateError("certificate_expired")

    def _public_der(key) -> bytes:
        return key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    if _public_der(certificate.public_key()) != _public_der(private_key.public_key()):
        raise CertificateError("key_mismatch")

    if ca_pem:
        try:
            if not x509.load_pem_x509_certificates(ca_pem.encode("ascii")):
                raise CertificateError("ca_invalid")
        except (ValueError, UnicodeEncodeError) as err:
            raise CertificateError("ca_invalid") from err

    return CertificateInfo(not_after=certificate.not_valid_after_utc)


def read_certificate_info(certificate_path: Path) -> CertificateInfo:
    """Read the expiry of a stored client certificate. Blocking."""
    certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
    return CertificateInfo(not_after=certificate.not_valid_after_utc)


def stage_credentials(
    directory: Path,
    certificate_pem: str,
    private_key_pem: str,
    ca_pem: Optional[str] = None,
) -> Path:
    """Validate credentials and write them to a new staging directory. Blocking.

    Returns the staging directory. Raises CertificateError and leaves nothing behind
    when the credentials are unusable.
    """
    certificate_pem = _normalize_pem(certificate_pem)
    private_key_pem = _normalize_pem(private_key_pem)
    ca_pem = _normalize_pem(ca_pem) if ca_pem and ca_pem.strip() else None
    validate_credentials(certificate_pem, private_key_pem, ca_pem)

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=directory))
    try:
        files = {CERT_FILE: certificate_pem, KEY_FILE: private_key_pem}
        if ca_pem:
            files[CA_FILE] = ca_pem
        for name, content in files.items():
            path = stage / name
            path.write_text(content, encoding="ascii")
            try:
                path.chmod(0o600)  # no-op on Windows
            except OSError:
                pass
    except OSError:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return stage


def commit_staged_credentials(stage: Path, directory: Path) -> None:
    """Move staged credentials over the active ones. Blocking.

    A staged set without a CA removes an existing custom CA, so the bundled one
    is used again.
    """
    for name in (CERT_FILE, KEY_FILE, CA_FILE):
        staged = stage / name
        if staged.exists():
            os.replace(staged, directory / name)
        elif name == CA_FILE:
            (directory / CA_FILE).unlink(missing_ok=True)
    shutil.rmtree(stage, ignore_errors=True)


def discard_staged_credentials(stage: Path) -> None:
    """Remove a staging directory and the Pulse directory if nothing else is in it."""
    shutil.rmtree(stage, ignore_errors=True)
    try:
        stage.parent.rmdir()  # only succeeds when there are no active credentials
    except OSError:
        pass


def remove_orphaned_credentials(root: Path, keep_pulse_ids: Iterable[str]) -> None:
    """Delete credentials of Pulse devices that no longer have forwarding. Blocking."""
    if not root.is_dir():
        return
    keep = set(keep_pulse_ids)
    now = time.time()
    for directory in root.iterdir():
        if not directory.is_dir() or directory.name in keep:
            continue
        try:
            if now - directory.stat().st_mtime < ORPHAN_MIN_AGE_SECONDS:
                continue
            shutil.rmtree(directory)
            _LOGGER.info("Removed credentials of Pulse %s, its forwarding was removed", directory.name)
        except OSError:
            _LOGGER.warning("Could not remove stored credentials of Pulse %s", directory.name)
