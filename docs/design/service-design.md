# goodwe-mqtt Service Design

## Goal

Build `goodwe-mqtt` as a container-first service that reads local GoodWe inverter
data with the Python `goodwe` library, publishes selected values to MQTT, and
exposes diagnostics plus configuration readback through a small REST API and web
frontend.

The app should remain easy to run on a local network. A normal deployment should
be one container with one mounted YAML config file.

## Product Shape

`goodwe-mqtt` should be a long-running service, not primarily a command-line
diagnostic script.

```text
goodwe-mqtt container
  |
  |-- loads /config/configuration.yaml
  |-- connects to GoodWe inverter over local network
  |-- polls runtime data on a fixed interval
  |-- keeps latest inverter state in memory
  |-- publishes selected state to MQTT
  |-- serves REST diagnostics and config readback
  `-- serves a small frontend for human inspection
```

The current script already proves discovery, DTLS handling, YAML parsing, sensor
selection, and polling. The next design step is to split that code into service
modules instead of adding MQTT and HTTP behavior directly into `goodwe-mqtt.py`.

## Runtime Components

### Config Loader

Responsibility:

- Load YAML from `/config/configuration.yaml` by default.
- Validate config into typed application settings.
- Preserve the Home Assistant-style operator model: edit YAML and restart.
- Allow environment variables only for container wiring and secrets.

Environment variables should not be the main config surface. Use them for:

- `GM_CONFIG` to override config path.
- Optional broker username/password references.
- Optional web bind overrides for container platforms.

### GoodWe Client

Responsibility:

- Discover or connect to the configured inverter.
- Handle DTLS when required by the dongle.
- Read inverter information.
- Poll runtime data.
- Normalize GoodWe sensor objects into internal readings.

This should wrap the upstream `goodwe` library so the rest of the app does not
depend on library-specific object shapes.

### State Store

Responsibility:

- Hold the latest successful poll.
- Track connection status, last poll time, last success time, last error, and
  consecutive failure count.
- Track per-sensor status, including `ok`, `missing`, `stale`, and `error`.
- Provide snapshots to MQTT publishing, REST handlers, and the frontend.

This can start as an in-memory store. No database is needed for the first
container version.

### MQTT Publisher

Responsibility:

- Connect to the configured broker.
- Publish selected sensor readings from the latest poll.
- Publish availability/status.
- Publish Home Assistant MQTT discovery messages for configured sensors.

The initial implementation should publish retained scalar state values, retained
status values, a non-retained `last_seen` heartbeat, and retained Home Assistant
MQTT discovery configs.

Sensor values should publish on startup, MQTT reconnect, and value change. The
service should also publish `last_seen` on every successful poll so consumers can
distinguish stable values from a stopped poll loop.

Do not convert missing values to zero. If a sensor is unavailable in a response,
keep its last known value, mark that sensor stale/missing in diagnostics, and
publish a sensor status change separately.

### REST API

Responsibility:

- Report health and readiness.
- Return effective configuration with secrets redacted.
- Return latest inverter status and readings.
- Trigger a manual poll for diagnostics.

The REST API is for diagnostics and inspection. Configuration edits should stay
file-based for now.

### Frontend

Responsibility:

- Show connection status, poll timing, inverter identity, and latest readings.
- Show effective config in read-only form with secrets redacted.
- Show recent errors and MQTT publish status.
- Highlight stale or missing sensors, especially battery state of charge.

The frontend should be small and operational. It is not the primary automation
surface.

## Inverter Communication Reliability

The inverter/dongle path can be partially healthy:

- UDP discovery may find the dongle while inverter runtime reads fail.
- DTLS may be required by the dongle but still fail if inverter-to-battery
  communication is unhealthy.
- Some runtime values may be missing or wrong even when other values are valid.

Treat these as normal operating states. The service should not assume one failed
poll means the inverter is permanently offline, and it should not assume one
successful poll means every configured sensor is trustworthy.

Recommended status model:

| Status | Meaning |
| --- | --- |
| `online` | Last poll completed and all configured required sensors were present. |
| `degraded` | Poll completed, but one or more configured sensors were missing. |
| `stale` | No successful poll within the configured stale threshold. |
| `offline` | Consecutive poll failures exceeded the configured failure threshold. |

Per-sensor status is separate from service status:

| Sensor status | Meaning |
| --- | --- |
| `ok` | Sensor was present in the latest successful poll. |
| `missing` | Poll succeeded, but this sensor was not present in the response. |
| `stale` | Last value exists, but it has not been refreshed within the stale threshold. |
| `error` | Reading this sensor failed or returned an invalid value. |
| `unknown` | No value has ever been read for this sensor. |

Battery state of charge should be treated as a required sensor by default for a
hybrid inverter profile, because it is high-value and known to be affected by
inverter/battery communication problems.

## Container Contract

The target image should support:

```sh
docker run \
  --rm \
  -p 8080:8080 \
  -v "$PWD/config:/config:ro" \
  ghcr.io/adamsteen/goodwe-mqtt:latest
```

Defaults:

| Setting | Default |
| --- | --- |
| Config path | `/config/configuration.yaml` |
| Web bind | `0.0.0.0:8080` |
| Log format | Plain text |
| State storage | In memory |

Required network behavior:

- The container must be able to reach the inverter IP.
- If autodiscovery is used, the deployment network must allow the UDP discovery
  behavior needed by the GoodWe dongle.
- In Docker, a known inverter host is preferred over relying on broadcast
  discovery across bridge networking.
- The first container version should require or strongly prefer `inverter.host`;
  autodiscovery can remain a diagnostic path, but should not be the normal
  container startup dependency.

## Target YAML

Use `configuration.yaml` as the container config name to match the Home
Assistant mental model.

```yaml
inverter:
  host: <inverter-ip>
  port: 8899
  family: ET
  timeout: 1
  dtls: true

poll:
  interval: 30

mqtt:
  host: <mqtt-broker-host>
  port: 1883
  username: goodwe
  password: ${GM_MQTT_PASSWORD}
  client_id: goodwe-mqtt
  base_topic: goodwe/inverter
  retain: true
  availability_topic: goodwe/inverter/status
  last_seen_topic: goodwe/inverter/last_seen
  precision:
    default: 3
    units:
      W: 0
      "%": 0
      Hz: 2
    sensors:
      meter_e_total_imp: 3
      meter_e_total_exp: 3
  discovery:
    enabled: false
    prefix: homeassistant
    device_id: goodwe_inverter
    device_name: GoodWe Inverter

sensors:
  - id: battery_soc
    topic: battery/soc
    required: true
  - id: ppv
    topic: solar/power
  - id: pbattery1
    topic: battery/power
  - id: house_consumption
    topic: house/power
  - id: active_power
    topic: grid/power

api:
  enabled: true
  host: 0.0.0.0
  port: 8080

frontend:
  enabled: true

diagnostics:
  stale_after: 120
  offline_after_failures: 3
```

Notes:

- `!secret` and `secrets.yaml` can be a later feature. The first version should
  support literal values and environment variable substitution for secrets.
- `sensors` should support simple strings for compatibility with the current
  config:

```yaml
sensors:
  - battery_soc
  - ppv
```

Internally, simple strings should normalize to:

```yaml
sensors:
  - id: battery_soc
    topic: battery_soc
  - id: ppv
    topic: ppv
```

## MQTT Topic Contract

Initial state topics:

```text
<base_topic>/<sensor_topic>
```

Example:

```text
goodwe/inverter/battery/soc          86
goodwe/inverter/solar/power          4210
goodwe/inverter/battery/power        -680
goodwe/inverter/house/power          1100
goodwe/inverter/grid/power           -2400
goodwe/inverter/status               online
goodwe/inverter/last_seen            2026-06-30T10:15:30Z
```

Initial sensor status topics:

```text
<base_topic>/<sensor_topic>/status
```

Example:

```text
goodwe/inverter/battery/soc/status   missing
```

Optional later JSON snapshot topic:

```text
goodwe/inverter/state
```

Payload:

```json
{
  "timestamp": "2026-06-30T10:15:30Z",
  "status": "degraded",
  "readings": {
    "battery_soc": {
      "value": 86,
      "unit": "%",
      "status": "stale",
      "last_seen": "2026-06-30T10:05:30Z"
    },
    "ppv": {
      "value": 4210,
      "unit": "W",
      "status": "ok",
      "last_seen": "2026-06-30T10:15:30Z"
    }
  }
}
```

Do not publish the JSON snapshot in the first implementation unless a concrete
consumer needs it. It includes timestamps and would publish every poll, which
works against the default change-only MQTT behavior. Use the REST API for
diagnostic snapshots in v1.

Value topics should compare the final serialized payload and publish only on
change. Status and `last_seen` topics follow their own rules:

- `last_seen` publishes after every successful poll.
- Service `status` publishes when it changes and is retained.
- Sensor status publishes when it changes and is retained.

Numeric values should be serialized with configurable precision before change
comparison. If no precision is configured, use 3 decimal places. Precision can
be overridden by unit or by sensor ID, with sensor ID taking precedence over unit.
Integer/string values should serialize without decimal padding.

## REST API Contract

Initial endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Process is alive. |
| `GET` | `/ready` | Inverter and MQTT connections are usable. |
| `GET` | `/api/config` | Effective config with secrets redacted. |
| `GET` | `/api/status` | Connection, poll, and publish status. |
| `GET` | `/api/inverter` | Inverter identity and capabilities when known. |
| `GET` | `/api/readings` | Latest normalized readings. |
| `POST` | `/api/poll` | Trigger one immediate poll. |

The frontend can be static files served from `/`, with API calls under `/api`.

## Suggested Python Layout

```text
goodwe_mqtt/
  __init__.py
  app.py              # service startup and lifecycle
  config.py           # YAML loading, normalization, validation
  goodwe_client.py    # upstream goodwe wrapper
  state.py            # in-memory state snapshots
  mqtt.py             # broker connection and publish logic
  api.py              # FastAPI routes
  frontend/           # built static assets or simple templates

tests/
  test_config.py
  test_mqtt_topics.py
  test_state.py

Dockerfile
docker-compose.example.yaml
configuration.example.yaml
```

Recommended dependencies:

- `goodwe[dtls]` for inverter access.
- `PyYAML` or `ruamel.yaml` for config.
- `pydantic` for typed validation.
- `fastapi` and `uvicorn` for REST/frontend serving.
- `paho-mqtt` or `asyncio-mqtt` for MQTT publishing.

Prefer an async architecture because the current inverter access is already
async and FastAPI can share the same event loop cleanly.

## Implementation Sequence

1. Keep the current CLI working, but move reusable GoodWe logic into a package.
2. Replace ad hoc config merging with a typed `AppConfig`.
3. Add MQTT config and publish from the existing polling loop.
4. Add the in-memory state store and status tracking.
5. Add Home Assistant MQTT discovery for configured sensors.
6. Add REST endpoints backed by the state store.
7. Add a minimal read-only frontend.
8. Add `Dockerfile`, `docker-compose.example.yaml`, and
   `configuration.example.yaml`.

After that first container version is stable, decide whether to add MQTT JSON
snapshot publishing.

## Non-Goals For First Container Version

- Editing YAML from the browser.
- Historical charts or database storage.
- Multi-inverter support.
- Authentication on the local diagnostics frontend.
- Runtime config editing or reload from the API/frontend.
- MQTT JSON snapshot publishing unless a real consumer needs it.

These can be added later without changing the core shape if config, state, MQTT,
and API are separated from the start.
