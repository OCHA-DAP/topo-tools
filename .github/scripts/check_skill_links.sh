#!/usr/bin/env bash
set -euo pipefail
exec lychee --no-progress \
  --remap "https://raw.githubusercontent.com/OCHA-DAP/topo-tools/main/(.*) file://${PWD}/\$1" \
  "$@"
