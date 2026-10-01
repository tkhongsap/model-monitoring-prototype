"""FastAPI app — AI Use Case Observability Control Tower.

Startup: ensure the DB exists and the 130-row registry is seeded. Baking is done
via scripts/demo_reset or POST /api/sim/bake (dev-only) — never at request time.

Serving: /api/* is the contract; if the dashboard has been built
(artifacts/control-tower/dist/public — see scripts/deploy-build.sh) it is served
from this same app, so a deployment is ONE service on ONE port. In dev the Vite
server (:5000, proxying /api -> :8000) is used instead and the mount is skipped.
"""
from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, logging_setup, seeds_loader
from .live_poller import poller

logging_setup.configure(config.LOG_FORMAT)
log = logging.getLogger(__name__)

if config.strict_live_mode():
    from .api.live_routes import router
else:
    from .api.routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.engine()
    if config.strict_live_mode():
        from .api.live_portfolio import LIVE_UCS
        for source_id in LIVE_UCS:
            db.ensure_live_source(source_id)
        blockers = config.live_configuration_errors()
        if blockers:
            log.error("startup: strict live configuration blocked: %s", "; ".join(blockers),
                      extra={"configuration_errors": blockers})
        log.info("startup: strict live plane; baked registry not loaded")
    else:
        n = seeds_loader.seed_registry()
        log.info("startup: registry rows: %d", n, extra={"registry_rows": n})
    poller().start()
    try:
        yield
    finally:
        poller().stop()


app = FastAPI(
    title="AI Use Case Observability Control Tower",
    version="1.1", lifespan=lifespan,
    docs_url=None if config.strict_live_mode() else "/docs",
    redoc_url=None if config.strict_live_mode() else "/redoc",
    openapi_url=None if config.strict_live_mode() else "/openapi.json",
)
app.include_router(router)


@app.middleware("http")
async def strict_live_route_isolation(request: Request, call_next):
    """Make baked/scenario APIs unreachable in production, even if code is installed.

    Only read-only live artifacts/observations plus service diagnostics are public.  The
    background worker owns all live mutations; browser POSTs cannot advance cursors.
    """
    if config.strict_live_mode() and request.url.path.startswith("/api/"):
        allowed_exact = {"/api/health", "/api/healthz", "/api/version", "/api/readiness"}
        path = request.url.path
        allowed = path in allowed_exact or path.startswith("/api/live/")
        # the only mutating surface: the worker-token poll and operator unstick routes
        worker_post = request.method == "POST" and (
            path == "/api/live/poll"
            or re.fullmatch(r"/api/live/sources/[^/]+/(skip|reset-ack)", path) is not None)
        if (request.method != "GET" and not worker_post) or not allowed:
            return JSONResponse({"detail": "not available in strict live mode"}, status_code=404)
    return await call_next(request)


@app.get("/api/health")
def healthcheck():
    errors = config.live_configuration_errors()
    body = {"ok": not errors, "mode": config.CONTROL_TOWER_MODE,
            "configuration_errors": errors}
    return JSONResponse(body, status_code=200 if not errors else 503)


@app.get("/api/healthz")
def healthz():
    errors = config.live_configuration_errors()
    body = {"status": "ok" if not errors else "not_ready",
            "mode": config.CONTROL_TOWER_MODE, "configuration_errors": errors}
    return JSONResponse(body, status_code=200 if not errors else 503)


@app.get("/api/version")
def version():
    producer: dict = {"git_sha": None, "status": "not_configured"}
    if config.LIVE_PRODUCER_URL:
        try:
            from .adapters.telemetry_http import pull_build_version
            producer = {**pull_build_version(config.LIVE_PRODUCER_URL), "status": "connected"}
        except Exception as exc:  # noqa: BLE001 — build diagnostics remain available
            producer = {"git_sha": None, "status": "error",
                        "error": f"{type(exc).__name__}: {exc}"}
    return {"service": "model-monitoring-prototype", "contract_version": "1.1",
            "build_sha": config.BUILD_SHA, "monitor_git_sha": config.BUILD_SHA,
            "producer": producer, "mode": config.CONTROL_TOWER_MODE}


@app.get("/api/readiness")
def readiness():
    database_ok, database_error = True, None
    try:
        with db.engine().connect() as cx:
            cx.exec_driver_sql("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        database_ok = False
        database_error = f"{type(exc).__name__}: {exc}"
    configuration_errors = config.live_configuration_errors()
    worker_required = config.strict_live_mode() and config.LIVE_POLL_SECONDS > 0
    worker_ok = poller().running if worker_required else True
    judge_ok = bool(config.ANTHROPIC_API_KEY) if config.strict_live_mode() else True
    ready = database_ok and worker_ok and not configuration_errors
    body = {
        "status": "ready" if ready and judge_ok else ("degraded" if ready else "not_ready"),
        "database": {"ok": database_ok, "error": database_error},
        "poller": {"required": worker_required, "running": poller().running,
                   "last_error": poller().last_cycle_error,
                   # last cycle: duration, outcome and the last error per source (D.2)
                   "last_cycle": poller().last_cycle},
        "real_judge": {"required": config.strict_live_mode(), "configured": judge_ok},
        "configuration": {"ok": not configuration_errors, "errors": configuration_errors},
        "mode": config.CONTROL_TOWER_MODE,
    }
    return JSONResponse(body, status_code=200 if ready and judge_ok else 503)


# --- dashboard (built SPA), mounted LAST so the /api routes above always win ---

_SPA_DIST = Path(__file__).resolve().parents[2] / "artifacts" / "control-tower" / "dist" / "public"


class _SPAFiles(StaticFiles):
    """Static files with an SPA fallback: unknown non-API paths (client-side routes
    like /heatmap, /use-case/AICT-L01) serve index.html; /api/* keeps real 404s.
    Starlette signals a missing file either by RAISING HTTPException(404) (current
    versions) or by returning a 404 response (older) — handle both."""

    async def get_response(self, path: str, scope):
        from starlette.exceptions import HTTPException as StarletteHTTPException
        try:
            resp = await super().get_response(path, scope)
        except StarletteHTTPException as e:
            if e.status_code == 404 and not path.startswith("api"):
                return await super().get_response("index.html", scope)
            raise
        if resp.status_code == 404 and not path.startswith("api"):
            return await super().get_response("index.html", scope)
        return resp


if _SPA_DIST.is_dir():  # present only after a frontend build — dev machines skip this
    app.mount("/", _SPAFiles(directory=_SPA_DIST, html=True), name="dashboard")
    log.info("startup: serving dashboard from %s", _SPA_DIST)
