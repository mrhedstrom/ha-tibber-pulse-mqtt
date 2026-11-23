"""Repairs issues for Tibber Cloud forwarding.

Issue IDs are stable (``<kind>_<pulse_id>``). The registry keeps an issue dismissed when
it is created again with the same ID, so an issue the user ignored does not come back
after a restart. Escalating a certificate warning uses a different ID, because that is
new information the user has not seen.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Deque, Dict, Optional

from homeassistant.helpers import issue_registry as ir

from .cloud_certs import read_certificate_info
from .const import (
    CLOUD_STATE_AUTH_FAILED,
    CLOUD_STATE_CONNECTED,
    CLOUD_STATE_DISCONNECTED,
    CLOUD_STATE_INVALID_CERTIFICATE,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

ISSUE_CERT_EXPIRING = "cloud_certificate_expiring"
ISSUE_CERT_EXPIRING_SOON = "cloud_certificate_expiring_soon"
ISSUE_CERT_EXPIRED = "cloud_certificate_expired"
ISSUE_CERT_INVALID = "cloud_certificate_invalid"
ISSUE_AUTH_REJECTED = "cloud_auth_rejected"
ISSUE_UNSTABLE = "cloud_connection_unstable"
ISSUE_PULSE_DISCOVERED = "cloud_pulse_discovered"

CERTIFICATE_ISSUES = (
    ISSUE_CERT_EXPIRING,
    ISSUE_CERT_EXPIRING_SOON,
    ISSUE_CERT_EXPIRED,
    ISSUE_CERT_INVALID,
)
# Issues that belong to an existing forwarding (as opposed to a discovered Pulse).
FORWARDING_ISSUES = CERTIFICATE_ISSUES + (ISSUE_AUTH_REJECTED, ISSUE_UNSTABLE)

EXPIRING_DAYS = 30
EXPIRING_SOON_DAYS = 7

# A connection that is lost this many times within the window almost certainly has a
# second client (an old bridge) using the same client ID.
UNSTABLE_DROPS = 5
UNSTABLE_WINDOW_SECONDS = 600


def issue_id(kind: str, pulse_id: str) -> str:
    return f"{kind}_{pulse_id}"


class CloudIssueReporter:
    """Turns state changes and certificate dates of one forwarding into Repairs issues."""

    def __init__(
        self,
        hass,
        entry_id: str,
        subentry_id: str,
        pulse_id: str,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._subentry_id = subentry_id
        self._pulse_id = pulse_id
        self._clock = clock
        self._now = now
        self._previous_state: Optional[str] = None
        self._drops: Deque[float] = deque()

    # ------------------------------- helpers ----------------------------------

    def _create(self, kind: str, severity, placeholders: Optional[Dict[str, str]] = None) -> None:
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            issue_id(kind, self._pulse_id),
            is_fixable=True,
            is_persistent=True,
            severity=severity,
            translation_key=kind,
            translation_placeholders={"device": self._pulse_id, **(placeholders or {})},
            data={
                "entry_id": self._entry_id,
                "subentry_id": self._subentry_id,
                "pulse_id": self._pulse_id,
            },
        )

    def _delete(self, *kinds: str) -> None:
        for kind in kinds:
            ir.async_delete_issue(self._hass, DOMAIN, issue_id(kind, self._pulse_id))

    # ----------------------------- connection state ---------------------------

    def on_state(self, state: str) -> None:
        """React to a forwarder state change (runs on the event loop)."""
        previous, self._previous_state = self._previous_state, state
        if state == CLOUD_STATE_AUTH_FAILED:
            self._create(ISSUE_AUTH_REJECTED, ir.IssueSeverity.ERROR)
        elif state == CLOUD_STATE_INVALID_CERTIFICATE:
            self._create(ISSUE_CERT_INVALID, ir.IssueSeverity.ERROR)
        elif state == CLOUD_STATE_CONNECTED:
            self._delete(ISSUE_AUTH_REJECTED, ISSUE_CERT_INVALID)

        if previous == CLOUD_STATE_CONNECTED and state == CLOUD_STATE_DISCONNECTED:
            self._record_drop()

    def _record_drop(self) -> None:
        now = self._clock()
        self._drops.append(now)
        while self._drops and now - self._drops[0] > UNSTABLE_WINDOW_SECONDS:
            self._drops.popleft()
        if len(self._drops) >= UNSTABLE_DROPS:
            self._create(ISSUE_UNSTABLE, ir.IssueSeverity.WARNING)

    def clear_unstable(self) -> None:
        """Forget earlier drops, e.g. after the user removed the old bridge."""
        self._drops.clear()
        self._delete(ISSUE_UNSTABLE)

    # ------------------------------ certificate -------------------------------

    async def async_check_certificate(self, certificate_path: Path) -> None:
        """Report a stored certificate that is unreadable, expired or about to expire."""
        try:
            info = await self._hass.async_add_executor_job(read_certificate_info, certificate_path)
        except (OSError, ValueError):
            self._create(ISSUE_CERT_INVALID, ir.IssueSeverity.ERROR)
            return
        self.apply_expiry(info.not_after)

    def apply_expiry(self, not_after: datetime) -> None:
        remaining = not_after - self._now()
        days = max(remaining.days, 0)
        placeholders = {"days": str(days), "date": not_after.date().isoformat()}

        if remaining.total_seconds() <= 0:
            current, severity = ISSUE_CERT_EXPIRED, ir.IssueSeverity.ERROR
        elif remaining.days <= EXPIRING_SOON_DAYS:
            current, severity = ISSUE_CERT_EXPIRING_SOON, ir.IssueSeverity.ERROR
        elif remaining.days <= EXPIRING_DAYS:
            current, severity = ISSUE_CERT_EXPIRING, ir.IssueSeverity.WARNING
        else:
            self._delete(*CERTIFICATE_ISSUES)
            return

        self._delete(*(kind for kind in CERTIFICATE_ISSUES if kind != current))
        self._create(current, severity, placeholders)


# ------------------------------ discovered Pulse --------------------------------


def async_report_pulse_discovered(hass, entry_id: str, pulse_id: str, topic: str) -> None:
    """Offer forwarding for a Pulse that has none. An ignored notice stays ignored."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(ISSUE_PULSE_DISCOVERED, pulse_id),
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_PULSE_DISCOVERED,
        translation_placeholders={"device": pulse_id},
        data={"entry_id": entry_id, "pulse_id": pulse_id, "topic": topic},
    )


def async_clear_pulse_discovered(hass, pulse_id: str) -> None:
    ir.async_delete_issue(hass, DOMAIN, issue_id(ISSUE_PULSE_DISCOVERED, pulse_id))


def async_remove_stale_issues(hass, entry) -> None:
    """Delete forwarding issues whose subentry no longer exists."""
    registry = ir.async_get(hass)
    for (domain, current_id), issue in list(registry.issues.items()):
        if domain != DOMAIN or not any(current_id.startswith(f"{kind}_") for kind in FORWARDING_ISSUES):
            continue
        data = issue.data or {}
        if data.get("entry_id") == entry.entry_id and data.get("subentry_id") not in entry.subentries:
            ir.async_delete_issue(hass, DOMAIN, current_id)
