from slugify import slugify
from amcrest import AmcrestCamera, AmcrestError
from datetime import datetime, timezone
import paho.mqtt.client as mqtt
import os
import sys
from json import dumps
import signal
from threading import Timer
import ssl

is_exiting = False
mqtt_client = None

# Read env variables
amcrest_host = os.getenv("AMCREST_HOST")
amcrest_port = int(os.getenv("AMCREST_PORT") or 80)
amcrest_username = os.getenv("AMCREST_USERNAME") or "admin"
amcrest_password = os.getenv("AMCREST_PASSWORD")

storage_poll_interval = int(os.getenv("STORAGE_POLL_INTERVAL") or 3600)
device_name = os.getenv("DEVICE_NAME")

mqtt_host = os.getenv("MQTT_HOST") or "localhost"
mqtt_qos = int(os.getenv("MQTT_QOS") or 0)
mqtt_port = int(os.getenv("MQTT_PORT") or 1883)
mqtt_username = os.getenv("MQTT_USERNAME")
mqtt_password = os.getenv("MQTT_PASSWORD")  # can be None
mqtt_tls_enabled = os.getenv("MQTT_TLS_ENABLED") == "true"
mqtt_tls_ca_cert = os.getenv("MQTT_TLS_CA_CERT")
mqtt_tls_cert = os.getenv("MQTT_TLS_CERT")
mqtt_tls_key = os.getenv("MQTT_TLS_KEY")

home_assistant = os.getenv("HOME_ASSISTANT") == "true"
home_assistant_prefix = os.getenv("HOME_ASSISTANT_PREFIX") or "homeassistant"

def read_file(file_name):
    with open(file_name, 'r') as file:
        data = file.read().replace('\n', '')

    return data

def read_version():
    if os.path.isfile("./VERSION"):
        return read_file("./VERSION")

    return read_file("../VERSION")

# Helper functions and callbacks
def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M:%S")
    print(f"{ts} [{level}] {msg}")

def mqtt_publish(topic, payload, exit_on_error=True, json=False):
    global mqtt_client

    msg = mqtt_client.publish(
        topic, payload=(dumps(payload) if json else payload), qos=mqtt_qos, retain=True
    )

    if msg.rc == mqtt.MQTT_ERR_SUCCESS:
        msg.wait_for_publish()
        return msg

    log(f"Error publishing MQTT message: {mqtt.error_string(msg.rc)}", level="ERROR")

    if exit_on_error:
        exit_gracefully(msg.rc, skip_mqtt=True)

def on_mqtt_disconnect(client, userdata, rc):
    if rc != 0:
        log(f"Unexpected MQTT disconnection", level="ERROR")
        exit_gracefully(rc, skip_mqtt=True)

def exit_gracefully(rc, skip_mqtt=False):
    global topics, mqtt_client

    log("Exiting app...")

    if mqtt_client is not None and mqtt_client.is_connected() and skip_mqtt == False:
        mqtt_publish(topics["status"], "offline", exit_on_error=False)
        mqtt_client.loop_stop(force=True)
        mqtt_client.disconnect()

    # Use os._exit instead of sys.exit to ensure an MQTT disconnect event causes the program to exit correctly as they
    # occur on a separate thread
    os._exit(rc)

def refresh_storage_sensors():
    global camera, topics, storage_poll_interval

    Timer(storage_poll_interval, refresh_storage_sensors).start()
    log("Fetching storage sensors...")

    try:
        storage = camera.storage_all

        mqtt_publish(topics["storage_used_percent"], str(storage["used_percent"]))
        mqtt_publish(topics["storage_used"], to_gb(storage["used"]))
        mqtt_publish(topics["storage_total"], to_gb(storage["total"]))
    except AmcrestError as error:
        log(f"Error fetching storage information {error}", level="WARNING")

def to_gb(total):
    return str(round(float(total[0]) / 1024 / 1024 / 1024, 2))

def ping_camera():
    Timer(30, ping_camera).start()
    response = os.system(f"ping -c1 -W100 {amcrest_host} >/dev/null 2>&1")

    if response != 0:
        log("Ping unsuccessful", level="ERROR")
        exit_gracefully(1)

def signal_handler(sig, frame):
    # exit immediately upon receiving a second SIGINT
    global is_exiting

    if is_exiting:
        os._exit(1)

    is_exiting = True
    exit_gracefully(0)

# Exit if any of the required vars are not provided
if amcrest_host is None:
    log("Please set the AMCREST_HOST environment variable", level="ERROR")
    sys.exit(1)

if amcrest_password is None:
    log("Please set the AMCREST_PASSWORD environment variable", level="ERROR")
    sys.exit(1)

if mqtt_username is None:
    log("Please set the MQTT_USERNAME environment variable", level="ERROR")
    sys.exit(1)

version = read_version()

log(f"App Version: {version}")

# Handle interruptions
signal.signal(signal.SIGINT, signal_handler)

# Connect to camera
camera = AmcrestCamera(
    amcrest_host, amcrest_port, amcrest_username, amcrest_password
).camera

# Fetch camera details
log("Fetching camera details...")

try:
    device_type = camera.device_type.replace("type=", "").strip()
    is_ad110 = device_type == "AD110"
    is_ad410 = device_type == "AD410"
    is_doorbell = is_ad110 or is_ad410
    serial_number = camera.serial_number

    if not isinstance(serial_number, str):
        log(f"Error fetching serial number", level="ERROR")
        exit_gracefully(1)

    sw_version = camera.software_information[0].replace("version=", "").strip()
    if not device_name:
        device_name = camera.machine_name.replace("name=", "").strip()

    device_slug = slugify(device_name, separator="_")
except AmcrestError as error:
    log(f"Error fetching camera details", level="ERROR")
    exit_gracefully(1)

log(f"Device type: {device_type}")
log(f"Serial number: {serial_number}")
log(f"Software version: {sw_version}")
log(f"Device name: {device_name}")

# MQTT topics
topics = {
    "config": f"amcrest2mqtt/{serial_number}/config",
    "status": f"amcrest2mqtt/{serial_number}/status",
    "event": f"amcrest2mqtt/{serial_number}/event",
    "motion": f"amcrest2mqtt/{serial_number}/motion",
    "doorbell": f"amcrest2mqtt/{serial_number}/doorbell",
    "human": f"amcrest2mqtt/{serial_number}/human",
    "storage_used": f"amcrest2mqtt/{serial_number}/storage/used",
    "storage_used_percent": f"amcrest2mqtt/{serial_number}/storage/used_percent",
    "storage_total": f"amcrest2mqtt/{serial_number}/storage/total",
    "home_assistant_legacy": {
        "doorbell": f"{home_assistant_prefix}/binary_sensor/amcrest2mqtt-{serial_number}/{device_slug}_doorbell/config",
        "human": f"{home_assistant_prefix}/binary_sensor/amcrest2mqtt-{serial_number}/{device_slug}_human/config",
        "motion": f"{home_assistant_prefix}/binary_sensor/amcrest2mqtt-{serial_number}/{device_slug}_motion/config",
        "storage_used": f"{home_assistant_prefix}/sensor/amcrest2mqtt-{serial_number}/{device_slug}_storage_used/config",
        "storage_used_percent": f"{home_assistant_prefix}/sensor/amcrest2mqtt-{serial_number}/{device_slug}_storage_used_percent/config",
        "storage_total": f"{home_assistant_prefix}/sensor/amcrest2mqtt-{serial_number}/{device_slug}_storage_total/config",
        "version": f"{home_assistant_prefix}/sensor/amcrest2mqtt-{serial_number}/{device_slug}_version/config",
        "host": f"{home_assistant_prefix}/sensor/amcrest2mqtt-{serial_number}/{device_slug}_host/config",
        "serial_number": f"{home_assistant_prefix}/sensor/amcrest2mqtt-{serial_number}/{device_slug}_serial_number/config",
    },
    "home_assistant": {
        "doorbell": f"{home_assistant_prefix}/binary_sensor/amcrest2mqtt-{serial_number}/doorbell/config",
        "human": f"{home_assistant_prefix}/binary_sensor/amcrest2mqtt-{serial_number}/human/config",
        "motion": f"{home_assistant_prefix}/binary_sensor/amcrest2mqtt-{serial_number}/motion/config",
        "storage_used": f"{home_assistant_prefix}/sensor/amcrest2mqtt-{serial_number}/storage_used/config",
        "storage_used_percent": f"{home_assistant_prefix}/sensor/amcrest2mqtt-{serial_number}/storage_used_percent/config",
        "storage_total": f"{home_assistant_prefix}/sensor/amcrest2mqtt-{serial_number}/storage_total/config",
        "version": f"{home_assistant_prefix}/sensor/amcrest2mqtt-{serial_number}/version/config",
        "host": f"{home_assistant_prefix}/sensor/amcrest2mqtt-{serial_number}/host/config",
        "serial_number": f"{home_assistant_prefix}/sensor/amcrest2mqtt-{serial_number}/serial_number/config",
    },
}

# Connect to MQTT
mqtt_client = mqtt.Client(
    client_id=f"amcrest2mqtt_{serial_number}", clean_session=False
)
mqtt_client.on_disconnect = on_mqtt_disconnect
mqtt_client.will_set(topics["status"], payload="offline", qos=mqtt_qos, retain=True)
if mqtt_tls_enabled:
    log(f"Setting up MQTT for TLS")
    if mqtt_tls_ca_cert is None:
        log("Missing var: MQTT_TLS_CA_CERT", level="ERROR")
        sys.exit(1)
    if mqtt_tls_cert is None:
        log("Missing var: MQTT_TLS_CERT", level="ERROR")
        sys.exit(1)
    if mqtt_tls_cert is None:
        log("Missing var: MQTT_TLS_KEY", level="ERROR")
        sys.exit(1)
    mqtt_client.tls_set(
        ca_certs=mqtt_tls_ca_cert,
        certfile=mqtt_tls_cert,
        keyfile=mqtt_tls_key,
        cert_reqs=ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLS,
    )
else:
    mqtt_client.username_pw_set(mqtt_username, password=mqtt_password)

try:
    mqtt_client.connect(mqtt_host, port=mqtt_port)
    mqtt_client.loop_start()
except ConnectionError as error:
    log(f"Could not connect to MQTT server: {error}", level="ERROR")
    sys.exit(1)

# Configure Home Assistant
if home_assistant:
    log("Writing Home Assistant discovery config...")

    base_config = {
        "availability_topic": topics["status"],
        "qos": mqtt_qos,
        "device": {
            "name": f"Amcrest {device_type}",
            "manufacturer": "Amcrest",
            "model": device_type,
            "identifiers": serial_number,
            "sw_version": sw_version,
            "via_device": "amcrest2mqtt",
        },
    }

    if is_doorbell:
        doorbell_name = "Doorbell" if device_name == "Doorbell" else f"{device_name} Doorbell"

        mqtt_publish(topics["home_assistant_legacy"]["doorbell"], "")
        mqtt_publish(
            topics["home_assistant"]["doorbell"],
            base_config
            | {
                "state_topic": topics["doorbell"],
                "payload_on": "on",
                "payload_off": "off",
                "icon": "mdi:doorbell",
                "name": doorbell_name,
                "unique_id": f"{serial_number}.doorbell",
            },
            json=True,
        )

    if is_ad410:
        mqtt_publish(topics["home_assistant_legacy"]["human"], "")
        mqtt_publish(
            topics["home_assistant"]["human"],
            base_config
            | {
                "state_topic": topics["human"],
                "payload_on": "on",
                "payload_off": "off",
                "device_class": "motion",
                "name": f"{device_name} Human",
                "unique_id": f"{serial_number}.human",
            },
            json=True,
        )

    mqtt_publish(topics["home_assistant_legacy"]["motion"], "")
    mqtt_publish(
        topics["home_assistant"]["motion"],
        base_config
        | {
            "state_topic": topics["motion"],
            "payload_on": "on",
            "payload_off": "off",
            "device_class": "motion",
            "name": f"{device_name} Motion",
            "unique_id": f"{serial_number}.motion",
        },
        json=True,
    )

    mqtt_publish(topics["home_assistant_legacy"]["version"], "")
    mqtt_publish(
        topics["home_assistant"]["version"],
        base_config
        | {
            "state_topic": topics["config"],
            "value_template": "{{ value_json.sw_version }}",
            "icon": "mdi:package-up",
            "name": f"{device_name} Version",
            "unique_id": f"{serial_number}.version",
            "entity_category": "diagnostic",
            "enabled_by_default": False
        },
        json=True,
    )

    mqtt_publish(topics["home_assistant_legacy"]["serial_number"], "")
    mqtt_publish(
        topics["home_assistant"]["serial_number"],
        base_config
        | {
            "state_topic": topics["config"],
            "value_template": "{{ value_json.serial_number }}",
            "icon": "mdi:alphabetical-variant",
            "name": f"{device_name} Serial Number",
            "unique_id": f"{serial_number}.serial_number",
            "entity_category": "diagnostic",
            "enabled_by_default": False
        },
        json=True,
    )

    mqtt_publish(topics["home_assistant_legacy"]["host"], "")
    mqtt_publish(
        topics["home_assistant"]["host"],
        base_config
        | {
            "state_topic": topics["config"],
            "value_template": "{{ value_json.host }}",
            "icon": "mdi:ip-network",
            "name": f"{device_name} Host",
            "unique_id": f"{serial_number}.host",
            "entity_category": "diagnostic",
            "enabled_by_default": False
        },
        json=True,
    )

    if storage_poll_interval > 0:
        mqtt_publish(topics["home_assistant_legacy"]["storage_used_percent"], "")
        mqtt_publish(
            topics["home_assistant"]["storage_used_percent"],
            base_config
            | {
                "state_topic": topics["storage_used_percent"],
                "unit_of_measurement": "%",
                "icon": "mdi:micro-sd",
                "name": f"{device_name} Storage Used %",
                "object_id": f"{device_slug}_storage_used_percent",
                "unique_id": f"{serial_number}.storage_used_percent",
                "entity_category": "diagnostic",
            },
            json=True,
        )

        mqtt_publish(topics["home_assistant_legacy"]["storage_used"], "")
        mqtt_publish(
            topics["home_assistant"]["storage_used"],
            base_config
            | {
                "state_topic": topics["storage_used"],
                "unit_of_measurement": "GB",
                "icon": "mdi:micro-sd",
                "name": f"{device_name} Storage Used",
                "unique_id": f"{serial_number}.storage_used",
                "entity_category": "diagnostic",
            },
            json=True,
        )

        mqtt_publish(topics["home_assistant_legacy"]["storage_total"], "")
        mqtt_publish(
            topics["home_assistant"]["storage_total"],
            base_config
            | {
                "state_topic": topics["storage_total"],
                "unit_of_measurement": "GB",
                "icon": "mdi:micro-sd",
                "name": f"{device_name} Storage Total",
                "unique_id": f"{serial_number}.storage_total",
                "entity_category": "diagnostic",
            },
            json=True,
        )

# Main loop
mqtt_publish(topics["status"], "online")
mqtt_publish(topics["config"], {
    "version": version,
    "device_type": device_type,
    "device_name": device_name,
    "sw_version": sw_version,
    "serial_number": serial_number,
    "host": amcrest_host,
}, json=True)

if storage_poll_interval > 0:
    refresh_storage_sensors()

ping_camera()

log("Listening for events...")

try:
    for code, payload in camera.event_actions("All", retries=5, timeout_cmd=(10.00, 3600)):
        if (is_ad110 and code == "ProfileAlarmTransmit") or (code == "VideoMotion" and not is_ad110):
            motion_payload = "on" if payload["action"] == "Start" else "off"
            mqtt_publish(topics["motion"], motion_payload)
        elif code == "CrossRegionDetection" and payload["data"]["ObjectType"] == "Human":
            human_payload = "on" if payload["action"] == "Start" else "off"
            mqtt_publish(topics["human"], human_payload)
        elif code == "_DoTalkAction_":
            doorbell_payload = "on" if payload["data"]["Action"] == "Invite" else "off"
            mqtt_publish(topics["doorbell"], doorbell_payload)

        mqtt_publish(topics["event"], payload, json=True)
        log(str(payload))

except AmcrestError as error:
    log(f"Amcrest error {error}", level="ERROR")
    exit_gracefully(1)
