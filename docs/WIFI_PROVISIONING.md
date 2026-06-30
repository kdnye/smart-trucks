# WiFi Provisioning — Field Guide

How to get a truck/warehouse Pi back onto WiFi when it can't reach the network,
using the on-device **setup hotspot + captive portal** run by the
`wifi-provisioner` container.

> **One owner of `wlan0`.** Only the `wifi-provisioner` service manages WiFi and
> the setup access point. The `telematics-edge` service never touches the radio —
> it just observes connectivity. If the portal misbehaves, look at the
> `wifi-provisioner` logs, not the edge logs.

---

## 1. What it does

When the Pi has been **offline** (no internet) for longer than the grace period
(~3 minutes by default), `wifi-provisioner`:

1. First tries to reconnect to any **saved** WiFi network that's in range.
2. If that fails, it raises a temporary **setup Wi-Fi access point** on the Pi.
3. It serves a **captive-portal web page** on that AP where you add or remove the
   Pi's saved WiFi networks.
4. Periodically it drops the AP and retries the saved networks; once the Pi gets
   back online the AP disappears on its own.

The setup AP is **only up while the Pi is offline**. If the Pi is already on
WiFi, there is no hotspot to connect to — that's expected.

---

## 2. Quick start (provision a Pi in the field)

1. **Wait for the hotspot.** After the Pi loses WiFi, give it up to ~3–4 minutes
   for the setup AP to appear (grace period + first attempt).
2. **Join the AP** from your phone/laptop WiFi list:
   - **SSID:** `smart-truck-setup-<vehicle_id>` (all lowercase — see §3).
   - **Password:** none — it's an **open** network by default.
3. **Open the portal.** Most phones pop the captive-portal page automatically.
   If not, browse to **`http://10.42.0.1/`**.
4. **Add the WiFi network** the Pi should join (yard/warehouse SSID + password),
   and enter the **portal PIN** (see §4) to authorize the change.
5. The page confirms the save and tells you the AP will drop shortly. The Pi
   then tries to join the network you added; if it succeeds, the setup AP
   disappears and the Pi is back online.

---

## 3. Finding the setup SSID

The SSID is derived from the device's `VEHICLE_ID`:

```
smart-truck-setup-<vehicle_id lowercased>
```

Example: `VEHICLE_ID=Truck_Test` → SSID **`smart-truck-setup-truck_test`**.

If `VEHICLE_ID` isn't set, it falls back to `smart-truck-setup-unknown_truck`
(a sign the device's `VEHICLE_ID` needs to be set in Balena).

You can override the name with the `WIFI_PROVISIONER_SETUP_SSID` env var, but the
default per-vehicle name is recommended so each truck's AP is identifiable.

---

## 4. Finding the portal PIN

Joining the open AP lets anyone reach the page, so **changing** the saved
networks (add/forget) requires a **PIN**. By default the PIN is derived
deterministically from the `VEHICLE_ID`, so it's stable per device.

**Option A — read it from the logs (most reliable).** At startup the service logs:

```
wifi-provisioner Setup hotspot SSID=smart-truck-setup-truck_test PIN=482913
```

Get it from the Balena dashboard logs, or:

```
balena ssh <uuid> wifi-provisioner
# then look at the service logs for the "Setup hotspot ... PIN=" line
```

**Option B — compute it from the VEHICLE_ID** (same algorithm the device uses —
sha256 of the *exact* `VEHICLE_ID`, original casing, first 8 hex digits mod 1e6,
zero-padded to 6 digits):

```bash
python3 -c "import hashlib,sys; v=sys.argv[1]; print(f'{int(hashlib.sha256(v.encode()).hexdigest()[:8],16)%1000000:06d}')" "Truck_Test"
```

> Note: the **SSID** uses the lowercased VEHICLE_ID; the **PIN** uses the
> VEHICLE_ID exactly as configured. Match the case of `VEHICLE_ID` when computing.

**Option C — set a fixed PIN** for the fleet via `WIFI_PROVISIONER_SETUP_PIN`
(Balena env var). If set, it overrides the derived PIN. Handy if you'd rather
hand techs one known PIN than look it up per truck.

---

## 5. Using the portal

- **Add a network:** pick a nearby SSID (or type one), enter its WiFi password
  (WPA password must be **8–63 characters**; leave blank for an open network),
  optionally set a priority (higher wins when several saved networks are in
  range), enter the **PIN**, and submit.
- **Forget a network:** each saved network has a *Forget* button; it also
  requires the **PIN**.
- Saved networks are stored as NetworkManager profiles and are tried
  automatically whenever they're in range — including **roaming** between trucks
  and warehouses that broadcast the same SSID (saved profiles use infinite
  autoconnect retries so they keep trying after going out of range).

After you save, the portal sets a flag, the AP is dropped within ~45 s, and the
Pi attempts to join. If it can't, the setup AP comes back so you can try again.

---

## 6. Configuration (Balena env vars)

| Variable | Default | Purpose |
|---|---|---|
| `VEHICLE_ID` | `UNKNOWN_TRUCK` | Drives the SSID and (by default) the PIN |
| `WIFI_PROVISIONER_SETUP_SSID` | `smart-truck-setup-<vehicle_id>` | Setup AP name |
| `WIFI_PROVISIONER_SETUP_PSK` | *(unset → open AP)* | Optional password to join the setup AP |
| `WIFI_PROVISIONER_SETUP_PIN` | *(derived from VEHICLE_ID)* | PIN to authorize add/forget in the portal |
| `WIFI_PROVISIONER_GRACE_SECONDS` | `180` | Offline time before the AP is raised |
| `WIFI_PROVISIONER_CHECK_INTERVAL_SECONDS` | `30` | Connectivity poll cadence |
| `WIFI_PROVISIONER_HOTSPOT_RETRY_INTERVAL_SECONDS` | `120` | How long the AP stays up before a retry of saved nets |
| `WIFI_PROVISIONER_HOTSPOT_RETRY_PROBE_SECONDS` | `45` | How long to test saved nets before re-raising the AP |
| `WIFI_PROVISIONER_PORTAL_PORT` | `80` | Portal HTTP port |
| `WIFI_PROVISIONER_DRY_RUN` | `false` | Log actions without touching NetworkManager (dev) |

"Offline" means the Pi can't open a TCP connection to `8.8.8.8:53` — i.e. no
working internet, not merely no association. An AP with no upstream internet will
still be treated as offline.

---

## 7. How it works (under the hood)

- The setup AP uses NetworkManager AP mode with `ipv4.method=shared`, so NM runs
  its own DHCP + DNS and puts the Pi at **`10.42.0.1`** on a `10.42.0.0/24`
  subnet. Any DNS lookup resolves to the Pi, which is what triggers the
  iOS/Android "sign in to network" captive-portal prompt.
- Health/status endpoint: `GET http://10.42.0.1/healthz` returns
  `{hotspot_active, last_up_age_seconds, setup_ssid}` while the AP is up.

---

## 8. Troubleshooting

**The setup SSID never appears.**
1. Confirm the Pi is genuinely offline and you've waited past the grace period
   (~3–4 min). If it's still on a (bad) WiFi, it won't raise the AP.
2. Check the `wifi-provisioner` logs (Balena dashboard or `balena ssh`). You're
   looking for one of:
   - `Captive portal listening on :80` — the service is up.
   - `Hotspot smart-truck-setup-… is up. Browse to http://10.42.0.1/` — AP raised.
   - `Failed to start hotspot: …` — NetworkManager refused to bring up AP mode;
     the message includes the `nmcli` error. Common causes: WiFi regulatory
     domain not set, `wlan0` marked unmanaged, or the radio is hard/soft-blocked.
   - `Failed to bind portal on :80` — something else holds port 80 on the host.
   - `nmcli is not available …` — the container is missing NetworkManager.
3. On the host: `nmcli device status` (is `wlan0` *managed*?), `nmcli radio wifi`
   (is the radio on?), and `nmcli -f WIFI-HW,WIFI g` for rfkill state.

**I see the AP but the portal page won't load.** Browse explicitly to
`http://10.42.0.1/` (not `https`). Make sure your device actually joined the
`smart-truck-setup-…` AP and isn't auto-switching back to cellular.

**"Bad PIN."** Use the PIN from the logs (§4 Option A) or compute it from the
exact `VEHICLE_ID` casing (§4 Option B). A fixed `WIFI_PROVISIONER_SETUP_PIN`
avoids per-truck lookups.

**I saved a network but it didn't reconnect.** Re-check the password and that the
SSID is actually in range. The AP re-raises after a failed attempt so you can
retry. Priority only matters when multiple saved networks are visible at once.

**It keeps flapping between the AP and trying to reconnect.** That's the normal
retry cycle (`HOTSPOT_RETRY_INTERVAL` up, then a `HOTSPOT_RETRY_PROBE` window
testing saved networks). Add a working network and it will settle once online.
