#!/usr/bin/env bash
# Replit production entrypoint: strict-live is explicit and cannot inherit the
# developer-mode default when a runtime secret is absent.
set -euo pipefail
cd "$(dirname -- "$0")/.."
export CONTROL_TOWER_MODE=live
exec bash backend/run.sh
