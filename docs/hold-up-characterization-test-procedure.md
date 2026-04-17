# Hold-Up Characterization Test Procedure

This procedure defines how to characterize shutdown hold-up time for each hardware profile so low-battery thresholds can be set with predictable safety headroom.

## Objective

Measure the elapsed time between a controlled low-battery shutdown request and complete power loss, then derive a minimum safe shutdown threshold that leaves **20–60 seconds** of hold-up headroom.

## Scope

Use this process for every deployed hardware profile (for example: Pi Zero 2 W + UPS HAT revision + battery pack type).

## Preconditions

1. Device battery is at **full charge**.
2. Device has a **representative load** (normal services running, normal telemetry cadence, expected peripherals connected).
3. Clock/log timestamps are synchronized and capture at least second-level precision.
4. Test operator can safely repeat discharge cycles for consistency checks.

## Procedure

1. **Start from full charge under representative load.**
2. **Trigger a controlled low-battery shutdown** using the current shutdown threshold logic.
3. **Measure hold-up duration:**
   - Record timestamp of the shutdown request log entry.
   - Record timestamp of final power loss (device fully off).
   - Compute: `hold_up_seconds = power_loss_timestamp - shutdown_request_timestamp`.
4. **Determine minimum safe threshold:**
   - Repeat across multiple runs per hardware profile.
   - Increase/decrease threshold until the resulting hold-up time consistently leaves **20–60 seconds** of headroom.
   - Record the lowest threshold that still meets the 20–60 second target consistently.

## Test Matrix and Results Template

Use one row per hardware profile.

| Hardware Profile ID | Build/Revision Notes | Trial Count | Candidate Threshold Tested | Hold-Up Range (s) | Recommended Threshold | Headroom Outcome | Operator | Date (UTC) |
| --- | --- | ---: | --- | ---: | --- | --- | --- | --- |
| example-profile-a | Pi Zero 2 W + UPS HAT B rev2 + 2x18650 | 5 | 3.42 V | 24–46 | 3.42 V | PASS (within 20–60s) | initials | YYYY-MM-DD |

## Acceptance Criteria

- At least **3 repeat runs** per hardware profile at the final candidate threshold.
- All final validation runs produce hold-up headroom in the **20–60 second** band.
- Recommended threshold is recorded in this repository and linked from `readme.md`.

## Recommended Thresholds by Hardware Profile

Populate this section after characterization is complete.

| Hardware Profile ID | Recommended Low-Battery Shutdown Threshold | Validation Summary |
| --- | --- | --- |
| _TBD_ | _TBD_ | _TBD_ |

## Notes

- If hold-up time is frequently below 20 seconds, raise the shutdown threshold and re-test.
- If hold-up time is consistently above 60 seconds, threshold may be conservative; validate whether it can be lowered without violating the minimum margin.
- Re-run characterization after any material change in battery chemistry, UPS hardware revision, firmware behavior, or sustained service load.
