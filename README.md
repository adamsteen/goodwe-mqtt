# goodwe-mqtt

Small Python helper for finding and reading local GoodWe inverter runtime data.

The current checkout is still a diagnostic helper. The intended next shape is a
container-first MQTT publisher with REST diagnostics and a small read-only
frontend; see [docs/design/service-design.md](docs/design/service-design.md).

Container images are published from `main` to the repository's GitHub Container
Registry package, `ghcr.io/<owner>/goodwe-mqtt:latest`.

This project exists because some newer GoodWe dongles do not behave like older plaintext UDP devices. In particular, some Kit-20 dongles advertise `dtls_port:8899` from the discovery endpoint and then drop normal plaintext inverter requests. This script tries the upstream `goodwe.search_inverters()` path first, falls back to a directed UDP discovery probe, detects DTLS-capable dongles, and then connects with an explicit inverter family.

## Links

- Upstream library: [marcelblijleven/goodwe](https://github.com/marcelblijleven/goodwe)
- Relevant DTLS issue: [marcelblijleven/goodwe#121](https://github.com/marcelblijleven/goodwe/issues/121)
- DTLS comment with test command and branch: [issue comment](https://github.com/marcelblijleven/goodwe/issues/121#issuecomment-4344389702)

## Setup

Create and activate a virtual environment:

```sh
python -m venv .venv
source .venv/bin/activate
```

Install the DTLS-capable GoodWe branch:

```sh
python -m pip install -r requirements.txt
```

The current PyPI `goodwe` release may not support `dtls=True`. If your dongle advertises `dtls_port:8899`, use the branch in `requirements.txt`.

## Container

The image expects a mounted YAML config at `/config/configuration.yaml`:

```sh
docker run --rm \
  -v "$PWD/config:/config:ro" \
  ghcr.io/<owner>/goodwe-mqtt:latest
```

For Home Assistant MQTT discovery, copy `goodwe-mqtt.example.yaml` to
`config/configuration.yaml`, then set the inverter and MQTT broker settings.

## Usage

Try autodiscovery:

```sh
python goodwe-mqtt.py
```

Configuration can come from a YAML file, environment variables, or CLI options.
Precedence is CLI options, then environment variables, then YAML config, then
built-in defaults.

By default, `goodwe-mqtt.py` loads `goodwe-mqtt.yaml` from the current directory
when that file exists. Use `--config` or `GM_CONFIG` to point at a different
path, such as a Docker-mounted config file:

```yaml
inverter:
  host: <inverter-ip>
  port: 8899
  family: ET
  dtls: true

poll: 30

sensors:
  - battery_soc
  - ppv
  - pbattery1
  - house_consumption
  - active_power
```

The same config can be run explicitly:

```sh
python goodwe-mqtt.py --config goodwe-mqtt.yaml
```

Or with Docker-style environment overrides:

```sh
GM_CONFIG=/config/goodwe-mqtt.yaml GM_POLL=10 python goodwe-mqtt.py
```

Boolean environment variables accept `1/0`, `true/false`, `yes/no`, and
`on/off`. Set `GM_POLL=true` to use the default 30 second polling
interval, a number such as `10` to choose the interval, or `false` to disable
polling. Set `GM_SENSORS` to a comma-separated list when you need to override
configured sensors:

```sh
GM_SENSORS=battery_soc,ppv,pbattery1,house_consumption,active_power python goodwe-mqtt.py
```

Skip discovery and connect to a known inverter address:

```sh
python goodwe-mqtt.py --host <inverter-ip>
```

By default the script prints every runtime sensor returned by the inverter. Set
`sensors` in YAML, `GM_SENSORS`, or `--sensors` to print only a supplied
shortlist.
Runtime values are printed as a `label | value | sensor` table.
Inverter information is shown only when no CLI arguments, environment variables,
or YAML config are supplied, or when `--info` / `GM_INFO=true` / `info: true`
is supplied.

Show inverter information with a known host:

```sh
python goodwe-mqtt.py --host <inverter-ip> --info
```

Poll runtime values every 30 seconds:

```sh
python goodwe-mqtt.py --host <inverter-ip> --sensors battery_soc,ppv --poll
```

Poll runtime values every 10 seconds:

```sh
python goodwe-mqtt.py --host <inverter-ip> --sensors battery_soc,ppv --poll 10
```

Force DTLS explicitly:

```sh
python goodwe-mqtt.py --host <inverter-ip> --dtls --family ET
```

Use a different directed broadcast address:

```sh
python goodwe-mqtt.py --broadcast-host <subnet-broadcast-ip>
```

Increase only the fallback discovery wait time:

```sh
python goodwe-mqtt.py --discovery-timeout 10
```

## Directed Discovery Fallback

`goodwe-mqtt.py` includes a directed UDP discovery fallback using the same packet from the upstream issue comment:

```sh
python goodwe-mqtt.py --broadcast-host <subnet-broadcast-ip> --discovery-timeout 10
```

A DTLS-capable dongle may reply with something like:

```text
dongle@sn,dtls_port:8899,<dongle-serial>
```

In that case the source address of the UDP reply is treated as the inverter host, and `goodwe-mqtt.py` enables `dtls=True`.

## CLI Options, Environment Variables, and YAML

| CLI option | Environment variable | YAML key | Description |
| --- | --- | --- | --- |
| `--config` | `GM_CONFIG` | n/a | YAML config file. Defaults to `goodwe-mqtt.yaml` when present. |
| `--host` | `GM_HOST` | `inverter.host` | Inverter IP or hostname. Skips discovery when provided. |
| `--port` | `GM_PORT` | `inverter.port` | Inverter communication port. Defaults to 8899. |
| `--family` | `GM_FAMILY` | `inverter.family` | GoodWe inverter family. Defaults to ET. |
| `--timeout` | `GM_TIMEOUT` | `inverter.timeout` | Timeout for inverter requests. Defaults to 1 second. |
| `--dtls` | `GM_DTLS` | `inverter.dtls` | Force DTLS mode. |
| `--info` | `GM_INFO` | `info` | Show inverter information before runtime values. |
| `--poll [SECONDS]` | `GM_POLL` | `poll` | Poll runtime values. Defaults to 30 seconds when enabled without an interval. |
| `--sensors` | `GM_SENSORS` | `sensors` | Sensor ID shortlist. |
| `--broadcast-host` | `GM_BROADCAST_HOST` | `discovery.broadcast_host` | Directed broadcast address for fallback discovery. |
| `--discovery-port` | `GM_DISCOVERY_PORT` | `discovery.port` | UDP discovery port. Defaults to 48899. |
| `--discovery-timeout` | `GM_DISCOVERY_TIMEOUT` | `discovery.timeout` | Timeout for directed fallback discovery. Defaults to 1 second. |

## Recommended Dashboard Sensors

For an ET/EH style hybrid inverter, the five high-signal runtime values are:

```text
battery_soc         Battery percentage
ppv                 Solar production in watts
pbattery1           Battery watts; positive discharging, negative charging
house_consumption   House draw in watts
active_power        Grid watts; positive exporting, negative importing
```

`ppv` is the normal total PV power sensor and is available across more GoodWe
families. `ppv_total` is an extended ET/EH MPPT value and is useful only if your
model exposes it.

Other useful sensors to keep in mind:

```text
battery_mode_label      Charging/discharging/standby state
grid_in_out_label       Importing/exporting/idle state
e_day                   Today's PV generation
e_total                 Lifetime PV generation
e_bat_charge_total      Lifetime battery charge energy
e_bat_discharge_total   Lifetime battery discharge energy
meter_e_total_imp       Lifetime grid import energy
meter_e_total_exp       Lifetime grid export energy
temperature             Inverter temperature
```

## Notes

- `dtls=True` requires an explicit inverter family, so this tool defaults `--family` to `ET`.
- Autodiscovery still starts with upstream plaintext discovery. If that fails, the directed UDP fallback probes the configured broadcast address.
- The project is a local diagnostic/helper script, not a replacement for the upstream `goodwe` package.
