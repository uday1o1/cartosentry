# Decision 0001: Select Sophus for SE(3)

- Status: accepted
- Date: 2026-08-13
- Scope: the native geometry foundation

## Context

The V1 geometry and trajectory contracts require checked `SO(3)` and `SE(3)` exponential, logarithm, and interpolation primitives.
The implementation plan prefers Sophus when one compatible release can be selected with Eigen and the supported compilers.
Only one implementation may remain after the compatibility spike.

Sophus publishes v1.0.0 as its latest stable tag.
Its headers compile and execute the checked `SE3d::exp` and `SE3d::log` probe with Eigen 3.4.0, C++20, and Apple Clang 21.
The same Sophus release does not compile with Eigen 5.0.0 because it directly includes Eigen internal geometry headers that Eigen 5 rejects.
Sophus v1.0.0 also uses a GNU variadic-macro extension in an assertion helper, so dependency headers must be marked as system includes rather than weakening warnings for project code.

## Decision

CartoSentry selects Sophus v1.0.0 at commit `db218a249202fe63ac13248b5f565b0d385f6640` with Eigen 3.4.0 at commit `3147391d946bb4b6c68edd901f2add6ac1f31f8c`.
Both are header-only, fetched from their official public repositories by exact commit, and exposed only through project-owned interfaces.
CartoSentry project targets keep warnings as errors.
Third-party headers are system includes so their upstream extension does not suppress or downgrade project warnings.

## Consequences

Eigen 5 is not part of the initial compatibility range.
An Eigen or Sophus upgrade requires a focused dependency change and the full geometry, compiler, wheel, and cross-platform gates.
No parallel internal `SE(3)` implementation will be maintained.
