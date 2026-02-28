from __future__ import annotations

from typing import Any, Dict, Optional
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    # Core
    CONF_BROKER_MODE,
    CONF_LANGUAGE,
    CONF_SUBSCRIBE,
    DEFAULT_LANGUAGE,
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
    DEFAULT_DEBUG_LOG_COMPONENT,
    DEFAULT_LOG_MISSED_BASE64,
    DEFAULT_LOG_OBIS,
)

# Select option lists (labels shown, values stored)
BROKER_MODES = [
    selector.SelectOptionDict(label="Home Assistant MQTT", value="homeassistant"),
    selector.SelectOptionDict(label="External MQTT", value="external"),
]

LANG_OPTIONS = [
    selector.SelectOptionDict(label="Dansk", value="da"),
    selector.SelectOptionDict(label="Deutsch", value="de"),
    selector.SelectOptionDict(label="English", value="en"),
    selector.SelectOptionDict(label="Nederlands", value="nl"),
    selector.SelectOptionDict(label="Norsk", value="no"),
    selector.SelectOptionDict(label="Svenska", value="sv")
]

TLS_VERSION_OPTIONS = [
    selector.SelectOptionDict(label="TLS 1.2", value="tlsv1.2"),
    selector.SelectOptionDict(label="TLS 1.3", value="tlsv1.3"),
]

# Allowed languages (2-letter codes) used by this integration
ALLOWED_LANGS = {"sv", "en", "no", "da", "nl", "de"}


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


def _language_selector() -> selector.SelectSelector:
    """Dropdown for language (labels shown, codes stored)."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=LANG_OPTIONS, mode=selector.SelectSelectorMode.DROPDOWN
        )
    )


def _tls_version_selector() -> selector.SelectSelector:
    """Dropdown for TLS version."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=TLS_VERSION_OPTIONS, mode=selector.SelectSelectorMode.DROPDOWN
        )
    )


def _normalize_lang(code: Optional[str]) -> Optional[str]:
    """Normalize language code to 2-letter form used by this integration."""
    if not code:
        return None
    code = code.lower()
    # Typical forms: "sv", "sv-se", "en", "en-us"
    two = code.split("-")[0]
    # Map HA's Norwegian Bokmål 'nb' to 'no' used by this integration
    if two == "nb":
        two = "no"
    return two


def _default_language_from_ha(hass) -> str:
    """Return default language from HA's general language setting, normalized and validated."""
    ha_lang = _normalize_lang(getattr(hass.config, "language", None))
    if ha_lang in ALLOWED_LANGS:
        return ha_lang
    # Fallback if HA language is not in our allowed options
    return DEFAULT_LANGUAGE


class TibberLocalConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Initial configuration flow for the integration."""
    VERSION = 1

    async def async_step_user(self, user_input: Optional[Dict[str, Any]] = None) -> FlowResult:
        """Step where the user configures broker mode, topic/language and debug flags."""
        if user_input is not None:
            # Normalize dependent debug flags: when master debug is OFF, force dependents to False
            debug_enabled = bool(user_input.get(CONF_DEBUG_LOG_COMPONENT, DEFAULT_DEBUG_LOG_COMPONENT))
            if not debug_enabled:
                user_input[CONF_LOG_MISSED_BASE64] = False
                user_input[CONF_LOG_OBIS] = False

            # Use HA language as default if the user didn't explicitly select a language
            selected_lang = user_input.get(CONF_LANGUAGE, _default_language_from_ha(self.hass))
            broker_mode = user_input.get(CONF_BROKER_MODE, "homeassistant")

            if broker_mode == "homeassistant":
                data = {
                    CONF_BROKER_MODE: "homeassistant",
                    CONF_SUBSCRIBE: user_input.get(CONF_SUBSCRIBE, DEFAULT_TOPIC),
                    CONF_LANGUAGE: selected_lang,
                    CONF_DEBUG_LOG_COMPONENT: debug_enabled,
                    CONF_LOG_MISSED_BASE64: user_input.get(CONF_LOG_MISSED_BASE64, DEFAULT_LOG_MISSED_BASE64),
                    CONF_LOG_OBIS: user_input.get(CONF_LOG_OBIS, DEFAULT_LOG_OBIS),
                }
                return self.async_create_entry(title="Tibber Pulse MQTT (HA MQTT)", data=data)

            # For external broker, cache step 1 values and continue
            self._cached_step1 = {
                CONF_BROKER_MODE: "external",
                CONF_SUBSCRIBE: user_input.get(CONF_SUBSCRIBE, DEFAULT_TOPIC),
                CONF_LANGUAGE: selected_lang,
                CONF_DEBUG_LOG_COMPONENT: debug_enabled,
                CONF_LOG_MISSED_BASE64: user_input.get(CONF_LOG_MISSED_BASE64, DEFAULT_LOG_MISSED_BASE64),
                CONF_LOG_OBIS: user_input.get(CONF_LOG_OBIS, DEFAULT_LOG_OBIS),
            }
            return await self.async_step_external_broker()

        # Initial form schema (language default comes from HA setting)
        schema = vol.Schema({
            vol.Required(CONF_BROKER_MODE, default="homeassistant"): _broker_mode_selector(),
            vol.Required(CONF_SUBSCRIBE, default=DEFAULT_TOPIC): _text_selector(),
            vol.Required(CONF_LANGUAGE, default=_default_language_from_ha(self.hass)): _language_selector(),
            vol.Required(CONF_DEBUG_LOG_COMPONENT, default=DEFAULT_DEBUG_LOG_COMPONENT): _bool_toggle_selector(),
            vol.Required(CONF_LOG_MISSED_BASE64, default=DEFAULT_LOG_MISSED_BASE64): _bool_toggle_selector(),
            vol.Required(CONF_LOG_OBIS, default=DEFAULT_LOG_OBIS): _bool_toggle_selector(),
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

            # Prefer explicitly chosen language; otherwise fall back to HA default
            selected_lang = user_input.get(CONF_LANGUAGE, cfg.get(CONF_LANGUAGE, _default_language_from_ha(self.hass)))

            self._step1_cache = {
                CONF_BROKER_MODE: user_input.get(CONF_BROKER_MODE, cfg.get(CONF_BROKER_MODE, "homeassistant")),
                CONF_SUBSCRIBE: user_input.get(CONF_SUBSCRIBE, cfg.get(CONF_SUBSCRIBE, DEFAULT_TOPIC)),
                CONF_LANGUAGE: selected_lang,
                CONF_DEBUG_LOG_COMPONENT: debug_enabled,
                CONF_LOG_MISSED_BASE64: user_input.get(CONF_LOG_MISSED_BASE64, cfg.get(CONF_LOG_MISSED_BASE64, DEFAULT_LOG_MISSED_BASE64)),
                CONF_LOG_OBIS: user_input.get(CONF_LOG_OBIS, cfg.get(CONF_LOG_OBIS, DEFAULT_LOG_OBIS)),
            }

            if self._step1_cache[CONF_BROKER_MODE] == "external":
                return await self.async_step_external_broker_options()

            return self.async_create_entry(title="", data=self._step1_cache)

        # Default for the options form: entry value > HA default
        default_lang = cfg.get(CONF_LANGUAGE, _default_language_from_ha(self.hass))

        schema = vol.Schema({
            vol.Required(CONF_BROKER_MODE, default=cfg.get(CONF_BROKER_MODE, "homeassistant")): _broker_mode_selector(),
            vol.Required(CONF_SUBSCRIBE, default=cfg.get(CONF_SUBSCRIBE, DEFAULT_TOPIC)): _text_selector(),
            vol.Required(CONF_LANGUAGE, default=default_lang): _language_selector(),
            vol.Required(CONF_DEBUG_LOG_COMPONENT, default=cfg.get(CONF_DEBUG_LOG_COMPONENT, DEFAULT_DEBUG_LOG_COMPONENT)): _bool_toggle_selector(),
            vol.Required(CONF_LOG_MISSED_BASE64, default=cfg.get(CONF_LOG_MISSED_BASE64, DEFAULT_LOG_MISSED_BASE64)): _bool_toggle_selector(),
            vol.Required(CONF_LOG_OBIS, default=cfg.get(CONF_LOG_OBIS, DEFAULT_LOG_OBIS)): _bool_toggle_selector(),
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