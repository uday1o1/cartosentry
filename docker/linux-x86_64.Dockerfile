FROM python:3.12.13-slim-bookworm@sha256:6e13e65c55e33adf203d77ee371cf8bf5d81bd4902ef07565721f46bf44917af

ARG DEBIAN_FRONTEND=noninteractive

RUN printf '%s\n' \
      'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20260813T000000Z bookworm main' \
      > /etc/apt/sources.list \
    && rm -f /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install --yes --no-install-recommends \
      build-essential=12.9 \
      g++=4:12.2.0-3 \
      ninja-build=1.11.1-2~deb12u1 \
      zlib1g-dev=1:1.2.13.dfsg-1 \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir \
  'https://files.pythonhosted.org/packages/19/ff/764e1c21ba988589d2b505d2b06876b5f06ffe7cc6858dff6cc3faf7cb14/uv-0.11.23-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl#sha256=7a85330de0a7eb0d5c6cf03c80edfb86facad19df367a0b52fc906db1ab15ce9'

ENV UV_CACHE_DIR=/opt/uv-cache \
    CMAKE_BUILD_PARALLEL_LEVEL=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /workspace
COPY pyproject.toml uv.lock .python-version README.md LICENSE ./

RUN CMAKE_GENERATOR='Unix Makefiles' \
  uv sync --frozen --no-install-project --python 3.12.13

COPY . .

RUN CMAKE_GENERATOR='Unix Makefiles' uv sync --frozen --python 3.12.13 \
    && uv run ruff check . \
    && uv run ruff format --check . \
    && uv run mypy \
    && uv run pytest -q \
    && uv run python -c 'import cartosentry; assert cartosentry.native_self_check()'

RUN uv run cmake --preset developer -DCMAKE_MAKE_PROGRAM=/usr/bin/ninja \
    && uv run cmake --build --preset developer -j 1 \
    && uv run ctest --preset developer \
    && uv run cmake --preset release -DCMAKE_MAKE_PROGRAM=/usr/bin/ninja \
    && uv run cmake --build --preset release -j 1 \
    && uv run ctest --preset release \
    && uv run cmake --preset sanitizer -DCMAKE_MAKE_PROGRAM=/usr/bin/ninja \
    && uv run cmake --build --preset sanitizer -j 1 \
    && uv run ctest --preset sanitizer

RUN uv run cmake --preset compatibility -DCMAKE_MAKE_PROGRAM=/usr/bin/ninja \
    && uv run cmake --build --preset compatibility -j 1 \
    && uv run ctest --preset compatibility

RUN CMAKE_GENERATOR='Unix Makefiles' SOURCE_DATE_EPOCH=0 uv build --wheel --sdist \
    && uv venv /opt/wheel-venv --python 3.12.13 \
    && uv pip install --python /opt/wheel-venv/bin/python dist/*.whl \
    && cd /tmp \
    && /opt/wheel-venv/bin/python -c 'import cartosentry; assert cartosentry.native_self_check()' \
    && /opt/wheel-venv/bin/python -c 'import glob, zipfile; wheel=glob.glob("/workspace/dist/*.whl")[0]; payload=b"".join(zipfile.ZipFile(wheel).read(name) for name in zipfile.ZipFile(wheel).namelist()); assert b"/workspace/" not in payload'
