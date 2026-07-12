#!/usr/bin/env bash
# Run the FastAPI backend on $PORT (dev + production).
set -euo pipefail
cd "$(dirname -- "$0")/.."
export MPLBACKEND="${MPLBACKEND:-Agg}"
# LightGBM (via nannyml/flaml) needs 64-bit libgomp.so.1; probe the nix store.
find_lib64() {
  local name="$1" f cls
  for f in /nix/store/*gcc*-lib/lib/"$name"; do
    [ -e "$f" ] || continue
    cls=$(od -An -j4 -N1 -tu1 "$f" 2>/dev/null | tr -d ' ')
    if [ "$cls" = "2" ]; then dirname "$f"; return 0; fi
  done
  return 1
}
GOMP_DIR=$(find_lib64 libgomp.so.1 || true)
if [ -n "${GOMP_DIR:-}" ]; then
  export LD_LIBRARY_PATH="${GOMP_DIR}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
PY=".venv-backend/bin/python"
if [ ! -x "$PY" ]; then
  echo "[run] ERROR: .venv-backend missing — run backend/build.sh first" >&2
  exit 1
fi
# Deterministic demo boot only.  Strict live never reads or mutates baked player state.
if [ "${CONTROL_TOWER_MODE:-demo}" != "live" ] && [ -f backend/control_tower.db ]; then
  (cd backend && "../$PY" -c "from app import db, config; db.put_state(scenario_id=config.DEFAULT_SCENARIO, tick=0, playing=0, speed=1, mode='baked', seed=config.DEMO_SEED)") || true
fi
cd backend
exec "../$PY" -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:?PORT is required}"
