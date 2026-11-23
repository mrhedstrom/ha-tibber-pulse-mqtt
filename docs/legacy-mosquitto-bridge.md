# Legacy: Mosquitto bridge to Tibber Cloud

> [!WARNING]
> **This is the old way to forward a Pulse to Tibber Cloud. It is kept only as a reference.**  
> The integration now has built-in Tibber Cloud forwarding that needs no broker bridge and works with any MQTT broker, see [Forward data to Tibber Cloud](https://github.com/mrhedstrom/ha-tibber-pulse-mqtt#forward-data-to-tibber-cloud).  
> Do not run this bridge and the built-in forwarding for the same Pulse at the same time.

To keep full Tibber functionality — including the Tibber app, firmware updates, and Tibber cloud–based features such as load balancing for EV chargers — the Tibber Pulse must continue communicating with Tibber Cloud (AWS IoT). With a bridge this is done by configuring:

  * Tibber Pulse → local MQTT (Mosquitto)
  * Mosquitto → Tibber Cloud (AWS IoT) via an MQTT bridge

## Architecture overview
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

## Prerequisites
Extract the Pulse certificates as described in [Extract Tibber Pulse Certificates](https://github.com/mrhedstrom/ha-tibber-pulse-mqtt#extract-tibber-pulse-certificates-optional). For the bridge you need all three files, `CA.ca`, `Cert.crt` and `Priv.key`, and your saved `mqtt_url`, `mqtt_topic` and `mqtt_topic_sub`.

## Configure the Mosquitto bridge
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

Now everything is set up to forward Pulse data from local Mosquitto to Tibber Cloud. Next step is to [configure the Pulse to use your local broker](https://github.com/mrhedstrom/ha-tibber-pulse-mqtt#configure-tibber-pulse-to-use-a-local-mosquitto-broker).

## Migrating to built-in forwarding
Moving from this bridge to the built-in forwarding is done per Pulse. **Remove the bridge first.** The bridge and the built-in forwarding use the same Tibber client ID (`tibber-pulse-<your device id>`), and Tibber Cloud allows one connection per client ID. If both run, Tibber Cloud keeps disconnecting one of them, the connection drops over and over and the Tibber app may stop receiving data.

1. Remove the bridge:
   * delete the `connection bridge-to-tibber` section from your Mosquitto configuration (or delete `bridge.conf` and remove any reference to it)
   * restart the Mosquitto broker
   * check in the broker log that the bridge no longer connects to Tibber Cloud
2. Add Tibber Cloud forwarding for the Pulse in the integration, see [Forward data to Tibber Cloud](https://github.com/mrhedstrom/ha-tibber-pulse-mqtt#forward-data-to-tibber-cloud). You can reuse the certificates you extracted for the bridge.
3. Wait a few minutes and check that the Tibber Cloud connection sensor says `connected` and that the Tibber app shows data.
4. Optionally delete the certificate files in `/share/mosquitto/tibber_cert/`. The integration keeps its own copy.

The Pulse itself does not need to be reconfigured: its local topics stay the same.
