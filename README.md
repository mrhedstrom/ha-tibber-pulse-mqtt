# Tibber Pulse MQTT for Home Assistant (P1, HAN, KM)
Tibber Pulse MQTT is a local Home Assistant integration for Tibber Pulse devices (P1, HAN, KM). 

It decodes compressed protobuf envelopes, parses OBIS data, and exposes real-time **native Home Assistant sensors**. No MQTT Discovery, extra topics, **or cloud dependencies** are required.

The integration supports multiple Pulse devices and can optionally forward data to Tibber Cloud through an external MQTT bridge, making data available in both Home Assistant and Tibber.

> [!WARNING]
> This is an unofficial integration and is not affiliated with or supported by Tibber.

For Tibber Pulse IR devices, see [marq24/ha-tibber-pulse-local](https://github.com/marq24/ha-tibber-pulse-local)

## Features
- Works with Home Assistant MQTT (built-in) or **external broker**
- External broker supports:
  - no auth
  - username/password
  - TLS with CA
  - TLS with client certificate + private key
- Dynamic entity creation: only OBIS codes actually observed are added
- Multiple language translation modules
- Robust binary parsers:
  - Protobuf + zlib (P1 / DSMR)
  - DLMS/COSEM DataNotification (HAN meters)

## HACS Installation
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mrhedstrom&repository=ha-tibber-pulse-mqtt)

This integration is available as an official HACS integration.

1. Open HACS in Home Assistant
2. Go to Integrations
3. Click Explore & Download Repositories
4. Search for Tibber Pulse MQTT
5. Click Install
6. Restart Home Assistant

> HACS docs: https://hacs.xyz  

## Manual Installation (without HACS)
Use this method only if you do not want to use HACS.
1. Download the latest release from GitHub  
   https://github.com/mrhedstrom/ha-tibber-pulse-mqtt/releases
2. Copy the `custom_components/tibber_pulse_mqtt` folder into your HA `config/custom_components/` directory.
3. Restart Home Assistant

## Configure MQTT Bridge (Pulse → AWS via local Mosquitto)
To keep full Tibber functionality — including the Tibber app, firmware updates, and Tibber cloud–based features such as load balancing for EV chargers — the Tibber Pulse must continue communicating with Tibber Cloud (AWS IoT).

This can be achieved by configuring:  

  * Tibber Pulse → local MQTT (Mosquitto)
  * Mosquitto → Tibber Cloud (AWS IoT) via an MQTT bridge

This setup allows you to use the Pulse locally in Home Assistant, while still forwarding all required traffic to and from Tibber Cloud.

With this setup, the Pulse publishes data locally (for Home Assistant) while all required traffic is transparently forwarded to and from Tibber Cloud. 

If you don't need this functionality you can skip this section and continue in [Configure Tibber Pulse to use a local Mosquitto broker](#configure-tibber-pulse-to-use-a-local-mosquitto-broker)

### Architecture Overview
```
Tibber Pulse
     │
     ▼
Local Mosquitto ──▶ Tibber Cloud (AWS IoT)
     ▲                     │
     └──────────── receive ◀┘
```

Both outgoing (Pulse → AWS) and incoming (AWS → Pulse) traffic must be bridged.  
Incoming messages are required for firmware updates and remote control from the Tibber app.

### Step 1 – Extract Tibber Pulse Certificates
Each Tibber Pulse uses device‑specific TLS certificates to authenticate against Tibber Cloud.  
These certificates must be extracted and later reused by Mosquitto when setting up the MQTT bridge.

The following method extracts the certificates by intercepting the configuration sent from the Tibber app.

The required files are:

  * CA.ca (Certificate Authority)
  * Cert.crt (client certificate)
  * Priv.key (private key)

#### Procedure
1. Reset the Tibber Pulse  
   Press and hold the reset button on the Pulse for approximately 5 seconds until it resets.
2. Disconnect the Pulse Power‑Up in the Tibber app (if previously connected)  
   In the Tibber app:  
   * Go to Power‑Ups
   * Select Pulse
   * Choose Disconnect
3. Start Pulse setup in the Tibber app  
   * Open the Tibber app  
   * Begin setting up the Pulse as usual  
   When prompted for your Wi‑Fi password, intentionally **enter the wrong password**.
4. Wait for Wi‑Fi error  
   The app should fail with a Wi‑Fi error.  
5. Force‑quit the Tibber app  
   Fully close the app (do not leave it running in the background).
6. Connect to the Tibber Pulse Wi‑Fi access point  
   After reset, the Pulse will start its own Wi‑Fi access point.  
     * Connect to it using the Wi‑Fi password printed on the back of the Pulse device (include the dash - in the password).
7. Open the pulse configuration page  
   Navigate to: <a href="http://10.133.70.1" target="_blank">http://10.133.70.1</a>  
   The page now contains all configuration data sent from the Tibber app, including the intentionally incorrect Wi‑Fi password.
8. Extract certificates  
   From the web interface, locate and copy the full contents of the following fields:
   
   * ca_cert → save as CA.ca
   * certificate → save as Cert.crt
   * private_key → save as Priv.key
   
   Make sure to copy everything, including:
   ```
   -----BEGIN CERTIFICATE-----
   …
   -----END CERTIFICATE-----
   ```
9. Save MQTT connection details (important)  
   Copy and save the following fields to a file for later reference (for example mqtt_info.txt):
   * mqtt_url
   * mqtt_topic
   * mqtt_topic_sub
   * update_url
10. Correct the Wi‑Fi password  
    Replace the incorrect Wi‑Fi password with the correct password for your network
11. Apply configuration  
   Click Send
   Then click Apply
12. Verify successful configuration  
    After a short while, the page should change to an almost empty screen with a short string of characters at the top.   
    This indicates success.  
    If the page shows WiFiErr or MQTTErr, you must repeat the process from the beginning.
13. Verify Pulse presence in the Tibber app  
    Open the Tibber app  
    Confirm that the Pulse appears on the main screen  
    It can take a few minutes for the tibber pulse to show up in the app  
    It may not show any data yet — this is expected.  
    If it does not appear, force‑quit and restart the app.

#### Final note step 1
At this point you should have:  

* Working Pulse ↔ Tibber app pairing
* Extracted device TLS certificates
* Saved MQTT connection details

You can now proceed to Step 2 – Configure Mosquitto MQTT Bridge.

### Step 2 – Configure Mosquitto MQTT Bridge
1. Make sure you have MQTT addon installed
2. You can use any file editor in home assistant. For example **Open Studio Code Server**.
3. Make sure your working folder is `/root/`  
   In **Open Studio Code Server** this is done by selecting File → Open Folder
4. Save certificate files to Homeassistant into folder `/share/mosquitto/tibber_cert/`  
   * CA.ca
   * Cert.crt
   * Priv.key
5. Create or edit a Mosquitto bridge configuration file, for example:  
   `/share/mosquitto/bridge.conf`  
   Example bridge configuration (two‑way):

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
    > **Important**  
    Replace <your device id> everywhere with the actual Tibber Pulse client ID found in your extracted mqtt_topic.  
    Make sure address in the configuration matches your extracted mqtt_url
6. After saving bridge.conf:   
   * Restart the Mosquitto broker
   * Verify in the logs that:
     * The bridge connects successfully
     * TLS handshake succeeds
     * No authorization errors are reported

Now everything is setup to forward pulse data from local Mosquitto to Tibber Cloud. Next step is to [Configure Tibber Pulse to use the local Mosquitto broker](#configure-tibber-pulse-to-use-a-local-mosquitto-broker)

## Configure Tibber Pulse to use a local Mosquitto broker
If you want to send data to the Tibber cloud, first follow the instructions in [Configure MQTT Bridge (Pulse → AWS via local Mosquitto)](#configure-mqtt-bridge-pulse--aws-via-local-mosquitto), then continue with the following steps.

If you only need local MQTT, follow only the steps below to configure Tibber Pulse to publish directly to your local MQTT broker (for example Home Assistant Mosquitto).

> ### Create a dedicated Home Assistant user for Tibber Pulse (recommended)
> When using Home Assistant’s built‑in Mosquitto broker, it is recommended to create a dedicated > > > local user for the Tibber Pulse instead of reusing your own account credentials.
> This improves security and makes it easier to manage or revoke access later.
> #### Steps to create local user
> 1. In Home Assistant, go to:  
>    Settings → People → Users
> 2. Click Add User  
> 3. Fill in the user details:  
>    Name: tibber_pulse (or similar)  
>    Username: tibber_pulse  
>    Password: Choose a strong password  
>    Can only log in locally: Enabled (recommended)
> 4. Click Create
> 5. (Optional but recommended)
>    * Open the newly created user  
>    * Set User type to Normal user  
>    * Do not grant administrator privileges

### Steps to Configure Tibber Pulse to use a local Mosquitto broker

1. Reset the Tibber Pulse  
   Press and hold the side button on the Tibber Pulse for 5 seconds until it resets.  
   **DO NOT** disconnect the Pulse Power-Up from the Tibber app this time.
2. Connect to the Tibber Pulse Wi‑Fi access point  
   After reset, the Pulse will start its own Wi‑Fi access point.  
     * Connect to it using the Wi‑Fi password printed on the back of the Pulse device (include the dash - in the password).
3. Open the pulse configuration page  
   Navigate to:  <a href="http://10.133.70.1" target="_blank">http://10.133.70.1</a>  
4. Configure MQTT settings  
   Set up the Pulse to connect to your local MQTT broker without tibber certificates.
   
   * Enter **ssid** for your WiFi network
   * Enter **psk** for yor WiFi network
   * The credentials for your local mqtt user can be included directly in the **mqtt_url**, for example:  
     `tibber_pulse:<password>@homeassistant.local`
   * Select **mqtt_port** (default 1883 normal or 8883 for ssl)
   * Set **mqtt_topic**  
     If using bridge make sure to use your device topic that should be sent to tibber cloud, for example:  
     `tibber-pulse-<your device id>/publish`  
   * Set **mqtt_topic_sub**  
     If using bridge make sure to use your device topic that tibber cloud will use, for example:  
     `tibber-pulse-<your device id>/receive`
   * For firmware updates, enter your saved **update_url**, for example:  
     `https://iot.tibber.com/devices/<your device id>/firmware`
5. Save and reboot  
   Save the configuration and allow the Tibber Pulse to reboot and connect to your local network.
12. Verify successful configuration  
    After a short while, the page should change to an almost empty screen with a short string of characters at the top.   
    This indicates success.
13. On success you will be disconnected from the pulse wifi.
14. Verify Pulse values in the Tibber app  
    Open the Tibber app  
    It can take a few minutes for the tibber pulse to start showing data in the app

## Configure Tibber Pulse integration

Before setting up the integration follow the steps to [configure tibber pulse to send updates to local MQTT broker](#configure-mqtt-bridge-pulse--aws-via-local-mosquitto).

After restarting Home Assistant and configured pulse to send mqtt to local broker:

1. Go to Settings → Devices & Services
2. Click Add Integration
3. Search for Tibber Pulse MQTT
4. Follow the configuration steps in the UI

No YAML configuration is required.
All configuration is done through the Home Assistant user interface.

### Sharing raw device data

Enable **Save sanitized raw MQTT data for sharing** to create sanitized MQTT captures for troubleshooting, new device support, and regression testing.

Since developers cannot test every device, meter, and firmware combination, shared captures help maintain compatibility across all supported devices.

Sensitive identifiers are redacted where possible, but you should always review captures before sharing them. Disable the option to stop capturing.

#### Finding and sharing captures

1. Enable **Save sanitized raw MQTT data for sharing** in the integration options.
2. Wait for data to be collected.
3. Locate the generated .jsonl file in `<Home Assistant config>/tibber_pulse_mqtt_raw/`. On Home Assistant OS, the usual path is `/config/tibber_pulse_mqtt_raw/`.
4. Review the file before sharing.
5. Create a **Raw Capture** issue and attach the file.

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
The integration supports multiple data formats used by different Tibber Pulse variants.

- Devices such as Pulse P1 typically emit protobuf-encapsulated data with zlib-compressed OBIS payloads.
- Sometimes sensors such as Pulse P1 emits raw OBIS payloads.
- HAN devices (e.g. Aidon V2) use DLMS/COSEM and provide OBIS values directly in binary DataNotification messages.

All formats are automatically detected and decoded.

While Pulse devices share common hardware, the data encoding can differ depending on meter type and firmware. Support has primarily been tested with P1 and HAN devices. If your device uses a different format, please [share raw device data](#sharing-raw-device-data) to help improve decoding support.

## Support development

Tibber Pulse MQTT is a hobby project created and maintained during evenings and weekends.

If the integration adds value to your Home Assistant setup and you'd like to help fund future development, you are welcome to support the project with a donation.

[![PayPal](https://img.shields.io/badge/PayPal-003087?logo=paypal&logoColor=white)](https://paypal.me/mrhedstrom1)  
[![Swish](images/swish-tibber-pulse-mqtt-qronly.png)](https://app.swish.nu/1/p/sw/?sw=0704542533&cur=SEK&msg=Tibber%20Pulse%20MQTT&edit=amt,msg&src=qr)

## Credits
Tibber Pulse community work and formats

[@MSkjel](https://github.com/MSkjel) for the clear AWS bridge configuration and cert extraction guidance 
https://github.com/MSkjel/LocalPulse2Tibber

[@JBerts](https://github.com/JBerts) for implementing DLMS/COSEM (HAN) support and enabling compatibility with additional Tibber Pulse variants
