FROM python:3.12.13-slim-bookworm@sha256:6e13e65c55e33adf203d77ee371cf8bf5d81bd4902ef07565721f46bf44917af

ARG DEBIAN_FRONTEND=noninteractive
ARG SOURCE_REVISION=working-tree

RUN printf '%s\n' \
      'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20260813T000000Z bookworm main' \
      > /etc/apt/sources.list \
    && rm -f /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install --yes --no-install-recommends \
      clang=1:14.0-55.7~deb12u1 \
      git=1:2.39.5-0+deb12u3 \
      libbz2-dev=1.0.8-5+b1 \
      libclang-rt-14-dev=1:14.0.6-12 \
      libexpat1-dev=2.5.0-1+deb12u2 \
      ninja-build=1.11.1-2~deb12u1 \
      zlib1g-dev=1:1.2.13.dfsg-1 \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir \
  'https://files.pythonhosted.org/packages/19/ff/764e1c21ba988589d2b505d2b06876b5f06ffe7cc6858dff6cc3faf7cb14/uv-0.11.23-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl#sha256=7a85330de0a7eb0d5c6cf03c80edfb86facad19df367a0b52fc906db1ab15ce9'

ENV UV_CACHE_DIR=/opt/uv-cache \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONPATH=/workspace/python \
    CARTOSENTRY_SOURCE_REVISION=${SOURCE_REVISION}

WORKDIR /workspace
COPY pyproject.toml uv.lock .python-version README.md LICENSE ./
RUN CMAKE_GENERATOR='Unix Makefiles' \
  uv sync --frozen --no-install-project --group fuzz --python 3.12.13

COPY . .

RUN CC=/usr/bin/clang-14 CXX=/usr/bin/clang++-14 \
      uv sync --frozen --group fuzz --python 3.12.13 \
    && CC=/usr/bin/clang-14 CXX=/usr/bin/clang++-14 \
      uv run --no-sync cmake --preset fuzz -DCMAKE_MAKE_PROGRAM=/usr/bin/ninja \
    && uv run --no-sync cmake --build --preset fuzz -j 2

ENTRYPOINT ["/workspace/.venv/bin/python", "scripts/run_fuzz.py"]
CMD ["--suite", "local", "--build-dir", "build/fuzz", "--output-root", "/fuzz-evidence"]
