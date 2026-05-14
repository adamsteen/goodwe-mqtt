import argparse
import asyncio
import ipaddress
import inspect
from pathlib import Path
import re
import socket
import sys

import goodwe


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


def get_default_broadcast_host():
    return str(ipaddress.ip_network("192.168.1.0/24").broadcast_address)


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


def load_sensor_ids(sensors_file):
    if sensors_file is None:
        return None

    path = Path(sensors_file)
    if not path.exists():
        return None

    sensor_ids = []
    for line in path.read_text(encoding="utf-8").splitlines():
        sensor_id = line.split("#", 1)[0].strip()
        if sensor_id and sensor_id not in sensor_ids:
            sensor_ids.append(sensor_id)
    return sensor_ids


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
        return

    print_sensor_table(build_sensor_rows(inverter.sensors(), runtime_data))


async def poll_runtime_data(inverter, interval, sensor_ids=None):
    first_poll = True
    while True:
        if not first_poll:
            print()
        await get_runtime_data(inverter, sensor_ids)
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
    sensors_file=None,
    show_info=False,
    poll_interval=None,
):
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

    sensor_ids = load_sensor_ids(sensors_file)
    if poll_interval is not None:
        await poll_runtime_data(inverter, poll_interval, sensor_ids)
        return

    if sensor_ids:
        print(f"Runtime data from {Path(sensors_file).name}:")
    await get_runtime_data(inverter, sensor_ids)


def positive_seconds(value):
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number") from None
    if seconds <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return seconds


def parse_args():
    parser = argparse.ArgumentParser(description="Read runtime data from a GoodWe inverter.")
    parser.add_argument("--host", help="Inverter IP address or hostname. Skips discovery when provided.")
    parser.add_argument("--port", type=int, default=8899, help="Inverter port to connect to. Use 502 for TCP/Modbus.")
    parser.add_argument("--family", default="ET", choices=("ET", "EH", "BT", "BH", "ES", "EM", "BP", "DT", "MS", "D-NS", "XS"), help="Inverter family hint.")
    parser.add_argument("--timeout", type=int, default=1, help="Seconds to wait for each inverter request before retrying.")
    parser.add_argument("--dtls", action="store_true", help="Use DTLS-encrypted local Modbus, required by some Kit-20 dongles.")
    parser.add_argument("--info", action="store_true", help="Show inverter information before runtime values.")
    parser.add_argument(
        "--poll",
        nargs="?",
        const=30.0,
        type=positive_seconds,
        metavar="SECONDS",
        help="Poll runtime values repeatedly. Defaults to 30 seconds when no interval is supplied.",
    )
    parser.add_argument("--broadcast-host", help="Directed broadcast address for fallback discovery.")
    parser.add_argument("--discovery-port", type=int, default=48899, help="UDP discovery port for fallback discovery.")
    parser.add_argument("--discovery-timeout", type=float, default=1, help="Seconds to wait for directed UDP fallback discovery.")
    parser.add_argument(
        "--sensors-file",
        default=None,
        help="File containing sensor IDs to print. Blank lines and # comments are ignored.",
    )
    return parser.parse_args()


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
                args.sensors_file,
                args.info or len(sys.argv) == 1,
                args.poll,
            )
        )
    except KeyboardInterrupt:
        pass
