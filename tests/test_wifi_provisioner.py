"""Tests for the wifi-provisioner service.

Exercises the nmcli wrappers' parsing, the PIN gate, the captive-portal
catch-all, and the supervisor's grace timer / hotspot lifecycle. Runs without
any real nmcli, hostapd, or wlan0 — the nm.* call sites are monkey-patched.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "wifi-provisioner")))

import pytest  # noqa: E402

import nm  # noqa: E402  (from wifi-provisioner/)
import main as wp_main  # noqa: E402  (from wifi-provisioner/main.py)


# ---------------------------------------------------------------------------
# nm.py — terse-output parser
# ---------------------------------------------------------------------------


def test_split_terse_handles_escaped_colons():
    line = r"my\:weird\:ssid:802-11-wireless:yes:10"
    assert nm._split_terse(line) == ["my:weird:ssid", "802-11-wireless", "yes", "10"]


def test_split_terse_plain():
    assert nm._split_terse("home:wifi:yes:5") == ["home", "wifi", "yes", "5"]


def test_list_known_networks_filters_and_parses(monkeypatch):
    sample = (
        "home:802-11-wireless:yes:10\n"
        "eth0:802-3-ethernet:yes:0\n"
        "_setup_ap:802-11-wireless:no:0\n"
        "yard\\:back:802-11-wireless:no:3\n"
    )

    class _Result:
        stdout = sample

    monkeypatch.setattr(nm, "_run", lambda *a, **kw: _Result())
    networks = nm.list_known_networks()
    names = sorted(n.name for n in networks)
    assert names == ["home", "yard:back"]
    home = next(n for n in networks if n.name == "home")
    assert home.priority == 10
    assert home.autoconnect is True


def test_scan_visible_dedupes_keeping_strongest(monkeypatch):
    sequence = iter(
        [
            type("R", (), {"stdout": ""})(),  # rescan
            type("R", (), {"stdout": "yard:42:WPA2\nyard:71:WPA2\nguest:55:--\n"})(),  # list
        ]
    )
    monkeypatch.setattr(nm, "_run", lambda *a, **kw: next(sequence))
    scan = nm.scan_visible_networks()
    by_ssid = {s.ssid: s for s in scan}
    assert by_ssid["yard"].signal == 71
    assert by_ssid["guest"].security == "--"
    # Sorted strongest-first
    assert scan[0].signal >= scan[-1].signal


def test_add_network_includes_psk_args(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, **_kw):
        calls.append(list(args))
        return type("R", (), {"stdout": ""})()

    monkeypatch.setattr(nm, "_run", fake_run)
    nm.add_network("yard-back", "hunter22", priority=15)
    # 1st call deletes any prior profile, 2nd call adds.
    add_args = calls[-1]
    assert "wifi-sec.key-mgmt" in add_args
    assert "wpa-psk" in add_args
    psk_idx = add_args.index("wifi-sec.psk")
    assert add_args[psk_idx + 1] == "hunter22"
    prio_idx = add_args.index("connection.autoconnect-priority")
    assert add_args[prio_idx + 1] == "15"


def test_add_network_omits_psk_for_open(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        nm,
        "_run",
        lambda args, **_kw: calls.append(list(args)) or type("R", (), {"stdout": ""})(),
    )
    nm.add_network("open-yard", None)
    add_args = calls[-1]
    assert "wifi-sec.psk" not in add_args


def test_delete_network_refuses_hotspot_profile(monkeypatch):
    monkeypatch.setattr(nm, "_run", lambda *a, **kw: type("R", (), {"stdout": ""})())
    with pytest.raises(ValueError):
        nm.delete_network(nm.HOTSPOT_PROFILE_NAME)


# ---------------------------------------------------------------------------
# main.py — PIN gate, supervisor lifecycle
# ---------------------------------------------------------------------------


def _make_config(**overrides):
    base = dict(
        vehicle_id="TRK-TEST",
        setup_ssid="smart-truck-setup-trk-test",
        setup_psk=None,
        setup_pin="424242",
        grace_seconds=2,
        check_interval_seconds=1,
        hotspot_retry_interval_seconds=2,
        hotspot_retry_probe_seconds=1,
        portal_port=8080,
        dry_run=True,
    )
    base.update(overrides)
    return wp_main.Config(**base)


def test_pin_gate_accepts_match():
    cfg = _make_config()
    assert wp_main._check_pin(cfg, "424242") is True


def test_pin_gate_rejects_mismatch_and_length():
    cfg = _make_config()
    assert wp_main._check_pin(cfg, "424241") is False
    assert wp_main._check_pin(cfg, "") is False
    assert wp_main._check_pin(cfg, "4242420") is False
    assert wp_main._check_pin(cfg, None) is False


def test_derive_pin_is_stable_and_six_digits():
    pin = wp_main._derive_pin("TRK-905")
    assert pin == wp_main._derive_pin("TRK-905")
    assert len(pin) == 6 and pin.isdigit()


def test_supervisor_waits_grace_before_starting_hotspot(monkeypatch):
    """Offline at boot but only briefly — must not raise hotspot before grace elapses."""
    cfg = _make_config(grace_seconds=5, check_interval_seconds=1)

    async def runner():
        starts: list[str] = []

        async def fake_start(c, s):
            starts.append("start")
            s.hotspot_active = True
            # Force supervisor exit after first start to keep the test bounded.
            raise asyncio.CancelledError

        monkeypatch.setattr(wp_main, "_start_hotspot", fake_start)
        monkeypatch.setattr(wp_main, "_async_is_network_connected", _always(False))
        monkeypatch.setattr(wp_main, "_has_saved_client_profile", lambda: True)

        state = wp_main.State(last_up_monotonic=_now())
        with pytest.raises(asyncio.CancelledError):
            await wp_main.supervisor(cfg, state)
        return starts, state

    starts, state = asyncio.run(runner())
    assert starts == ["start"]
    assert state.hotspot_active is True


def test_supervisor_skips_hotspot_when_online(monkeypatch):
    cfg = _make_config(grace_seconds=1, check_interval_seconds=1)

    async def runner():
        starts: list[str] = []

        async def fake_start(c, s):
            starts.append("start")

        monkeypatch.setattr(wp_main, "_start_hotspot", fake_start)
        monkeypatch.setattr(wp_main, "_async_is_network_connected", _always(True))
        monkeypatch.setattr(wp_main, "_has_saved_client_profile", lambda: True)

        state = wp_main.State(last_up_monotonic=_now())
        task = asyncio.create_task(wp_main.supervisor(cfg, state))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return starts

    starts = asyncio.run(runner())
    assert starts == []


# ---------------------------------------------------------------------------
# Portal HTTP handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_portal_add_rejects_bad_pin(monkeypatch, aiohttp_client):
    cfg = _make_config(setup_pin="111111")
    state = wp_main.State(last_up_monotonic=_now())

    monkeypatch.setattr(nm, "list_known_networks", lambda: [])
    monkeypatch.setattr(nm, "scan_visible_networks", lambda: [])

    app = wp_main.build_portal_app(cfg, state)
    client = await aiohttp_client(app)

    resp = await client.post("/api/networks", data={"ssid": "x", "psk": "y", "pin": "wrong"})
    assert resp.status == 401


@pytest.mark.asyncio
async def test_portal_add_accepts_good_pin_and_wakes_supervisor(monkeypatch, aiohttp_client):
    cfg = _make_config(setup_pin="222222", dry_run=True)
    state = wp_main.State(last_up_monotonic=_now())

    monkeypatch.setattr(nm, "list_known_networks", lambda: [])
    monkeypatch.setattr(nm, "scan_visible_networks", lambda: [])

    app = wp_main.build_portal_app(cfg, state)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/networks",
        data={"ssid": "yard-2", "psk": "abcd1234", "priority": "20", "pin": "222222"},
    )
    assert resp.status == 200
    assert state.rejoin_event.is_set()


@pytest.mark.asyncio
async def test_captive_redirect_unknown_path(monkeypatch, aiohttp_client):
    cfg = _make_config()
    state = wp_main.State(last_up_monotonic=_now())
    monkeypatch.setattr(nm, "list_known_networks", lambda: [])
    monkeypatch.setattr(nm, "scan_visible_networks", lambda: [])

    app = wp_main.build_portal_app(cfg, state)
    client = await aiohttp_client(app)
    resp = await client.get("/generate_204", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _now():
    import time

    return time.monotonic()


def _always(value):
    async def _inner():
        return value

    return _inner
