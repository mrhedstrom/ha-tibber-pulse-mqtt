from __future__ import annotations

import json
import logging
import asyncio
from typing import Dict, Any, Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN, CONF_LOG_MISSED_BASE64, CONF_LOG_OBIS

try:
    from .protobuf import pulse_pb2
except Exception:
    pulse_pb2 = None

from .parsers.pulse_envelope import pick_best_candidate_from_blob
from .parsers.obis_text import parse_obis_text

from .obis.streaming import ObisStreamManager
from .util.diagnostics import DiagnosticsRegistry
from .ha.invoke import call_sm_threadsafe, call_sm_on_loop

_LOGGER = logging.getLogger(__name__)


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

        # 2) PROTOBUF ENVELOPE
        blob = None
        try:
            env = pulse_pb2.Envelope()
            env.ParseFromString(payload)
            blob = getattr(env, "blob", None)
        except Exception:
            blob = None

        if blob:
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

            frames, skip_bump, off_used = self._streams.feed_blob(dev_id, blob)

            # Non-deflate blob ⇒ do NOT bump MISS
            if not frames and skip_bump:
                if self.debug:
                    _LOGGER.debug("Blob had no deflate for %s — skipping MISS bump", dev_id)
                    self._diag.maybe_log_payload_as_base64(payload)
                self._streams.consume_skip_bump(dev_id)
                return

            if frames:
                for frame in frames:
                    obis = parse_obis_text(frame)
                    if obis:
                        self.hass.loop.call_soon_threadsafe(self._apply_obis, dev_id, obis)
                        self._diag.bump(dev_id, True, topic=topic, payload=payload, offset=off_used, had_blob=True)
                return

            if self._streams.consume_skip_bump(dev_id):
                if self.debug:
                    _LOGGER.debug("Blob had no deflate for %s — skipping MISS bump (tail)", dev_id)
                    self._diag.maybe_log_payload_as_base64(payload)
                return

            self._diag.bump(dev_id, False, topic=topic, payload=payload, offset=off_used, had_blob=True)
            return

        # 3) RAW OBIS FALLBACK
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

        # UNKNOWN PAYLOAD
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

        meter = obis.get("0-0:96.1.1")
        if meter:
            meter_s = str(meter).strip()
            self._meter_id[pulse_id] = meter_s
            sm = self._get_sensor_manager() if self._sensor_manager_ready() else None
            if sm and hasattr(sm, "update_meter_id_for_device"):
                call_sm_on_loop(self.hass, sm.update_meter_id_for_device, pulse_id, meter_s)

        sm = self._get_sensor_manager() if self._sensor_manager_ready() else None
        if sm and hasattr(sm, "set_obis_units"):
            call_sm_on_loop(self.hass, sm.set_obis_units, obis.get("_units", {}))

        if not self._sensor_manager_ready():
            return

        for code, value in obis.items():
            if code == "_units":
                continue
            if sm and hasattr(sm, "add_or_update"):
                call_sm_on_loop(self.hass, sm.add_or_update, pulse_id, code, value, status)