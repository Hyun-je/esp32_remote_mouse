#!/usr/bin/env bash
set -euo pipefail

firmware_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v pio >/dev/null 2>&1; then
  printf 'Error: PlatformIO (pio) is not installed or not on PATH.\n' >&2
  exit 1
fi

pio run -d "$firmware_dir" -t upload "$@"
