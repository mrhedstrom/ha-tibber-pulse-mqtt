# Home Assistant Tibber Pulse (P1, HAN, KM) MQTT integration
Local MQTT integration for Tibber Pulse devices (P1, HAN, KM). Decodes compressed protobuf envelopes, parses OBIS data, and exposes real‑time **native HA sensor entities** in Home Assistant (no MQTT Discovery, no extra topics, no cloud dependencies). Supports multiple Pulse devices. Can also be forwarded to Tibber Cloud via an external MQTT bridge to keep data in both HA and Tibber.

Support development at [![PayPal](https://img.shields.io/badge/PayPal-003087?logo=paypal&logoColor=white)](https://paypal.me/mrhedstrom1)

For Tibber pulse IR devices, have a look at [marq24/ha-tibber-pulse-local](https://github.com/marq24/ha-tibber-pulse-local)

## Features
- Works with Home Assistant MQTT (built-in) or **external broker**
- External broker supports:
  - no auth
  - username/password
  - TLS with CA
  - TLS with client certificate + private key
- Dynamic entity creation: only OBIS codes actually observed are added
- Multiple language translation modules
- Robust binary parser: protobuf wire + zlib

## HACS Installation
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mrhedstrom&repository=ha-tibber-pulse-mqtt)

You can install this integration via **HACS** as a **Custom repository**:

1. Open **HACS → Integrations**.
2. Click **⋯** (overflow menu) → **Custom repositories**.
3. Add repository URL:  
   `https://github.com/mrhedstrom/ha-tibber-pulse-mqtt`  
   Category: **Integration**
4. Click **Add**.
5. Search for **Tibber Pulse MQTT** in HACS and **Install**.
6. Restart Home Assistant.
7. Configure **Tibber Pulse MQTT** by adding integration in **Settings → Devises and Services** and press **Add integration**, search for **Tibber Pulse MQTT**
8. The entities will appear automatically when your Pulse starts publishing to the configured MQTT topic. Devices will not be created until the first Status update from tibber pulse. This typically should happen within the first minute.

> HACS docs: https://hacs.xyz  
> HA Custom Repositories: https://hacs.xyz/docs/faq/custom_repositories

## Manual Installation (without HACS)
Copy the `custom_components/tibber_pulse_mqtt` folder into your HA `config/custom_components/` directory, then restart Home Assistant.

## Point tibber pulse to local Mosquitto ##
If you would like to send the messages to Tibber cloud continue reading next chapter.

If you are happy with only local mqtt follow these simple steps.  
After resetting tibber pulse by holding the side button for 5 seconds tibber will start a WiFi Access Point. Connect to it with the password printed on the back of the pulse meter. Navigate to http://10.133.70.1 and setup tibber pulse to connect to your local mqtt broker. If you are using HA built-in Mosquitto, it is reccomended to first create a HA user for the Pulse device.

## Configure MQTT Bridge (Pulse → AWS via local Mosquitto)
To keep Tibber app functionality and Tibber integrations for load balancing car chargers etc. and support firmware updates, it is recommended to forward mesages both to and from Tibber cloud (AWS iot).
We **recommend** the documented method by **MSkjel/LocalPulse2Tibber** to extract the pulse certificates and setting up a mqtt bridge from local mqtt to Tibber cloud.  
Credit and reference: https://github.com/MSkjel/LocalPulse2Tibber

Extract your tibber pulse device certificates (CA.ca, Cert.crt, Priv.key) and save as files in `/share/mosquitto/tibber_cert`  
Example `/share/mosquitto/bridge.conf` for two way mqtt bridge:

```properties
connection bridge-to-tibber
bridge_cafile /share/mosquitto/tibber_cert/CA.ca
bridge_certfile /share/mosquitto/tibber_cert/Cert.crt
bridge_keyfile /share/mosquitto/tibber_cert/Priv.key
bridge_tls_version tlsv1.2
bridge_insecure false
bridge_protocol_version mqttv311
address a1zhmn1192zl1a.iot.eu-west-1.amazonaws.com:8883
clientid tibber-pulse-<your device id>
# Replace tibber-pulse-<your device id> with your tibber pulse client id
try_private false
notifications false
restart_timeout 5
round_robin false
cleansession true

# OUT: local → AWS
topic tibber-pulse-<your device id>/publish out 1
# Replace tibber-pulse-<your device id> with your tibber pulse client id

# IN: AWS → local (Important for firmware updates etc. from tibber app)
topic tibber-pulse-<your device id>/receive in 1
# Replace tibber-pulse-<your device id> with your tibber pulse client id
```

## Topics
By default, this integration subscribes to: `tibber-pulse-+/publish`  

This is an extended wildcard pattern that matches **all Tibber Pulse devices**, regardless of their individual device ID.  

Examples of topics captured by this pattern:

> tibber-pulse-1029a71625514301a8d5aa2c6ec0f84a/publish  
> tibber-pulse-2029a71625514301a8d5aa2c6ec0f84b/publish  
> tibber-pulse-3029a71625514301a8d5aa2c6ec0f84c/publish

Normally, MQTT does **not** allow wildcards inside a topic level (for example, `tibber-pulse-+/publish` cannot be subscribed directly by the broker).  
However, this integration adds **extended wildcard support**, meaning:

- A broader valid MQTT topic is used for the actual subscription.
- Incoming messages are then **locally filtered** so that only topics matching the original pattern (`tibber-pulse-+/publish`) are processed.

This allows the integration to automatically detect and receive messages from *any* Tibber Pulse device without manually specifying the device ID.

### Performance Note
If you specify the **exact device ID** (e.g. `tibber-pulse-<your device id>/publish`) instead of using a wildcard, the integration can skip the local filtering step.  
This reduces CPU usage slightly and may be beneficial on low‑power systems.

You can adjust the topic pattern in the integration options if needed.

## Multiple devices
Each Pulse unit becomes a distinct Device in HA.  
Entity IDs are of the form:
```conf
sensor.tibber_<deviceid>_<obis_code_slug>
```
## Translations
Currently there are translations for all main languages in the countries where Tibber Pulse is sold. They have been generated with AI since developers don't speak them all. If you find something wrong with translations let us know.

Selected language follows HA global settings. If your HA language is not supported, English will be the default language.

Supported languages

- Svenska
- English
- Norsk
- Suomi
- Dansk
- Nederlands
- Deutsch

## Protobuf
We use the official protobuf library to parse wire format generically and extract the compressed payload. An experimental pulse.proto is included for reference; the integration does not depend on a compiled .pb2 file at runtime.

## Notes
If your device emits zlib-compressed OBIS text, it will be parsed as such.
If your device uses a proprietary binary layout after zlib, a fallback parser is included but not used; please share sample frames to improve decoding tables. This has only been developed using Pulse P1 and has not seen other models messages. The models P1, HAN, KM, should follow the same protocols since they are in the same product family and share common hardware.

## Credits
Tibber Pulse community work and formats  
MSkjel/LocalPulse2Tibber for the clear AWS bridge configuration and cert extraction guidance https://github.com/MSkjel/LocalPulse2Tibber
