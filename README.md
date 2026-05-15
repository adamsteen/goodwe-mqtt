# goodwe-mqtt

Small Python helper for finding and reading local GoodWe inverter runtime data.

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

## Usage

Try autodiscovery:

```sh
python goodwe-mqtt.py
```

All CLI options can also be set with environment variables. CLI arguments take
precedence when both are supplied:

```sh
GM_HOST=<inverter-ip> GM_SENSORS_FILE=sensors.txt GM_POLL=10 python goodwe-mqtt.py
```

Boolean environment variables accept `1/0`, `true/false`, `yes/no`, and
`on/off`. Set `GM_POLL=true` to use the default 30 second polling
interval, a number such as `10` to choose the interval, or `false` to disable
polling. When neither CLI options nor environment variables are supplied,
inverter information is shown by default; setting any `GM_*` option suppresses
that unless `GM_INFO=true` is set.

Skip discovery and connect to a known inverter address:

```sh
python goodwe-mqtt.py --host <inverter-ip>
```

By default the script prints every runtime sensor returned by the inverter. Pass
`--sensors-file sensors.txt` to print only a supplied shortlist.
Runtime values are printed as a `label | value | sensor` table.
Inverter information is shown only when no CLI arguments or environment
variables are supplied, or when `--info` / `GM_INFO=true` is supplied.

Show inverter information with a known host:

```sh
python goodwe-mqtt.py --host <inverter-ip> --info
```

Poll runtime values every 30 seconds:

```sh
python goodwe-mqtt.py --host <inverter-ip> --sensors-file sensors.txt --poll
```

Poll runtime values every 10 seconds:

```sh
python goodwe-mqtt.py --host <inverter-ip> --sensors-file sensors.txt --poll 10
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

## CLI Options and Environment Variables

| CLI option | Environment variable | Description |
| --- | --- | --- |
| `--host` | `GM_HOST` | Inverter IP or hostname. Skips discovery when provided. |
| `--port` | `GM_PORT` | Inverter communication port. Defaults to 8899. |
| `--family` | `GM_FAMILY` | GoodWe inverter family. Defaults to ET. |
| `--timeout` | `GM_TIMEOUT` | Timeout for inverter requests. Defaults to 1 second. |
| `--dtls` | `GM_DTLS` | Force DTLS mode. |
| `--info` | `GM_INFO` | Show inverter information before runtime values. |
| `--poll [SECONDS]` | `GM_POLL` | Poll runtime values. Defaults to 30 seconds when enabled without an interval. |
| `--broadcast-host` | `GM_BROADCAST_HOST` | Directed broadcast address for fallback discovery. |
| `--discovery-port` | `GM_DISCOVERY_PORT` | UDP discovery port. Defaults to 48899. |
| `--discovery-timeout` | `GM_DISCOVERY_TIMEOUT` | Timeout for directed fallback discovery. Defaults to 1 second. |
| `--sensors-file` | `GM_SENSORS_FILE` | Optional sensor ID shortlist. |

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
