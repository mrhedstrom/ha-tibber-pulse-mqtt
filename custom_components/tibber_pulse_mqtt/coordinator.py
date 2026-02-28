from __future__ import annotations

import logging
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry

from .const import *
from .mqtt_client import HAMQTTBridge, ExternalMQTTClient
from .dispatcher import TibberDispatcher

_LOGGER = logging.getLogger(__name__)

class TibberLocalHub:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        self.cfg = {**entry.data, **entry.options}
        self.dispatcher = TibberDispatcher(hass, entry)
        self._ha_mqtt: HAMQTTBridge | None = None
        self._ext_mqtt: ExternalMQTTClient | None = None
        self.sensor_manager = None  # set by sensor.py

    async def async_start(self):
        data = self.entry.data
        topic = self.cfg.get(CONF_SUBSCRIBE, DEFAULT_TOPIC)

        if data.get(CONF_BROKER_MODE) == "homeassistant":
            self._ha_mqtt = HAMQTTBridge(self.hass)
            await self._ha_mqtt.async_subscribe(topic, self.dispatcher.on_mqtt_message)
            _LOGGER.info("Tibber Pulse MQTT listening via HA MQTT on %s", topic)
        else:
            self._ext_mqtt = ExternalMQTTClient(
                host=self.cfg.get(CONF_HOST, "127.0.0.1"),
                port=self.cfg.get(CONF_PORT, DEFAULT_PORT),
                topic=topic,
                cb=self.dispatcher.on_mqtt_message,
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
            _LOGGER.info("Tibber Pulse MQTT listening via External MQTT on %s:%s topic=%s", self.cfg.get(CONF_HOST, "127.0.0.1"), self.cfg.get(CONF_PORT, DEFAULT_PORT), topic)

    async def async_stop(self):
        try:
            await self._ha_mqtt.async_stop()
        except Exception:
            pass
        try:
            await self.hass.async_add_executor_job(self._ext_mqtt.stop)
        except Exception:
            pass