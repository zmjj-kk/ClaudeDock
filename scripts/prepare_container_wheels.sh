#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv_command="${UV_BIN:-uv}"
"$uv_command" export --frozen --no-dev --no-emit-project --output-file build-assets/runtime-requirements.txt
mkdir -p build-assets/wheels
"${PYTHON_BIN:-python3}" -m pip download --require-hashes --no-deps --only-binary=:all: \
    -r build-assets/runtime-requirements.txt --dest build-assets/wheels
"${PYTHON_BIN:-python3}" -m pip download --only-binary=:all: \
    'hatchling==1.29.0' --dest build-assets/wheels
