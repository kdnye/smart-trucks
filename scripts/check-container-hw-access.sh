#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
MODE="runtime"
if [[ "${1:-}" == "--config-only" ]]; then
  MODE="config"
fi

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "ERROR: Compose file not found: $COMPOSE_FILE" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker command not found. Install Docker and rerun." >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: docker compose plugin is unavailable." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is required to inspect compose configuration" >&2
  exit 1
fi

services_json="$(docker compose -f "$COMPOSE_FILE" config --format json)"

python3 - "$MODE" "$services_json" <<'PY'
import json
import subprocess
import sys

mode = sys.argv[1]
config = json.loads(sys.argv[2])
services = config.get("services", {})

if not services:
    print("No services found in compose config.")
    raise SystemExit(1)

checks = {
    "/dev/i2c-1": "I2C bus",
    "/dev/serial0": "GPS UART",
}

print("Compose hardware access audit")
print("=" * 40)
for name, svc in services.items():
    privileged = bool(svc.get("privileged", False))
    devices = svc.get("devices") or []
    mapped = []
    for item in devices:
        if isinstance(item, str):
            mapped.append(item.split(":", 1)[0])
        elif isinstance(item, dict):
            src = item.get("source")
            if src:
                mapped.append(str(src))

    print(f"\n{name}")
    print(f"  privileged: {'yes' if privileged else 'no'}")
    for device, label in checks.items():
        has_map = device in mapped
        print(f"  mapping {device} ({label}): {'yes' if has_map else 'no'}")

    if mode != "runtime":
        continue

    if not mapped:
        print("  runtime checks: skipped (no device mappings configured)")
        continue

    # Container must be running for exec checks.
    try:
        cid = subprocess.check_output(
            ["docker", "compose", "ps", "-q", name], text=True
        ).strip()
    except subprocess.CalledProcessError:
        cid = ""

    if not cid:
        print("  runtime checks: skipped (container not running)")
        continue

    for device in checks:
        cmd = ["docker", "compose", "exec", "-T", name, "test", "-e", device]
        ok = subprocess.call(cmd) == 0
        print(f"  runtime sees {device}: {'yes' if ok else 'no'}")
PY
