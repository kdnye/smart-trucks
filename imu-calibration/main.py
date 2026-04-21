import asyncio
import contextlib
import json
import logging
import os
import sys
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from aiohttp import web

from imu_reader import IMUReader

ENABLE_CALIBRATION = os.getenv("ENABLE_CALIBRATION", "")
if ENABLE_CALIBRATION.lower() != "true":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    logging.getLogger(__name__).info(
        "imu-calibration is dormant by default; set ENABLE_CALIBRATION=true to run this container."
    )
    sys.exit(0)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

DB_PATH = os.getenv("IMU_CALIBRATION_DB_PATH", "/data/imu-calibration.db")
IMU_I2C_BUS = int(os.getenv("IMU_I2C_BUS", "1"))
SAMPLE_INTERVAL_SECONDS = float(os.getenv("IMU_CALIBRATION_SAMPLE_INTERVAL_SECONDS", "0.05"))
MAX_LIVE_POINTS = int(os.getenv("IMU_CALIBRATION_MAX_LIVE_POINTS", "350"))

DEFAULT_EVENT_TYPES = [
    "door_open",
    "door_close",
    "pothole",
    "speed_bump",
    "railroad_track",
    "hard_brake",
    "hard_accel",
    "sharp_left_turn",
    "sharp_right_turn",
    "rough_road",
    "curb_impact",
    "cargo_shift",
]


class CalibrationState:
    def __init__(self) -> None:
        self.session_id: int | None = None
        self.session_label: str | None = None
        self.sample_counter = 0
        self.live_buffer: deque[dict[str, Any]] = deque(maxlen=MAX_LIVE_POINTS)
        self.event_types = list(DEFAULT_EVENT_TYPES)


async def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS calibration_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at_utc TEXT NOT NULL,
                stopped_at_utc TEXT,
                label TEXT,
                vehicle_id TEXT,
                metadata_json TEXT NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS imu_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                captured_at_utc TEXT NOT NULL,
                sample_index INTEGER NOT NULL,
                accel_x REAL NOT NULL,
                accel_y REAL NOT NULL,
                accel_z REAL NOT NULL,
                gyro_x REAL NOT NULL,
                gyro_y REAL NOT NULL,
                gyro_z REAL NOT NULL,
                magnitude_2d REAL NOT NULL,
                magnitude_3d REAL NOT NULL,
                FOREIGN KEY(session_id) REFERENCES calibration_sessions(id)
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS calibration_marks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                captured_at_utc TEXT NOT NULL,
                event_type TEXT NOT NULL,
                custom_label TEXT,
                notes TEXT,
                sample_index INTEGER,
                FOREIGN KEY(session_id) REFERENCES calibration_sessions(id)
            )
            """
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_imu_samples_session ON imu_samples(session_id, sample_index)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_marks_session ON calibration_marks(session_id, captured_at_utc)")
        await db.commit()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def start_session(state: CalibrationState, label: str | None, vehicle_id: str | None) -> int:
    if state.session_id is not None:
        return state.session_id

    metadata = {
        "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "imu_i2c_bus": IMU_I2C_BUS,
    }
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO calibration_sessions(started_at_utc, label, vehicle_id, metadata_json) VALUES(?, ?, ?, ?)",
            (utc_now(), label, vehicle_id, json.dumps(metadata)),
        )
        await db.commit()
        state.session_id = cursor.lastrowid

    state.session_label = label
    state.sample_counter = 0
    state.live_buffer.clear()
    return state.session_id


async def stop_session(state: CalibrationState) -> None:
    if state.session_id is None:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE calibration_sessions SET stopped_at_utc = ? WHERE id = ?",
            (utc_now(), state.session_id),
        )
        await db.commit()

    state.session_id = None
    state.session_label = None


async def write_sample(state: CalibrationState, sample: dict[str, Any], captured_at: str) -> None:
    if state.session_id is None:
        return
    state.sample_counter += 1

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO imu_samples(
                session_id, captured_at_utc, sample_index,
                accel_x, accel_y, accel_z, gyro_x, gyro_y, gyro_z,
                magnitude_2d, magnitude_3d
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.session_id,
                captured_at,
                state.sample_counter,
                sample["accel_x"],
                sample["accel_y"],
                sample["accel_z"],
                sample["gyro_x"],
                sample["gyro_y"],
                sample["gyro_z"],
                sample["magnitude_2d"],
                sample["magnitude_3d"],
            ),
        )
        await db.commit()


async def sampling_worker(app: web.Application) -> None:
    state: CalibrationState = app["state"]
    imu_reader = IMUReader(bus_num=IMU_I2C_BUS)

    while not imu_reader.connect():
        logger.warning("Waiting for IMU on I2C bus %s", IMU_I2C_BUS)
        await asyncio.sleep(2)

    logger.info("IMU calibration worker connected")
    while True:
        try:
            sample = asdict(imu_reader.read_sample())
            captured_at = utc_now()
            point = {"captured_at_utc": captured_at, **sample}
            state.live_buffer.append(point)
            await write_sample(state, sample, captured_at)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Sampling error: %s", exc)
        await asyncio.sleep(SAMPLE_INTERVAL_SECONDS)


async def on_startup(app: web.Application) -> None:
    await init_db()
    app["sampler"] = asyncio.create_task(sampling_worker(app))


async def on_cleanup(app: web.Application) -> None:
    sampler: asyncio.Task = app["sampler"]
    sampler.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sampler


async def index(_: web.Request) -> web.Response:
    html = Path(__file__).with_name("static").joinpath("index.html").read_text(encoding="utf-8")
    return web.Response(text=html, content_type="text/html")


async def api_state(request: web.Request) -> web.Response:
    state: CalibrationState = request.app["state"]
    return web.json_response(
        {
            "recording": state.session_id is not None,
            "session_id": state.session_id,
            "session_label": state.session_label,
            "event_types": state.event_types,
        }
    )


async def api_start(request: web.Request) -> web.Response:
    state: CalibrationState = request.app["state"]
    payload = await request.json()
    label = payload.get("label")
    vehicle_id = payload.get("vehicle_id") or os.getenv("VEHICLE_ID")
    session_id = await start_session(state, label, vehicle_id)
    return web.json_response({"session_id": session_id, "recording": True})


async def api_stop(request: web.Request) -> web.Response:
    state: CalibrationState = request.app["state"]
    await stop_session(state)
    return web.json_response({"recording": False})


async def api_mark(request: web.Request) -> web.Response:
    state: CalibrationState = request.app["state"]
    if state.session_id is None:
        return web.json_response({"error": "No active session"}, status=400)

    payload = await request.json()
    event_type = str(payload.get("event_type", "")).strip()
    custom_label = str(payload.get("custom_label", "")).strip() or None
    notes = str(payload.get("notes", "")).strip() or None

    if not event_type:
        return web.json_response({"error": "event_type is required"}, status=400)

    if event_type == "custom" and not custom_label:
        return web.json_response({"error": "custom_label required when event_type=custom"}, status=400)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO calibration_marks(session_id, captured_at_utc, event_type, custom_label, notes, sample_index)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (state.session_id, utc_now(), event_type, custom_label, notes, state.sample_counter),
        )
        await db.commit()

    return web.json_response({"ok": True, "sample_index": state.sample_counter})


async def api_live(request: web.Request) -> web.Response:
    state: CalibrationState = request.app["state"]
    return web.json_response({"points": list(state.live_buffer)})


def create_app() -> web.Application:
    app = web.Application()
    app["state"] = CalibrationState()
    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)
    app.router.add_get("/api/live", api_live)
    app.router.add_post("/api/session/start", api_start)
    app.router.add_post("/api/session/stop", api_stop)
    app.router.add_post("/api/mark", api_mark)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    import uvloop

    uvloop.install()
    web.run_app(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
