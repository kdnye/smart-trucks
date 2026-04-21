import asyncio
import contextlib
import json
import logging
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

import aiosqlite
from aiohttp import web
from bleak import BleakScanner

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

DB_PATH = os.getenv("BLE_CALIBRATION_DB_PATH", "/data/ble-calibration.db")
SCAN_DURATION_SECONDS = max(2.0, float(os.getenv("BLE_CALIBRATION_SCAN_DURATION_SECONDS", "4")))
REFRESH_SECONDS = max(3.0, float(os.getenv("BLE_CALIBRATION_REFRESH_SECONDS", "6")))
PORT = int(os.getenv("PORT", "8080"))


@dataclass
class BeaconObservation:
    mac: str
    name: str | None
    rssi: int
    timestamp_utc: str


class State:
    def __init__(self) -> None:
        self.session_id: int | None = None
        self.session_label: str | None = None
        self.grid_width = 8
        self.grid_height = 8
        self.pi_x = 0
        self.pi_y = 0
        self.obstructions: set[tuple[int, int]] = set()
        self.beacons: dict[str, BeaconObservation] = {}
        self.calibration_models: dict[str, dict[str, Any]] = {}


state = State()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def euclidean_meters(a_x: int, a_y: int, b_x: int, b_y: int) -> float:
    return math.sqrt((a_x - b_x) ** 2 + (a_y - b_y) ** 2)


async def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS ble_calibration_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at_utc TEXT NOT NULL,
                stopped_at_utc TEXT,
                session_label TEXT,
                vehicle_id TEXT,
                metadata_json TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS ble_grid_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                captured_at_utc TEXT NOT NULL,
                grid_width INTEGER NOT NULL,
                grid_height INTEGER NOT NULL,
                pi_x INTEGER NOT NULL,
                pi_y INTEGER NOT NULL,
                obstructions_json TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES ble_calibration_sessions(id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS ble_captures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                captured_at_utc TEXT NOT NULL,
                beacon_mac TEXT NOT NULL,
                beacon_name TEXT,
                rssi INTEGER NOT NULL,
                grid_x INTEGER NOT NULL,
                grid_y INTEGER NOT NULL,
                distance_m REAL NOT NULL,
                is_obstructed INTEGER NOT NULL,
                obstruction_notes TEXT,
                capture_group TEXT,
                FOREIGN KEY(session_id) REFERENCES ble_calibration_sessions(id)
            )
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ble_capture_beacon ON ble_captures(session_id, beacon_mac)")
        await db.commit()


async def start_session(label: str | None) -> int:
    if state.session_id is not None:
        return state.session_id

    metadata = {"scan_duration_seconds": SCAN_DURATION_SECONDS, "refresh_seconds": REFRESH_SECONDS}
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO ble_calibration_sessions(started_at_utc, session_label, vehicle_id, metadata_json) VALUES(?, ?, ?, ?)",
            (utc_now(), label, os.getenv("VEHICLE_ID"), json.dumps(metadata)),
        )
        await db.commit()
        state.session_id = cursor.lastrowid

    state.session_label = label
    await snapshot_grid_state()
    return state.session_id


async def stop_session() -> None:
    if state.session_id is None:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE ble_calibration_sessions SET stopped_at_utc = ? WHERE id = ?", (utc_now(), state.session_id))
        await db.commit()

    state.session_id = None
    state.session_label = None


async def snapshot_grid_state() -> None:
    if state.session_id is None:
        return
    obstructions = [{"x": x, "y": y} for x, y in sorted(state.obstructions)]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO ble_grid_state(session_id, captured_at_utc, grid_width, grid_height, pi_x, pi_y, obstructions_json)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.session_id,
                utc_now(),
                state.grid_width,
                state.grid_height,
                state.pi_x,
                state.pi_y,
                json.dumps(obstructions),
            ),
        )
        await db.commit()


async def scan_worker() -> None:
    while True:
        try:
            devices = await BleakScanner.discover(timeout=SCAN_DURATION_SECONDS, return_adv=False)
            now = utc_now()
            fresh: dict[str, BeaconObservation] = {}
            for dev in devices:
                if dev.address:
                    fresh[dev.address] = BeaconObservation(
                        mac=dev.address,
                        name=dev.name,
                        rssi=int(dev.rssi or -127),
                        timestamp_utc=now,
                    )
            state.beacons = fresh
            logger.info("BLE calibration scan found %d beacons", len(fresh))
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("BLE scan failed: %s", exc)
        await asyncio.sleep(REFRESH_SECONDS)


def fit_path_loss(samples: list[tuple[float, int]]) -> dict[str, float]:
    """Fit RSSI = intercept + slope*log10(distance_m), with distance >= 1m."""
    if len(samples) < 3:
        return {"intercept": 0.0, "slope": 0.0, "r2": 0.0}

    x = [math.log10(max(distance, 1.0)) for distance, _ in samples]
    y = [rssi for _, rssi in samples]
    x_mean = mean(x)
    y_mean = mean(y)
    denom = sum((i - x_mean) ** 2 for i in x)
    if denom <= 1e-9:
        return {"intercept": y_mean, "slope": 0.0, "r2": 0.0}

    slope = sum((x[i] - x_mean) * (y[i] - y_mean) for i in range(len(x))) / denom
    intercept = y_mean - slope * x_mean
    ss_tot = sum((yi - y_mean) ** 2 for yi in y)
    ss_res = sum((y[i] - (intercept + slope * x[i])) ** 2 for i in range(len(y)))
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 1e-9 else 0.0
    return {"intercept": round(intercept, 4), "slope": round(slope, 4), "r2": round(r2, 4)}


async def rebuild_models() -> None:
    if state.session_id is None:
        state.calibration_models = {}
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT beacon_mac, beacon_name, distance_m, rssi, is_obstructed
            FROM ble_captures
            WHERE session_id = ?
            ORDER BY id ASC
            """,
            (state.session_id,),
        )
        rows = await cursor.fetchall()

    grouped: dict[str, list[tuple[float, int, int, str | None]]] = defaultdict(list)
    for row in rows:
        grouped[row[0]].append((float(row[2]), int(row[3]), int(row[4]), row[1]))

    models: dict[str, dict[str, Any]] = {}
    for mac, values in grouped.items():
        clear_samples = [(d, r) for d, r, obstructed, _ in values if obstructed == 0]
        blocked_samples = [(d, r) for d, r, obstructed, _ in values if obstructed == 1]
        clear_model = fit_path_loss(clear_samples)
        blocked_model = fit_path_loss(blocked_samples)
        obstruction_offset = 0.0
        if clear_samples and blocked_samples:
            obstruction_offset = mean(r for _, r in blocked_samples) - mean(r for _, r in clear_samples)

        models[mac] = {
            "beacon_name": values[0][3],
            "sample_count": len(values),
            "clear_sample_count": len(clear_samples),
            "obstructed_sample_count": len(blocked_samples),
            "clear_model": clear_model,
            "obstructed_model": blocked_model,
            "obstruction_offset_dbm": round(obstruction_offset, 4),
        }
    state.calibration_models = models


async def api_state(_: web.Request) -> web.Response:
    beacons = [vars(item) for item in sorted(state.beacons.values(), key=lambda x: x.rssi, reverse=True)]
    return web.json_response(
        {
            "session_id": state.session_id,
            "session_label": state.session_label,
            "recording": state.session_id is not None,
            "grid": {
                "width": state.grid_width,
                "height": state.grid_height,
                "pi": {"x": state.pi_x, "y": state.pi_y},
                "obstructions": [{"x": x, "y": y} for x, y in sorted(state.obstructions)],
            },
            "beacons": beacons,
            "models": state.calibration_models,
        }
    )


async def api_start(request: web.Request) -> web.Response:
    payload = await request.json()
    session_id = await start_session(payload.get("label"))
    return web.json_response({"ok": True, "session_id": session_id})


async def api_stop(_: web.Request) -> web.Response:
    await stop_session()
    return web.json_response({"ok": True})


async def api_layout(request: web.Request) -> web.Response:
    payload = await request.json()
    width = int(payload.get("width", state.grid_width))
    height = int(payload.get("height", state.grid_height))
    pi_x = int(payload.get("pi_x", state.pi_x))
    pi_y = int(payload.get("pi_y", state.pi_y))

    if width < 2 or width > 40 or height < 2 or height > 40:
        return web.json_response({"error": "grid must be between 2 and 40 meters per side"}, status=400)
    if not (0 <= pi_x < width and 0 <= pi_y < height):
        return web.json_response({"error": "pi location must be inside grid"}, status=400)

    state.grid_width = width
    state.grid_height = height
    state.pi_x = pi_x
    state.pi_y = pi_y
    state.obstructions = {(x, y) for x, y in state.obstructions if x < width and y < height}
    await snapshot_grid_state()
    return web.json_response({"ok": True})


async def api_obstruction(request: web.Request) -> web.Response:
    payload = await request.json()
    x = int(payload.get("x"))
    y = int(payload.get("y"))
    is_blocked = bool(payload.get("blocked", True))

    if not (0 <= x < state.grid_width and 0 <= y < state.grid_height):
        return web.json_response({"error": "cell out of range"}, status=400)

    if is_blocked:
        state.obstructions.add((x, y))
    else:
        state.obstructions.discard((x, y))
    await snapshot_grid_state()
    return web.json_response({"ok": True})


async def api_capture(request: web.Request) -> web.Response:
    if state.session_id is None:
        return web.json_response({"error": "start a session before capturing"}, status=400)

    payload = await request.json()
    beacon_mac = str(payload.get("beacon_mac", "")).strip()
    x = int(payload.get("x"))
    y = int(payload.get("y"))
    obstruction_notes = str(payload.get("obstruction_notes", "")).strip() or None
    capture_group = str(payload.get("capture_group", "")).strip() or None

    if beacon_mac not in state.beacons:
        return web.json_response({"error": "beacon not in latest scan; wait for refresh and retry"}, status=400)
    if not (0 <= x < state.grid_width and 0 <= y < state.grid_height):
        return web.json_response({"error": "cell out of range"}, status=400)

    beacon = state.beacons[beacon_mac]
    dist = euclidean_meters(state.pi_x, state.pi_y, x, y)
    is_obstructed = 1 if (x, y) in state.obstructions else 0

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO ble_captures(
                session_id, captured_at_utc, beacon_mac, beacon_name,
                rssi, grid_x, grid_y, distance_m, is_obstructed,
                obstruction_notes, capture_group
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.session_id,
                utc_now(),
                beacon.mac,
                beacon.name,
                beacon.rssi,
                x,
                y,
                dist,
                is_obstructed,
                obstruction_notes,
                capture_group,
            ),
        )
        await db.commit()

    await rebuild_models()
    return web.json_response({"ok": True, "distance_m": round(dist, 3), "rssi": beacon.rssi})


async def api_export(_: web.Request) -> web.Response:
    await rebuild_models()
    return web.json_response(
        {
            "session_id": state.session_id,
            "generated_at_utc": utc_now(),
            "grid": {
                "width": state.grid_width,
                "height": state.grid_height,
                "pi": {"x": state.pi_x, "y": state.pi_y},
            },
            "models": state.calibration_models,
        }
    )


async def index(_: web.Request) -> web.Response:
    html = Path(__file__).with_name("static").joinpath("index.html").read_text(encoding="utf-8")
    return web.Response(text=html, content_type="text/html")


async def on_startup(app: web.Application) -> None:
    await init_db()
    app["scan_task"] = asyncio.create_task(scan_worker())


async def on_cleanup(app: web.Application) -> None:
    task: asyncio.Task = app["scan_task"]
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/export", api_export)
    app.router.add_post("/api/session/start", api_start)
    app.router.add_post("/api/session/stop", api_stop)
    app.router.add_post("/api/layout", api_layout)
    app.router.add_post("/api/obstruction", api_obstruction)
    app.router.add_post("/api/capture", api_capture)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    import uvloop

    uvloop.install()
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
