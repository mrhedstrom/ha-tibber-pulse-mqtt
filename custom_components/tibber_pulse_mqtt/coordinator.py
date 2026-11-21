from __future__ import annotations

import logging
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from .const import *
from .mqtt_client import HAMQTTBridge, ExternalMQTTClient
from .dispatcher import TibberDispatcher
from .cloud_certs import credentials_root, remove_orphaned_credentials
from .cloud_forwarder import (
    CloudForwarder,
    CloudForwardingConfig,
    StateCallback,
    config_from_subentry,
)

_LOGGER = logging.getLogger(__name__)

class TibberLocalHub:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        self.cfg = {**entry.data, **entry.options}
        self.dispatcher = TibberDispatcher(hass, entry)
        self._ha_mqtt: HAMQTTBridge | None = None
        self._ext_mqtt: ExternalMQTTClient | None = None
        self._forwarders: dict[str, CloudForwarder] = {}  # keyed by Pulse ID
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

        for subentry in self.entry.get_subentries_of_type(SUBENTRY_TYPE_CLOUD_FORWARDING):
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

    async def async_add_forwarder(
        self,
        config: CloudForwardingConfig,
        on_state: StateCallback | None = None,
    ) -> CloudForwarder:
        """Start forwarding one Pulse to Tibber Cloud, replacing any previous setup."""
        await self.async_remove_forwarder(config.pulse_id)
        forwarder = CloudForwarder(self.hass, config, self._async_publish_local, on_state)
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