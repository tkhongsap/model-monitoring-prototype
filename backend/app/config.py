"""Configuration — env-driven, all optional (PRD Appendix E §E.4 / NF1).

The demo runs with an empty .env: zero keys, zero network (Langfuse stub default).
"""
from __future__ import annotations

import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent

try:  # best-effort .env loading (repo root, then backend-local); never clobber real env
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env", override=False)
    load_dotenv(BACKEND_DIR / ".env", override=False)
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str) -> str:
    v = os.getenv(name, "").strip()
    return v or default


DEMO_SEED = int(_env("DEMO_SEED", "42"))
LLM_EVAL_ADAPTER = _env("LLM_EVAL_ADAPTER", "langfuse_stub")
ML_MONITOR_ADAPTER = _env("ML_MONITOR_ADAPTER", "evidently_nannyml")
EXPLAIN_ADAPTER = _env("EXPLAIN_ADAPTER", "lime_shap")

LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "").strip() or "https://cloud.langfuse.com"

DB_PATH = Path(_env("RAI_DB_PATH", str(BACKEND_DIR / "control_tower.db")))
# Managed Postgres is selected with DATABASE_URL.  Keeping this optional preserves the
# zero-configuration SQLite developer/test path; db.engine() resolves DB_PATH lazily so
# tests can still swap it after importing this module.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ARTIFACTS_DIR = Path(_env("RAI_ARTIFACTS_DIR", str(BACKEND_DIR / "artifacts")))
SCENARIOS_YAML = BACKEND_DIR / "scenarios" / "simulation.yaml"
SEEDS_DIR = BACKEND_DIR / "seeds"

DEFAULT_SCENARIO = _env("RAI_DEFAULT_SCENARIO", "DEMO-FULL")

# --- live monitoring (Stage 2+): the monitor PULLS telemetry from external model apps ---
CONTROL_TOWER_MODE = _env("CONTROL_TOWER_MODE", "demo").lower()
ALLOW_INSECURE_LIVE_TESTING = _env("ALLOW_INSECURE_LIVE_TESTING", "0") == "1"
# Base URLs are operator-configured; the demo still runs fully offline (baked DEMO-FULL)
# when the model apps aren't up — the live path is only exercised via /api/live/*.
LIVE_CHURN_URL = _env("LIVE_CHURN_URL", "http://127.0.0.1:8083")
LIVE_CHATBOT_URL = _env("LIVE_CHATBOT_URL", "http://127.0.0.1:8082")
LIVE_NBA_URL = _env("LIVE_NBA_URL", "http://127.0.0.1:8084")
LIVE_POLL_SECONDS = float(_env(
    "LIVE_POLL_SECONDS", "5" if CONTROL_TOWER_MODE == "live" else "0"))
LIVE_POLL_LEASE_SECONDS = float(_env("LIVE_POLL_LEASE_SECONDS", "30"))
LIVE_IDLE_SECONDS = float(_env("LIVE_IDLE_SECONDS", "30"))
LIVE_STALE_SECONDS = float(_env("LIVE_STALE_SECONDS", "120"))
# Public portfolio gateway used for durable observation acknowledgements.  It is
# intentionally separate from the per-model URLs above.
LIVE_PRODUCER_URL = os.getenv("LIVE_PRODUCER_URL", "").strip().rstrip("/")
# contract v1.0 bearer auth: when set, every /telemetry/* and /model/artifact pull sends
# Authorization: Bearer <token> (the apps enforce it when THEIR RAI_TELEMETRY_TOKEN is set)
LIVE_TELEMETRY_TOKEN = os.getenv("LIVE_TELEMETRY_TOKEN", "").strip()
# Dedicated wake token for the scheduled Autoscale poll request. It is intentionally
# distinct from producer telemetry auth and is never exposed to the browser bundle.
LIVE_WORKER_TOKEN = os.getenv("LIVE_WORKER_TOKEN", "").strip()
# rolling per-signal history kept for the live dashboard sparklines/charts (bounded so a
# long continuously-observing session doesn't grow the buffer / detail payloads without end)
LIVE_HIST_MAX = int(_env("LIVE_HIST_MAX", "240"))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
# Haiku-class default per the contract's judge policy (§14) — one call PER TRACE, so the
# default tier must be the cheap one; override for higher-stakes evaluation.
LLM_JUDGE_MODEL = _env("LLM_JUDGE_MODEL", "claude-haiku-4-5")
# Judge sampling cap (§14): the REAL Claude judge is one API call per trace, so a large
# window makes a live tick slow. When set, judge a uniform sample of at most this many
# traces per window (the offline heuristic judge ignores the cap — it is instant). 0 = no cap.
LLM_JUDGE_MAX_TRACES = int(_env("LLM_JUDGE_MAX_TRACES", "20"))

_BUILD_SHA_FILE = BACKEND_DIR / ".build-sha"
BUILD_SHA = (
    os.getenv("REPLIT_DEPLOYMENT_SHA", "").strip()
    or os.getenv("GIT_SHA", "").strip()
    or os.getenv("COMMIT_SHA", "").strip()
    or (_BUILD_SHA_FILE.read_text(encoding="utf-8").strip()
        if _BUILD_SHA_FILE.is_file() else "")
    or "unknown"
)


def langfuse_cloud_configured() -> bool:
    return bool(LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY)


def live_enabled() -> bool:
    return bool(LIVE_CHURN_URL)


def strict_live_mode() -> bool:
    """True only for the production control-tower plane.

    Demo/baked routes stay available in developer mode, but are made unreachable by
    the FastAPI application when this switch is on.
    """
    return CONTROL_TOWER_MODE == "live"


def live_configuration_errors() -> list[str]:
    """Return production-safety blockers for strict live mode.

    The only bypass is an explicit test-only switch; production must never silently
    fall back to process-local SQLite, localhost producers, an unauthenticated telemetry
    contract, or heuristic judging.
    """
    if not strict_live_mode() or ALLOW_INSECURE_LIVE_TESTING:
        return []
    from urllib.parse import urlsplit

    errors: list[str] = []
    if not DATABASE_URL:
        errors.append("DATABASE_URL is required (managed PostgreSQL; SQLite is not durable)")
    elif not DATABASE_URL.startswith(("postgres://", "postgresql://", "postgresql+psycopg://")):
        errors.append("DATABASE_URL must use PostgreSQL in strict live mode")
    for name, value in (
        ("LIVE_CHURN_URL", LIVE_CHURN_URL),
        ("LIVE_CHATBOT_URL", LIVE_CHATBOT_URL),
        ("LIVE_NBA_URL", LIVE_NBA_URL),
        ("LIVE_PRODUCER_URL", LIVE_PRODUCER_URL),
    ):
        host = (urlsplit(value).hostname or "").lower() if value else ""
        if not value:
            errors.append(f"{name} is required")
        elif host in {"127.0.0.1", "localhost", "::1"}:
            errors.append(f"{name} must be an external deployment URL")
    if not LIVE_TELEMETRY_TOKEN:
        errors.append("LIVE_TELEMETRY_TOKEN is required")
    if not LIVE_WORKER_TOKEN:
        errors.append("LIVE_WORKER_TOKEN is required for authenticated Autoscale wake polls")
    if not ANTHROPIC_API_KEY:
        errors.append("ANTHROPIC_API_KEY is required for real live judging")
    if not langfuse_cloud_configured():
        errors.append("LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required")
    if LIVE_POLL_SECONDS <= 0:
        errors.append("LIVE_POLL_SECONDS must be greater than zero")
    return errors
