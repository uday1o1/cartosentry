# Qualified toolchain

## Support boundary

CartoSentry currently supports Python 3.12 and C++20.
The reproducible Python environment is fixed to Python 3.12.13 by `.python-version` and `uv.lock`.
The declared Python package range is `>=3.12,<3.13` so later Python 3.12 patch releases may be evaluated without changing the public compatibility claim.
Only Python 3.12.13 is qualified by M0.3.

The native M0.3 build is qualified on macOS ARM64 and Linux x86-64.
These checks establish correctness and packaging compatibility only.
They are not runtime performance evidence.

| Platform | Qualified environment | Compiler | Build tools | Result |
| --- | --- | --- | --- | --- |
| macOS ARM64 | macOS 26.5.2, build 25F84 | AppleClang 21.0.0 | CMake 4.4.2, Ninja 1.13.0, uv 0.11.23 | Editable install, developer/release/sanitizer builds, locked dependency probe, wheel, source distribution, and clean wheel import passed. |
| Linux x86-64 | `python:3.12.13-slim-bookworm` amd64 manifest `sha256:6e13e65c55e33adf203d77ee371cf8bf5d81bd4902ef07565721f46bf44917af` | GCC 12.2.0-14+deb12u1 | CMake 4.4.2, Debian Ninja 1.11.1-2~deb12u1, uv 0.11.23 | Frozen sync, editable install, developer/release/sanitizer builds, locked dependency probe, wheel, source distribution, and clean wheel import passed. |

The Linux image uses packages from the immutable Debian snapshot `20260813T000000Z`.
Its top-level package versions and uv wheel hash are fixed in `docker/linux-x86_64.Dockerfile`.
The local qualification ran the amd64 image on an ARM64 host through Apple Rosetta in a Linux virtual machine.
The same image is suitable for a native amd64 Docker host, but M0.3 did not measure or compare performance across those hosts.

The supported compiler range is currently AppleClang 21 on macOS ARM64 and GCC 12.2 on Linux x86-64.
Other compilers and operating-system releases are unqualified until their full gate passes.
The optional CUDA preset intentionally fails closed until the M12 profiling gate selects and records a CUDA toolchain.

## Dependency selection

`cmake/dependencies.lock.json` records full source identities for every selected native dependency.
Every source archive consumed by the M0.3 build also has an exact byte count and SHA-256 digest.
`uv.lock` records the exact Python resolution and wheel or source hashes for both mandatory platforms.

The compatibility executable compiles and calls the locked GeographicLib, OpenCV, Arrow, SQLite, nlohmann/json, yaml-cpp, spdlog, and fmt releases together.
The core native tests compile the selected Sophus and Eigen pair with Catch2.
The Python tests call the checked C++ extension through pybind11.
Sophus 1.0.0 with Eigen 3.4.0 is the only selected `SE(3)` implementation.

Libosmium 2.23.1 is linked for streaming OSM XML import through the exact archive identity in the native dependency lock.
Its XML path uses Expat, BZip2, Zlib, and threads from the qualified platform toolchains.
Conditional dependencies such as libcurl, zstd, nanoflann, CLI11, RapidCheck, and libosmium's Protozero-backed PBF path have immutable source selections in the lock but are not yet linked into the foundation.
Their feature-specific integration gates remain authoritative and may reject or replace a selection through a focused dependency commit.
No feature may claim those integrations merely because a source identity is present in the lock.

## Local verification

Install the exact Python environment and validate both dependency locks.

```console
uv sync --frozen --python 3.12.13
uv run python scripts/check_dependency_lock.py
uv run python scripts/check_public_safety.py
```

Run the standard native path.

```console
uv run cmake --preset developer
uv run cmake --build --preset developer -j 4
uv run ctest --preset developer
```

Run the full locked native dependency probe.

```console
uv run cmake --preset compatibility
uv run cmake --build --preset compatibility -j 4
uv run ctest --preset compatibility
```

Build release artifacts through the configured PEP 517 backend.

```console
SOURCE_DATE_EPOCH=0 uv build --wheel --sdist
```

Run the Linux x86-64 qualification from an ARM64 Colima host only after Rosetta support is enabled.
On a native x86-64 Docker host, the same command does not require translation support.

```console
docker build --platform linux/amd64 --file docker/linux-x86_64.Dockerfile --tag cartosentry:m0.3-linux .
```

The image is local verification output and must not be pushed to a registry.
