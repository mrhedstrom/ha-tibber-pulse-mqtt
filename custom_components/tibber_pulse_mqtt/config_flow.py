from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult, section
from homeassistant.helpers import selector

from .cloud_certs import (
    CERT_FILE,
    KEY_FILE,
    CA_FILE,
    CertificateError,
    commit_staged_credentials,
    credentials_dir,
    discard_staged_credentials,
    stage_credentials,
)
from .cloud_forwarder import (
    CloudForwardingConfig,
    async_test_connection,
    config_from_subentry,
    receive_topic_for,
)
from .const import (
    DOMAIN,
    SUBENTRY_TYPE_CLOUD_FORWARDING,
    CONF_PULSE_ID,
    CONF_LOCAL_TOPIC,
    CONF_ADVANCED,
    CONF_CLOUD_CERTIFICATE,
    CONF_CLOUD_PRIVATE_KEY,
    CONF_CLOUD_CA,
    CONF_CLOUD_HOST,
    CONF_CLOUD_PORT,
    CONF_CLOUD_PUBLISH_TOPIC,
    CONF_CLOUD_RECEIVE_TOPIC,
    CONF_CLOUD_TLS_VERSION,
    DEFAULT_CLOUD_HOST,
    DEFAULT_CLOUD_PORT,
    DEFAULT_CLOUD_TLS_VERSION,
    # Core
    CONF_BROKER_MODE,
    CONF_SUBSCRIBE,
    DEFAULT_TOPIC,
    # External MQTT
    CONF_HOST,
    CONF_PORT,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_CLIENT_ID,
    CONF_TLS,
    CONF_TLS_INSECURE,
    CONF_CAFILE,
    CONF_CERTFILE,
    CONF_KEYFILE,
    CONF_TLS_VERSION,
    DEFAULT_PORT,
    # Debug flags
    CONF_DEBUG_LOG_COMPONENT,
    CONF_LOG_MISSED_BASE64,
    CONF_LOG_OBIS,
    CONF_SAVE_RAW_DATA,
    DEFAULT_DEBUG_LOG_COMPONENT,
    DEFAULT_LOG_MISSED_BASE64,
    DEFAULT_LOG_OBIS,
    DEFAULT_SAVE_RAW_DATA,
)

# Select option lists (labels shown, values stored)
BROKER_MODES = [
    selector.SelectOptionDict(label="Home Assistant MQTT", value="homeassistant"),
    selector.SelectOptionDict(label="External MQTT", value="external"),
]

TLS_VERSION_OPTIONS = [
    selector.SelectOptionDict(label="TLS 1.2", value="tlsv1.2"),
    selector.SelectOptionDict(label="TLS 1.3", value="tlsv1.3"),
]


def _effective_config(entry: config_entries.ConfigEntry) -> Dict[str, Any]:
    """Return a merged configuration where options override data."""
    return {**entry.data, **entry.options}


def _bool_toggle_selector() -> selector.BooleanSelector:
    """Boolean selector rendered as a toggle."""
    return selector.BooleanSelector()


def _text_selector() -> selector.TextSelector:
    """Plain text selector."""
    return selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
    )


def _multiline_text_selector() -> selector.TextSelector:
    """Multiline text, used for pasted PEM certificates and keys."""
    return selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT, multiline=True)
    )


def _password_selector() -> selector.TextSelector:
    """Password-masked text selector."""
    return selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
    )


def _broker_mode_selector() -> selector.SelectSelector:
    """Dropdown for broker mode."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=BROKER_MODES, mode=selector.SelectSelectorMode.DROPDOWN
        )
    )


def _tls_version_selector() -> selector.SelectSelector:
    """Dropdown for TLS version."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=TLS_VERSION_OPTIONS, mode=selector.SelectSelectorMode.DROPDOWN
        )
    )


class TibberLocalConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Initial configuration flow for the integration."""
    VERSION = 1

    async def async_step_user(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """Step where the user configures broker mode, topic and debug flags."""
        if user_input is not None:
            # Normalize dependent debug flags: when master debug is OFF, force dependents to False
            debug_enabled = bool(user_input.get(CONF_DEBUG_LOG_COMPONENT, DEFAULT_DEBUG_LOG_COMPONENT))
            if not debug_enabled:
                user_input[CONF_LOG_MISSED_BASE64] = False
                user_input[CONF_LOG_OBIS] = False

            broker_mode = user_input.get(CONF_BROKER_MODE, "homeassistant")

            if broker_mode == "homeassistant":
                data = {
                    CONF_BROKER_MODE: "homeassistant",
                    CONF_SUBSCRIBE: user_input.get(CONF_SUBSCRIBE, DEFAULT_TOPIC),
                    CONF_DEBUG_LOG_COMPONENT: debug_enabled,
                    CONF_LOG_MISSED_BASE64: user_input.get(CONF_LOG_MISSED_BASE64, DEFAULT_LOG_MISSED_BASE64),
                    CONF_LOG_OBIS: user_input.get(CONF_LOG_OBIS, DEFAULT_LOG_OBIS),
                    CONF_SAVE_RAW_DATA: user_input.get(CONF_SAVE_RAW_DATA, DEFAULT_SAVE_RAW_DATA),
                }
                return self.async_create_entry(title="Tibber Pulse MQTT (HA MQTT)", data=data)

            # For external broker, cache step 1 values and continue
            self._cached_step1 = {
                CONF_BROKER_MODE: "external",
                CONF_SUBSCRIBE: user_input.get(CONF_SUBSCRIBE, DEFAULT_TOPIC),
                CONF_DEBUG_LOG_COMPONENT: debug_enabled,
                CONF_LOG_MISSED_BASE64: user_input.get(CONF_LOG_MISSED_BASE64, DEFAULT_LOG_MISSED_BASE64),
                CONF_LOG_OBIS: user_input.get(CONF_LOG_OBIS, DEFAULT_LOG_OBIS),
                CONF_SAVE_RAW_DATA: user_input.get(CONF_SAVE_RAW_DATA, DEFAULT_SAVE_RAW_DATA),
            }
            return await self.async_step_external_broker()

        # Initial form schema
        schema = vol.Schema({
            vol.Required(CONF_BROKER_MODE, default="homeassistant"): _broker_mode_selector(),
            vol.Required(CONF_SUBSCRIBE, default=DEFAULT_TOPIC): _text_selector(),
            vol.Required(CONF_DEBUG_LOG_COMPONENT, default=DEFAULT_DEBUG_LOG_COMPONENT): _bool_toggle_selector(),
            vol.Required(CONF_LOG_MISSED_BASE64, default=DEFAULT_LOG_MISSED_BASE64): _bool_toggle_selector(),
            vol.Required(CONF_LOG_OBIS, default=DEFAULT_LOG_OBIS): _bool_toggle_selector(),
            vol.Required(CONF_SAVE_RAW_DATA, default=DEFAULT_SAVE_RAW_DATA): _bool_toggle_selector(),
        })
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_external_broker(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """External broker details (host/port/auth/TLS switch)."""
        if user_input is not None:
            if user_input.get(CONF_TLS):
                self._cached_ext = user_input
                return await self.async_step_external_tls()

            data = {**self._cached_step1, **user_input}
            return self.async_create_entry(title="Tibber Pulse MQTT (External MQTT)", data=data)

        schema = vol.Schema({
            vol.Required(CONF_HOST): _text_selector(),
            vol.Required(CONF_PORT, default=DEFAULT_PORT): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=65535, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(CONF_USERNAME): _text_selector(),
            vol.Optional(CONF_PASSWORD): _password_selector(),
            vol.Optional(CONF_CLIENT_ID): _text_selector(),
            vol.Required(CONF_TLS, default=False): _bool_toggle_selector(),
        })
        return self.async_show_form(step_id="external_broker", data_schema=schema)

    async def async_step_external_tls(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """TLS-specific options for external broker."""
        if user_input is not None:
            data = {**self._cached_step1, **self._cached_ext, **user_input}
            return self.async_create_entry(title="Tibber Pulse MQTT (External MQTT TLS)", data=data)

        schema = vol.Schema({
            vol.Optional(CONF_CAFILE): _text_selector(),
            vol.Optional(CONF_CERTFILE): _text_selector(),
            vol.Optional(CONF_KEYFILE): _text_selector(),
            vol.Optional(CONF_TLS_VERSION, default="tlsv1.2"): _tls_version_selector(),
            vol.Required(CONF_TLS_INSECURE, default=False): _bool_toggle_selector(),
        })
        return self.async_show_form(step_id="external_tls", data_schema=schema)

    @staticmethod
    def async_get_options_flow(entry: config_entries.ConfigEntry):
        """Return the OptionsFlow for editing after initial setup."""
        return TibberLocalOptionsFlow(entry)

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[config_entries.ConfigSubentryFlow]]:
        """Tibber Cloud forwarding is configured per Pulse as a subentry."""
        return {SUBENTRY_TYPE_CLOUD_FORWARDING: CloudForwardingSubentryFlow}


class TibberLocalOptionsFlow(config_entries.OptionsFlow):
    """Multi-step options flow allowing changing all settings after initial setup."""

    def __init__(self, entry: config_entries.ConfigEntry):
        self.entry = entry
        self._step1_cache: Dict[str, Any] = {}
        self._ext_cache: Dict[str, Any] = {}

    def _cfg(self) -> Dict[str, Any]:
        """Return effective configuration for defaults."""
        return _effective_config(self.entry)

    async def async_step_init(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """Options step 1: broker mode + common + debug (always visible), with normalization."""
        cfg = self._cfg()

        if user_input is not None:
            # Normalize dependent flags if master debug is OFF
            debug_enabled = bool(user_input.get(CONF_DEBUG_LOG_COMPONENT, cfg.get(CONF_DEBUG_LOG_COMPONENT, DEFAULT_DEBUG_LOG_COMPONENT)))
            if not debug_enabled:
                user_input[CONF_LOG_MISSED_BASE64] = False
                user_input[CONF_LOG_OBIS] = False

            self._step1_cache = {
                CONF_BROKER_MODE: user_input.get(CONF_BROKER_MODE, cfg.get(CONF_BROKER_MODE, "homeassistant")),
                CONF_SUBSCRIBE: user_input.get(CONF_SUBSCRIBE, cfg.get(CONF_SUBSCRIBE, DEFAULT_TOPIC)),
                CONF_DEBUG_LOG_COMPONENT: debug_enabled,
                CONF_LOG_MISSED_BASE64: user_input.get(CONF_LOG_MISSED_BASE64, cfg.get(CONF_LOG_MISSED_BASE64, DEFAULT_LOG_MISSED_BASE64)),
                CONF_LOG_OBIS: user_input.get(CONF_LOG_OBIS, cfg.get(CONF_LOG_OBIS, DEFAULT_LOG_OBIS)),
                CONF_SAVE_RAW_DATA: user_input.get(CONF_SAVE_RAW_DATA, cfg.get(CONF_SAVE_RAW_DATA, DEFAULT_SAVE_RAW_DATA)),
            }

            if self._step1_cache[CONF_BROKER_MODE] == "external":
                return await self.async_step_external_broker_options()

            return self.async_create_entry(title="", data=self._step1_cache)

        schema = vol.Schema({
            vol.Required(CONF_BROKER_MODE, default=cfg.get(CONF_BROKER_MODE, "homeassistant")): _broker_mode_selector(),
            vol.Required(CONF_SUBSCRIBE, default=cfg.get(CONF_SUBSCRIBE, DEFAULT_TOPIC)): _text_selector(),
            vol.Required(CONF_DEBUG_LOG_COMPONENT, default=cfg.get(CONF_DEBUG_LOG_COMPONENT, DEFAULT_DEBUG_LOG_COMPONENT)): _bool_toggle_selector(),
            vol.Required(CONF_LOG_MISSED_BASE64, default=cfg.get(CONF_LOG_MISSED_BASE64, DEFAULT_LOG_MISSED_BASE64)): _bool_toggle_selector(),
            vol.Required(CONF_LOG_OBIS, default=cfg.get(CONF_LOG_OBIS, DEFAULT_LOG_OBIS)): _bool_toggle_selector(),
            vol.Required(CONF_SAVE_RAW_DATA, default=cfg.get(CONF_SAVE_RAW_DATA, DEFAULT_SAVE_RAW_DATA)): _bool_toggle_selector(),
        })
        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_external_broker_options(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """Options step 2: external broker details."""
        cfg = self._cfg()

        if user_input is not None:
            # Preserve existing password if the field was left empty
            provided_password = user_input.get(CONF_PASSWORD)
            if provided_password in (None, ""):
                if cfg.get(CONF_PASSWORD):
                    user_input[CONF_PASSWORD] = cfg[CONF_PASSWORD]
                else:
                    user_input.pop(CONF_PASSWORD, None)

            self._ext_cache = user_input

            if user_input.get(CONF_TLS):
                return await self.async_step_external_tls_options()

            options = {**self._step1_cache, **self._ext_cache}
            return self.async_create_entry(title="", data=options)

        schema = vol.Schema({
            vol.Required(CONF_HOST, default=cfg.get(CONF_HOST, "")): _text_selector(),
            vol.Required(CONF_PORT, default=cfg.get(CONF_PORT, DEFAULT_PORT)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=65535, mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Optional(CONF_USERNAME, default=cfg.get(CONF_USERNAME, "")): _text_selector(),
            vol.Optional(CONF_PASSWORD): _password_selector(),  # masked input, no default shown
            vol.Optional(CONF_CLIENT_ID, default=cfg.get(CONF_CLIENT_ID, "")): _text_selector(),
            vol.Required(CONF_TLS, default=cfg.get(CONF_TLS, False)): _bool_toggle_selector(),
        })
        return self.async_show_form(step_id="external_broker_options", data_schema=schema)

    async def async_step_external_tls_options(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """Options step 3: TLS parameters for external broker."""
        cfg = self._cfg()

        if user_input is not None:
            options = {**self._step1_cache, **self._ext_cache, **user_input}
            return self.async_create_entry(title="", data=options)

        schema = vol.Schema({
            vol.Optional(CONF_CAFILE, default=cfg.get(CONF_CAFILE, "")): _text_selector(),
            vol.Optional(CONF_CERTFILE, default=cfg.get(CONF_CERTFILE, "")): _text_selector(),
            vol.Optional(CONF_KEYFILE, default=cfg.get(CONF_KEYFILE, "")): _text_selector(),
            vol.Optional(CONF_TLS_VERSION, default=cfg.get(CONF_TLS_VERSION, "tlsv1.2")): _tls_version_selector(),
            vol.Required(CONF_TLS_INSECURE, default=cfg.get(CONF_TLS_INSECURE, False)): _bool_toggle_selector(),
        })
        return self.async_show_form(step_id="external_tls_options", data_schema=schema)

class CloudForwardingSubentryFlow(config_entries.ConfigSubentryFlow):
    """Add or reconfigure Tibber Cloud forwarding for one Pulse.

    The user only has to paste the certificate and private key shown on the Pulse
    setup page. Everything else has a default and sits in a collapsed section.
    """

    def __init__(self) -> None:
        self._pulse_id: Optional[str] = None

    # ------------------------------ helpers -----------------------------------

    def _hub(self):
        return self.hass.data.get(DOMAIN, {}).get(self._get_entry().entry_id)

    def _free_pulses(self, hub) -> Dict[str, Dict[str, Any]]:
        """Discovered Pulse devices that do not have forwarding yet."""
        taken = {
            subentry.unique_id
            for subentry in self._get_entry().get_subentries_of_type(SUBENTRY_TYPE_CLOUD_FORWARDING)
        }
        free: Dict[str, Dict[str, Any]] = {}
        for pulse_id, info in hub.dispatcher.discovered_pulses.items():
            if pulse_id in taken:
                continue
            try:
                receive_topic_for(info["topic"])
                credentials_dir(self.hass.config.path, pulse_id)
            except ValueError:
                continue  # a topic or ID that forwarding cannot be built from
            free[pulse_id] = info
        return free

    @staticmethod
    def _pulse_label(pulse_id: str, status: Dict[str, Any]) -> str:
        model = status.get("hwmodel") or status.get("Hw") or "Pulse"
        return f"{model} ({pulse_id})"

    # ------------------------------- steps ------------------------------------

    async def async_step_user(self, user_input: Optional[Dict[str, Any]] = None):
        """Choose which discovered Pulse to forward."""
        hub = self._hub()
        if hub is None:
            return self.async_abort(reason="entry_not_loaded")
        free = self._free_pulses(hub)
        if not free:
            return self.async_abort(reason="no_pulse_available")

        if user_input is not None:
            self._pulse_id = user_input[CONF_PULSE_ID]
            return await self.async_step_credentials()

        options = [
            selector.SelectOptionDict(
                value=pulse_id, label=self._pulse_label(pulse_id, info["status"])
            )
            for pulse_id, info in free.items()
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_PULSE_ID, default=options[0]["value"]): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options, mode=selector.SelectSelectorMode.DROPDOWN
                    )
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_credentials(self, user_input: Optional[Dict[str, Any]] = None):
        """Paste certificate and private key for the chosen Pulse."""
        hub = self._hub()
        if hub is None:
            return self.async_abort(reason="entry_not_loaded")
        pulse = self._free_pulses(hub).get(self._pulse_id or "")
        if pulse is None:
            return self.async_abort(reason="no_pulse_available")
        return await self._async_credentials_step(
            "credentials", user_input, hub, self._pulse_id, pulse["topic"], {}, None
        )

    async def async_step_reconfigure(self, user_input: Optional[Dict[str, Any]] = None):
        """Replace credentials or change settings of an existing forwarding."""
        hub = self._hub()
        if hub is None:
            return self.async_abort(reason="entry_not_loaded")
        subentry = self._get_reconfigure_subentry()
        return await self._async_credentials_step(
            "reconfigure",
            user_input,
            hub,
            subentry.data[CONF_PULSE_ID],
            subentry.data[CONF_LOCAL_TOPIC],
            dict(subentry.data),
            subentry,
        )

    async def _async_credentials_step(
        self,
        step_id: str,
        user_input: Optional[Dict[str, Any]],
        hub,
        pulse_id: str,
        local_topic: str,
        current: Dict[str, Any],
        subentry,
    ):
        errors: Dict[str, str] = {}
        if user_input is not None:
            outcome = await self._async_save(hub, pulse_id, local_topic, user_input, subentry)
            if isinstance(outcome, str):
                errors["base"] = outcome
            else:
                return outcome

        schema = self._credentials_schema(local_topic, current, optional=subentry is not None)
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id=step_id,
            data_schema=schema,
            errors=errors,
            description_placeholders={"pulse": pulse_id},
        )

    @staticmethod
    def _credentials_schema(local_topic: str, current: Dict[str, Any], optional: bool) -> vol.Schema:
        """Certificate and key on top, everything else in a collapsed section.

        When reconfiguring, certificate and key are optional: leaving both empty keeps
        the stored credentials and only the settings change.
        """
        credential = vol.Optional if optional else vol.Required
        advanced = vol.Schema(
            {
                vol.Optional(CONF_CLOUD_CA): _multiline_text_selector(),
                vol.Required(
                    CONF_CLOUD_PUBLISH_TOPIC,
                    default=current.get(CONF_CLOUD_PUBLISH_TOPIC) or local_topic,
                ): _text_selector(),
                vol.Required(
                    CONF_CLOUD_RECEIVE_TOPIC,
                    default=current.get(CONF_CLOUD_RECEIVE_TOPIC) or receive_topic_for(local_topic),
                ): _text_selector(),
                vol.Required(
                    CONF_CLOUD_HOST, default=current.get(CONF_CLOUD_HOST, DEFAULT_CLOUD_HOST)
                ): _text_selector(),
                vol.Required(
                    CONF_CLOUD_PORT, default=current.get(CONF_CLOUD_PORT, DEFAULT_CLOUD_PORT)
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=65535, mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(
                    CONF_CLOUD_TLS_VERSION,
                    default=current.get(CONF_CLOUD_TLS_VERSION, DEFAULT_CLOUD_TLS_VERSION),
                ): _tls_version_selector(),
            }
        )
        return vol.Schema(
            {
                credential(CONF_CLOUD_CERTIFICATE): _multiline_text_selector(),
                credential(CONF_CLOUD_PRIVATE_KEY): _multiline_text_selector(),
                vol.Required(CONF_ADVANCED): section(advanced, {"collapsed": True}),
            }
        )

    # ------------------------------- saving -----------------------------------

    async def _async_save(
        self,
        hub,
        pulse_id: str,
        local_topic: str,
        user_input: Dict[str, Any],
        subentry,
    ):
        """Validate, test and store. Returns an error key (str) or the flow result."""
        advanced = user_input.get(CONF_ADVANCED) or {}
        certificate = (user_input.get(CONF_CLOUD_CERTIFICATE) or "").strip()
        private_key = (user_input.get(CONF_CLOUD_PRIVATE_KEY) or "").strip()
        ca_cert = (advanced.get(CONF_CLOUD_CA) or "").strip()
        reconfigure = subentry is not None
        new_credentials = bool(certificate or private_key)

        if bool(certificate) != bool(private_key) or (not reconfigure and not new_credentials):
            return "credentials_incomplete"

        default_receive = receive_topic_for(local_topic)
        try:
            host = str(advanced.get(CONF_CLOUD_HOST, DEFAULT_CLOUD_HOST)).strip()
            port = int(advanced.get(CONF_CLOUD_PORT, DEFAULT_CLOUD_PORT))
            publish_topic = str(advanced.get(CONF_CLOUD_PUBLISH_TOPIC) or local_topic).strip()
            receive_topic = str(advanced.get(CONF_CLOUD_RECEIVE_TOPIC) or default_receive).strip()
            tls_version = advanced.get(CONF_CLOUD_TLS_VERSION, DEFAULT_CLOUD_TLS_VERSION)
        except (TypeError, ValueError):
            return "invalid_settings"

        directory = credentials_dir(self.hass.config.path, pulse_id)
        stage: Optional[Path] = None
        if new_credentials:
            try:
                stage = await self.hass.async_add_executor_job(
                    stage_credentials, directory, certificate, private_key, ca_cert or None
                )
            except CertificateError as err:
                return err.reason
            except OSError:
                return "storage_failed"
        source = stage or directory

        try:
            config = CloudForwardingConfig(
                pulse_id=pulse_id,
                local_publish_topic=local_topic,
                cert_path=source / CERT_FILE,
                key_path=source / KEY_FILE,
                ca_path=source / CA_FILE if (source / CA_FILE).is_file() else None,
                host=host,
                port=port,
                tls_version=tls_version,
                aws_publish_topic=publish_topic,
                aws_receive_topic=receive_topic,
            )
        except (TypeError, ValueError):
            if stage is not None:
                await self.hass.async_add_executor_job(discard_staged_credentials, stage)
            return "invalid_settings"

        # The cloud allows one connection per client ID, so a running forwarder has to
        # step aside while the new settings are tested.
        previous_config = None
        was_running = reconfigure and hub.has_forwarder(pulse_id)
        if was_running:
            try:
                previous_config = config_from_subentry(self.hass.config.path, subentry)
            except (KeyError, TypeError, ValueError):
                previous_config = None
            await hub.async_remove_forwarder(pulse_id)

        error = await async_test_connection(self.hass, config)
        if error is not None:
            if stage is not None:
                await self.hass.async_add_executor_job(discard_staged_credentials, stage)
            if previous_config is not None:
                await hub.async_add_forwarder(previous_config)
            return error

        if stage is not None:
            await self.hass.async_add_executor_job(commit_staged_credentials, stage, directory)

        # Only deviations from the defaults are stored, so a changed default reaches everyone.
        data: Dict[str, Any] = {CONF_PULSE_ID: pulse_id, CONF_LOCAL_TOPIC: local_topic}
        if host != DEFAULT_CLOUD_HOST:
            data[CONF_CLOUD_HOST] = host
        if port != DEFAULT_CLOUD_PORT:
            data[CONF_CLOUD_PORT] = port
        if tls_version != DEFAULT_CLOUD_TLS_VERSION:
            data[CONF_CLOUD_TLS_VERSION] = tls_version
        if publish_topic != local_topic:
            data[CONF_CLOUD_PUBLISH_TOPIC] = publish_topic
        if receive_topic != default_receive:
            data[CONF_CLOUD_RECEIVE_TOPIC] = receive_topic

        title = f"Pulse {pulse_id}"
        if not reconfigure:
            return self.async_create_entry(title=title, data=data, unique_id=pulse_id)

        changed = self.hass.config_entries.async_update_subentry(
            self._get_entry(), subentry, title=title, data=data
        )
        if not changed and was_running:
            # Nothing triggers a reload, so start forwarding again with the saved files.
            await hub.async_add_forwarder(config_from_subentry(self.hass.config.path, subentry))
        return self.async_abort(reason="reconfigure_successful")
