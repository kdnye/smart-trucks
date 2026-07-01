"""anchor-bridge: gateway-ATOM USB serial -> local MQTT broker.

Reads the NDJSON stream emitted by the gateway ATOM Lite (see
``anchor-firmware/README.md`` for the line format) and republishes each frame
to the Pi's local Mosquitto broker:

    anchors/<anchor_id>/scan      <- anchor_scan frames (BLE observations)
    anchors/<anchor_id>/status    <- anchor_heartbeat frames
    anchors/<gateway_id>/gateway  <- gateway_status frames
    anchors/bridge/status         <- this bridge's own liveness (retained)

The bridge is intentionally dumb: no SQLite, no business rules. Consumers
(ble-sensor's anchor ingest, future debug tooling) subscribe to the broker.
On connect it pushes the fleet-tunable scan settings (report interval / RSSI
floor) down to the gateway, which re-broadcasts them to every anchor over
ESP-NOW — so tuning is a Balena env-var change, not a reflash.

Runs as a supervised co-process in the edge container (edge/start.sh). The
main loop never exits: with no gateway plugged in it idles and re-probes the
serial candidates, so sites without anchors pay ~zero cost.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from shared.env import read_float_env, read_int_env, read_str_env

logger = logging.getLogger(__name__)

# Frame types the gateway emits that we forward, mapped to a topic suffix.
FRAME_TOPIC_SUFFIXES = {
    "anchor_scan": "scan",
    "anchor_heartbeat": "status",
    "gateway_status": "gateway",
    "node_info": "info",
}

MAX_LINE_BYTES = 8192  # gateway lines are <2 KB; anything bigger is corruption


@dataclass(frozen=True)
class Config:
    serial_candidates: tuple[str, ...]
    baud_rate: int
    mqtt_host: str
    mqtt_port: int
    probe_interval_seconds: float
    report_interval_s: int | None
    rssi_floor: int | None


def load_config() -> Config:
    raw_candidates = read_str_env(
        "ANCHOR_SERIAL_CANDIDATES",
        "/dev/ttyACM0,/dev/ttyUSB0,/dev/ttyACM1,/dev/ttyUSB1",
    )
    candidates = tuple(p.strip() for p in raw_candidates.split(",") if p.strip())

    # Optional config push to the gateway (broadcast to all anchors). Unset ->
    # leave whatever is persisted in the nodes' NVS alone.
    raw_interval = os.getenv("ANCHOR_REPORT_INTERVAL_S", "").strip()
    raw_floor = os.getenv("ANCHOR_RSSI_FLOOR", "").strip()
    report_interval_s: int | None = None
    rssi_floor: int | None = None
    if raw_interval:
        try:
            value = int(raw_interval)
            if 5 <= value <= 300:
                report_interval_s = value
        except ValueError:
            logger.warning("Ignoring non-integer ANCHOR_REPORT_INTERVAL_S=%r", raw_interval)
    if raw_floor:
        try:
            value = int(raw_floor)
            if -120 <= value <= 0:
                rssi_floor = value
        except ValueError:
            logger.warning("Ignoring non-integer ANCHOR_RSSI_FLOOR=%r", raw_floor)

    return Config(
        serial_candidates=candidates,
        baud_rate=read_int_env("ANCHOR_SERIAL_BAUD", 115200, minimum=9600),
        mqtt_host=read_str_env("ANCHOR_MQTT_HOST", "127.0.0.1"),
        mqtt_port=read_int_env("ANCHOR_MQTT_PORT", 1883, minimum=1),
        probe_interval_seconds=read_float_env("ANCHOR_SERIAL_PROBE_SECONDS", 30.0, minimum=1.0),
        report_interval_s=report_interval_s,
        rssi_floor=rssi_floor,
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_frame(raw_line: str | bytes) -> tuple[str, dict[str, Any]] | None:
    """Validate one gateway NDJSON line -> ``(topic, enriched_payload)``.

    Returns None for anything that is not a well-formed, known frame from a
    plausibly-named node. Pure function — unit-tested without hardware.
    """
    if isinstance(raw_line, bytes):
        if len(raw_line) > MAX_LINE_BYTES:
            return None
        try:
            raw_line = raw_line.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None
    line = raw_line.strip()
    if not line or not line.startswith("{"):
        return None  # boot noise / non-JSON logs from the gateway
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    frame_type = payload.get("type")
    suffix = FRAME_TOPIC_SUFFIXES.get(str(frame_type))
    if suffix is None:
        return None

    # anchor_* frames are attributed strictly by anchor_id — a missing/empty
    # anchor_id must NOT fall back to the gateway's id (misattribution).
    if frame_type == "gateway_status":
        node_id = payload.get("gateway_id")
    else:
        node_id = payload.get("anchor_id")
    if not _valid_node_id(node_id):
        return None

    if frame_type == "anchor_scan":
        devices = payload.get("devices")
        if not isinstance(devices, list):
            return None
        # Drop malformed device entries rather than the whole frame.
        payload["devices"] = [
            d
            for d in devices
            if isinstance(d, dict) and isinstance(d.get("mac"), str) and _is_int(d.get("rssi"))
        ]

    # Receipt timestamp: the anchors have no clock, so the Pi stamps time.
    payload["bridge_received_at_utc"] = _utc_now_iso()
    return f"anchors/{node_id}/{suffix}", payload


def _valid_node_id(node_id: Any) -> bool:
    if not isinstance(node_id, str) or len(node_id) != 13:
        return False
    if not node_id.startswith("anchor-"):
        return False
    tail = node_id[len("anchor-") :]
    return all(ch in "0123456789abcdef" for ch in tail)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def build_config_command(config: Config) -> str | None:
    """The JSON config line pushed to the gateway on connect (or None)."""
    body: dict[str, Any] = {"cmd": "config"}
    if config.report_interval_s is not None:
        body["report_interval_s"] = config.report_interval_s
    if config.rssi_floor is not None:
        body["rssi_floor"] = config.rssi_floor
    if len(body) == 1:
        return None
    return json.dumps(body)


def _find_serial_port(candidates: tuple[str, ...]) -> str | None:
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _connect_mqtt(config: Config):
    import paho.mqtt.client as mqtt

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="anchor-bridge",
        clean_session=True,
    )
    client.will_set(
        "anchors/bridge/status",
        json.dumps({"online": False}),
        qos=0,
        retain=True,
    )
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(config.mqtt_host, config.mqtt_port, keepalive=60)
    client.loop_start()
    return client


def _publish_bridge_status(client, *, online: bool, port: str | None) -> None:
    client.publish(
        "anchors/bridge/status",
        json.dumps({"online": online, "serial_port": port, "at_utc": _utc_now_iso()}),
        qos=0,
        retain=True,
    )


def _pump_serial(client, config: Config, port: str) -> None:
    """Read frames from one serial session until it errors/unplugs."""
    import serial

    with serial.Serial(port, config.baud_rate, timeout=5) as conn:
        logger.info("Gateway serial connected on %s", port)
        _publish_bridge_status(client, online=True, port=port)

        command = build_config_command(config)
        if command is not None:
            conn.write((command + "\n").encode("utf-8"))
        conn.write(b'{"cmd":"show"}\n')

        while True:
            raw = conn.readline()
            if raw == b"":
                continue  # read timeout: idle gateway, keep listening
            parsed = parse_frame(raw)
            if parsed is None:
                logger.debug("Dropped unparseable gateway line (%d bytes)", len(raw))
                continue
            topic, payload = parsed
            client.publish(topic, json.dumps(payload), qos=0, retain=False)


def run() -> None:
    config = load_config()
    logger.info(
        "anchor-bridge starting: serial_candidates=%s mqtt=%s:%d",
        ",".join(config.serial_candidates),
        config.mqtt_host,
        config.mqtt_port,
    )
    client = _connect_mqtt(config)
    announced_waiting = False
    while True:
        port = _find_serial_port(config.serial_candidates)
        if port is None:
            if not announced_waiting:
                logger.info(
                    "No gateway ATOM found (probed %s); idling. Plug the gateway "
                    "into the Pi's USB port to enable anchor ingest.",
                    ",".join(config.serial_candidates),
                )
                announced_waiting = True
                _publish_bridge_status(client, online=False, port=None)
            time.sleep(config.probe_interval_seconds)
            continue
        announced_waiting = False
        try:
            _pump_serial(client, config, port)
        except Exception as exc:  # unplug, permission, framing — retry fresh
            logger.warning("Gateway serial session on %s ended: %s", port, exc)
            _publish_bridge_status(client, online=False, port=port)
            time.sleep(min(config.probe_interval_seconds, 10.0))


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [anchor-bridge] %(message)s",
    )
    run()
