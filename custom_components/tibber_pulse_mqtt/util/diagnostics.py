from __future__ import annotations

import base64
import logging
from typing import Dict, Any, Optional


class DiagnosticsRegistry:
    """Per-device counters and diagnostics for OK/MISS, with optional payload logging."""

    def __init__(self, logger: logging.Logger, cfg: Dict[str, Any], debug: bool):
        self._logger = logger
        self._cfg = cfg or {}
        self._debug = debug
        self._counters: Dict[str, Dict[str, int]] = {}

    def bump(
        self,
        dev_id: str,
        ok: bool,
        *,
        topic: Optional[str] = None,
        payload: Optional[bytes] = None,
        offset: Optional[int] = None,
        had_blob: Optional[bool] = None,
        zerr: Optional[str] = None,
    ) -> None:
        c = self._counters.setdefault(dev_id, {"total": 0, "ok": 0, "err": 0, "consec_err": 0})
        c["total"] += 1

        if ok:
            c["ok"] += 1
            c["consec_err"] = 0
            if self._debug:
                self._logger.debug(
                    "OBIS OK dev=%s total=%d ok=%d err=%d",
                    dev_id, c["total"], c["ok"], c["err"]
                )
            return

        # MISS
        c["err"] += 1
        c["consec_err"] += 1

        plen = len(payload) if isinstance(payload, (bytes, bytearray)) else "-"
        head = payload[:16].hex() if isinstance(payload, (bytes, bytearray)) else "-"

        if self._debug:
            self._logger.debug(
                "OBIS MISS dev=%s consec_err=%d total_err=%d total=%d len=%s head=%s topic=%s offset=%s had_blob=%s zerr=%s",
                dev_id, c["consec_err"], c["err"], c["total"],
                plen, head, topic, offset, had_blob, (zerr or "-")
            )
            self.maybe_log_payload_as_base64(payload)

        if c["consec_err"] in (5, 20, 50, 100):
            self._logger.warning("OBIS consecutive decode errors for %s: %s", dev_id, c)

    def maybe_log_payload_as_base64(self, payload: Optional[bytes]) -> None:
        if self._cfg.get("log_missed_base64", False) and isinstance(payload, (bytes, bytearray)):
            try:
                self._logger.debug("Base64 payload:\n%s", base64.b64encode(payload).decode())
            except Exception:
                pass

    @property
    def counters(self) -> Dict[str, Dict[str, int]]:
        return self._counters