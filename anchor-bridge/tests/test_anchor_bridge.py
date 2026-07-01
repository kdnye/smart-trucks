"""Unit tests for anchor-bridge frame parsing + config push (no hardware)."""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
REPO_ROOT = MODULE_PATH.parents[1]
sys.path.insert(0, str(REPO_ROOT))  # for `import shared`
SPEC = importlib.util.spec_from_file_location("anchor_bridge_main", MODULE_PATH)
bridge = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
# Register before exec: the module uses `from __future__ import annotations`,
# and dataclass processing resolves string annotations via sys.modules.
sys.modules["anchor_bridge_main"] = bridge
SPEC.loader.exec_module(bridge)


def _scan_line(**overrides):
    frame = {
        "type": "anchor_scan",
        "anchor_id": "anchor-a1b2c3",
        "gateway_id": "anchor-ffeedd",
        "seq": 42,
        "part": 0,
        "part_count": 1,
        "window_s": 15,
        "link_rssi": -58,
        "devices": [
            {"mac": "AA:BB:CC:DD:EE:FF", "rssi": -67, "max_rssi": -60, "count": 5},
        ],
    }
    frame.update(overrides)
    return json.dumps(frame)


class ParseFrameTests(unittest.TestCase):
    def test_scan_frame_routes_to_scan_topic_and_gets_receipt_stamp(self):
        parsed = bridge.parse_frame(_scan_line())
        self.assertIsNotNone(parsed)
        topic, payload = parsed
        self.assertEqual(topic, "anchors/anchor-a1b2c3/scan")
        self.assertEqual(payload["devices"][0]["mac"], "AA:BB:CC:DD:EE:FF")
        self.assertIn("bridge_received_at_utc", payload)

    def test_heartbeat_routes_to_status_topic(self):
        line = json.dumps(
            {
                "type": "anchor_heartbeat",
                "anchor_id": "anchor-a1b2c3",
                "uptime_s": 3600,
                "fw": "1.0.0",
                "link_rssi": -58,
            }
        )
        topic, _ = bridge.parse_frame(line)
        self.assertEqual(topic, "anchors/anchor-a1b2c3/status")

    def test_gateway_status_routes_to_gateway_topic(self):
        line = json.dumps(
            {"type": "gateway_status", "gateway_id": "anchor-ffeedd", "anchors_recent": 3}
        )
        topic, _ = bridge.parse_frame(line)
        self.assertEqual(topic, "anchors/anchor-ffeedd/gateway")

    def test_accepts_bytes_input(self):
        parsed = bridge.parse_frame(_scan_line().encode("utf-8"))
        self.assertIsNotNone(parsed)

    def test_rejects_boot_noise_and_bad_json(self):
        self.assertIsNone(bridge.parse_frame("ets Jul 29 2019 12:21:46"))
        self.assertIsNone(bridge.parse_frame(""))
        self.assertIsNone(bridge.parse_frame("{not json"))
        self.assertIsNone(bridge.parse_frame(b"\xff\xfe\x00"))

    def test_rejects_unknown_frame_type(self):
        self.assertIsNone(bridge.parse_frame(json.dumps({"type": "mystery"})))

    def test_rejects_invalid_anchor_ids(self):
        for bad_id in ("anchor-XYZ123", "anchor-a1b2c", "beacon-a1b2c3", "", None, 7):
            self.assertIsNone(
                bridge.parse_frame(_scan_line(anchor_id=bad_id)),
                msg=f"anchor_id={bad_id!r} should be rejected",
            )

    def test_rejects_scan_without_devices_list(self):
        self.assertIsNone(bridge.parse_frame(_scan_line(devices="oops")))

    def test_drops_malformed_device_entries_but_keeps_frame(self):
        line = _scan_line(
            devices=[
                {"mac": "AA:BB:CC:DD:EE:FF", "rssi": -67},
                {"mac": 123, "rssi": -60},          # bad mac type
                {"rssi": -60},                       # missing mac
                {"mac": "11:22:33:44:55:66", "rssi": "loud"},  # bad rssi
                "not-a-dict",
            ]
        )
        _, payload = bridge.parse_frame(line)
        self.assertEqual(len(payload["devices"]), 1)

    def test_oversized_line_rejected(self):
        huge = _scan_line().encode() + b" " * (bridge.MAX_LINE_BYTES + 1)
        self.assertIsNone(bridge.parse_frame(huge))


class ConfigCommandTests(unittest.TestCase):
    def _config(self, interval=None, floor=None):
        return bridge.Config(
            serial_candidates=("/dev/ttyACM0",),
            baud_rate=115200,
            mqtt_host="127.0.0.1",
            mqtt_port=1883,
            probe_interval_seconds=30.0,
            report_interval_s=interval,
            rssi_floor=floor,
        )

    def test_no_tuning_env_means_no_command(self):
        self.assertIsNone(bridge.build_config_command(self._config()))

    def test_command_includes_only_set_values(self):
        command = bridge.build_config_command(self._config(interval=30))
        body = json.loads(command)
        self.assertEqual(body, {"cmd": "config", "report_interval_s": 30})

        command = bridge.build_config_command(self._config(interval=30, floor=-85))
        body = json.loads(command)
        self.assertEqual(
            body, {"cmd": "config", "report_interval_s": 30, "rssi_floor": -85}
        )


class LoadConfigTests(unittest.TestCase):
    def test_defaults(self):
        config = bridge.load_config()
        self.assertIn("/dev/ttyACM0", config.serial_candidates)
        self.assertEqual(config.mqtt_host, "127.0.0.1")
        self.assertEqual(config.mqtt_port, 1883)
        self.assertIsNone(config.report_interval_s)
        self.assertIsNone(config.rssi_floor)


if __name__ == "__main__":
    unittest.main()
