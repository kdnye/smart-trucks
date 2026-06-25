"""WiFi failover + captive-portal provisioning service.

Watches host connectivity; if a saved WiFi network has been unreachable for
`WIFI_PROVISIONER_GRACE_SECONDS` (default 180), raises a NetworkManager AP on
wlan0 and serves a captive-portal config page. The page lets a phone or laptop
add/remove client WiFi profiles. After every
`WIFI_PROVISIONER_HOTSPOT_RETRY_INTERVAL_SECONDS` the AP is briefly torn down
so NetworkManager can try its saved profiles again; if connectivity returns
the hotspot stays down.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

sys.path.append(str(Path(__file__).resolve().parents[1]))

from shared.env import sanitize_env_value as _sanitize_env_value  # noqa: E402

import nm  # noqa: E402  (local module)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s wifi-provisioner %(message)s",
)
logger = logging.getLogger("wifi-provisioner")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _env(name: str, default: str | None = None) -> str | None:
    value = _sanitize_env_value(os.getenv(name))
    return value if value is not None else default


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning("%s=%r is not an int; falling back to %s", name, raw, default)
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    vehicle_id: str
    setup_ssid: str
    setup_psk: str | None
    setup_pin: str
    grace_seconds: int
    check_interval_seconds: int
    hotspot_retry_interval_seconds: int
    hotspot_retry_probe_seconds: int
    portal_port: int
    dry_run: bool


def _derive_pin(vehicle_id: str) -> str:
    digest = hashlib.sha256(vehicle_id.encode("utf-8")).hexdigest()
    # Take 6 digits derived from the digest so it stays stable per device.
    return f"{int(digest[:8], 16) % 1_000_000:06d}"


def load_config() -> Config:
    vehicle_id = _env("VEHICLE_ID", "UNKNOWN_TRUCK") or "UNKNOWN_TRUCK"
    setup_ssid_default = f"smart-truck-setup-{vehicle_id.lower()}"
    setup_pin = _env("WIFI_PROVISIONER_SETUP_PIN") or _derive_pin(vehicle_id)
    return Config(
        vehicle_id=vehicle_id,
        setup_ssid=_env("WIFI_PROVISIONER_SETUP_SSID", setup_ssid_default) or setup_ssid_default,
        setup_psk=_env("WIFI_PROVISIONER_SETUP_PSK") or None,
        setup_pin=setup_pin,
        grace_seconds=_int_env("WIFI_PROVISIONER_GRACE_SECONDS", 180, minimum=30),
        check_interval_seconds=_int_env("WIFI_PROVISIONER_CHECK_INTERVAL_SECONDS", 30, minimum=5),
        hotspot_retry_interval_seconds=_int_env(
            "WIFI_PROVISIONER_HOTSPOT_RETRY_INTERVAL_SECONDS", 120, minimum=30
        ),
        hotspot_retry_probe_seconds=_int_env(
            "WIFI_PROVISIONER_HOTSPOT_RETRY_PROBE_SECONDS", 45, minimum=10
        ),
        portal_port=_int_env("WIFI_PROVISIONER_PORTAL_PORT", 80, minimum=1),
        dry_run=_bool_env("WIFI_PROVISIONER_DRY_RUN", False),
    )


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------


@dataclass
class State:
    last_up_monotonic: float
    hotspot_active: bool = False
    last_save_message: str | None = None
    rejoin_event: asyncio.Event = field(default_factory=asyncio.Event)


# ---------------------------------------------------------------------------
# Connectivity probe
# ---------------------------------------------------------------------------


def is_network_connected() -> bool:
    """Mirror of telematics-edge.is_network_connected — quick TCP/53 reach test."""
    try:
        with socket.create_connection(("8.8.8.8", 53), timeout=1.5):
            return True
    except OSError:
        return False


async def _async_is_network_connected() -> bool:
    return await asyncio.to_thread(is_network_connected)


# ---------------------------------------------------------------------------
# Hotspot supervisor
# ---------------------------------------------------------------------------


async def supervisor(config: Config, state: State) -> None:
    """Main control loop: grace timer → hotspot → periodic re-try."""
    logger.info(
        "WiFi provisioner running: grace=%ss check=%ss retry_every=%ss probe=%ss",
        config.grace_seconds,
        config.check_interval_seconds,
        config.hotspot_retry_interval_seconds,
        config.hotspot_retry_probe_seconds,
    )
    logger.info("Setup hotspot SSID=%s PIN=%s", config.setup_ssid, config.setup_pin)

    while True:
        if await _async_is_network_connected():
            state.last_up_monotonic = time.monotonic()
            if state.hotspot_active:
                await _stop_hotspot(state)
            await _sleep_or_wake(config.check_interval_seconds, state)
            continue

        offline_for = time.monotonic() - state.last_up_monotonic
        if not state.hotspot_active and offline_for < config.grace_seconds:
            logger.info(
                "WiFi offline for %ds (< %ds grace) — waiting before hotspot.",
                int(offline_for),
                config.grace_seconds,
            )
            await _sleep_or_wake(config.check_interval_seconds, state)
            continue

        if not state.hotspot_active:
            if not _has_saved_client_profile():
                # No saved profile means the device was never provisioned. Still raise
                # the hotspot so a tech can add the first one.
                logger.info("No saved client WiFi — raising setup hotspot for first-time provisioning.")
            else:
                logger.warning(
                    "WiFi offline for %ds — raising setup hotspot %s.",
                    int(offline_for),
                    config.setup_ssid,
                )
            await _start_hotspot(config, state)

        # Hotspot is up. Wait, then briefly drop it to let NM retry saved profiles.
        woke = await _sleep_or_wake(config.hotspot_retry_interval_seconds, state)
        if woke:
            logger.info("Portal saved a network — dropping hotspot to attempt rejoin.")
        else:
            logger.info("Periodic rejoin attempt — dropping hotspot to test saved profiles.")
        await _stop_hotspot(state)

        # Give NM a moment to reconnect, then probe.
        rejoined = False
        deadline = time.monotonic() + config.hotspot_retry_probe_seconds
        while time.monotonic() < deadline:
            await asyncio.sleep(5)
            if await _async_is_network_connected():
                rejoined = True
                break
        if rejoined:
            logger.info("Rejoined client WiFi — hotspot stays down.")
            state.last_up_monotonic = time.monotonic()
        else:
            logger.warning("Still offline — raising hotspot again.")
            await _start_hotspot(config, state)


async def _sleep_or_wake(seconds: int, state: State) -> bool:
    """Sleep `seconds`, or return early if the portal flags a save. Returns True if woken."""
    try:
        await asyncio.wait_for(state.rejoin_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return False
    state.rejoin_event.clear()
    return True


def _has_saved_client_profile() -> bool:
    try:
        return len(nm.list_known_networks()) > 0
    except nm.NmcliError as exc:
        logger.warning("Could not list saved networks: %s", exc)
        return False


async def _start_hotspot(config: Config, state: State) -> None:
    if config.dry_run:
        logger.info("[dry-run] would start hotspot %s", config.setup_ssid)
        state.hotspot_active = True
        return
    try:
        await asyncio.to_thread(nm.start_hotspot, config.setup_ssid, config.setup_psk)
        state.hotspot_active = True
        logger.info("Hotspot %s is up. Browse to http://10.42.0.1/ on your phone.", config.setup_ssid)
    except nm.NmcliError as exc:
        logger.error("Failed to start hotspot: %s", exc)


async def _stop_hotspot(state: State) -> None:
    try:
        await asyncio.to_thread(nm.stop_hotspot)
    except nm.NmcliError as exc:
        logger.warning("Failed to stop hotspot cleanly: %s", exc)
    finally:
        state.hotspot_active = False


# ---------------------------------------------------------------------------
# Captive portal
# ---------------------------------------------------------------------------


TEMPLATE_PATH = Path(__file__).parent / "templates" / "index.html"


async def _render_portal(config: Config, state: State, *, message: str | None = None, error: str | None = None) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    try:
        known = nm.list_known_networks()
    except nm.NmcliError as exc:
        logger.warning("list_known_networks failed: %s", exc)
        known = []
    try:
        scan = nm.scan_visible_networks()
    except nm.NmcliError as exc:
        logger.warning("scan failed: %s", exc)
        scan = []

    def _esc(text: str) -> str:
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    known_rows = "".join(
        f'<li><span class="ssid">{_esc(n.ssid)}</span>'
        f'<span class="prio">priority {n.priority}</span>'
        f'<form method="post" action="/api/networks/delete" class="inline">'
        f'<input type="hidden" name="name" value="{_esc(n.name)}">'
        f'<input type="password" name="pin" placeholder="PIN" required>'
        f'<button type="submit">Forget</button></form></li>'
        for n in known
    )
    if not known_rows:
        known_rows = "<li class=\"empty\">No saved networks yet.</li>"

    scan_options = "".join(
        f'<option value="{_esc(s.ssid)}">{_esc(s.ssid)} ({s.signal}%, {_esc(s.security or "open")})</option>'
        for s in scan
        if s.ssid
    )
    if not scan_options:
        scan_options = '<option value="" disabled>No nearby networks detected</option>'

    banner = ""
    if message:
        banner = f'<div class="banner ok">{_esc(message)}</div>'
    elif error:
        banner = f'<div class="banner err">{_esc(error)}</div>'

    return (
        template.replace("{{VEHICLE_ID}}", _esc(config.vehicle_id))
        .replace("{{KNOWN_ROWS}}", known_rows)
        .replace("{{SCAN_OPTIONS}}", scan_options)
        .replace("{{BANNER}}", banner)
    )


async def _captive_redirect(request: web.Request) -> web.Response:
    # iOS hits /hotspot-detect.html; Android hits /generate_204 and similar.
    # Anything that isn't our portal or API gets a 302 to the portal root.
    if request.path.startswith("/api/") or request.path == "/":
        return web.Response(status=404)
    raise web.HTTPFound("/")


def _check_pin(config: Config, supplied: str | None) -> bool:
    if not supplied:
        return False
    # Constant-time compare to avoid timing leaks on a low-entropy PIN.
    a = supplied.encode("utf-8")
    b = config.setup_pin.encode("utf-8")
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a, b):
        result |= x ^ y
    return result == 0


def build_portal_app(config: Config, state: State) -> web.Application:
    app = web.Application()

    async def index(_request: web.Request) -> web.Response:
        msg = state.last_save_message
        state.last_save_message = None
        body = await _render_portal(config, state, message=msg)
        return web.Response(text=body, content_type="text/html")

    async def add_handler(request: web.Request) -> web.Response:
        form = await request.post()
        ssid = (form.get("ssid") or "").strip()
        psk = (form.get("psk") or "").strip() or None
        try:
            priority = int(form.get("priority") or "0")
        except ValueError:
            priority = 0
        pin = form.get("pin") or ""

        if not _check_pin(config, pin):
            logger.warning("Portal add: bad PIN from %s", request.remote)
            body = await _render_portal(config, state, error="Bad PIN.")
            return web.Response(text=body, content_type="text/html", status=401)
        if not ssid:
            body = await _render_portal(config, state, error="SSID is required.")
            return web.Response(text=body, content_type="text/html", status=400)

        if config.dry_run:
            logger.info("[dry-run] would add network ssid=%s priority=%s", ssid, priority)
        else:
            try:
                await asyncio.to_thread(nm.add_network, ssid, psk, priority=priority)
            except (nm.NmcliError, ValueError) as exc:
                logger.error("Failed to save network %s: %s", ssid, exc)
                body = await _render_portal(config, state, error=f"Save failed: {exc}")
                return web.Response(text=body, content_type="text/html", status=500)

        state.last_save_message = (
            f"Saved {ssid}. Rejoining in ~{config.hotspot_retry_probe_seconds}s; "
            "this hotspot will drop shortly."
        )
        state.rejoin_event.set()
        body = await _render_portal(config, state, message=state.last_save_message)
        state.last_save_message = None
        return web.Response(text=body, content_type="text/html")

    async def delete_handler(request: web.Request) -> web.Response:
        form = await request.post()
        name = (form.get("name") or "").strip()
        pin = form.get("pin") or ""
        if not _check_pin(config, pin):
            body = await _render_portal(config, state, error="Bad PIN.")
            return web.Response(text=body, content_type="text/html", status=401)
        if not name:
            body = await _render_portal(config, state, error="Network name required.")
            return web.Response(text=body, content_type="text/html", status=400)

        if config.dry_run:
            logger.info("[dry-run] would delete network %s", name)
        else:
            try:
                await asyncio.to_thread(nm.delete_network, name)
            except (nm.NmcliError, ValueError) as exc:
                logger.error("Failed to delete %s: %s", name, exc)
                body = await _render_portal(config, state, error=f"Delete failed: {exc}")
                return web.Response(text=body, content_type="text/html", status=500)

        body = await _render_portal(config, state, message=f"Forgot {name}.")
        return web.Response(text=body, content_type="text/html")

    async def health(_request: web.Request) -> web.Response:
        payload = {
            "hotspot_active": state.hotspot_active,
            "last_up_age_seconds": int(time.monotonic() - state.last_up_monotonic),
            "setup_ssid": config.setup_ssid,
        }
        return web.json_response(payload)

    app.router.add_get("/", index)
    app.router.add_post("/api/networks", add_handler)
    app.router.add_post("/api/networks/delete", delete_handler)
    app.router.add_get("/healthz", health)
    # Catch-all for captive-portal probes from iOS/Android/Windows.
    app.router.add_route("*", "/{tail:.*}", _captive_redirect)
    return app


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def run() -> None:
    config = load_config()
    if not nm.nmcli_available() and not config.dry_run:
        logger.error(
            "nmcli is not available in this container — install network-manager or "
            "set WIFI_PROVISIONER_DRY_RUN=1 for local development."
        )
        # Keep the process alive so Balena doesn't crash-loop and so the portal
        # can still report health.
    state = State(last_up_monotonic=time.monotonic())

    app = build_portal_app(config, state)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=config.portal_port)
    try:
        await site.start()
        logger.info("Captive portal listening on :%s", config.portal_port)
    except OSError as exc:
        logger.error("Failed to bind portal on :%s — %s", config.portal_port, exc)

    await supervisor(config, state)


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("wifi-provisioner shutting down.")


if __name__ == "__main__":
    main()
