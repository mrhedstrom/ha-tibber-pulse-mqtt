from __future__ import annotations

import json
import logging
import asyncio
from typing import Dict, Any, List

from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN, CONF_LOG_MISSED_BASE64, CONF_LOG_OBIS

try:
    from .protobuf import pulse_pb2
except Exception:
    pulse_pb2 = None

# We will use this only for quick “does this buffer contain any deflate?” checks.
try:
    from .parsers.pulse_envelope import pick_best_candidate_from_blob, extract_zlib_payload_if_any
except Exception:
    # Fallback: keep the import compatible even if the helper is not present
    def pick_best_candidate_from_blob(_blob: bytes):
        return None  # for debug only

    def extract_zlib_payload_if_any(_buf: bytes):
        return None  # “no deflate” by default

from .parsers.obis_text import parse_obis_text

from .obis.streaming import ObisStreamManager
from .util.diagnostics import DiagnosticsRegistry
from .ha.invoke import call_sm_threadsafe, call_sm_on_loop

_LOGGER = logging.getLogger(__name__)

# Tibber’s in‑payload delimiter between frames seen on /publish topics.
_MARKER = bytes.fromhex("00000000ffff20c105")


def _split_tibber_frames(payload: bytes) -> List[bytes]:
    """
    Split one MQTT payload into one or more Tibber frames using the known delimiter.
    Returns a list of non-empty chunks. If no delimiter is present, returns [payload].
    """
    if _MARKER not in payload:
        return [payload]
    parts: list[bytes] = []
    pos = 0
    n = len(payload)
    while True:
        p = payload.find(_MARKER, pos)
        if p == -1:
            tail = payload[pos:]
            if tail:
                parts.append(tail)
            break
        chunk = payload[pos:p]
        if chunk:
            parts.append(chunk)
        pos = p + len(_MARKER)
    return parts


class TibberDispatcher:
    """Receives MQTT payloads, decodes Tibber Pulse messages and pushes OBIS to SensorManager."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        self.cfg = {**entry.data, **entry.options}
        self.debug = _LOGGER.isEnabledFor(logging.DEBUG)

        # Device state / mappings
        self._devices: Dict[str, Dict[str, Any]] = {}
        self._topic_to_pulse: Dict[str, str] = {}
        self._pending_by_topic: Dict[str, list] = {}
        self._meter_id: Dict[str, str] = {}

        # Async worker infrastructure
        self._queues: dict[str, asyncio.Queue[tuple[str, str, bytes]]] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._stop_event = asyncio.Event()

        # Stream manager for deflate/OBIS stream handling
        self._streams = ObisStreamManager(
            logger=_LOGGER,
            log_obis_frames=bool(self.cfg.get(CONF_LOG_OBIS, False) and self.debug),
            force_cont_fail_threshold=3,
            debug=self.debug,
        )

        # Diagnostics for OK/MISS counters and base64 logging of missed payloads
        di_cfg = dict(self.cfg)
        di_cfg["log_missed_base64"] = bool(self.cfg.get(CONF_LOG_MISSED_BASE64, False))
        self._diag = DiagnosticsRegistry(_LOGGER, di_cfg, self.debug)

    # ----------------------------- Lifecycle -----------------------------------

    async def async_start(self) -> None:
        """Prepare dispatcher to start receiving work."""
        self._stop_event.clear()

    async def async_stop(self) -> None:
        """Cancel and await all workers; drain queues and clear references."""
        self._stop_event.set()
        for t in list(self._workers.values()):
            if not t.done():
                t.cancel()
        if self._workers:
            await asyncio.gather(*self._workers.values(), return_exceptions=True)
        self._workers.clear()
        self._queues.clear()

    # ----------------------------- SensorManager helpers -----------------------

    def _sensor_manager_ready(self) -> bool:
        hub = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        return bool(hub and getattr(hub, "sensor_manager", None))

    def _get_sensor_manager(self):
        hub = self.hass.data[DOMAIN][self.entry.entry_id]
        return hub.sensor_manager

    # ------------------------------- MQTT layer --------------------------------

    @callback
    def on_mqtt_message(self, topic: str, payload: bytes):
        """Entry point for incoming MQTT messages."""
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
            try:
                while not self._stop_event.is_set():
                    try:
                        # Periodic timeout so we can check for shutdown
                        item = await asyncio.wait_for(q.get(), timeout=5.0)
                    except asyncio.TimeoutError:
                        continue
                    except asyncio.CancelledError:
                        break

                    dev_id_work, topic, payload = item
                    try:
                        # Offload sync parsing to a threadpool
                        await self.hass.async_add_executor_job(
                            self._process_payload_sync, dev_id_work, topic, payload
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        _LOGGER.exception("Worker error for %s", dev_id_work)
                    finally:
                        q.task_done()
            except asyncio.CancelledError:
                # Normal cancellation
                pass
            except Exception:
                _LOGGER.exception("Worker crashed for %s", dev_id)
            finally:
                # Drain queue to avoid unbalanced unfinished_tasks
                try:
                    while True:
                        _ = q.get_nowait()
                        q.task_done()
                except asyncio.QueueEmpty:
                    pass

        task = self.hass.async_create_background_task(
            _worker(),
            name=f"tibber_dispatcher_worker:{dev_id}"
        )

        def _done_cb(t: asyncio.Task):
            try:
                _ = t.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                _LOGGER.exception("Worker task failed for %s", dev_id)

        task.add_done_callback(_done_cb)
        self._workers[dev_id] = task

    # ------------------------------- Main logic --------------------------------

    def _process_payload_sync(self, dev_id: str, topic: str, payload: bytes):
        """Runs in a threadpool. Must use thread-safe hops to call HA loop APIs."""
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
                    d["has_status"] = True
                    self._topic_to_pulse[dev_id] = pulse_id

                    # Also cache STATUS under pulse_id (used for device_info)
                    dp = self._devices.setdefault(pulse_id, {"status": {}, "sensors": {}, "has_status": True})
                    dp["status"] = d["status"]
                    dp["has_status"] = True

                    # Flush pending OBIS for this topic
                    pend = self._pending_by_topic.pop(dev_id, [])
                    for (obis, _st_cached) in pend:
                        self.hass.loop.call_soon_threadsafe(self._apply_obis_with_pulse_id, pulse_id, obis)

                    # Push STATUS to SensorManager
                    if self._sensor_manager_ready():
                        sm = self._get_sensor_manager()
                        if hasattr(sm, "update_status_for_device"):
                            call_sm_threadsafe(self.hass, sm.update_status_for_device, pulse_id, d["status"])

                if self.debug:
                    _LOGGER.debug("STATUS UPDATE %s", s)

                self._diag.bump(dev_id, True, topic=topic, payload=payload, offset=None, had_blob=None, zerr=None)
                return
        except Exception:
            pass

        # 2) MODEL-AGNOSTIC MULTI-FRAME HANDLING (safe guards)
        any_ok = False
        any_skip = False

        try:
            frames_raw = _split_tibber_frames(payload)
            if self.debug and len(frames_raw) > 1:
                _LOGGER.debug("Payload split into %d Tibber frame(s)", len(frames_raw))

            for raw_frame in frames_raw:
                # Try parsing as Envelope to get 'blob'. If that fails, we can still consider the raw frame.
                blob = None
                if pulse_pb2 is not None:
                    try:
                        env = pulse_pb2.Envelope()
                        env.ParseFromString(raw_frame)
                        blob = getattr(env, "blob", None)
                    except Exception:
                        blob = None

                # Optional quick probe (debug/telemetry only, does NOT gate feeding anymore)
                try:
                    deflate_probe = extract_zlib_payload_if_any(blob if blob is not None else raw_frame)
                except Exception:
                    deflate_probe = None
                
                if self.debug and blob:
                    try:
                        cand = pick_best_candidate_from_blob(blob)
                        if cand is not None:
                            _LOGGER.debug("Envelope blob best-candidate len=%d head=%s", len(cand), cand[:16].hex())
                    except Exception as exc:
                        _LOGGER.debug("Error picking candidate: %s", exc)
                
                # Feed: always feed 'blob' if present; otherwise feed the raw frame.
                candidate = blob if blob is not None else raw_frame
                frames, skip_bump, off_used = self._streams.feed_blob(dev_id, candidate)
 
                
                if frames:
                    obis_found = False
                    for frame in frames:
                        obis = parse_obis_text(frame)
                        if obis:
                            obis_found = True
                            self.hass.loop.call_soon_threadsafe(self._apply_obis, dev_id, obis)
                    if obis_found:
                        self._diag.bump(dev_id, True, topic=topic, payload=payload, offset=off_used, had_blob=(blob is not None))
                        any_ok = True
                    elif skip_bump:
                        any_skip = True
                    # else: neither OBIS nor skip flag; fallthrough to try other inner frames
                else:                        
                    # No frames from this candidate
                    if skip_bump:
                        any_skip = True
            
            if any_ok:
                return
            if any_skip:
                # Stream likely started or partial; don't count as MISS for this publish.
                if self._streams.consume_skip_bump(dev_id):
                    if self.debug:
                        _LOGGER.debug("No complete deflate/OBIS in this publish for %s — skipping MISS bump", dev_id)
                        self._diag.maybe_log_payload_as_base64(payload)
                return
            
        except Exception:
            # Fall through to RAW OBIS fallback and final MISS bump below
            pass

        # 3) RAW OBIS FALLBACK (plain text telegram in the payload)
        try:
            if (b"/" in payload) and (b"!" in payload):
                s = payload.decode("utf-8", errors="ignore")
                obis = parse_obis_text(s)
                if obis:
                    self.hass.loop.call_soon_threadsafe(self._apply_obis, dev_id, obis)
                    self._diag.bump(dev_id, True, topic=topic, payload=payload, offset=None, had_blob=False)
                    if self.debug and self.cfg.get(CONF_LOG_OBIS, False):
                        _LOGGER.debug("RAW OBIS:\n%s", s)
                    return
        except Exception:
            pass

        # 4) UNKNOWN PAYLOAD
        if self._streams.consume_skip_bump(dev_id):
            if self.debug:
                _LOGGER.debug("Unknown payload for %s — skipping MISS bump due to skip_bump", dev_id)
                self._diag.maybe_log_payload_as_base64(payload)
            return

        self._diag.bump(dev_id, False, topic=topic, payload=payload, offset=None, had_blob=False)

    # ------------------------------ Apply to HA --------------------------------

    def _apply_obis(self, dev_id_topic: str, obis: Dict[str, Any]):
        """Apply OBIS for a given inbound topic key (may not yet be mapped to pulse_id)."""
        d = self._devices.setdefault(dev_id_topic, {"status": {}, "sensors": {}, "has_status": False})
        if not d.get("has_status"):
            self._pending_by_topic.setdefault(dev_id_topic, []).append((obis, d["status"]))
            return

        pulse_id = self._topic_to_pulse.get(dev_id_topic)
        if not pulse_id:
            self._pending_by_topic.setdefault(dev_id_topic, []).append((obis, d["status"]))
            return

        self._apply_obis_with_pulse_id(pulse_id, obis)

    def _apply_obis_with_pulse_id(self, pulse_id: str, obis: Dict[str, Any]):
        """
        Apply OBIS to the SensorManager under the canonical pulse_id device key.
        Runs on the HA event loop.
        """
        if pulse_id not in self._devices:
            self._devices[pulse_id] = {"status": {}, "sensors": {}, "has_status": False}
        status = self._devices[pulse_id].get("status")

        # Optional: update meter-id (0-0:96.1.1)
        meter = obis.get("0-0:96.1.1")
        if meter:
            meter_s = str(meter).strip()
            self._meter_id[pulse_id] = meter_s
            sm = self._get_sensor_manager() if self._sensor_manager_ready() else None
            if sm and hasattr(sm, "update_meter_id_for_device"):
                call_sm_on_loop(self.hass, sm.update_meter_id_for_device, pulse_id, meter_s)

        sm = self._get_sensor_manager() if self._sensor_manager_ready() else None

        # Update per-device units cache if this frame carries _units (REPLACE semantics).
        units_map = obis.get("_units", {})
        if sm and units_map:
            if hasattr(sm, "set_obis_units_for_device"):
                call_sm_on_loop(self.hass, sm.set_obis_units_for_device, pulse_id, units_map)

        if not self._sensor_manager_ready():
            return

        # Apply each OBIS code; pass this frame's units so conversion is deterministic for this update.
        for code, value in obis.items():
            if code == "_units":
                continue
            if sm and hasattr(sm, "add_or_update"):
                call_sm_on_loop(self.hass, sm.add_or_update, pulse_id, code, value, status, units_map or None)