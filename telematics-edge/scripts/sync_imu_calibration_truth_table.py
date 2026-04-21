#!/usr/bin/env python3
"""Build truth-table style IMU event profiles from calibration sessions."""

import argparse
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, pstdev


@dataclass
class EventWindow:
    magnitude_3d: list[float]
    magnitude_2d: list[float]
    gyro_abs: list[float]


def load_windows(db_path: Path, window_samples: int) -> dict[str, EventWindow]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    existing_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'") }
    if not {"calibration_marks", "imu_samples"}.issubset(existing_tables):
        conn.close()
        return {}

    rows = conn.execute(
        """
        SELECT
          m.event_type,
          COALESCE(NULLIF(TRIM(m.custom_label), ''), m.event_type) AS event_label,
          m.session_id,
          m.sample_index,
          s.sample_index AS source_index,
          s.magnitude_3d,
          s.magnitude_2d,
          ABS(s.gyro_x) + ABS(s.gyro_y) + ABS(s.gyro_z) AS gyro_abs
        FROM calibration_marks m
        JOIN imu_samples s
          ON s.session_id = m.session_id
         AND s.sample_index BETWEEN m.sample_index - ? AND m.sample_index + ?
        ORDER BY m.id, s.sample_index
        """,
        (window_samples, window_samples),
    ).fetchall()
    conn.close()

    windows: dict[str, EventWindow] = defaultdict(lambda: EventWindow([], [], []))
    for row in rows:
        label = row["event_label"]
        windows[label].magnitude_3d.append(float(row["magnitude_3d"]))
        windows[label].magnitude_2d.append(float(row["magnitude_2d"]))
        windows[label].gyro_abs.append(float(row["gyro_abs"]))
    return windows


def summarize(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "stddev": 0.0, "min": 0.0, "max": 0.0}
    return {
        "mean": round(mean(values), 6),
        "stddev": round(pstdev(values) if len(values) > 1 else 0.0, 6),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
    }


def build_truth_table(db_path: Path, output_path: Path, window_samples: int) -> None:
    windows = load_windows(db_path, window_samples=window_samples)
    truth_table = {
        "source_db": str(db_path),
        "window_samples": window_samples,
        "event_profiles": {},
    }

    for event_name, window in sorted(windows.items()):
        truth_table["event_profiles"][event_name] = {
            "sample_count": len(window.magnitude_3d),
            "magnitude_3d": summarize(window.magnitude_3d),
            "magnitude_2d": summarize(window.magnitude_2d),
            "gyro_abs": summarize(window.gyro_abs),
        }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(truth_table, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="/data/imu-calibration.db")
    parser.add_argument("--output", default="/data/imu-truth-table.json")
    parser.add_argument("--window-samples", type=int, default=30)
    args = parser.parse_args()

    build_truth_table(Path(args.db_path), Path(args.output), args.window_samples)
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
