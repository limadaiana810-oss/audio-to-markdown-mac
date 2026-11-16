#!/bin/bash
# Build an offline, relocatable Apple Silicon DMG from an installed light runtime.
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$SOURCE_DIR/package_release.py" "$@"
