from __future__ import annotations

import json
import zlib
import logging
import base64
import asyncio
from typing import Dict, Any, Literal, Tuple, Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN, CONF_LOG_MISSED_BASE64, CONF_LOG_OBIS

try:
    from .protobuf import pulse_pb2
except Exception:
    pulse_pb2 = None

from .parsers.pulse_envelope import pick_best_candidate_from_blob
from .parsers.obis_text import parse_obis_text

_LOGGER = logging.getLogger(__name__)


class TibberDispatcher:

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        self.cfg = {**entry.data, **entry.options}
        self.debug = _LOGGER.isEnabledFor(logging.DEBUG)

        # Device and streaming state
        self._devices: Dict[str, Dict[str, Any]] = {}
        self._pending: list[tuple[str, Dict[str, Any], Dict[str, Any] | None]] = []
        self._topic_to_pulse: Dict[str, str] = {}
        self._pending_by_topic: Dict[str, list] = {}
        self._meter_id: Dict[str, str] = {}

        # Per-device stream state
        # st = {"off": int|None, "z": zlib.decompressobj|None, "buf": bytearray(),
        #       "blob": bytes|None, "dev_id": str,
        #       "cont_fail": int, "force_mode": bool, "force_no_deflate": int,
        #       "just_primed": bool, "skip_bump": bool}
        self._streams: dict[str, dict] = {}

        # Worker queues & tasks
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}

        # Counters
        self._counters: Dict[str, Dict[str, int]] = {}

        # Last known good offset per device
        self._last_off: Dict[str, int] = {}

        # Threshold for enabling force-prime (no timeout)
        self._force_cont_fail_threshold = 3  # consecutive continue->fail while re-prime->same off -> ok

    # ----------------------------- Manager helpers -----------------------------

    def _sensor_manager_ready(self) -> bool:
        hub = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        return bool(hub and getattr(hub, "sensor_manager", None))

    def _get_sensor_manager(self):
        hub = self.hass.data[DOMAIN][self.entry.entry_id]
        return hub.sensor_manager

    # ----------------------------- Counter / logging ---------------------------

    def _bump(
        self,
        dev_id: str,
        ok: bool,
        topic: str | None = None,
        payload: bytes | None = None,
        offset: int | None = None,
        had_blob: bool | None = None,
        zerr: str | None = None,
    ):
        """
        Per-device counters and diagnostics for OK/MISS.
        """
        c = self._counters.setdefault(dev_id, {"total": 0, "ok": 0, "err": 0, "consec_err": 0})
        c["total"] += 1

        if ok:
            c["ok"] += 1
            c["consec_err"] = 0
            if self.debug:
                _LOGGER.debug("OBIS OK dev=%s total=%d ok=%d err=%d",
                                dev_id, c["total"], c["ok"], c["err"])
            return

        # MISS
        c["err"] += 1
        c["consec_err"] += 1

        plen = len(payload) if isinstance(payload, (bytes, bytearray)) else "-"
        head = payload[:16].hex() if isinstance(payload, (bytes, bytearray)) else "-"

        if self.debug:
            _LOGGER.debug(
                "OBIS MISS dev=%s consec_err=%d total_err=%d total=%d len=%s head=%s topic=%s offset=%s had_blob=%s zerr=%s",
                dev_id, c["consec_err"], c["err"], c["total"],
                plen, head, topic, offset, had_blob, (zerr or "-")
            )
            self._maybe_log_payload_as_base64(payload)

        if c["consec_err"] in (5, 20, 50, 100):
            _LOGGER.warning("OBIS consecutive decode errors for %s: %s", dev_id, c)

    def _maybe_log_payload_as_base64(self, payload: bytes | None = None):
        if self.cfg.get(CONF_LOG_MISSED_BASE64, False) and isinstance(payload, (bytes, bytearray)):
            try:
                _LOGGER.debug("Base64 payload:\n%s", base64.b64encode(payload).decode())
            except Exception:
                pass

    # ----------------------------- Stream helpers ------------------------------

    def _get_stream(self, dev_id: str) -> Dict[str, Any]:
        st = self._streams.get(dev_id)
        if not st:
            st = {
                "off": None,
                "z": None,
                "buf": bytearray(),
                "blob": None,
                "dev_id": dev_id,
                "cont_fail": 0,
                "force_mode": False,
                "force_no_deflate": 0,
                "just_primed": False,
                "skip_bump": False,
            }
            self._streams[dev_id] = st
        return st

    def _reset_stream(self, st: Dict[str, Any]) -> None:
        st["off"] = None
        st["z"] = None
        st["buf"].clear()

    def _continue_decompress(
        self, st: Dict[str, Any], blob: bytes
    ) -> Tuple[Literal["ok", "stall", "exception"], Optional[str]]:
        """
        Try to continue the existing deflate stream.
        Returns ("ok", None) and extends buffer on success,
                ("stall", None) if out == b'',
                ("exception", str(err)) if zlib blows up.
        """
        try:
            out = st["z"].decompress(blob[st["off"]:])
            if out:
                st["buf"].extend(out)
                return "ok", None
            return "stall", None
        except Exception as exc:  # zlib.error
            return "exception", str(exc)

    def _has_obis_markers(self, data: bytes) -> bool:
        # strict: require at least one complete frame '/...!'
        return (b'/' in data) and (b'!' in data)

    def _probe_offset(self, blob: bytes, max_off: int = 128) -> Optional[int]:
        """
        Non-destructive probe: find a plausible raw-deflate start that *looks like OBIS*.
        We require that the first decompressed chunk already contains OBIS marker(s).
        """
        import zlib
        limit = min(max_off, len(blob) - 8)
        for off in range(0, limit + 1):
            try:
                z = zlib.decompressobj(-15)
                out = z.decompress(blob[off:])
                if out and len(out) > 20 and self._has_obis_markers(out):
                    return off
            except Exception:
                continue
        return None

    def _find_offset_and_prime(self, st: Dict[str, Any], blob: bytes, max_off: int = 64) -> bool:
        """
        Hard prime: create a NEW zlib object and decompress starting at the found offset.
        Accept only if the initial output both is meaningful and shows OBIS markers.
        """
        import zlib

        # Try last known good offset first
        last = self._last_off.get(st["dev_id"])
        if last is not None and last < len(blob):
            try:
                z = zlib.decompressobj(-15)
                out = z.decompress(blob[last:])
                if out and len(out) > 50 and self._has_obis_markers(out):
                    st["off"] = last
                    st["z"] = z
                    st["buf"].extend(out)
                    return True
            except Exception:
                pass

        # Scan early bytes
        limit = min(max_off, len(blob) - 8)
        for off in range(0, limit + 1):
            try:
                z = zlib.decompressobj(-15)
                out = z.decompress(blob[off:])
                # require both size and P1 hint
                if out and len(out) > 50 and self._has_obis_markers(out):
                    st["off"] = off
                    st["z"] = z
                    st["buf"].extend(out)
                    self._last_off[st["dev_id"]] = off
                    if self.debug:
                        _LOGGER.debug("Primed zlib at REAL offset=%d (initial out_len=%d, has_obis=%s)",
                                        off, len(out), True)
                    return True
            except Exception:
                continue

        return False

    def _extract_frames(self, st: Dict[str, Any]) -> list[str]:
        """
        Pull complete '/...!' OBIS frames out of the buffer.
        """
        out = []
        b = st["buf"]
        while True:
            start = b.find(b"/")
            if start < 0:
                break
            end = b.find(b"!", start + 1)
            if end < 0:
                break
            frame = bytes(b[start: end + 1])
            del b[: end + 1]
            try:
                s = frame.decode("utf-8", errors="ignore")
                if s:
                    if self.debug and self.cfg.get(CONF_LOG_OBIS, False):
                        _LOGGER.debug("OBIS FRAME:\n%s", s)
                    out.append(s)
            except Exception:
                pass
        return out

    def _handle_no_deflate_blob(self, st: Dict[str, Any]) -> list[str]:
        """
        Blob does not carry deflate; do not reset or bump.
        """
        st["skip_bump"] = True
        st["cont_fail"] = 0
        if st.get("force_mode"):
            st["force_no_deflate"] = st.get("force_no_deflate", 0) + 1
            if st["force_no_deflate"] >= 3:
                st["force_mode"] = False
                if self.debug:
                    _LOGGER.debug("Disable force-prime due to repeated no-deflate blobs")
        return self._extract_frames(st)

    def _reprime_and_extract(self, st: Dict[str, Any], blob: bytes, max_off: int = 128) -> list[str]:
        """
        Reset stream, hard-prime again on the given blob, then extract frames.
        If priming fails, set skip_bump and return whatever is in buffer (likely none).
        """
        self._reset_stream(st)
        if not self._find_offset_and_prime(st, blob, max_off=max_off):
            st["skip_bump"] = True
            return self._extract_frames(st)
        return self._extract_frames(st)

    def _learn_from_reprime(self, st: Dict[str, Any], last_off: Optional[int]) -> None:
        """
        If re-priming landed at the same offset as before, increase cont_fail and enable force-mode
        after a threshold; otherwise reset cont_fail.
        """
        if last_off is not None and st["off"] == last_off:
            st["cont_fail"] = st.get("cont_fail", 0) + 1
            if st["cont_fail"] >= self._force_cont_fail_threshold:
                st["force_mode"] = True
                st["force_no_deflate"] = 0
                if self.debug:
                    _LOGGER.debug("Enable force-prime (no timeout), threshold reached (off=%s)", st["off"])
        else:
            st["cont_fail"] = 0

    # ------------------------ Feeding (normal + force) -------------------------

    def _feed_stream_and_collect_obis(self, st: dict, blob: bytes) -> list[str]:
        """
        Feed one blob into the current zlib stream, with adaptive recovery.
        """
        if st["off"] is None or st["z"] is None:
            return []

        # After priming in this frame: do NOT decompress again; just extract.
        if st.pop("just_primed", False):
            return self._extract_frames(st)

        # FORCE MODE: try quick continue first; if it fails, controlled re-prime (no timeout).
        if st.get("force_mode", False):
            if self.debug:
                _LOGGER.debug("Force-prime active (no timeout): attempting quick continue first")
            status, err = self._continue_decompress(st, blob)
            if status == "ok":
                st["cont_fail"] = 0
                st["force_mode"] = False
                st["force_no_deflate"] = 0
                return self._extract_frames(st)

            new_off = self._probe_offset(blob, max_off=128)
            if new_off is None:
                return self._handle_no_deflate_blob(st)

            frames = self._reprime_and_extract(st, blob, max_off=128)
            st["force_no_deflate"] = 0
            # If we primed but still got no frames, treat this blob as non-OBIS for bumping purposes
            if not frames:
                st["skip_bump"] = True
            return frames

        # NORMAL MODE: try to continue; if fails, probe and possibly re-prime; learn from the result.
        status, err = self._continue_decompress(st, blob)
        if status == "ok":
            st["cont_fail"] = 0
            st["force_no_deflate"] = 0
            return self._extract_frames(st)

        if self.debug and status == "exception":
            _LOGGER.debug("Decompress exception at off=%s: %s; probing blob", st["off"], err)

        new_off = self._probe_offset(blob, max_off=128)
        if new_off is None:
            return self._handle_no_deflate_blob(st)

        last_off = self._last_off.get(st["dev_id"])
        frames = self._reprime_and_extract(st, blob, max_off=128)
        self._learn_from_reprime(st, last_off)
        return frames

    # ------------------------------- MQTT layer --------------------------------

    @callback
    def on_mqtt_message(self, topic: str, payload: bytes):
        if self.debug:
            _LOGGER.debug("MQTT %s len=%d head=%s", topic, len(payload), payload[:16].hex())

        dev_id = "unknown"
        try:
            if "/publish" in topic and "tibber-pulse-" in topic:
                dev_id = topic.split("tibber-pulse-", 1)[1].split("/", 1)[0]
        except Exception:
            pass

        self._ensure_worker(dev_id)
        q = self._queues[dev_id]

        if q.full():
            try:
                q.get_nowait()
                q.task_done()
            except Exception:
                pass

        q.put_nowait((dev_id, topic, payload))

    # ---------------------------- Worker / Executor ----------------------------

    def _ensure_worker(self, dev_id: str):
        if dev_id in self._workers:
            return

        q = self._queues.setdefault(dev_id, asyncio.Queue(maxsize=200))

        async def _worker():
            while True:
                dev_id_work, topic, payload = await q.get()
                try:
                    await self.hass.async_add_executor_job(self._process_payload_sync, dev_id_work, topic, payload)
                except Exception as exc:
                    if self.debug:
                        _LOGGER.debug("Worker error %s: %s", dev_id_work, exc)
                finally:
                    q.task_done()

        self._workers[dev_id] = self.hass.loop.create_task(_worker())

    # ------------------------------- Main logic --------------------------------

    def _process_payload_sync(self, dev_id: str, topic: str, payload: bytes):
        # 1) STATUS JSON
        try:
            if payload.startswith(b"{") and b'"status"' in payload:
                s = payload.decode("utf-8")
                d = self._devices.setdefault(dev_id, {"status": {}, "sensors": {}, "has_status": False})
                obj = json.loads(s)
                d["status"] = obj.get("status", {}) or {}

                # Extract pulse_id from STATUS
                pulse_id = str(d["status"].get("ID") or "").strip()

                if pulse_id:
                    # Mark topic entry as "has_status"
                    d["has_status"] = True

                    # Map topic to pulse_id
                    self._topic_to_pulse[dev_id] = pulse_id

                    # Ensure we also store STATUS under pulse_id (this is critical for device_info)
                    dp = self._devices.setdefault(pulse_id, {"status": {}, "sensors": {}, "has_status": True})
                    dp["status"] = d["status"]
                    dp["has_status"] = True

                    # Flush pending OBIS for this topic
                    pend = self._pending_by_topic.pop(dev_id, [])
                    for (obis, _st_cached) in pend:
                        self._apply_obis_with_pulse_id(pulse_id, obis)

                    # Push STATUS to SensorManager
                    if self._sensor_manager_ready():
                        sm = self._get_sensor_manager()
                        if hasattr(sm, "update_status_for_device"):
                            # run on HA loop
                            self.hass.loop.call_soon_threadsafe(sm.update_status_for_device, pulse_id, d["status"])

                if self.debug:
                    _LOGGER.debug("STATUS UPDATE %s", s)

                # Count status as OK
                self._bump(dev_id, True, topic=topic, payload=payload, offset=None, had_blob=None, zerr=None)
                return
        except Exception:
            pass

        # 2) PROTOBUF ENVELOPE
        blob = None
        try:
            env = pulse_pb2.Envelope()
            env.ParseFromString(payload)
            blob = getattr(env, "blob", None)
        except Exception:
            blob = None

        if blob:
            st = self._get_stream(dev_id)
            st["blob"] = blob

            if self.debug:
                try:
                    cand = pick_best_candidate_from_blob(blob)
                    _LOGGER.debug(
                        "Envelope blob best-candidate len=%d head=%s",
                        len(cand),
                        cand[:16].hex()
                    )
                except Exception as exc:
                    _LOGGER.debug("Error picking candidate: %s", exc)

            # Use full blob for deflate
            if st["off"] is None or st["z"] is None:
                if not self._find_offset_and_prime(st, blob):
                    self._bump(dev_id, False, topic=topic, payload=payload, offset=None, had_blob=True)
                    return
                st["just_primed"] = True
                frames = self._feed_stream_and_collect_obis(st, blob)
            else:
                frames = self._feed_stream_and_collect_obis(st, blob)

            # Non-deflate blob => do NOT bump MISS
            if not frames and st.get("skip_bump"):
                if self.debug:
                    _LOGGER.debug("Blob had no deflate for %s — skipping MISS bump", dev_id)
                    self._maybe_log_payload_as_base64(payload)
                st["skip_bump"] = False
                return

            if frames:
                for frame in frames:
                    obis = parse_obis_text(frame)
                    if obis:
                        self.hass.loop.call_soon_threadsafe(self._apply_obis, dev_id, obis)
                        self._bump(dev_id, True, topic=topic, payload=payload, offset=st.get("off"), had_blob=True)
                return

            # Respect skip_bump at tail as well
            if st.get("skip_bump"):
                if self.debug:
                    _LOGGER.debug("Blob had no deflate for %s — skipping MISS bump (tail)", dev_id)
                    self._maybe_log_payload_as_base64(payload)
                st["skip_bump"] = False
                return

            self._bump(dev_id, False, topic=topic, payload=payload, offset=st["off"], had_blob=True)
            return

        # 3) RAW OBIS FALLBACK
        try:
            if (b"/" in payload) and (b"!" in payload):
                s = payload.decode("utf-8", errors="ignore")
                obis = parse_obis_text(s)
                if obis:
                    self.hass.loop.call_soon_threadsafe(self._apply_obis, dev_id, obis)
                    self._bump(dev_id, True, topic=topic, payload=payload, offset=None, had_blob=False)
                    if self.debug and self.cfg.get(CONF_LOG_OBIS, False):
                        _LOGGER.debug("RAW OBIS:\n%s", s)
                    return
        except Exception:
            pass

        # UNKNOWN PAYLOAD
        if st := self._streams.get(dev_id):
            if st.get("skip_bump"):
                if self.debug:
                    _LOGGER.debug("Unknown payload for %s — skipping MISS bump due to skip_bump", dev_id)
                    self._maybe_log_payload_as_base64(payload)
                st["skip_bump"] = False
                return

        self._bump(dev_id, False, topic=topic, payload=payload, offset=None, had_blob=False)

    # ------------------------------ Apply to HA --------------------------------

    def _apply_obis(self, dev_id_topic: str, obis: Dict[str, Any]):
        d = self._devices.setdefault(dev_id_topic, {"status": {}, "sensors": {}, "has_status": False})
        if not d.get("has_status"):
            # buffer until we know pulse_id
            self._pending_by_topic.setdefault(dev_id_topic, []).append((obis, d["status"]))
            return

        # map to pulse id
        pulse_id = self._topic_to_pulse.get(dev_id_topic)
        if not pulse_id:
            # should not happen if has_status True, but guard
            self._pending_by_topic.setdefault(dev_id_topic, []).append((obis, d["status"]))
            return

        self._apply_obis_with_pulse_id(pulse_id, obis)

    def _apply_obis_with_pulse_id(self, pulse_id: str, obis: Dict[str, Any]):
        """
        Apply OBIS to the SensorManager under the canonical pulse_id device key.
        """
        # Make sure status is also stored under pulse_id for device_info
        if pulse_id not in self._devices:
            self._devices[pulse_id] = {"status": {}, "sensors": {}, "has_status": False}
        status = self._devices[pulse_id].get("status")

        # Learn and push meter serial (0-0:96.1.1) as connections
        meter = obis.get("0-0:96.1.1")
        if meter:
            meter_s = str(meter).strip()
            self._meter_id[pulse_id] = meter_s
            sm = self._get_sensor_manager() if self._sensor_manager_ready() else None
            if sm and hasattr(sm, "update_meter_id_for_device"):
                self.hass.loop.call_soon_threadsafe(sm.update_meter_id_for_device, pulse_id, meter_s)

        sm = self._get_sensor_manager() if self._sensor_manager_ready() else None
        if sm and hasattr(sm, "set_obis_units"):
            sm.set_obis_units(obis.get("_units", {}))

        if not self._sensor_manager_ready():
            # If you also want to keep a pulse-based pending, add a structure for that here
            return

        for code, value in obis.items():
            if code == "_units":
                continue
            
            self.hass.loop.call_soon_threadsafe(
                self.hass.async_create_task,
                sm.add_or_update(pulse_id, code, value, status),
            )