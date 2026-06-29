# Power optimization (battery / solar)

This device is designed to run on a UPS-HAT battery recharged by a small solar
panel. When the panel cannot supply enough surplus to both run the Pi and
recharge the battery, the system drifts into an overnight energy deficit. The
biggest lever we control in software is the Pi's **average draw**, especially
while the truck is parked and on battery.

The services now back off their highest-cost work when the truck is parked and
the battery is discharging, and immediately return to full cadence when the
truck is driving or the battery is charging. All behavior is tunable per device
via Balena environment variables — no code changes required.

## Software power-saving knobs

### `telematics-edge` — IMU duty-cycling while parked
The accelerometer is polled at 10 Hz while driving (for harsh-event detection)
but is throttled while parked to let the CPU reach deep idle — the dominant
overnight draw. Motion still wakes the device back to full rate within one
parked interval.

| Variable | Description | Default |
| :--- | :--- | :--- |
| `IMU_PARKED_SAMPLE_INTERVAL_SECONDS` | Accelerometer poll interval while parked. Larger = lower draw, slower motion-wake latency. | `1.0` |

### `telematics-edge` — Sentry Mode (software sleep)
The Pi Zero 2 W has no true hardware deep-sleep. Sentry Mode is the software
equivalent: when the truck has been idle (no IMU motion, GPS below the moving
threshold) for `SENTRY_IDLE_TIMEOUT_SECONDS`, `telematics-edge` acts as a master
"sentry" and **stops the heavy containers** in `SENTRY_PAUSE_SERVICES` via the
local Balena Supervisor API. Stopping `ble-sensor` removes the dominant radio +
CPU-polling load instantly; stopping `sync-service` lets the CPU reach deep idle
between checks. The worker then polls on a short cadence and, on the first IMU
harsh event / charging / WiFi return, **restarts** the containers within
`SENTRY_WAKE_INTERVAL_SECONDS`.

Before stopping `sync-service`, the device emits one heartbeat marked
`sentry_mode.active=true` and waits `SENTRY_SYNC_FLUSH_GRACE_SECONDS` so that
"going to sleep" announcement reaches the cloud — the dashboard then shows the
truck as **Sleeping** rather than offline while it is silent. Breadcrumbs logged
during sleep flush on wake.

Enabling requires the `io.balena.features.supervisor-api: '1'` label on the
`telematics-edge` service (already in `docker-compose.yml`); without it the
worker logs a warning and no-ops.

| Variable | Description | Default |
| :--- | :--- | :--- |
| `SENTRY_MODE_ENABLED` | Master switch (opt-in). | `false` |
| `SENTRY_IDLE_TIMEOUT_SECONDS` | Idle duration before sleeping. Recommended 300–600 (5–10 min). | `300` |
| `SENTRY_WAKE_INTERVAL_SECONDS` | Poll cadence while watching for motion; also the restart latency on wake. | `10` |
| `SENTRY_SYNC_FLUSH_GRACE_SECONDS` | Window for `sync-service` to ship the sleep announcement before it is stopped. | `90` |
| `SENTRY_PAUSE_SERVICES` | Comma list of services to stop. Set to `ble-sensor` to keep cloud sync alive (visibility) instead of max savings. | `ble-sensor,sync-service` |

> ⚠️ Tradeoff: with the default pause-set, a sleeping truck stops uploading
> until it wakes, so the dashboard sees it as Sleeping (last-known position +
> battery) rather than live. Set `SENTRY_PAUSE_SERVICES=ble-sensor` to keep
> `sync-service` running if continuous visibility matters more than the last bit
> of overnight draw.

### `ble-sensor` — scan throttling on battery
BLE active scanning is the largest radio consumer. On battery the scan cadence
is stretched (further when the battery is low) and the scan window is shortened.
Full cadence resumes when charging is detected. Battery state is read from the
shared `power_readings` table written by `power-monitor`.

| Variable | Description | Default |
| :--- | :--- | :--- |
| `BLE_BATTERY_SAVER_ENABLED` | Master switch for BLE battery throttling. | `true` |
| `BLE_POLL_INTERVAL_DISCHARGING_SECONDS` | Poll interval on battery when SOC is normal. | `300` |
| `BLE_POLL_INTERVAL_LOW_BATTERY_SECONDS` | Poll interval on battery when SOC is below `BLE_LOW_BATTERY_SOC_PCT`. | `900` |
| `BLE_LOW_BATTERY_SOC_PCT` | SOC (%) below which the low-battery interval applies. | `25` |
| `BLE_SCAN_DURATION_LOW_POWER_SECONDS` | Scan window length while throttled. | `10` |

> ⚠️ Tradeoff: when throttled on battery, BLE **key-beacon wake** latency grows
> to roughly the active poll interval. This is acceptable when the priority is
> overnight battery life; lower the intervals (or set
> `BLE_BATTERY_SAVER_ENABLED=false`) if faster beacon wake matters more.

### `power-monitor` — adaptive sampling
The INA219 is sampled every 2 s near the shutdown thresholds (and on
charging-state changes), but backs off to a slower idle cadence when the battery
is healthy and far from the cliff. This cuts I2C reads and — more importantly —
SD-card writes (one per sample). Brownout-shutdown safety is unchanged because
the fast band covers the approach to the shutdown thresholds, and the coulomb
counter integrates over real elapsed time regardless of interval.

| Variable | Description | Default |
| :--- | :--- | :--- |
| `POWER_SAMPLE_INTERVAL_SECONDS` | Fast sampling interval (near thresholds / on state change). | `2` |
| `POWER_SAMPLE_INTERVAL_IDLE_SECONDS` | Idle sampling interval when the battery is healthy. | `20` |
| `POWER_FAST_SAMPLE_SOC_MARGIN_PCT` | Stay fast while SOC is within this margin above the shutdown SOC trip. | `15` |
| `POWER_FAST_SAMPLE_VOLTAGE_MARGIN_V` | Stay fast while voltage is within this margin above the shutdown voltage trip. | `0.20` |

## Host-level tuning (Balena, outside the containers)

These cut draw at the OS/board level and are applied via Balena **device
configuration variables** / the host `config.txt`, not by the application
containers. Apply them per device or fleet in the Balena dashboard.

- **Disable HDMI output.** The Pi Zero 2 W is headless in this deployment.
  Saves a steady ~25–30 mA. Run `tvservice -o` at boot, or disable the video
  pipeline in `config.txt`.
- **Disable the onboard ACT/PWR LEDs.** A small but free saving on a 24/7
  device. Set the relevant `dtparam=act_led_trigger=none` /
  `dtparam=act_led_activelow` (and PWR LED) entries via
  `BALENA_HOST_CONFIG_*` variables.
- **Enable Wi-Fi power-save.** `iw dev wlan0 set power_save on`. Meaningful on
  the Pi Zero 2 W's onboard radio when idle/parked.
- **Optional: conservative underclock / `ondemand` governor.** Lowering the max
  clock and using the on-demand CPU governor reduces draw during the long idle
  stretches; validate it does not affect sensor timing.

> Leave **Bluetooth enabled** — `ble-sensor` requires it.

## Sizing reality check

Software savings reduce the deficit but cannot manufacture energy. For a Pi
Zero 2 W running 24/7, a 10 W panel is undersized; a 20–30 W panel plus the
existing battery buffer gives genuine margin to run the device *and* recharge.
Confirm the charge module runs the Pi off the **battery** (load-sharing) while
the panel charges, rather than running the Pi directly off the panel — the
latter browns out whenever a cloud passes.
