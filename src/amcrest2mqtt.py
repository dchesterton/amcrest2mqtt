from slugify import slugify
from amcrest import AmcrestCamera,AmcrestError
from datetime import datetime,timezone
import paho.mqtt.client as mqtt
import os
import sys
import json
import signal

amcrest_host = os.getenv('AMCREST_HOST')
amcrest_port = int(os.getenv('AMCREST_PORT') or 80)
amcrest_username = os.getenv('AMCREST_USERNAME') or "admin"
amcrest_password = os.getenv('AMCREST_PASSWORD')

mqtt_host = os.getenv('MQTT_HOST') or "localhost"
mqtt_qos = int(os.getenv("MQTT_QOS") or 0)
mqtt_port = int(os.getenv('MQTT_PORT') or 1883)
mqtt_username = os.getenv('MQTT_USERNAME')
mqtt_password = os.getenv('MQTT_PASSWORD') # can be None

home_assistant = os.getenv("HOME_ASSISTANT") == "true"
home_assistant_prefix = os.getenv("HOME_ASSISTANT_PREFIX") or "homeassistant"

def log(msg, level="INFO"):
    ts = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M:%S")
    print(f"{ts} [{level}] {msg}")

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

# Connect to camera
camera = AmcrestCamera(amcrest_host, amcrest_port, amcrest_username, amcrest_password).camera

device_type = camera.device_type.replace("type=", "").strip()
log(f"Device type: {device_type}")

serial_number = camera.serial_number.strip()
log(f"Serial number: {serial_number}")

sw_version = camera.software_information[0].replace("version=", "").strip()
log(f"Software version: {sw_version}")

device_name = camera.machine_name.replace("name=", "").strip()
device_slug = slugify(device_name, separator="_")
log(f"Device name: {device_name}")

# Connect to MQTT
status_topic = f"amcrest2mqtt/{serial_number}/status"
event_topic = f"amcrest2mqtt/{serial_number}/event"
motion_topic = f"amcrest2mqtt/{serial_number}/motion"
doorbell_topic = f"amcrest2mqtt/{serial_number}/doorbell"

doorbell_home_assistant_topic = f"{home_assistant_prefix}/binary_sensor/amcrest2mqtt-{serial_number}/{device_slug}_doorbell/config"
motion_home_assistant_topic = f"{home_assistant_prefix}/binary_sensor/amcrest2mqtt-{serial_number}/{device_slug}_motion/config"

client_id = f"amcrest2mqtt_{serial_number}"

def on_mqtt_disconnect(client, userdata, rc):
    if rc != 0:
        log(f"Unexpected MQTT disconnection", level="ERROR")
        exit_gracefully(rc, skip_mqtt=True)

mqtt_client = mqtt.Client(client_id=client_id, clean_session=False)
mqtt_client.suppress_exceptions = False
mqtt_client.on_disconnect = on_mqtt_disconnect
mqtt_client.username_pw_set(mqtt_username, password=mqtt_password)
mqtt_client.will_set(status_topic, payload="offline", qos=mqtt_qos, retain=True)

try:
    mqtt_client.connect(mqtt_host, port=mqtt_port)
except ConnectionError as error:
    log(f"Could not connect to MQTT server: {error}", level="ERROR")
    sys.exit(1)

mqtt_client.loop_start()

def mqtt_publish(topic, payload, exit_on_error=True):
    global mqtt_client

    msg = mqtt_client.publish(topic, payload=payload, qos=mqtt_qos, retain=True)

    if msg.rc == mqtt.MQTT_ERR_SUCCESS:
        msg.wait_for_publish()
        return msg

    log(f"Error publishing MQTT message: {mqtt.error_string(msg.rc)}", level="ERROR")

    if exit_on_error:
        exit_gracefully(msg.rc, skip_mqtt=True)

def exit_gracefully(rc, skip_mqtt=False):
    global status_topic, mqtt_client

    if mqtt_client.is_connected() and skip_mqtt == False:
        mqtt_publish(status_topic, "offline", exit_on_error=False)
        mqtt_client.loop_stop(force=True)
        mqtt_client.disconnect()

    # Use os._exit instead of sys.exit to ensure an MQTT disconnect event causes the program to exit correctly as they
    # occur on a separate thread
    os._exit(rc)

is_exiting = False

def signal_handler(sig, frame):
    # exit immediately upon receiving a second SIGINT
    global is_exiting

    if is_exiting:
        os._exit(1)

    is_exiting = True
    exit_gracefully(0)

signal.signal(signal.SIGINT, signal_handler)

mqtt_publish(status_topic, "online")

if home_assistant:
    device_obj = {
        "name": f"Amcrest {device_type}",
        "manufacturer": "Amcrest",
        "model": device_type,
        "identifiers": serial_number,
        "sw_version": sw_version,
        "via_device": "amcrest2mqtt"
    }

    log("Writing Home Assistant discovery config...")

    if device_type in ["AD110", "AD410"]:
        mqtt_publish(doorbell_home_assistant_topic, json.dumps({
            "availability_topic": status_topic,
            "state_topic": doorbell_topic,
            "payload_on": "on",
            "payload_off": "off",
            "name": f"{device_name} Doorbell",
            "unique_id": f"{serial_number}.doorbell",
            "device": device_obj
        }))

    mqtt_publish(motion_home_assistant_topic, json.dumps({
        "availability_topic": status_topic,
        "state_topic": motion_topic,
        "payload_on": "on",
        "payload_off": "off",
        "device_class": "motion",
        "name": f"{device_name} Motion",
        "unique_id": f"{serial_number}.motion",
        "device": device_obj,
    }))

log("Listening for events...")

try:
    for code, payload in camera.event_actions("All", retries=5):
        if code == "ProfileAlarmTransmit":
            mqtt_payload = "on" if payload["action"] == "Start" else "off"
            mqtt_publish(motion_topic, mqtt_payload)
        elif code == "_DoTalkAction_":
            mqtt_payload = "on" if payload["data"]["Action"] == "Invite" else "off"
            mqtt_publish(doorbell_topic, mqtt_payload)

        mqtt_publish(event_topic, json.dumps(payload))
        log(str(payload))

except AmcrestError as error:
    log(f"Amcrest error {error}", level="ERROR")
    exit_gracefully(1)
