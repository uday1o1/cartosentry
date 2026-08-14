# ADR 0003: Zero-event false-critical rate bound

- Status: accepted before M3.2 implementation qualification
- Date: 2026-08-14

## Context

The frozen charter requires the false-critical rate to use a one-sided 95 percent upper decision bound.
The build plan also states that zero observed failures are not a zero failure-rate estimate.
For correlated event rates, the plan selects a source-group clustered bootstrap.

When every eligible clean source group has zero false-critical events, every clustered bootstrap replicate is also zero.
That procedure returns an upper value of zero and directly contradicts the plan's zero-observation rule.
The conflict appears only at the all-zero boundary and does not justify changing the detector threshold, clean workload, source groups, bootstrap seed, replicate count, or nonzero-event method.

## Decision

The qualification continues to compute and retain the frozen clustered-bootstrap result.
When and only when the total observed false-critical count is zero, the reported conservative upper value uses the exact one-sided Poisson exposure upper bound `-ln(0.05) / eligible_sensor_hours`.
The report records both values, the observed event count, eligible exposure, and method identifier `exact_poisson_exposure_95`.
Nonzero event rates continue to use the clustered bootstrap without this fallback.

The frozen M3.2 profile predeclares the method identifier and provides 900 seconds of clean trajectory exposure per source group.
The eight-group development partition remains descriptive.
The 12-group calibration partition supplies exactly three clean sensor-hours without splitting bootstrap clusters.
M3.2 uses this result only for a nonconfirmatory frozen synthetic engineering and calibration checkpoint.

## Consequences

An all-zero clean result retains a positive upper uncertainty bound and cannot be presented as zero risk.
The correction is narrower than changing the charter or gate and preserves the original false-critical objective.
Any confirmatory release evaluation must apply a predeclared method that satisfies its frozen charter before any final result is visible.
