# FSI BLE Anchor Firmware (M5Stack ATOM Lite)

ESP32 "anchor" nodes that give a Pi site (truck or warehouse) **multiple BLE
vantage points**. Each anchor passively scans BLE asset beacons, aggregates
per-device RSSI over a window, and ships the batch over an **ESP-NOW** link to
a **gateway** node plugged into the Raspberry Pi's USB port. The Pi's
`anchor-bridge` co-process reads the gateway's NDJSON stream, publishes it to
the local MQTT broker, and `ble-sensor` folds the observations into the same
`ble_device_observations` store the Pi's own scanner uses — every anchor is
one more `observer_id`.

With 3–7 anchors deployed, the resolver can rank observers per asset
(nearest-anchor zone presence now; trilateration to (x,y) later once anchor
positions are configured in `ble_anchor_registry`).

```
asset beacons ──BLE──▶ anchors ──ESP-NOW──▶ gateway ──USB serial──▶ Pi
                                                      (NDJSON)
```

## Hardware

- **M5Stack ATOM Lite** (ESP32-PICO-D4, SK6812 status LED on GPIO27, button on
  GPIO39). Any ESP32 dev board works, but LED/button pins assume ATOM Lite.
- Anchors are powered by any USB supply (5 V). The **gateway** must be on the
  Pi's USB port (it enumerates as `/dev/ttyACM0` / `/dev/ttyUSB0`).

## Build & flash

```bash
cd anchor-firmware
pio run                # build
pio run -t upload      # flash (auto-detects the serial port)
pio device monitor     # 115200 baud
```

## Roles

One binary, two roles, stored in NVS (survives reflash of the same image):

| Role | What it does |
|------|--------------|
| `anchor` (default) | Scan BLE → aggregate → ESP-NOW unicast to gateway |
| `gateway` | Receive anchor frames → NDJSON over USB serial; also scans BLE itself |

Set the role either way:

- Serial command: `role gateway` or `role anchor` (device reboots).
- **No laptop:** hold the front button while powering on for ~3 s — the LED
  flashes purple 3× and the role toggles.

Identity is derived from the factory MAC: `anchor-a1b2c3` (last 3 bytes). This
is the `anchor_id` used everywhere downstream (`ble_anchor_registry`,
`observer_id` on sightings).

## LED codes

| Pattern | Meaning |
|---------|---------|
| Yellow | Booting |
| Blinking blue | Anchor: searching for gateway beacon |
| Dim green | Anchor: linked to gateway |
| Green blip | Anchor: batch delivered |
| Dim white | Gateway idle |
| Blue blip | Gateway: frame received from an anchor |
| 3× purple | Role toggled via button |

## Serial commands (115200, newline-terminated)

Text (for humans on `pio device monitor`):

```
show                # print node_info JSON
role gateway|anchor # persist role + reboot
channel <1-13>      # persist Wi-Fi/ESP-NOW channel + reboot (site-wide!)
interval <5-300>    # report window seconds (gateway also pushes to anchors)
floor <-120..0>     # RSSI floor dBm (gateway also pushes to anchors)
reboot
```

JSON (used by the Pi's `anchor-bridge`):

```json
{"cmd":"config","report_interval_s":15,"rssi_floor":-90}
{"cmd":"show"}
```

On the gateway, a `config` command applies locally **and** is broadcast to all
anchors over ESP-NOW (`MSG_CONFIG`), so fleet-wide tuning needs no reflashing.

## Gateway NDJSON output (contract with `anchor-bridge`)

One JSON object per line at 115200 baud:

```json
{"type":"anchor_scan","anchor_id":"anchor-a1b2c3","gateway_id":"anchor-ffeedd",
 "seq":42,"part":0,"part_count":1,"window_s":15,"link_rssi":-58,
 "devices":[{"mac":"AA:BB:CC:DD:EE:FF","rssi":-67,"max_rssi":-60,"count":5}]}

{"type":"anchor_heartbeat","anchor_id":"anchor-a1b2c3","gateway_id":"anchor-ffeedd",
 "seq":7,"uptime_s":3600,"free_heap":123456,"scan_devices_total":9000,
 "report_interval_s":15,"rssi_floor":-90,"channel":1,"fw":"1.0.0","link_rssi":-58}

{"type":"gateway_status","gateway_id":"anchor-ffeedd","fw":"1.0.0","uptime_s":3600,
 "free_heap":123456,"anchors_recent":4,"channel":1,"report_interval_s":15,
 "rssi_floor":-90}

{"type":"node_info", ...}   // reply to `show`
```

Notes:

- `rssi` is the **median** RSSI over the window (robust to multipath);
  `max_rssi` and `count` are provided for diagnostics/model fitting.
- `link_rssi` is the ESP-NOW RSSI of the anchor as heard **at the gateway** —
  a free anchor↔gateway ranging signal. `null` on the gateway's own scans.
- Anchors have no clock: the Pi stamps `captured_at` at receipt. The window
  spans at most `window_s` seconds before that.
- A snapshot larger than 24 devices is split into `part_count` frames sharing
  one `seq`.

## ESP-NOW protocol (anchor ↔ gateway)

- Packed binary frames (`src/frames.h`), ≤250 B, magic `FA`, versioned.
- **Pairing is zero-config:** the gateway broadcasts `MSG_GATEWAY_BEACON`
  every 5 s; anchors lock onto the sender MAC and unicast to it. If the
  gateway disappears (>30 s), anchors keep scanning and **keep accumulating**
  — the aggregation window is only cleared after confirmed delivery, so brief
  outages lose nothing.
- All nodes at a site must share one Wi-Fi channel (default 1; `channel N`).
- Link RSSI is sniffed via a promiscuous RX hook (Arduino core 2.x's ESP-NOW
  callback does not expose RSSI).

## Deployment recipe (per site)

1. Flash all ATOMs with this firmware.
2. Pick one as gateway (`role gateway`), plug it into the Pi's USB port.
3. Power the remaining anchors around the truck/warehouse (3–7 recommended);
   they auto-pair — LED turns dim green.
4. Register the anchors to the Pi in the dashboard (**BLE Assets → Anchor
   Registry**): unknown anchors auto-appear inactive for approval; set label,
   parent Pi, and optionally an `environment_class` override and (Phase D)
   x/y/z position for trilateration.

## Future (Phase D hooks)

`ble_anchor_registry.position_{x,y,z}_ft` plus per-anchor calibrated
RSSI→distance estimates enable least-squares (x,y) multilateration. The
firmware already reports everything needed (median RSSI per anchor per asset);
the solver lands cloud-side.
