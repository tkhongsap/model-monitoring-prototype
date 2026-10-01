# Testing and Verification

## Required checks

Backend, run from `backend/`:

```bash
.venv/bin/python -m pytest -q -m "not slow"   # fast suite: unit, route, persistence, docs
.venv/bin/python -m pytest -q                 # full suite, adds the slow DEMO-FULL bake tests
.venv/bin/python scripts/migrate.py           # migrations apply and are idempotent
```

Frontend and workspace, run from the repository root:

```bash
pnpm run typecheck
pnpm run build:live
pnpm run check:strict-live
```

Prerequisites: Python 3.12 with the `backend/.venv` created as in
[README.md](README.md#first-success); Node 24 with pnpm 10.15.1 (`corepack enable &&
corepack prepare pnpm@10.15.1 --activate`) and `pnpm install --frozen-lockfile`.

Host limits: `pnpm run build:live` and `pnpm run check:strict-live` run only on Linux
x64. `pnpm-workspace.yaml` deliberately drops every non-Linux native binary
(`@rollup/rollup-darwin-*`, `lightningcss-darwin-*`, …) so the Replit image stays small,
so on macOS the Vite build fails with "Cannot find module @rollup/rollup-darwin-arm64".
The strict-live frontend workflow (`.github/workflows/strict-live-frontend.yml`) is the
proof for those two commands; report them as unavailable when running on macOS.

CI runs the backend suite against PostgreSQL 16 in
`.github/workflows/backend-live.yml` and also validates the strict-live configuration
with CI placeholder URLs and tokens. Locally the suite uses SQLite under a temporary
directory; no test needs a real producer, Langfuse, or a live Claude judge, and those
three are always reported as unavailable in this environment.

## Change-specific proof

| Change type | Required evidence |
|---|---|
| Telemetry adapter or runner | Fake producer test (monkeypatched `telemetry_http.pull`) covering the happy path, `count=0`, 404, and digest mismatch; cursor position asserted |
| Schema (`backend/app/db.py`) | New ordered migration; `scripts/migrate.py` run twice; a test in `test_live_persistence.py` style that writes and reads the table |
| Health grading or rollup | Unit test against `engines/health.py` bands; sparkline history unchanged for old ticks |
| API route | TestClient test in live mode asserting redaction and that demo routes stay 404 |
| Frontend | `pnpm run typecheck`; `build:live` and `check:strict-live` in CI (bundle contains no demo identifiers or secret names) |
| Documentation | `backend/tests/test_docs.py` (required files, one H1, CLAUDE.md ≤ 120 lines, links resolve, no machine-local paths) |
| Deployment scripts | `bash -n` on the script; `scripts/post-merge.sh` never runs a schema push (`test_docs.py`) |
| Model, prompt or judge | Deterministic adapter test with a fake judge; no live Claude call is ever counted as evidence |

## Reporting

Record passed, failed, skipped, and unavailable checks; commands, environment,
release identity (`GET /api/version` reports `build_sha`), raw evidence location, and
remaining risk. Never report an unavailable dependency as a pass. Work-log entries in
[DEVLOG.md](DEVLOG.md) carry the evidence for each slice.
