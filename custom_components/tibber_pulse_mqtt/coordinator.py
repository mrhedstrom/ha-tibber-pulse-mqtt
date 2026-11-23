from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import *
from .mqtt_client import HAMQTTBridge, ExternalMQTTClient
from .dispatcher import TibberDispatcher
from .cloud_certs import credentials_dir, credentials_root, remove_orphaned_credentials
from .cloud_forwarder import (
    CloudForwarder,
    CloudForwardingConfig,
    config_from_subentry,
    receive_topic_for,
)
from .cloud_issues import (
    CloudIssueReporter,
    async_clear_pulse_discovered,
    async_remove_stale_issues,
    async_report_pulse_discovered,
)

_LOGGER = logging.getLogger(__name__)

CERTIFICATE_CHECK_INTERVAL = timedelta(hours=24)

class TibberLocalHub:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        self.cfg = {**entry.data, **entry.options}
        self.dispatcher = TibberDispatcher(hass, entry)
        self._ha_mqtt: HAMQTTBridge | None = None
        self._ext_mqtt: ExternalMQTTClient | None = None
        self._forwarders: dict[str, CloudForwarder] = {}  # keyed by Pulse ID
        self._cloud_states: dict[str, str] = {}
        self._cloud_disabled: set[str] = set()  # forwardings switched off by the user
        self._cloud_listeners: list[Callable[[str], None]] = []
        self._reporters: dict[str, CloudIssueReporter] = {}
        self._cloud_store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.cloud_forwarding")
        self._unsub_certificate_check: Callable[[], None] | None = None
        self.sensor_manager = None  # set by sensor.py
        self._unsub_ha_stop = None  # type: Optional[callable]

    async def async_start(self):
        # Start dispatcher first so workers/lifecycle are ready
        await self.dispatcher.async_start()

        topic = self.cfg.get(CONF_SUBSCRIBE, DEFAULT_TOPIC)

        if self.cfg.get(CONF_BROKER_MODE) == "homeassistant":
            self._ha_mqtt = HAMQTTBridge(self.hass)
            await self._ha_mqtt.async_subscribe(topic, self._on_mqtt_message)
            _LOGGER.info("Tibber Pulse MQTT listening via HA MQTT on %s", topic)
        else:
            self._ext_mqtt = ExternalMQTTClient(
                hass=self.hass,
                host=self.cfg.get(CONF_HOST, "127.0.0.1"),
                port=self.cfg.get(CONF_PORT, DEFAULT_PORT),
                topic=topic,
                cb=self._on_mqtt_message,
                username=self.cfg.get(CONF_USERNAME),
                password=self.cfg.get(CONF_PASSWORD),
                client_id=self.cfg.get(CONF_CLIENT_ID),
                tls=self.cfg.get(CONF_TLS, False),
                cafile=self.cfg.get(CONF_CAFILE),
                certfile=self.cfg.get(CONF_CERTFILE),
                keyfile=self.cfg.get(CONF_KEYFILE),
                tls_insecure=self.cfg.get(CONF_TLS_INSECURE, False),
                tls_version=self.cfg.get(CONF_TLS_VERSION, "tlsv1.2")
            )
            await self.hass.async_add_executor_job(self._ext_mqtt.start)
            _LOGGER.info(
                "Tibber Pulse MQTT listening via External MQTT on %s:%s topic=%s",
                self.cfg.get(CONF_HOST, "127.0.0.1"),
                self.cfg.get(CONF_PORT, DEFAULT_PORT),
                topic
            )

        self.dispatcher.on_pulse_discovered = self._on_pulse_discovered
        await self._async_start_cloud_forwarders()

        # Robust HA-stop listener (NOT listen_once). We self-unsubscribe on first fire.
        # Must be async so HA schedules it on the event loop (sync listeners run in executor).
        async def _on_ha_stop(_):
            if self._unsub_ha_stop is not None:
                try:
                    self._unsub_ha_stop()
                except Exception:
                    pass
                self._unsub_ha_stop = None
            await self.async_stop()

        self._unsub_ha_stop = self.hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, _on_ha_stop)

        # Unload callback should be an ASYNC FUNCTION so HA can create the Task itself
        async def _on_unload_async():
            await self.async_stop()

        self.entry.async_on_unload(_on_unload_async)

    async def _async_start_cloud_forwarders(self) -> None:
        """Start Tibber Cloud forwarding for every configured Pulse."""
        # Credentials of removed forwardings are deleted; every hub entry counts so
        # one entry never cleans up another's files.
        in_use = {
            subentry.data.get(CONF_PULSE_ID)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            for subentry in entry.get_subentries_of_type(SUBENTRY_TYPE_CLOUD_FORWARDING)
        }
        await self.hass.async_add_executor_job(
            remove_orphaned_credentials, credentials_root(self.hass.config.path), in_use
        )
        async_remove_stale_issues(self.hass, self.entry)

        stored = await self._cloud_store.async_load() or {}
        self._cloud_disabled = set(stored.get("disabled", []))

        for subentry in self.entry.get_subentries_of_type(SUBENTRY_TYPE_CLOUD_FORWARDING):
            pulse_id = subentry.data.get(CONF_PULSE_ID)
            async_clear_pulse_discovered(self.hass, pulse_id)
            if pulse_id in self._cloud_disabled:
                self._cloud_states[pulse_id] = CLOUD_STATE_DISABLED
                continue
            try:
                config = config_from_subentry(self.hass.config.path, subentry)
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.warning(
                    "Skipping Tibber Cloud forwarding %s, its stored settings are invalid (%s)",
                    subentry.title,
                    err,
                )
                continue
            await self.async_add_forwarder(config)

        await self.async_check_certificates()
        self._unsub_certificate_check = async_track_time_interval(
            self.hass, self._async_certificate_check_due, CERTIFICATE_CHECK_INTERVAL
        )

    # ----------------------------- Tibber Cloud state ----------------------------

    def cloud_state(self, pulse_id: str) -> str:
        """Return the connection state of a Pulse's forwarding."""
        return self._cloud_states.get(pulse_id, CLOUD_STATE_DISABLED)

    def cloud_enabled(self, pulse_id: str) -> bool:
        """Return False when the user switched the forwarding off."""
        return pulse_id not in self._cloud_disabled

    def async_add_cloud_listener(self, listener: Callable[[str], None]) -> Callable[[], None]:
        """Call ``listener(pulse_id)`` when a forwarding changes. Returns the unsubscribe."""
        self._cloud_listeners.append(listener)

        def _unsubscribe() -> None:
            if listener in self._cloud_listeners:
                self._cloud_listeners.remove(listener)

        return _unsubscribe

    def _notify_cloud_listeners(self, pulse_id: str) -> None:
        for listener in tuple(self._cloud_listeners):
            try:
                listener(pulse_id)
            except Exception:
                _LOGGER.exception("Tibber Cloud listener failed")

    def _subentry_for(self, pulse_id: str):
        return next(
            (
                subentry
                for subentry in self.entry.get_subentries_of_type(SUBENTRY_TYPE_CLOUD_FORWARDING)
                if subentry.data.get(CONF_PULSE_ID) == pulse_id
            ),
            None,
        )

    def _reporter_for(self, pulse_id: str) -> CloudIssueReporter | None:
        reporter = self._reporters.get(pulse_id)
        if reporter is None:
            subentry = self._subentry_for(pulse_id)
            if subentry is None:
                return None
            reporter = self._reporters[pulse_id] = CloudIssueReporter(
                self.hass, self.entry.entry_id, subentry.subentry_id, pulse_id
            )
        return reporter

    @callback
    def _on_cloud_state(self, pulse_id: str, state: str) -> None:
        self._cloud_states[pulse_id] = state
        reporter = self._reporter_for(pulse_id)
        if reporter is not None:
            reporter.on_state(state)
        self._notify_cloud_listeners(pulse_id)

    async def async_set_cloud_enabled(self, pulse_id: str, enabled: bool) -> None:
        """Switch a forwarding on or off. The choice survives restarts and reloads."""
        subentry = self._subentry_for(pulse_id)
        if subentry is None:
            raise ValueError(f"No Tibber Cloud forwarding for Pulse {pulse_id}")
        if enabled:
            config = config_from_subentry(self.hass.config.path, subentry)
            self._cloud_disabled.discard(pulse_id)
        else:
            self._cloud_disabled.add(pulse_id)
        await self._cloud_store.async_save({"disabled": sorted(self._cloud_disabled)})

        if enabled:
            await self.async_add_forwarder(config)
        else:
            await self.async_remove_forwarder(pulse_id)
        self._notify_cloud_listeners(pulse_id)

    async def async_reset_cloud_connection(self, pulse_id: str) -> None:
        """Restart a forwarding and forget earlier connection drops (after removing a bridge)."""
        reporter = self._reporter_for(pulse_id)
        if reporter is not None:
            reporter.clear_unstable()
        subentry = self._subentry_for(pulse_id)
        if subentry is not None and self.cloud_enabled(pulse_id):
            await self.async_add_forwarder(config_from_subentry(self.hass.config.path, subentry))

    async def async_check_certificates(self) -> None:
        """Report certificates that cannot be read, have expired or expire soon."""
        for subentry in self.entry.get_subentries_of_type(SUBENTRY_TYPE_CLOUD_FORWARDING):
            pulse_id = subentry.data.get(CONF_PULSE_ID)
            reporter = self._reporter_for(pulse_id)
            try:
                directory = credentials_dir(self.hass.config.path, pulse_id)
            except ValueError:
                continue
            if reporter is not None:
                await reporter.async_check_certificate(directory / "cert.pem")

    async def _async_certificate_check_due(self, _now: datetime) -> None:
        await self.async_check_certificates()

    @callback
    def _on_pulse_discovered(self, pulse_id: str, topic: str) -> None:
        """Offer forwarding for a Pulse that has none. An ignored offer stays ignored."""
        if self._subentry_for(pulse_id) is not None:
            async_clear_pulse_discovered(self.hass, pulse_id)
            return
        try:
            receive_topic_for(topic)
            credentials_dir(self.hass.config.path, pulse_id)
        except ValueError:
            return  # forwarding cannot be built from this topic or ID
        async_report_pulse_discovered(self.hass, self.entry.entry_id, pulse_id, topic)

    def _on_mqtt_message(self, topic: str, payload: bytes) -> None:
        """Hand every local message to the cloud forwarders, then to the dispatcher."""
        for forwarder in tuple(self._forwarders.values()):
            try:
                forwarder.forward_upstream(topic, payload)
            except Exception:
                _LOGGER.exception("Tibber Cloud forwarding failed")
        self.dispatcher.on_mqtt_message(topic, payload)

    async def _async_publish_local(self, topic: str, payload: bytes) -> None:
        """Publish a message (from the cloud) on the local broker."""
        if self._ha_mqtt is not None:
            await self._ha_mqtt.async_publish(topic, payload)
        elif self._ext_mqtt is not None:
            await self._ext_mqtt.async_publish(topic, payload)
        else:
            raise ConnectionError("Local MQTT is not running")

    async def async_add_forwarder(self, config: CloudForwardingConfig) -> CloudForwarder:
        """Start forwarding one Pulse to Tibber Cloud, replacing any previous setup."""
        await self.async_remove_forwarder(config.pulse_id)
        forwarder = CloudForwarder(
            self.hass, config, self._async_publish_local, self._on_cloud_state
        )
        self._forwarders[config.pulse_id] = forwarder
        await forwarder.async_start()
        return forwarder

    def has_forwarder(self, pulse_id: str) -> bool:
        """Return True while a Pulse is being forwarded to Tibber Cloud."""
        return pulse_id in self._forwarders

    async def async_remove_forwarder(self, pulse_id: str) -> None:
        """Stop forwarding a Pulse to Tibber Cloud."""
        forwarder = self._forwarders.pop(pulse_id, None)
        if forwarder is not None:
            await forwarder.async_stop()

    async def async_stop(self):
        # Idempotent stop: safe to call multiple times
        if self._unsub_ha_stop is not None:
            try:
                self._unsub_ha_stop()
            except Exception:
                pass
            self._unsub_ha_stop = None

        if self._unsub_certificate_check is not None:
            self._unsub_certificate_check()
            self._unsub_certificate_check = None

        # Stop cloud forwarders before the local MQTT clients they publish through
        for pulse_id in list(self._forwarders):
            try:
                await self.async_remove_forwarder(pulse_id)
            except Exception:
                pass

        # Stop dispatcher (cancels and awaits worker tasks)
        try:
            await self.dispatcher.async_stop()
        except Exception:
            pass

        # Stop MQTT bridges/clients
        try:
            if self._ha_mqtt is not None:
                await self._ha_mqtt.async_stop()
        except Exception:
            pass
        finally:
            self._ha_mqtt = None

        try:
            if self._ext_mqtt is not None:
                await self.hass.async_add_executor_job(self._ext_mqtt.stop)
        except Exception:
            pass
        finally:
            self._ext_mqtt = None