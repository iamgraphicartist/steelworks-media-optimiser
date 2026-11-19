#!/bin/bash
# Build the macOS app + .dmg.   Usage:  ./scripts/build_macos.sh
# Builds for the Mac you run it on (Apple silicon -> arm64, Intel -> x86_64).
# For a universal build use a universal2 Python and: SMO_TARGET_ARCH=universal2 ./scripts/build_macos.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
if [ ! -d .venv ]; then "$PY" -m venv .venv; fi
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python scripts/build.py "$@"
