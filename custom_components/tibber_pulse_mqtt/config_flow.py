from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any, Dict, Optional
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    CONF_ENTRY_TYPE,
    ENTRY_TYPE_AWS_FORWARDING,
    CONF_SOURCE_ENTRY_ID,
    CONF_DEVICE_ID,
    CONF_PUBLISH_TOPIC,
    CONF_RECEIVE_TOPIC,
    CONF_AWS_ENDPOINT,
    CONF_AWS_CERT_PATH,
    CONF_AWS_KEY_PATH,
    CONF_AWS_CA_PATH,
    CONF_AWS_CONFIGURED,
    CONF_AWS_PUBLISH_TOPIC_OVERRIDE,
    CONF_AWS_RECEIVE_TOPIC_OVERRIDE,
    CONF_TEST_CONNECTION,
    AWS_UNCONFIGURED_ISSUE_PREFIX,
    AWS_INVALID_CERT_ISSUE_PREFIX,
    AWS_CONNECTION_ISSUE_PREFIX,
    DEFAULT_AWS_ENDPOINT,
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
from .aws import (
    certificate_directory,
    commit_staged_certificate_material,
    discard_staged_certificate_material,
    parse_aws_endpoint,
    receive_topic_for_publish,
    stage_certificate_material,
    test_aws_connection,
    validate_device_id,
    validate_mqtt_topic,
    validate_certificate_files,
)
from homeassistant.helpers import issue_registry as ir

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
    """Multiline selector for PEM material pasted by the user."""
    return selector.TextSelector(
        selector.TextSelectorConfig(
            type=selector.TextSelectorType.TEXT,
            multiline=True,
        )
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
    VERSION = 2

    async def async_step_integration_discovery(self, user_input=None) -> FlowResult:
        """Create a persistent, device-linked AWS forwarding entry after STATUS discovery."""
        device_id = self.context.get(CONF_DEVICE_ID, "")
        publish_topic = self.context.get(CONF_PUBLISH_TOPIC, "")
        try:
            device_id = validate_device_id(str(device_id))
            receive_topic = receive_topic_for_publish(str(publish_topic))
        except ValueError:
            return self.async_abort(reason="invalid_discovery")

        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured()

        cert_dir = certificate_directory(self.hass, device_id)
        return self.async_create_entry(
            title=f"Tibber Pulse {device_id} AWS forwarding",
            data={
                CONF_ENTRY_TYPE: ENTRY_TYPE_AWS_FORWARDING,
                CONF_SOURCE_ENTRY_ID: self.context.get(CONF_SOURCE_ENTRY_ID),
                CONF_DEVICE_ID: device_id,
                CONF_PUBLISH_TOPIC: publish_topic,
                CONF_RECEIVE_TOPIC: receive_topic,
                CONF_AWS_ENDPOINT: DEFAULT_AWS_ENDPOINT,
                CONF_AWS_CERT_PATH: str(cert_dir / "cert.pem"),
                CONF_AWS_KEY_PATH: str(cert_dir / "key.pem"),
                CONF_AWS_CA_PATH: str(cert_dir / "ca.pem"),
            },
        )

    async def async_step_reconfigure(
        self,
        user_input: Optional[Dict[str, Any]] = None,
    ) -> FlowResult:
        """Configure or rotate the device's client certificate at any time."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            device_id = entry.data[CONF_DEVICE_ID]
            cert_dir = certificate_directory(self.hass, device_id)
            stage_dir = None
            try:
                stage_dir = await self.hass.async_add_executor_job(
                    stage_certificate_material,
                    cert_dir,
                    user_input["certificate"],
                    user_input["private_key"],
                    user_input.get("ca_certificate") or None,
                )
                cfg = {**entry.data, **entry.options}
                parse_aws_endpoint(
                    cfg.get(CONF_AWS_ENDPOINT, DEFAULT_AWS_ENDPOINT)
                )
                forwarder = self.hass.data.get(DOMAIN, {}).get(
                    "_aws_forwarders", {}
                ).get(entry.entry_id)
                if forwarder:
                    await forwarder.async_test_connection(
                        cfg.get(CONF_AWS_ENDPOINT, DEFAULT_AWS_ENDPOINT),
                        stage_dir / "cert.pem",
                        stage_dir / "key.pem",
                        stage_dir / "ca.pem" if (stage_dir / "ca.pem").exists() else None,
                    )
                else:
                    await self.hass.async_add_executor_job(
                        test_aws_connection,
                        cfg.get(CONF_AWS_ENDPOINT, DEFAULT_AWS_ENDPOINT),
                        device_id,
                        stage_dir / "cert.pem",
                        stage_dir / "key.pem",
                        stage_dir / "ca.pem" if (stage_dir / "ca.pem").exists() else None,
                    )
                await self.hass.async_add_executor_job(
                    commit_staged_certificate_material,
                    stage_dir,
                    cert_dir,
                )
            except ValueError:
                errors["base"] = "invalid_certificate"
            except (TimeoutError, ConnectionError, OSError, ssl.SSLError):
                errors["base"] = "cannot_connect"
            finally:
                if stage_dir:
                    await self.hass.async_add_executor_job(
                        discard_staged_certificate_material,
                        stage_dir,
                    )

            if not errors:
                for prefix in (
                    AWS_UNCONFIGURED_ISSUE_PREFIX,
                    AWS_INVALID_CERT_ISSUE_PREFIX,
                    AWS_CONNECTION_ISSUE_PREFIX,
                ):
                    ir.async_delete_issue(
                        self.hass,
                        DOMAIN,
                        f"{prefix}{device_id}",
                    )
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_AWS_CONFIGURED: True},
                )

        schema = vol.Schema(
            {
                vol.Required("certificate"): _multiline_text_selector(),
                vol.Required("private_key"): _multiline_text_selector(),
                vol.Optional("ca_certificate"): _multiline_text_selector(),
            }
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
        )

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
        if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_AWS_FORWARDING:
            return TibberAWSOptionsFlow(entry)
        return TibberLocalOptionsFlow(entry)


class TibberAWSOptionsFlow(config_entries.OptionsFlow):
    """Permanent advanced settings and connection-test controls for one Pulse."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self.entry = entry
        self._form_values: dict[str, Any] = {}

    def _cfg(self) -> Dict[str, Any]:
        return _effective_config(self.entry)

    def _schema(self) -> vol.Schema:
        cfg = self._cfg()
        values = {**cfg, **self._form_values}
        return vol.Schema(
            {
                vol.Optional(
                    CONF_AWS_ENDPOINT,
                    default=values.get(CONF_AWS_ENDPOINT, DEFAULT_AWS_ENDPOINT),
                ): _text_selector(),
                vol.Optional(
                    CONF_AWS_PUBLISH_TOPIC_OVERRIDE,
                    default=values.get(CONF_AWS_PUBLISH_TOPIC_OVERRIDE, ""),
                ): _text_selector(),
                vol.Optional(
                    CONF_AWS_RECEIVE_TOPIC_OVERRIDE,
                    default=values.get(CONF_AWS_RECEIVE_TOPIC_OVERRIDE, ""),
                ): _text_selector(),
                vol.Optional(CONF_TEST_CONNECTION, default=False): _bool_toggle_selector(),
            }
        )

    async def async_step_init(
        self,
        user_input: Optional[Dict[str, Any]] = None,
    ) -> FlowResult:
        return await self.async_step_aws_forwarding(user_input)

    async def async_step_aws_forwarding(
        self,
        user_input: Optional[Dict[str, Any]] = None,
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._form_values = dict(user_input)
            endpoint = user_input.get(CONF_AWS_ENDPOINT) or DEFAULT_AWS_ENDPOINT
            publish_override = user_input.get(CONF_AWS_PUBLISH_TOPIC_OVERRIDE, "").strip()
            receive_override = user_input.get(CONF_AWS_RECEIVE_TOPIC_OVERRIDE, "").strip()

            try:
                parse_aws_endpoint(endpoint)
                if publish_override:
                    validate_mqtt_topic(publish_override)
                    receive_topic_for_publish(publish_override)
                if receive_override:
                    validate_mqtt_topic(receive_override)
            except ValueError:
                errors["base"] = "invalid_advanced_setting"

            if not errors and user_input.get(CONF_TEST_CONNECTION):
                cfg = self._cfg()
                cert_path = cfg.get(CONF_AWS_CERT_PATH)
                key_path = cfg.get(CONF_AWS_KEY_PATH)
                ca_path = cfg.get(CONF_AWS_CA_PATH)
                if not cert_path or not key_path:
                    errors["base"] = "missing_certificate"
                else:
                    try:
                        await self.hass.async_add_executor_job(
                            validate_certificate_files,
                            cert_path,
                            key_path,
                            ca_path if ca_path and Path(ca_path).is_file() else None,
                        )
                        forwarder = self.hass.data.get(DOMAIN, {}).get(
                            "_aws_forwarders", {}
                        ).get(self.entry.entry_id)
                        if forwarder:
                            await forwarder.async_test_connection(
                                endpoint,
                                cert_path,
                                key_path,
                                ca_path if ca_path and Path(ca_path).is_file() else None,
                            )
                        else:
                            await self.hass.async_add_executor_job(
                                test_aws_connection,
                                endpoint,
                                cfg[CONF_DEVICE_ID],
                                cert_path,
                                key_path,
                                ca_path if ca_path and Path(ca_path).is_file() else None,
                            )
                        errors["base"] = "connection_test_succeeded"
                    except (TimeoutError, ConnectionError, OSError, ssl.SSLError):
                        errors["base"] = "cannot_connect"
                    except ValueError:
                        errors["base"] = "invalid_certificate"
                return self.async_show_form(
                    step_id="aws_forwarding",
                    data_schema=self._schema(),
                    errors=errors,
                )

            if not errors:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_AWS_ENDPOINT: endpoint,
                        CONF_AWS_PUBLISH_TOPIC_OVERRIDE: publish_override,
                        CONF_AWS_RECEIVE_TOPIC_OVERRIDE: receive_override,
                    },
                )

        return self.async_show_form(
            step_id="aws_forwarding",
            data_schema=self._schema(),
            errors=errors,
        )


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