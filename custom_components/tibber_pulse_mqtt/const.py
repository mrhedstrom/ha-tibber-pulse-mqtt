DOMAIN = "tibber_pulse_mqtt"

CONF_ENTRY_TYPE = "entry_type"
ENTRY_TYPE_AWS_FORWARDING = "aws_forwarding"
CONF_SOURCE_ENTRY_ID = "source_entry_id"
CONF_DEVICE_ID = "device_id"
CONF_PUBLISH_TOPIC = "publish_topic"
CONF_RECEIVE_TOPIC = "receive_topic"
CONF_AWS_ENDPOINT = "aws_endpoint"
CONF_AWS_CERT_PATH = "aws_cert_path"
CONF_AWS_KEY_PATH = "aws_key_path"
CONF_AWS_CA_PATH = "aws_ca_path"
CONF_AWS_CONFIGURED = "aws_configured"
CONF_AWS_PUBLISH_TOPIC_OVERRIDE = "aws_publish_topic_override"
CONF_AWS_RECEIVE_TOPIC_OVERRIDE = "aws_receive_topic_override"
CONF_TEST_CONNECTION = "test_connection"
CONF_AWS_FORWARDING_STATE = "aws_forwarding_state"
AWS_FORWARDING_NOT_CONFIGURED = "not_configured"
AWS_FORWARDING_CONNECTING = "connecting"
AWS_FORWARDING_CONNECTED = "connected"
AWS_FORWARDING_ERROR = "error"
AWS_UNCONFIGURED_ISSUE_PREFIX = "aws_forwarding_unconfigured_"
AWS_INVALID_CERT_ISSUE_PREFIX = "aws_forwarding_certificate_invalid_"
AWS_CONNECTION_ISSUE_PREFIX = "aws_forwarding_connection_"

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
CONF_SAVE_RAW_DATA = "save_raw_data"

DEFAULT_DEBUG_LOG_COMPONENT = False
DEFAULT_LOG_MISSED_BASE64 = False
DEFAULT_LOG_OBIS = False
DEFAULT_SAVE_RAW_DATA = False

# Defaults
DEFAULT_PORT = 1883
DEFAULT_TLS_PORT = 8883
DEFAULT_TOPIC = "tibber-pulse-+/publish"
DEFAULT_DEBUG = False
DEFAULT_AWS_ENDPOINT = "a1zhmn1192zl1a.iot.eu-west-1.amazonaws.com:8883"

# Constants for raw capture management
RAW_CAPTURE_DIR = "www/tibber_pulse_mqtt/raw"
RAW_CAPTURE_API_URL = "/api/tibber_pulse_mqtt/raw_capture"
CAPTURE_RETENTION_DAYS = 10
CAPTURE_WARNING_FILES = 5
CAPTURE_WARNING_SIZE_MB = 25