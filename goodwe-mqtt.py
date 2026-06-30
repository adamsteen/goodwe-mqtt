import argparse
import asyncio
from datetime import datetime, timezone
import json
import ipaddress
import inspect
import os
from pathlib import Path
import re
import socket
import sys

import goodwe


INVERTER_FAMILIES = (
    "ET",
    "EH",
    "BT",
    "BH",
    "ES",
    "EM",
    "BP",
    "DT",
    "MS",
    "D-NS",
    "XS",
)
DEFAULT_POLL_INTERVAL = 30.0
DEFAULT_CONFIG_FILE = "goodwe-mqtt.yaml"
DEFAULTS = {
    "config": None,
    "host": None,
    "port": 8899,
    "family": "ET",
    "timeout": 1,
    "dtls": False,
    "info": False,
    "poll": None,
    "sensors": None,
    "broadcast_host": None,
    "discovery_port": 48899,
    "discovery_timeout": 1,
    "mqtt": None,
}
ENV_VARS = {
    "config": "GM_CONFIG",
    "host": "GM_HOST",
    "port": "GM_PORT",
    "family": "GM_FAMILY",
    "timeout": "GM_TIMEOUT",
    "dtls": "GM_DTLS",
    "info": "GM_INFO",
    "poll": "GM_POLL",
    "sensors": "GM_SENSORS",
    "broadcast_host": "GM_BROADCAST_HOST",
    "discovery_port": "GM_DISCOVERY_PORT",
    "discovery_timeout": "GM_DISCOVERY_TIMEOUT",
    "mqtt_password": "GM_MQTT_PASSWORD",
}
DEFAULT_MQTT = {
    "host": None,
    "port": 1883,
    "username": None,
    "password": None,
    "client_id": "goodwe-mqtt",
    "base_topic": "goodwe/inverter",
    "retain": True,
    "availability_topic": "goodwe/inverter/status",
    "last_seen_topic": "goodwe/inverter/last_seen",
    "precision": {
        "default": 3,
        "units": {},
        "sensors": {},
    },
    "discovery": {
        "enabled": False,
        "prefix": "homeassistant",
        "device_id": "goodwe_inverter",
        "device_name": "GoodWe Inverter",
    },
}
SENSOR_DEFAULTS = {
    "battery_soc": {
        "topic": "battery/soc",
        "name": "Battery State of Charge",
        "device_class": "battery",
        "state_class": "measurement",
    },
    "ppv": {
        "topic": "solar/power",
        "name": "Solar Power",
        "device_class": "power",
        "state_class": "measurement",
    },
    "pbattery1": {
        "topic": "battery/power",
        "name": "Battery Power",
        "device_class": "power",
        "state_class": "measurement",
    },
    "house_consumption": {
        "topic": "house/power",
        "name": "House Consumption",
        "device_class": "power",
        "state_class": "measurement",
    },
    "active_power": {
        "topic": "grid/power",
        "name": "Grid Power",
        "device_class": "power",
        "state_class": "measurement",
    },
}


def handle_max_retries_exception():
    print("No GoodWe inverter responded to network discovery.")
    print("Check that your computer is on the same network as the inverter and that UDP broadcast is allowed.")


def handle_directed_broadcast_timeout(broadcast_host, discovery_port, discovery_timeout):
    print(f"No reply from directed UDP broadcast to {broadcast_host}:{discovery_port} within {discovery_timeout} seconds.")
    print("Try a different --broadcast-host if your Mac is on a different subnet.")


def handle_permission_error():
    print("Python was not allowed to send the GoodWe network request.")
    print("Check your macOS local network/firewall permissions, VPN, or router UDP broadcast settings.")


def handle_inverter_error(error):
    print("Unable to connect to the GoodWe inverter.")
    print(error)


def handle_unsupported_dtls():
    print("This inverter dongle advertises DTLS, but the installed goodwe package does not support dtls=True.")
    print('Install the DTLS branch with: python -m pip install "goodwe[dtls] @ git+https://github.com/botts7/goodwe.git@feature/dtls-transport"')


def handle_discovery_parse_error(discovery_response):
    print("GoodWe network discovery responded, but no inverter IP address could be found.")
    print(f"Raw discovery response: {discovery_response!r}")


def extract_inverter_host(discovery_response, source_host=None):
    match = re.search(rb"\b(?:\d{1,3}\.){3}\d{1,3}\b", discovery_response)
    if not match:
        return source_host
    return match.group(0).decode("ascii")


def discovery_response_uses_dtls(discovery_response):
    return b"dtls_port:" in discovery_response


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_topic_part(value):
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_").lower()


def join_topic(*parts):
    return "/".join(str(part).strip("/") for part in parts if part is not None and str(part).strip("/"))


def get_default_broadcast_host():
    return str(ipaddress.ip_network("255.255.255.255/32").broadcast_address)


def search_inverters_with_directed_broadcast(broadcast_host, discovery_port, timeout):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)

    try:
        sock.sendto(b"WIFIKIT-214028-READ", (broadcast_host, discovery_port))
        data, address = sock.recvfrom(2048)
        return data, address[0]
    except TimeoutError:
        return None, None
    finally:
        sock.close()


async def connect_inverter(host, port, family, timeout, dtls):
    connect_params = inspect.signature(goodwe.connect).parameters
    supports_dtls = "dtls" in connect_params

    if dtls and not supports_dtls:
        handle_unsupported_dtls()
        return None

    kwargs = {"port": port, "family": family, "timeout": timeout}
    if dtls:
        kwargs["dtls"] = True

    return await goodwe.connect(host, **kwargs)


async def find_inverter(port, family, timeout, dtls, broadcast_host, discovery_port, discovery_timeout):
    try:
        discovery_response = await goodwe.search_inverters()
        source_host = None
    except goodwe.exceptions.MaxRetriesException:
        print("GoodWe library discovery timed out; trying directed UDP broadcast.")
        discovery_response, source_host = search_inverters_with_directed_broadcast(
            broadcast_host,
            discovery_port,
            discovery_timeout,
        )
        if discovery_response is None:
            handle_directed_broadcast_timeout(broadcast_host, discovery_port, discovery_timeout)
            return None

    host = extract_inverter_host(discovery_response, source_host)
    if host is None:
        handle_discovery_parse_error(discovery_response)
        return None

    if discovery_response_uses_dtls(discovery_response):
        print("Discovery response advertises DTLS; using dtls=True.")
        dtls = True

    print(f"Found inverter at {host}:{port}")
    return await connect_inverter(host, port, family, timeout, dtls)


def configured_sensor_ids(sensor_configs):
    if not sensor_configs:
        return None
    return [sensor_config["id"] for sensor_config in sensor_configs]


def normalize_sensor_config(value):
    if isinstance(value, str):
        sensor_id = value.strip()
        if not sensor_id:
            raise argparse.ArgumentTypeError("sensor id must not be blank")
        defaults = SENSOR_DEFAULTS.get(sensor_id, {})
        return {
            "id": sensor_id,
            "topic": defaults.get("topic", sensor_id),
            "name": defaults.get("name"),
            "device_class": defaults.get("device_class"),
            "state_class": defaults.get("state_class"),
            "required": sensor_id == "battery_soc",
        }
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("sensor must be a string or mapping")

    sensor_id = string(value.get("id") or value.get("sensor"))
    if not sensor_id:
        raise argparse.ArgumentTypeError("sensor mapping requires id")

    defaults = SENSOR_DEFAULTS.get(sensor_id, {})
    required = value.get("required", sensor_id == "battery_soc")
    return {
        "id": sensor_id,
        "topic": string(value.get("topic") or defaults.get("topic") or sensor_id),
        "name": string(value.get("name") or defaults.get("name")),
        "device_class": string(value.get("device_class") or value.get("device-class") or defaults.get("device_class")),
        "state_class": string(value.get("state_class") or value.get("state-class") or defaults.get("state_class")),
        "unit": string(value.get("unit") or value.get("unit_of_measurement") or value.get("unit-of-measurement")),
        "required": boolean(required),
    }


def sensor_configs(value):
    if value is None:
        return None
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise argparse.ArgumentTypeError(
            "must be a comma-separated string or YAML list"
        )

    parsed = []
    seen = set()
    for item in values:
        sensor_config = normalize_sensor_config(item)
        sensor_id = sensor_config["id"]
        if sensor_id and sensor_id not in seen:
            parsed.append(sensor_config)
            seen.add(sensor_id)
    return parsed or None


def format_mqtt_number(value, precision):
    if precision <= 0:
        return str(int(round(value)))
    payload = f"{value:.{precision}f}".rstrip("0").rstrip(".")
    return "0" if payload == "-0" else payload


def precision_for_sensor(mqtt_config, sensor_id, unit):
    precision_config = mqtt_config.get("precision") or {}
    sensors = precision_config.get("sensors") or {}
    if sensor_id in sensors:
        return integer(sensors[sensor_id])
    units = precision_config.get("units") or {}
    if unit in units:
        return integer(units[unit])
    return integer(precision_config.get("default", 3))


def serialize_sensor_value(value, unit, sensor_id, mqtt_config):
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format_mqtt_number(
            value,
            precision_for_sensor(mqtt_config, sensor_id, unit),
        )
    return str(value)


def build_readings(sensor_configs, sensors_by_id, runtime_data, mqtt_config):
    readings = []
    for sensor_config in sensor_configs or []:
        sensor_id = sensor_config["id"]
        sensor = sensors_by_id.get(sensor_id)
        if sensor is None:
            readings.append({
                "config": sensor_config,
                "sensor": None,
                "value": None,
                "payload": None,
                "unit": sensor_config.get("unit") or "",
                "status": "missing",
            })
            continue
        unit = sensor_config.get("unit") or sensor.unit
        if sensor_id not in runtime_data:
            readings.append({
                "config": sensor_config,
                "sensor": sensor,
                "value": None,
                "payload": None,
                "unit": unit,
                "status": "missing",
            })
            continue
        value = runtime_data[sensor_id]
        readings.append({
            "config": sensor_config,
            "sensor": sensor,
            "value": value,
            "payload": serialize_sensor_value(value, unit, sensor_id, mqtt_config),
            "unit": unit,
            "status": "ok",
        })
    return readings


class MqttPublisher:
    def __init__(self, config):
        self.config = config
        self.client = None
        self.published_values = {}
        self.published_statuses = {}

    def enabled(self):
        return bool(self.config and self.config.get("host"))

    def connect(self):
        if not self.enabled() or self.client is not None:
            return
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            raise RuntimeError(
                "MQTT publishing requires paho-mqtt. "
                "Install dependencies with: python -m pip install -r requirements.txt"
            ) from None

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, self.config["client_id"])
        if self.config.get("username") is not None:
            self.client.username_pw_set(
                self.config.get("username"),
                self.config.get("password"),
            )
        self.client.will_set(
            self.config["availability_topic"],
            payload="offline",
            qos=0,
            retain=True,
        )
        self.client.connect(self.config["host"], self.config["port"])
        self.client.loop_start()
        self.publish(self.config["availability_topic"], "online", retain=True)

    def publish(self, topic, payload, retain=False):
        if not self.enabled():
            return
        self.connect()
        self.client.publish(topic, payload, retain=retain)

    def publish_if_changed(self, topic, payload, retain=True):
        if self.published_values.get(topic) == payload:
            return
        self.publish(topic, payload, retain=retain)
        self.published_values[topic] = payload

    def publish_status_if_changed(self, topic, status):
        if self.published_statuses.get(topic) == status:
            return
        self.publish(topic, status, retain=True)
        self.published_statuses[topic] = status

    def publish_discovery(self, inverter, readings):
        discovery = self.config.get("discovery") or {}
        if not discovery.get("enabled"):
            return
        prefix = discovery.get("prefix") or "homeassistant"
        device_identifier = normalize_topic_part(
            discovery.get("device_id") or "goodwe_inverter"
        )
        device = {
            "identifiers": [device_identifier],
            "name": discovery.get("device_name") or "GoodWe Inverter",
            "manufacturer": "GoodWe",
        }
        if inverter.model_name:
            device["model"] = inverter.model_name

        for reading in readings:
            sensor_config = reading["config"]
            sensor = reading["sensor"]
            sensor_id = sensor_config["id"]
            unique_id = f"{device_identifier}_{normalize_topic_part(sensor_id)}"
            state_topic = join_topic(self.config["base_topic"], sensor_config["topic"])
            sensor_availability_topic = join_topic(
                self.config["base_topic"],
                sensor_config["topic"],
                "availability",
            )
            payload = {
                "name": sensor_config.get("name") or (sensor.name if sensor else sensor_id),
                "unique_id": unique_id,
                "state_topic": state_topic,
                "availability": [
                    {
                        "topic": self.config["availability_topic"],
                        "payload_available": "online",
                        "payload_not_available": "offline",
                    },
                    {
                        "topic": sensor_availability_topic,
                        "payload_available": "online",
                        "payload_not_available": "offline",
                    },
                ],
                "availability_mode": "all",
                "device": device,
            }
            unit = reading["unit"]
            if unit:
                payload["unit_of_measurement"] = unit
            if sensor_config.get("device_class"):
                payload["device_class"] = sensor_config["device_class"]
            if sensor_config.get("state_class"):
                payload["state_class"] = sensor_config["state_class"]
            if isinstance(reading["value"], float):
                payload["suggested_display_precision"] = precision_for_sensor(
                    self.config,
                    sensor_id,
                    unit,
                )
            discovery_topic = join_topic(
                prefix,
                "sensor",
                unique_id,
                "config",
            )
            self.publish(discovery_topic, json.dumps(payload, sort_keys=True), retain=True)

    def publish_readings(self, inverter, readings):
        if not self.enabled():
            return
        self.publish_discovery(inverter, readings)
        for reading in readings:
            sensor_config = reading["config"]
            state_topic = join_topic(self.config["base_topic"], sensor_config["topic"])
            status_topic = join_topic(self.config["base_topic"], sensor_config["topic"], "status")
            availability_topic = join_topic(
                self.config["base_topic"],
                sensor_config["topic"],
                "availability",
            )
            self.publish_status_if_changed(status_topic, reading["status"])
            self.publish_status_if_changed(
                availability_topic,
                "online" if reading["status"] == "ok" else "offline",
            )
            if reading["payload"] is not None:
                self.publish_if_changed(
                    state_topic,
                    reading["payload"],
                    retain=self.config["retain"],
                )
        self.publish(self.config["last_seen_topic"], now_iso(), retain=False)

    def close(self):
        if self.client is None:
            return
        self.publish(self.config["availability_topic"], "offline", retain=True)
        self.client.loop_stop()
        self.client.disconnect()
        self.client = None


def format_sensor_value(sensor, runtime_data):
    return f"{runtime_data[sensor.id_]} {sensor.unit}".rstrip()


def print_sensor_table(rows):
    if not rows:
        return

    label_width = max(len("label"), *(len(label) for label, _, _ in rows))
    value_width = max(len("value"), *(len(value) for _, value, _ in rows))
    sensor_width = max(len("sensor"), *(len(sensor_id) for _, _, sensor_id in rows))

    print(
        f"{'label':<{label_width}} | "
        f"{'value':>{value_width}} | "
        "sensor"
    )
    print(
        f"{'-' * label_width}-|-"
        f"{'-' * value_width}-|-"
        f"{'-' * sensor_width}"
    )
    for label, value, sensor_id in rows:
        print(
            f"{label:<{label_width}} | "
            f"{value:>{value_width}} | "
            f"{sensor_id}"
        )


def build_filtered_sensor_rows(sensor_ids, sensors_by_id, runtime_data):
    rows = []
    for sensor_id in sensor_ids:
        sensor = sensors_by_id.get(sensor_id)
        if sensor is None:
            rows.append((sensor_id, "unavailable on this inverter family", sensor_id))
        elif sensor.id_ not in runtime_data:
            rows.append((sensor.name, "unavailable in this response", sensor.id_))
        else:
            rows.append(
                (sensor.name, format_sensor_value(sensor, runtime_data), sensor.id_)
            )
    return rows


def build_sensor_rows(sensors, runtime_data):
    return [
        (sensor.name, format_sensor_value(sensor, runtime_data), sensor.id_)
        for sensor in sensors
        if sensor.id_ in runtime_data
    ]


async def get_runtime_data(inverter, sensor_ids=None):
    runtime_data = await inverter.read_runtime_data()
    sensors_by_id = {sensor.id_: sensor for sensor in inverter.sensors()}

    if sensor_ids:
        print_sensor_table(
            build_filtered_sensor_rows(sensor_ids, sensors_by_id, runtime_data)
        )
        return runtime_data

    print_sensor_table(build_sensor_rows(inverter.sensors(), runtime_data))
    return runtime_data


async def poll_runtime_data(inverter, interval, sensor_configs=None, mqtt_publisher=None):
    sensor_ids = configured_sensor_ids(sensor_configs)
    sensors_by_id = {sensor.id_: sensor for sensor in inverter.sensors()}
    first_poll = True
    while True:
        if not first_poll:
            print()
        runtime_data = await get_runtime_data(inverter, sensor_ids)
        if mqtt_publisher and mqtt_publisher.enabled():
            mqtt_publisher.publish_readings(
                inverter,
                build_readings(sensor_configs, sensors_by_id, runtime_data, mqtt_publisher.config),
            )
        first_poll = False
        await asyncio.sleep(interval)


def print_inverter_info(inverter):
    fields = (
        ("Model", inverter.model_name),
        ("Serial number", inverter.serial_number),
        ("Rated power", inverter.rated_power),
        ("AC output type", inverter.ac_output_type),
        ("Firmware", inverter.firmware),
        ("ARM firmware", inverter.arm_firmware),
        ("Modbus version", inverter.modbus_version),
        ("DSP1 version", inverter.dsp1_version),
        ("DSP2 version", inverter.dsp2_version),
        ("DSP SVN version", inverter.dsp_svn_version),
        ("ARM version", inverter.arm_version),
        ("ARM SVN version", inverter.arm_svn_version),
    )

    print("Inverter information:")
    for label, value in fields:
        if value is not None:
            print(f"{label}: {value}")
    print()


async def main(
    host=None,
    port=8899,
    family="ET",
    timeout=1,
    dtls=False,
    broadcast_host=None,
    discovery_port=48899,
    discovery_timeout=1,
    sensor_configs=None,
    show_info=False,
    poll_interval=None,
    mqtt_config=None,
):
    mqtt_publisher = MqttPublisher(mqtt_config)
    try:
        if host:
            print(f"Connecting to inverter at {host}:{port}")
            inverter = await connect_inverter(host, port, family, timeout, dtls)
        else:
            broadcast_host = broadcast_host or get_default_broadcast_host()
            inverter = await find_inverter(port, family, timeout, dtls, broadcast_host, discovery_port, discovery_timeout)
    except goodwe.exceptions.MaxRetriesException:
        handle_max_retries_exception()
        return
    except goodwe.exceptions.InverterError as error:
        handle_inverter_error(error)
        return
    except PermissionError:
        handle_permission_error()
        return

    if inverter is None:
        return

    if show_info:
        print_inverter_info(inverter)

    sensor_ids = configured_sensor_ids(sensor_configs)
    sensors_by_id = {sensor.id_: sensor for sensor in inverter.sensors()}

    if poll_interval is not None:
        await poll_runtime_data(inverter, poll_interval, sensor_configs, mqtt_publisher)
        return

    if sensor_ids:
        print("Runtime data from configured sensors:")
    runtime_data = await get_runtime_data(inverter, sensor_ids)
    if mqtt_publisher.enabled():
        mqtt_publisher.publish_readings(
            inverter,
            build_readings(sensor_configs, sensors_by_id, runtime_data, mqtt_config),
        )
        mqtt_publisher.close()


def positive_seconds(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a number") from None
    if seconds <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return seconds


def integer(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be an integer") from None


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("must be a number") from None


def string(value):
    if value is None:
        return None
    return str(value)


def inverter_family(value):
    value = str(value)
    if value not in INVERTER_FAMILIES:
        choices = ", ".join(INVERTER_FAMILIES)
        raise argparse.ArgumentTypeError(f"must be one of: {choices}")
    return value


def boolean(value):
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in ("1", "true", "yes", "y", "on"):
        return True
    if normalized in ("0", "false", "no", "n", "off"):
        return False
    raise argparse.ArgumentTypeError(
        "must be one of: 1, true, yes, on, 0, false, no, off"
    )


def poll_interval(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return DEFAULT_POLL_INTERVAL if value else None
    normalized = str(value).strip().lower()
    if normalized in ("1", "true", "yes", "y", "on"):
        return DEFAULT_POLL_INTERVAL
    if normalized in ("0", "false", "no", "n", "off"):
        return None
    return positive_seconds(value)


ENV_PARSERS = {
    "config": string,
    "host": string,
    "port": integer,
    "family": inverter_family,
    "timeout": integer,
    "dtls": boolean,
    "info": boolean,
    "poll": poll_interval,
    "sensors": sensor_configs,
    "broadcast_host": string,
    "discovery_port": integer,
    "discovery_timeout": number,
    "mqtt_password": string,
}


def parse_env_config(parser):
    config = {}
    for key, env_var in ENV_VARS.items():
        if env_var not in os.environ:
            continue
        try:
            config[key] = ENV_PARSERS[key](os.environ[env_var])
        except argparse.ArgumentTypeError as error:
            parser.error(f"{env_var}: {error}")
    return config


def expand_env_value(value):
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [expand_env_value(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_env_value(item) for key, item in value.items()}
    return value


def nested_yaml_mapping(data, path):
    found, value = yaml_value(data, path)
    if not found:
        return None
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("must be a mapping")
    return value


def mqtt_precision(value):
    default = {"default": 3, "units": {}, "sensors": {}}
    if value is None:
        return default
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("must be a mapping")
    precision = {
        "default": integer(value.get("default", 3)),
        "units": {},
        "sensors": {},
    }
    for key, item in (value.get("units") or {}).items():
        precision["units"][string(key)] = integer(item)
    for key, item in (value.get("sensors") or {}).items():
        precision["sensors"][string(key)] = integer(item)
    return precision


def parse_mqtt_config(parser, data, config_path):
    try:
        raw_mqtt = nested_yaml_mapping(data, ("mqtt",))
    except argparse.ArgumentTypeError as error:
        parser.error(f"{config_path}:mqtt: {error}")
    if raw_mqtt is None:
        return None

    raw_mqtt = expand_env_value(raw_mqtt)
    config = {
        **DEFAULT_MQTT,
        "precision": {
            **DEFAULT_MQTT["precision"],
            "units": dict(DEFAULT_MQTT["precision"]["units"]),
            "sensors": dict(DEFAULT_MQTT["precision"]["sensors"]),
        },
        "discovery": dict(DEFAULT_MQTT["discovery"]),
    }

    try:
        if "host" in raw_mqtt:
            config["host"] = string(raw_mqtt["host"])
        if "port" in raw_mqtt:
            config["port"] = integer(raw_mqtt["port"])
        if "username" in raw_mqtt:
            config["username"] = string(raw_mqtt["username"])
        if "password" in raw_mqtt:
            config["password"] = string(raw_mqtt["password"])
        if "client_id" in raw_mqtt or "client-id" in raw_mqtt:
            config["client_id"] = string(raw_mqtt.get("client_id", raw_mqtt.get("client-id")))
        if "base_topic" in raw_mqtt or "base-topic" in raw_mqtt:
            config["base_topic"] = string(raw_mqtt.get("base_topic", raw_mqtt.get("base-topic")))
        if "retain" in raw_mqtt:
            config["retain"] = boolean(raw_mqtt["retain"])
        if "availability_topic" in raw_mqtt or "availability-topic" in raw_mqtt:
            config["availability_topic"] = string(raw_mqtt.get("availability_topic", raw_mqtt.get("availability-topic")))
        if "last_seen_topic" in raw_mqtt or "last-seen-topic" in raw_mqtt:
            config["last_seen_topic"] = string(raw_mqtt.get("last_seen_topic", raw_mqtt.get("last-seen-topic")))
        if "precision" in raw_mqtt:
            parsed_precision = mqtt_precision(raw_mqtt["precision"])
            config["precision"] = {
                **config["precision"],
                **parsed_precision,
                "units": {
                    **config["precision"]["units"],
                    **parsed_precision["units"],
                },
                "sensors": {
                    **config["precision"]["sensors"],
                    **parsed_precision["sensors"],
                },
            }
        if "discovery" in raw_mqtt:
            discovery = raw_mqtt["discovery"]
            if not isinstance(discovery, dict):
                raise argparse.ArgumentTypeError("mqtt.discovery must be a mapping")
            if "enabled" in discovery:
                config["discovery"]["enabled"] = boolean(discovery["enabled"])
            if "prefix" in discovery:
                config["discovery"]["prefix"] = string(discovery["prefix"])
            if "device_id" in discovery or "device-id" in discovery:
                config["discovery"]["device_id"] = string(discovery.get("device_id", discovery.get("device-id")))
            if "device_name" in discovery or "device-name" in discovery:
                config["discovery"]["device_name"] = string(discovery.get("device_name", discovery.get("device-name")))
    except argparse.ArgumentTypeError as error:
        parser.error(f"{config_path}:mqtt: {error}")

    return config


YAML_CONFIG_FIELDS = (
    (("inverter", "host"), "host", string),
    (("inverter", "port"), "port", integer),
    (("inverter", "family"), "family", inverter_family),
    (("inverter", "timeout"), "timeout", integer),
    (("inverter", "dtls"), "dtls", boolean),
    (("discovery", "broadcast_host"), "broadcast_host", string),
    (("discovery", "port"), "discovery_port", integer),
    (("discovery", "timeout"), "discovery_timeout", number),
    (("info",), "info", boolean),
    (("poll",), "poll", poll_interval),
    (("sensors",), "sensors", sensor_configs),
)


def yaml_key(mapping, key):
    candidates = (key, key.replace("_", "-"))
    for candidate in candidates:
        if candidate in mapping:
            return candidate
    return None


def yaml_value(data, path):
    current = data
    for key in path:
        if not isinstance(current, dict):
            raise argparse.ArgumentTypeError("must be a mapping")
        actual_key = yaml_key(current, key)
        if actual_key is None:
            return False, None
        current = current[actual_key]
    return True, current


def load_yaml_module(parser):
    try:
        import yaml
    except ImportError:
        parser.error(
            "YAML config requires PyYAML. "
            "Install dependencies with: python -m pip install -r requirements.txt"
        )
    return yaml


def parse_yaml_config(parser, data, config_path):
    config = {}
    for path, key, parser_func in YAML_CONFIG_FIELDS:
        try:
            found, value = yaml_value(data, path)
            if not found:
                continue
            config[key] = parser_func(value)
        except argparse.ArgumentTypeError as error:
            parser.error(f"{config_path}:{'.'.join(path)}: {error}")
    mqtt_config = parse_mqtt_config(parser, data, config_path)
    if mqtt_config is not None:
        config["mqtt"] = mqtt_config
    return config


def load_yaml_config(parser, config_path, required=False):
    if config_path is None:
        default_path = Path(DEFAULT_CONFIG_FILE)
        if not default_path.exists():
            return {}
        config_path = default_path

    path = Path(config_path)
    if not path.exists():
        if required:
            parser.error(f"{path}: config file does not exist")
        return {}

    yaml = load_yaml_module(parser)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        parser.error(f"{path}: invalid YAML: {error}")

    if data is None:
        return {"config": str(path)}
    if not isinstance(data, dict):
        parser.error(f"{path}: config must be a YAML mapping")

    config = parse_yaml_config(parser, data, path)
    config["config"] = str(path)
    return config


def parse_args(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    env_var_list = ", ".join(ENV_VARS.values())
    parser = argparse.ArgumentParser(
        description="Read runtime data from a GoodWe inverter.",
        epilog=(
            f"Config file: {DEFAULT_CONFIG_FILE} if present, or --config/GM_CONFIG. "
            f"Environment variables: {env_var_list}. "
            "Precedence is CLI, then environment, then YAML config."
        ),
    )
    default = argparse.SUPPRESS
    parser.add_argument(
        "--config",
        default=default,
        help="YAML config file. Defaults to goodwe-mqtt.yaml when present.",
    )
    parser.add_argument(
        "--host",
        default=default,
        help="Inverter IP address or hostname. Skips discovery when provided.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=default,
        help="Inverter port to connect to. Use 502 for TCP/Modbus.",
    )
    parser.add_argument(
        "--family",
        default=default,
        choices=INVERTER_FAMILIES,
        help="Inverter family hint.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=default,
        help="Seconds to wait for each inverter request before retrying.",
    )
    parser.add_argument(
        "--dtls",
        dest="dtls",
        action="store_true",
        default=default,
        help="Use DTLS-encrypted local Modbus, required by some Kit-20 dongles.",
    )
    parser.add_argument(
        "--no-dtls",
        dest="dtls",
        action="store_false",
        default=default,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--info",
        dest="info",
        action="store_true",
        default=default,
        help="Show inverter information before runtime values.",
    )
    parser.add_argument(
        "--no-info",
        dest="info",
        action="store_false",
        default=default,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--poll",
        nargs="?",
        const=DEFAULT_POLL_INTERVAL,
        default=default,
        type=positive_seconds,
        metavar="SECONDS",
        help="Poll runtime values repeatedly. Defaults to 30 seconds when no interval is supplied.",
    )
    parser.add_argument(
        "--no-poll",
        dest="poll",
        action="store_const",
        const=None,
        default=default,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--sensors",
        type=sensor_configs,
        default=default,
        metavar="SENSOR[,SENSOR...]",
        help="Comma-separated sensor IDs to print.",
    )
    parser.add_argument(
        "--broadcast-host",
        default=default,
        help="Directed broadcast address for fallback discovery.",
    )
    parser.add_argument(
        "--discovery-port",
        type=int,
        default=default,
        help="UDP discovery port for fallback discovery.",
    )
    parser.add_argument(
        "--discovery-timeout",
        type=float,
        default=default,
        help="Seconds to wait for directed UDP fallback discovery.",
    )
    cli_config = vars(parser.parse_args(argv))
    env_config = parse_env_config(parser)
    config_path = cli_config.get("config") or env_config.get("config")
    yaml_config = load_yaml_config(
        parser,
        config_path,
        required=config_path is not None,
    )
    config = {**DEFAULTS, **yaml_config, **env_config, **cli_config}
    if config.get("mqtt") and config.get("mqtt_password") is not None:
        config["mqtt"]["password"] = config["mqtt_password"]
    config.pop("mqtt_password", None)
    config["show_info"] = config["info"] or (
        not argv and not env_config and not yaml_config
    )
    return argparse.Namespace(**config)


if __name__ == "__main__":
    args = parse_args()
    try:
        asyncio.run(
            main(
                args.host,
                args.port,
                args.family,
                args.timeout,
                args.dtls,
                args.broadcast_host,
                args.discovery_port,
                args.discovery_timeout,
                args.sensors,
                args.show_info,
                args.poll,
                args.mqtt,
            )
        )
    except KeyboardInterrupt:
        pass
