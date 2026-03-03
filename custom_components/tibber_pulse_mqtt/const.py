DOMAIN = "tibber_pulse_mqtt"

CONF_BROKER_MODE = "broker_mode"    # "homeassistant" | "external"

# External broker fields
CONF_HOST = "host"
CONF_PORT = "port"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_CLIENT_ID = "client_id"
CONF_TLS = "tls"
CONF_TLS_INSECURE = "tls_insecure"
CONF_CAFILE = "cafile"
CONF_CERTFILE = "certfile"
CONF_KEYFILE = "keyfile"
CONF_TLS_VERSION = "tls_version"

# Topics
CONF_SUBSCRIBE = "subscribe_topic"

# Debug flags
CONF_DEBUG_LOG_COMPONENT = "debug_log_component"
CONF_LOG_MISSED_BASE64 = "log_missed_base64"
CONF_LOG_OBIS = "log_obis"

DEFAULT_DEBUG_LOG_COMPONENT = False
DEFAULT_LOG_MISSED_BASE64 = False
DEFAULT_LOG_OBIS = False

# Defaults
DEFAULT_PORT = 1883
DEFAULT_TLS_PORT = 8883
DEFAULT_TOPIC = "tibber-pulse-+/publish"
DEFAULT_DEBUG = False