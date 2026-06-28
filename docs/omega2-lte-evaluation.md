# Omega2 LTE as a Raspberry Pi Replacement — Evaluation

**Date:** 2026-06-28
**Status:** Evaluation / recommendation
**Scope:** Whether the Onion **Omega2 LTE** can replace the Raspberry Pi (Zero 2 W / 3B)
edge node that runs this telematics stack.

---

## TL;DR

The Omega2 LTE is an attractive **connectivity and positioning** module — it folds the two
weakest parts of our current rig (intermittent Wi-Fi backhaul and the finicky GPS HAT) into a
single board with built-in **4G LTE Cat 4** and **multi-constellation GNSS**. But as a *drop-in
replacement* for the Pi it fails on two hard requirements of our actual workload:

1. **No Bluetooth radio.** Our core mission is BLE asset/sensor inventory (Govee
   hygrometers via `bleak`/BlueZ). The Omega2 LTE ships Wi-Fi only — there is no
   Bluetooth/BLE on the Omega2S+ module. **This is a dealbreaker for the base-model stack.**
2. **Cannot run our containerized stack.** The edge runs ~9 Docker containers under
   **Balena** on a 512 MB-RAM ARM Pi. The Omega2 LTE is a **MIPS** SBC with **128 MB RAM /
   32 MB flash** running **OpenWRT** — no Docker, no Balena, no glibc/ARM wheels.

**Recommendation:** Do **not** adopt the Omega2 LTE as a 1:1 Pi replacement. If the driving
pain is cellular backhaul + GPS, the lower-risk move is to **keep the Pi and add an LTE modem**
(USB dongle or HAT) — optionally with a GNSS that we already understand. Reserve the Omega2 LTE
for a *future, scoped* "connectivity gateway" role or a ground-up rewrite, not a swap.

---

## What the Pi actually does today

Sources: `smart-trucks/readme.md`, `docker-compose.yml`, per-container `main.py` /
`requirements.txt`, and the store-and-forward contract in
`motive-dashboard/scripts/edge/edge_store_forward.py`.

| Capability | How it's done now | Hard dependency |
|---|---|---|
| **BLE sensor/asset scan** | `ble-sensor` container, `bleak.BleakScanner` (BlueZ + D-Bus) | **Bluetooth radio** + BlueZ |
| **GPS** | `gps-multiplexer` reads NMEA from UART (`/dev/ttyAMA0`), rebroadcasts on TCP 2947 | UART serial / GNSS source |
| **IMU / motion** | `telematics-edge` reads BerryIMU over **I2C** (`smbus2`) | I2C bus |
| **Power / UPS** | `power-monitor` (parked) reads INA219 over **I2C** | I2C bus |
| **Store-and-forward** | SQLite **WAL** queues (`gps_points`, `heartbeats`, `ble_scans`, `edge_health`) drained by `sync-service` | Writable storage + SQLite |
| **Backhaul** | `sync-service` POSTs batches to cloud ingest over **Wi-Fi** | IP connectivity |
| **Orchestration / OTA** | **Balena** fleet, Docker Compose, privileged containers, `cpuset` pinning | Docker + Balena host OS |
| **Runtime** | Python 3 async: `uvloop`, `aiohttp`, `bleak`, `pyserial`, `pynmea2`, `smbus2`, `aiosqlite` | glibc + ARM wheels |

Two things stand out: the system is **Bluetooth-centric** (BLE inventory is the product), and
it is **container/Balena-native** (every capability is a privileged container with device
pass-through and OTA updates).

---

## Omega2 LTE capability fit

| Requirement | Omega2 LTE | Verdict |
|---|---|---|
| **Bluetooth / BLE scan** | **None.** Wi-Fi 2.4 GHz only; no BT on Omega2S+ | ❌ **Blocker** |
| LTE backhaul | LTE Cat 4 (150/50 Mbps), Nano-SIM, Linux-managed | ✅ **Major win** |
| GNSS | Multi-constellation (GPS/GLONASS/BeiDou/Galileo/QZSS), U.FL antenna | ✅ Win (NMEA-over-USB-serial; requires AT init) |
| I2C (IMU, INA219) | Yes — I2C, SPI, UART, GPIO exposed | ✅ Electrically OK |
| Storage for SQLite queue | 32 MB flash; microSD slot for expansion | ⚠️ microSD **required** |
| RAM for our workload | **128 MB** | ⚠️ Very tight |
| Docker / Balena | **No** (OpenWRT/MIPS) | ❌ Blocker for current arch |
| Python runtime | Python3 on OpenWRT (musl libc, MIPS) | ⚠️ Heavy deps need OpenWRT packaging / source builds |
| Power / battery | USB-C + LiPo (JST-PH) with charging | ✅ Overlaps UPS HAT role |

### Where it genuinely helps
- **LTE removes our biggest operational weakness.** Store-and-forward exists *because* Wi-Fi is
  intermittent on moving trucks. Always-on cellular shrinks queue depth, tightens location
  latency, and removes Wi-Fi provisioning (`wifi-provisioner`) as a field chore.
- **Built-in GNSS removes the GPS-HAT footguns.** No BerryGPS UART wiring, no
  `dtoverlay=disable-bt` vs mini-UART clock instability (see `readme.md` UART pitfall).
- **LiPo + charging on-board** overlaps the parked `power-monitor`/UPS HAT function.

### Where it breaks

1. **No Bluetooth = no BLE inventory.** `bleak` needs a BlueZ/D-Bus stack on a real BT radio.
   The Omega2 LTE has neither. You'd have to bolt on a **USB BT dongle** *and* get **BlueZ +
   D-Bus + Python `bleak`** working on **OpenWRT/MIPS/musl** — a heavy, poorly-trodden port.
   Without that, the product's primary capability is gone.
2. **The Balena/Docker model doesn't transfer.** Nine privileged containers, `cpuset` pinning,
   device pass-through, and OTA via Balena assume a Docker host with ≥512 MB RAM. OpenWRT on
   128 MB MIPS runs **native processes**, not containers. Adopting the Omega2 LTE means a
   **ground-up rewrite** of the edge into OpenWRT init services and a new OTA story
   (`opkg`/sysupgrade), not a lift-and-shift.
3. **Resource headroom is thin.** 128 MB RAM for concurrent async Python (`uvloop` + `aiohttp`
   + scanner + SQLite WAL) is doable only with care; today we even pin CPUs (`cpuset: "1,2"`)
   for the GPS/edge path on a more capable Pi.
4. **Toolchain friction.** Our wheels (`bleak`, `uvloop`, `pynmea2`, `smbus2`, `pg8000`, etc.)
   are glibc/ARM; OpenWRT is musl/MIPS. Expect OpenWRT package builds or cross-compilation, and
   re-validation of every dependency.
5. **GNSS is modem-managed, but still exposes NMEA-over-USB-serial.** It's not a hardware UART,
   but the Quectel modem streams standard NMEA on a virtual USB serial port (typically
   `/dev/ttyUSB1`) once GNSS is enabled via AT commands (e.g. `AT+QGPS=1` on the AT port). Our
   `gps-multiplexer` already probes `/dev/ttyUSB0`/`/dev/ttyACM0` candidates and rebroadcasts on
   TCP 2947, so it can read this stream by re-pointing at the virtual port — no rewrite, just a
   one-time boot init step to turn GNSS on. (A minor integration item, not a blocker.)

---

## Options

### Option A — Keep the Pi, add LTE (recommended)
Add a cellular modem (USB LTE dongle or a Pi LTE HAT) to the existing Pi Zero 2 W / 3B.
- ✅ Keeps Bluetooth, I2C, Balena/Docker, and the entire codebase intact.
- ✅ Captures the #1 benefit (always-on backhaul) with the least risk.
- ✅ Optional: add a UART/USB GNSS we already speak NMEA to, or keep the BerryGPS.
- ⚠️ Slightly more BOM/wiring than an all-in-one board; manage the modem as another device.

### Option B — Omega2 LTE as a dedicated connectivity/GPS gateway (future, scoped)
Use the Omega2 LTE only for **LTE + GNSS**, and let it share its cellular link (Wi-Fi AP /
ethernet) with a Pi that still does **BLE + I2C + containers**.
- ✅ Plays to the Omega2's strengths; no Python/BLE port required.
- ❌ Two boards = more cost, power, and packaging; only worth it if a single LTE link should
  serve multiple on-truck devices.

### Option C — Full migration to Omega2 LTE (not recommended now)
Rewrite the edge as native OpenWRT services, add a USB BT dongle + BlueZ for BLE, repackage all
Python deps, and replace Balena OTA.
- ❌ High effort, high risk, loses container/OTA maturity, fights 128 MB RAM — for a board whose
  only unique advantage over Option A is integration, not capability.

---

## Verdict

For *our* workload — **BLE inventory first, containerized/Balena-managed, GPS+IMU+UPS over the
Pi's interfaces** — the Omega2 LTE is **not a drop-in Pi replacement**. Its real value is
connectivity and positioning, which we can capture far more cheaply and safely by **adding LTE to
the Pi (Option A)**. Revisit the Omega2 LTE only if we deliberately want a separate connectivity
gateway (Option B) or are prepared to fund a from-scratch OpenWRT edge with external Bluetooth.

> **Open questions to close before any purchase:** (a) does the deployment region match the
> NA vs Global LTE variant and our carrier's bands; (b) confirmed BLE-via-USB-dongle support on
> the Omega2's OpenWRT build; (c) measured RAM headroom for our async Python under load.
