#!/usr/bin/env bash
# Deployment build (Replit Autoscale): build the strict-live SPA, then the backend.
# The FastAPI backend serves the built SPA from artifacts/control-tower/dist/public,
# so the deployed monitor is ONE service on $PORT (dashboard + /api together).
set -euo pipefail
cd "$(dirname -- "$0")/.."
export CONTROL_TOWER_MODE="${CONTROL_TOWER_MODE:-live}"

BUILD_SHA="${REPLIT_GIT_COMMIT:-$(git rev-parse HEAD)}"
test -n "$BUILD_SHA"
printf '%s\n' "$BUILD_SHA" > backend/.build-sha
# Demo output is tracked only as developer reference; it must not enter the strict-live
# deployment image. Runtime live artifacts are generated and persisted in PostgreSQL.
find backend/artifacts -maxdepth 1 -type f -delete 2>/dev/null || true

echo "[deploy-build] 1/3 workspace install (pnpm)…"
corepack enable >/dev/null 2>&1 || true
CI=true corepack pnpm install --frozen-lockfile

echo "[deploy-build] 2/3 dashboard build (vite)…"
# vite.config.ts requires PORT + BASE_PATH; PORT is unused by `build` but validated.
(cd artifacts/control-tower && PORT="${PORT:-5000}" BASE_PATH="/" \
  CONTROL_TOWER_MODE="$CONTROL_TOWER_MODE" node node_modules/vite/bin/vite.js build)

echo "[deploy-build] verifying strict-live dashboard bundle…"
node scripts/check-strict-live-bundle.mjs

echo "[deploy-build] 3/3 backend build (venv + deps)…"
bash backend/build.sh

echo "[deploy-build] done"
