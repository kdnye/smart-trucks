"""Thin wrappers around `nmcli` for the wifi-provisioner service.

NetworkManager is the source of truth for the device's saved WiFi networks and
for the on-demand setup hotspot. Every saved client network is a normal
`802-11-wireless` connection profile with `connection.autoconnect=yes`; the
priority field lets NM pick the strongest one when multiple are in range.

The hotspot uses a dedicated profile name (default `_setup_ap`) and
`ipv4.method=shared`, which has NM bring up hostapd + dnsmasq internally —
no extra apt packages needed.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

NMCLI_TIMEOUT_SECONDS = 20
HOTSPOT_PROFILE_NAME = "_setup_ap"


@dataclass(frozen=True)
class KnownNetwork:
    name: str
    ssid: str
    autoconnect: bool
    priority: int


@dataclass(frozen=True)
class ScanResult:
    ssid: str
    signal: int
    security: str


class NmcliError(RuntimeError):
    pass


def nmcli_available() -> bool:
    return shutil.which("nmcli") is not None


def _run(args: list[str], *, timeout: int = NMCLI_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """Run an `nmcli` command. Raises NmcliError on non-zero exit."""
    try:
        result = subprocess.run(
            ["nmcli", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise NmcliError(f"nmcli {' '.join(args)} failed to launch: {exc}") from exc

    if result.returncode != 0:
        raise NmcliError(
            f"nmcli {' '.join(args)} exited {result.returncode}: {(result.stderr or '').strip()}"
        )
    return result


def _split_terse(line: str) -> list[str]:
    fields: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            buf.append(line[i + 1])
            i += 2
            continue
        if ch == ":":
            fields.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    fields.append("".join(buf))
    return fields


def list_known_networks() -> list[KnownNetwork]:
    """Return saved client (non-AP) WiFi connection profiles."""
    result = _run([
        "-t",
        "-f",
        "NAME,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY",
        "connection",
        "show",
    ])
    networks: list[KnownNetwork] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        parts = _split_terse(line)
        if len(parts) < 4:
            continue
        name, conn_type, autoconnect, priority = parts[0], parts[1], parts[2], parts[3]
        if conn_type != "802-11-wireless":
            continue
        if name == HOTSPOT_PROFILE_NAME:
            continue
        try:
            prio = int(priority or "0")
        except ValueError:
            prio = 0
        networks.append(
            KnownNetwork(
                name=name,
                ssid=name,  # con-name == ssid in our `add_network`
                autoconnect=(autoconnect.lower() == "yes"),
                priority=prio,
            )
        )
    return networks


def scan_visible_networks() -> list[ScanResult]:
    """Rescan and return visible APs sorted by signal strength (best first)."""
    try:
        _run(["device", "wifi", "rescan"], timeout=10)
    except NmcliError as exc:
        # Rescans frequently bounce when one is already in flight — log + carry on.
        logger.debug("nmcli rescan complained, using cached scan: %s", exc)

    try:
        result = _run(["-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"])
    except NmcliError as exc:
        logger.warning("nmcli wifi list failed: %s", exc)
        return []

    seen: dict[str, ScanResult] = {}
    for line in result.stdout.splitlines():
        if not line:
            continue
        parts = _split_terse(line)
        if len(parts) < 3:
            continue
        ssid, signal_str, security = parts[0], parts[1], parts[2]
        if not ssid:
            continue
        try:
            signal = int(signal_str or "0")
        except ValueError:
            signal = 0
        prior = seen.get(ssid)
        if prior is None or signal > prior.signal:
            seen[ssid] = ScanResult(ssid=ssid, signal=signal, security=security or "--")

    return sorted(seen.values(), key=lambda s: s.signal, reverse=True)


def add_network(ssid: str, psk: str | None, *, priority: int = 0) -> None:
    """Add (or replace) a saved WiFi profile.

    The connection name is the SSID so the portal's list/delete operations don't
    need a separate identifier. If a profile with the same name already exists
    it is deleted first so a re-add overwrites the stored PSK and priority.
    """
    ssid = ssid.strip()
    if not ssid:
        raise ValueError("ssid must not be empty")

    # Best-effort delete of any existing profile with this name.
    try:
        _run(["connection", "delete", ssid])
    except NmcliError:
        pass

    args = [
        "connection",
        "add",
        "type",
        "wifi",
        "con-name",
        ssid,
        "ifname",
        "wlan0",
        "ssid",
        ssid,
        "connection.autoconnect",
        "yes",
        "connection.autoconnect-priority",
        str(priority),
    ]
    if psk:
        args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk]
    _run(args)


def delete_network(name: str) -> None:
    if name == HOTSPOT_PROFILE_NAME:
        raise ValueError("refusing to delete the setup hotspot profile via the public API")
    _run(["connection", "delete", name])


def activate_network(name: str) -> None:
    _run(["connection", "up", name])


def start_hotspot(ssid: str, psk: str | None) -> None:
    """Bring up the on-demand setup AP on wlan0.

    Uses `ipv4.method=shared` so NM runs its own dnsmasq and hands out DHCP +
    DNS on 10.42.0.0/24, with the Pi at 10.42.0.1. Any DNS query gets resolved
    to that IP, which is what triggers iOS/Android captive-portal detection.
    """
    # Tear down any leftover instance from a previous boot.
    try:
        _run(["connection", "delete", HOTSPOT_PROFILE_NAME])
    except NmcliError:
        pass

    args = [
        "connection",
        "add",
        "type",
        "wifi",
        "ifname",
        "wlan0",
        "con-name",
        HOTSPOT_PROFILE_NAME,
        "autoconnect",
        "no",
        "ssid",
        ssid,
        "mode",
        "ap",
        "ipv4.method",
        "shared",
        "ipv6.method",
        "ignore",
        "802-11-wireless.band",
        "bg",
    ]
    if psk:
        args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk]
    _run(args)
    _run(["connection", "up", HOTSPOT_PROFILE_NAME])


def stop_hotspot() -> None:
    """Bring the setup AP down and remove the profile so client profiles can autoconnect."""
    try:
        _run(["connection", "down", HOTSPOT_PROFILE_NAME])
    except NmcliError as exc:
        logger.debug("hotspot down complained (likely already down): %s", exc)
    try:
        _run(["connection", "delete", HOTSPOT_PROFILE_NAME])
    except NmcliError as exc:
        logger.debug("hotspot delete complained: %s", exc)


def is_hotspot_active() -> bool:
    try:
        result = _run([
            "-t",
            "-f",
            "NAME,STATE",
            "connection",
            "show",
            "--active",
        ])
    except NmcliError:
        return False
    for line in result.stdout.splitlines():
        if not line:
            continue
        parts = _split_terse(line)
        if parts and parts[0] == HOTSPOT_PROFILE_NAME:
            return True
    return False
