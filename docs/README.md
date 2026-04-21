# Calibration Services: Dormant-by-Default Trigger

The `imu-calibration` and `ble-calibration` services are intentionally dormant by default.

## How to run a calibration session

1. In Balena, add `ENABLE_CALIBRATION=true` as a **service-scoped device variable** for the target container (`imu-calibration` or `ble-calibration`).
2. Start the target calibration service and complete your session.
3. Reset or remove `ENABLE_CALIBRATION` after the run so the service returns to dormant mode.

This keeps calibration containers available on-device without running unless explicitly enabled.
