# Monitoring Gap Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

- **Status:** Approved
- **Author:** Claude Code session, 2026-10-01
- **Date:** 2026-10-01
- **Spec:** [spec.md](spec.md)
- **Approved by:** project owner, 2026-10-01 ("write the plan … implement … you don't need to ask my permission")

**Goal:** Make the strict-live monitor trustworthy: empty windows and lagged labels are handled per contract, Red/Amber transitions are persisted and delivered to a webhook, producers are pulled with retry, a stuck source can be skipped by an operator, and the repository carries the playbook's required documents.

**Architecture:** Five sequential slices, each its own branch → PR → merge to `main`. Backend changes are additive tables (migrations 4–6) plus new focused modules (`label_backfill.py`, `engines/alerts.py`, `alerting.py`, `alert_delivery.py`, `http_retry.py`, `logging_setup.py`); runners and routes call them. Stored observations are never rewritten. Frontend gains two read-only alert components. Slice E removes dead scaffold.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy Core, httpx, pytest; React 19 + Vite + Tailwind; pnpm 10.15.1; GitHub Actions.

## Global Constraints

- Contract version stays `"1.1"`; no producer-side changes.
- `live_observations.payload` and `content_sha256` are never updated after insert.
- Cursors advance only inside `db.put_live_observation` (or the new `db.skip_live_tick`).
- No new paid dependencies; no new npm packages; no new pip packages.
- `CLAUDE.md` ≤ 120 lines. Every new doc linked from `README.md`.
- Conventional commit prefixes: `docs:`, `feat:`, `fix:`, `test:`, `chore:`; trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Verification commands (working directory `backend/`): `.venv/bin/python -m pytest -q -m "not slow"`; (repo root): `pnpm run typecheck && pnpm run build:live && pnpm run check:strict-live`.
- Never report an unavailable check (real producer, Langfuse, live Claude) as passed.

## Review Focus

1. A `count=0` window immediately followed by a normal window: second tick must grade normally, with no stale "empty window" reason leaking into it. → Task B2 test `test_empty_window_then_normal_window`.
2. Labels for tick *u* arrive, then the producer evicts tick *u* inferences (404) on a later cycle: the realized row must stay `realized`, never flip to `evicted`. → Task B4 test `test_realized_row_is_final`.
3. A lane that goes Red → Unknown (source stale) → Red again: must not open a duplicate alert. → Task C1 test `test_unknown_gap_does_not_reopen`.
4. Webhook returns 500 twice then 200: alert must deliver once, never twice. → Task C3 test `test_delivery_retries_then_marks_once`.
5. Skip endpoint called with no held error (healthy source): must refuse with 409, never advance a healthy cursor. → Task D3 test `test_skip_requires_held_cursor`.

---

## Slice A — Playbook baseline and `postMerge` fix

Branch: `feat/monitoring-gap-closure` (already holds intent + spec). PR title: `docs: playbook baseline, safe postMerge hook`.

### Task A1: Docs conformance test

**Files:**
- Create: `backend/tests/test_docs.py`

**Interfaces:**
- Produces: nothing importable; a test that later tasks make pass.

- [ ] **Step 1: Write the failing test**

```python
"""Repository documentation contract (mirrors the playbook's check_docs rules)."""
from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
REQUIRED = ["README.md", "CLAUDE.md", "AGENTS.md", "CHANGELOG.md", "DEVLOG.md", "TESTING.md"]


def _md_files() -> list[Path]:
    skip = {".migration-backup", "node_modules", ".venv", "dist", ".git"}
    return [p for p in ROOT.rglob("*.md")
            if not any(part in skip for part in p.parts)]


def test_required_documents_exist():
    missing = [name for name in REQUIRED if not (ROOT / name).is_file()]
    assert not missing, f"missing playbook documents: {missing}"


def test_claude_md_is_navigational():
    lines = (ROOT / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 120, f"CLAUDE.md has {len(lines)} lines; budget is 120"


def test_markdown_hygiene():
    problems = []
    for path in _md_files():
        text = path.read_text(encoding="utf-8")
        h1 = [l for l in text.splitlines() if l.startswith("# ")]
        if path.name in REQUIRED and len(h1) != 1:
            problems.append(f"{path.relative_to(ROOT)}: expected one H1, found {len(h1)}")
        if not text.endswith("\n"):
            problems.append(f"{path.relative_to(ROOT)}: missing final newline")
        if "/Users/" in text or "file://" in text:
            problems.append(f"{path.relative_to(ROOT)}: machine-local path")
    assert not problems, "\n".join(problems)


def test_internal_links_resolve():
    broken = []
    link = re.compile(r"\]\((?!https?://|mailto:|#)([^)#]+)(?:#[^)]*)?\)")
    for path in _md_files():
        for target in link.findall(path.read_text(encoding="utf-8")):
            if not (path.parent / target).exists():
                broken.append(f"{path.relative_to(ROOT)} -> {target}")
    assert not broken, "\n".join(broken)


def test_post_merge_hook_never_pushes_schema():
    hook = (ROOT / "scripts" / "post-merge.sh").read_text(encoding="utf-8")
    assert "db push" not in hook and "drizzle-kit" not in hook
```

- [ ] **Step 2: Run it to verify it fails**

Run (from `backend/`): `.venv/bin/python -m pytest tests/test_docs.py -q`
Expected: FAIL on `test_required_documents_exist` and `test_post_merge_hook_never_pushes_schema`.

### Task A2: Fix the `postMerge` hook

**Files:**
- Modify: `scripts/post-merge.sh`

- [ ] **Step 1: Replace the hook body**

```bash
#!/bin/bash
# Replit postMerge hook. The Python backend owns DATABASE_URL and migrates itself at
# startup (backend/app/db.py migrate_engine); never run a JS schema push against it.
set -e
pnpm install --frozen-lockfile
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_docs.py::test_post_merge_hook_never_pushes_schema -q` → PASS.
- [ ] **Step 3: Commit** `fix: stop postMerge hook from pushing an empty Drizzle schema`

### Task A3: Write CLAUDE.md, AGENTS.md, README.md, TESTING.md, CHANGELOG.md, DEVLOG.md; shrink replit.md

**Files:**
- Create: `CLAUDE.md`, `AGENTS.md`, `README.md`, `TESTING.md`, `CHANGELOG.md`, `DEVLOG.md`
- Modify: `replit.md`

- [ ] **Step 1: CLAUDE.md** (≤120 lines) using the playbook `project-agents` template. Required content:
  - Project contract: Outcome (strict-live RAI monitor for the three `ai-use-cases` models); Source of truth `docs/MONITORING-CONTRACT.md`; Risk tier R1; Architecture `README.md#architecture`; Current work and gaps `DEVLOG.md`; Applicable playbook version `tkhongsap-ai-engineering-playbook@5ba9dc8`; Release path `docs/STRICT-LIVE.md`; Incident path "none — pilot; see DEVLOG known gaps".
  - Before changing code: read `README.md`, `docs/MONITORING-CONTRACT.md`, `docs/STRICT-LIVE.md`, `DEVLOG.md`; a `changes/<date>-<slug>/` plan for non-trivial work.
  - Commands (exact, with working directory): backend venv create (`uv venv --python 3.12 .venv && uv pip install -r requirements.txt`), tests, typecheck, build:live, check:strict-live, migrate.
  - Architecture rules: browser never advances cursors; observations immutable; demo routes 404 in live; engines pure, adapters do I/O, runners orchestrate.
  - Permissions: may run tests/builds; ask before deploying, deleting data, changing secrets; never commit `.env`.
  - Lessons section (newest first) seeded with: "When a producer window has `count=0`, store it as an observation and advance — never hold the cursor."
- [ ] **Step 2: AGENTS.md** — three lines: title, "Instructions live in [CLAUDE.md](../../CLAUDE.md); this file exists so Codex-style tools find them.", link (relative to the repo root in the real file).
- [ ] **Step 3: README.md** from the `project-readme` template: First success (prereqs: Python 3.12, uv, Node 24 + corepack; working dir; commands; expected result "35 passed" and `/api/readiness` JSON); Scope; Architecture (ASCII: producer → monitor poller → Postgres → API → SPA; list engines); Development table; Release and operations (Replit Autoscale, `scripts/deploy-*.sh`, readiness/version endpoints, `autoscale-poll.yml`); Repository guide linking every doc incl. `changes/`; Known limitations (LIME off in prod, sampling policy uniform, alerts absent until slice C — update per slice).
- [ ] **Step 4: TESTING.md** from the template: required checks with working directories; change-specific proof table; reporting rule.
- [ ] **Step 5: CHANGELOG.md** Keep-a-Changelog with `## [Unreleased]` → `### Added` (playbook docs, docs conformance test) and `### Fixed` (postMerge hook).
- [ ] **Step 6: DEVLOG.md** template sections. Current outcome = intent's outcome. Done when = the five slices' verification lines. Current plan = five slices with status. Work log entry `### 2026-10-01 — audit and playbook baseline` with Changed/Evidence/Learned/Remaining. Known gaps = every item from spec §Current state marked not met, each with impact and trigger (slice letter).
- [ ] **Step 7: replit.md** → keep title, one-paragraph summary, "Run and operate" (fix: remove `cd frontend…`; say migrations run at backend startup), Required Replit Secrets, and a pointer: "Project documentation lives in README.md; agent instructions in CLAUDE.md."
- [ ] **Step 8: Run** `.venv/bin/python -m pytest tests/test_docs.py -q` → 5 passed. Fix any broken link it reports.
- [ ] **Step 9: Commit** `docs: add playbook baseline (README, CLAUDE, CHANGELOG, DEVLOG, TESTING)`

### Task A4: Open PR, merge

- [ ] `git push -u origin feat/monitoring-gap-closure`; `gh pr create` with the playbook PR sections (Outcome, Scope, Change class docs, Verification block listing commands run and "unavailable: none"), body ends with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- [ ] Wait for both workflows; merge with `gh pr merge --squash --delete-branch`; `git checkout main && git pull`.

---

## Slice B — Monitoring correctness

Branch from `main`: `fix/count-zero-and-label-backfill`.

### Task B1: Pure realized-metric join

**Files:**
- Create: `backend/app/adapters/ml_monitor/realized.py`
- Test: `backend/tests/test_realized_join.py`

**Interfaces:**
- Produces: `join_realized(inferences, labels, *, id_field, label_field, proba_field) -> RealizedResult` with fields `value: float|None`, `coverage: float|None`, `status: str`, `matched: list[int]`, `reason: str|None`. Statuses: `realized`, `no_labels`, `insufficient_coverage`, `single_class`.

- [ ] **Step 1: Failing test**

```python
from app.adapters.ml_monitor.realized import join_realized

def _inf(n):
    return [{"inference_id": f"i{i}", "churn_proba": i / n} for i in range(n)]

def test_realized_when_coverage_and_both_classes():
    inf = _inf(10)
    labels = [{"inference_id": f"i{i}", "label": int(i >= 5)} for i in range(10)]
    r = join_realized(inf, labels, id_field="inference_id", label_field="label", proba_field="churn_proba")
    assert r.status == "realized" and r.value == 1.0 and r.coverage == 1.0

def test_no_labels():
    r = join_realized(_inf(4), [], id_field="inference_id", label_field="label", proba_field="churn_proba")
    assert r.status == "no_labels" and r.value is None and r.coverage == 0.0

def test_insufficient_coverage():
    labels = [{"inference_id": "i0", "label": 1}, {"inference_id": "i1", "label": 0}]
    r = join_realized(_inf(10), labels, id_field="inference_id", label_field="label", proba_field="churn_proba")
    assert r.status == "insufficient_coverage" and r.coverage == 0.2

def test_single_class():
    labels = [{"inference_id": f"i{i}", "label": 1} for i in range(10)]
    r = join_realized(_inf(10), labels, id_field="inference_id", label_field="label", proba_field="churn_proba")
    assert r.status == "single_class" and r.value is None
```

- [ ] **Step 2: Run** → ImportError.
- [ ] **Step 3: Implement**

```python
"""Pure label→inference join used by the live tick and the label-lag backfill."""
from __future__ import annotations

from dataclasses import dataclass, field

from sklearn.metrics import roc_auc_score

MIN_COVERAGE = 0.5


@dataclass
class RealizedResult:
    value: float | None
    coverage: float | None
    status: str
    matched: list[int] = field(default_factory=list)
    reason: str | None = None


def join_realized(inferences: list[dict], labels: list[dict], *, id_field: str,
                  label_field: str, proba_field: str) -> RealizedResult:
    if not inferences:
        return RealizedResult(None, None, "no_labels", reason="empty window")
    if not labels:
        return RealizedResult(None, 0.0, "no_labels", reason="label lag")
    lab = {l[id_field]: int(l[label_field]) for l in labels}
    pairs = [(lab[r[id_field]], float(r[proba_field])) for r in inferences if r[id_field] in lab]
    coverage = len(pairs) / len(inferences)
    matched = [y for y, _ in pairs]
    if coverage < MIN_COVERAGE:
        return RealizedResult(None, coverage, "insufficient_coverage", matched,
                              f"label coverage {coverage:.0%} below 50%")
    if len(set(matched)) < 2:
        return RealizedResult(None, coverage, "single_class", matched,
                              "single class in matched labels")
    return RealizedResult(float(roc_auc_score(matched, [p for _, p in pairs])),
                          coverage, "realized", matched)
```

- [ ] **Step 4: Run** → 4 passed. **Step 5: Commit** `feat: pure realized-metric join`

### Task B2: `count=0` is an observation

**Files:**
- Modify: `backend/app/adapters/ml_monitor/live_http.py:142-146` (empty branch) and `:198-222` (use `join_realized`)
- Modify: `backend/app/adapters/ml_monitor/nba_live_http.py` (`_on_empty_window` hook)
- Modify: `backend/app/scenario/live_runner.py` (`tick()` in `LiveRunner` and `LiveNBARunner`: skip explain when `"empty_window" in ml_res.errors`)
- Test: `backend/tests/test_count_zero_window.py`

**Interfaces:**
- Produces: `LaneResult.errors["empty_window"] == "count=0"`, `records["realized_pending_reason"] == "empty window"`, `records["empty_window"] is True`; no `errors["telemetry"]`.

- [ ] **Step 1: Failing test** — fake producer via `monkeypatch.setattr(telemetry_http, "pull", fake_pull)` where `fake_pull(base, path, params=None, timeout=30)` returns `/telemetry/meta`-less envelopes: for `/telemetry/inferences` tick 0 → `{"records": [], "count": 0, "window_id": "w0", "content_sha256": canonical_records_sha256([])}`; tick 1 → 600 synthetic records `{"inference_id": f"i{i}", "features": {"a": float(i%7), "b": float(i%3)}, "churn_proba": (i%10)/10}`; `/telemetry/reference` → 600 rows `{"features": {...}, "label": i%2}`; `/telemetry/labels` → `{"records": [], "available_at_tick": 4}`. Patch `telemetry_http.pull_model` to raise (artifact optional) and `telemetry_http.pull_meta` to return `{"latest_tick": 3, "label_lag_ticks": 3}`. Patch `live_runner.pull_meta` likewise (it is imported by name). Use the `isolated_db` fixture pattern from `test_live_release_hardening.py`. Also patch `engines.evidently_drift` to return `(0.0, [], "<html/>")` to keep the test fast.

```python
def test_empty_window_is_observed_and_cursor_advances(isolated_db, fake_producer):
    runner = live_runner_module.LiveRunner(churn_url="https://producer.test")
    out = runner.tick()
    assert out.get("cursor_held") is not True
    assert out["record_count"] == 0 and out["errors"].get("empty_window") == "count=0"
    assert "telemetry" not in out["errors"]
    assert db.get_live_source("AICT-L01")["next_tick"] == 1
    assert out["signals"]["realized_roc_auc"]["pending_reason"] == "empty window"

def test_empty_window_then_normal_window(isolated_db, fake_producer):
    runner = live_runner_module.LiveRunner(churn_url="https://producer.test")
    runner.tick()
    second = runner.tick()
    assert second["tick"] == 1 and second["record_count"] == 600
    assert "empty_window" not in second["errors"]
    assert second["signals"]["realized_roc_auc"]["pending_reason"] == "label lag"
```

- [ ] **Step 2: Run** → first test fails on `cursor_held`.
- [ ] **Step 3: Implement.** In `live_http.py` empty branch:

```python
            if not inf:
                for k in self._signal_keys:
                    res.signals[k] = None
                res.errors["empty_window"] = "count=0"
                res.records = {
                    "drifted_features": [], "reference_auc": self._reference_auc,
                    "model_version": self._version, "empty_window": True,
                    "realized_pending_reason": "empty window",
                    "realized_label_coverage": None, "realized_window_tick": t,
                }
                self._on_empty_window(res, t)
                return res
```

  Add `def _on_empty_window(self, res, t): """Subclass hook (NBA marks its extra signals pending)."""` to the base; in NBA: set `res.errors["acceptance_pending"] = "empty window"` and `res.errors["recommendation_drift_pending"] = "empty window"`. Replace the inline realized block (`:198-222`) with:

```python
        realized, pending, coverage, matched = None, None, None, []
        try:
            env = pull(self.base_url, self.labels_path, {"tick": t})
            if env.get("available_at_tick") is not None and not env.get("records"):
                pending = "label lag"
            else:
                joined = join_realized(inf, env.get("records", []), id_field=self.id_field,
                                       label_field=self._label_field, proba_field=self.proba_field)
                realized, coverage, matched = joined.value, joined.coverage, joined.matched
                pending = None if joined.status == "realized" else (joined.reason or joined.status)
        except Exception as e:  # noqa: BLE001
            pending = f"labels pull failed: {type(e).__name__}: {e}"
```

  In both runners' `tick()`: `ex_res = ExplainResult() if ({"insufficient_sample", "empty_window"} & ml_res.errors.keys()) else self.explain.explain(...)`.
- [ ] **Step 4: Run** full fast suite → all pass. **Step 5: Commit** `fix: treat count=0 windows as observations and advance the cursor`

### Task B3: `live_realized_metrics` table and accessors

**Files:**
- Modify: `backend/app/db.py` (table after `live_signal_history`; migration 4; functions)
- Test: `backend/tests/test_realized_store.py`

**Interfaces:**
- Produces:
  - `db.put_realized_metric(source_id: str, tick: int, metric_key: str, *, value: float|None, coverage: float|None, status: str, reason: str|None = None) -> None` (upsert on `(source_id, tick, metric_key)`; a row with status `realized` or `evicted` is never overwritten).
  - `db.get_realized_metric(source_id, tick, metric_key) -> dict|None`
  - `db.latest_realized(source_id, metric_key) -> dict|None` (highest tick with status `realized`)
  - `db.realized_history(source_id, metric_key, limit) -> list[{"tick","value","health"}]` (realized rows only, ascending tick; `health` computed via `engines.health.evaluate`)
  - `db.ticks_needing_realization(source_id, metric_key, low: int, high: int) -> list[int]` (persisted observation ticks in `[low, high)` whose row is absent or non-final)
- FINAL statuses: `{"realized", "evicted"}`.

- [ ] **Step 1: Failing test**

```python
def test_upsert_and_finality(isolated_live_db):
    db.put_realized_metric("AICT-L01", 3, "realized_roc_auc", value=None, coverage=0.1, status="insufficient_coverage")
    db.put_realized_metric("AICT-L01", 3, "realized_roc_auc", value=0.81, coverage=0.9, status="realized")
    db.put_realized_metric("AICT-L01", 3, "realized_roc_auc", value=None, coverage=None, status="evicted")
    row = db.get_realized_metric("AICT-L01", 3, "realized_roc_auc")
    assert row["status"] == "realized" and row["value"] == 0.81
    assert db.latest_realized("AICT-L01", "realized_roc_auc")["tick"] == 3
    assert db.realized_history("AICT-L01", "realized_roc_auc", 10) == [{"tick": 3, "value": 0.81, "health": "Green"}]

def test_ticks_needing_realization(isolated_live_db):
    for t in range(5):
        db.put_live_observation("AICT-L01", {**_payload(), "tick": t, "observed_tick": t, "window_id": f"w{t}"},
                                source_tick=t, next_tick=t + 1, backlog=0, state="at_tail")
    db.put_realized_metric("AICT-L01", 1, "realized_roc_auc", value=0.8, coverage=1.0, status="realized")
    db.put_realized_metric("AICT-L01", 2, "realized_roc_auc", value=None, coverage=0.0, status="no_labels")
    assert db.ticks_needing_realization("AICT-L01", "realized_roc_auc", 0, 4) == [0, 2, 3]
```

- [ ] **Step 2: Run** → AttributeError. **Step 3: Implement** table:

```python
live_realized_metrics = Table(
    "live_realized_metrics", metadata,
    Column("source_id", String, nullable=False),
    Column("tick", Integer, nullable=False),
    Column("metric_key", String, nullable=False),
    Column("value", Float),
    Column("coverage", Float),
    Column("status", String, nullable=False),
    Column("reason", Text),
    Column("computed_at", Float, nullable=False),
    UniqueConstraint("source_id", "tick", "metric_key", name="uq_live_realized_metric"),
)
```

  Migration `(4, "label-lag realized metrics", lambda cx: live_realized_metrics.create(bind=cx, checkfirst=True))`. Implement the five functions with `select/insert/update`; `realized_history` imports `from .engines import health` lazily.
- [ ] **Step 4: Run** → pass. **Step 5: Commit** `feat: persist label-lag realized metrics`

### Task B4: Label backfill

**Files:**
- Create: `backend/app/label_backfill.py`
- Modify: `backend/app/adapters/ml_monitor/live_http.py` (add `realize_tick`), `nba_live_http.py` (override adds `acceptance_rate`)
- Modify: `backend/app/scenario/live_runner.py` (`_commit_tick` writes the current tick's realized row; `tick()` calls backfill after commit **and** on the waiting path; `_ahead_of_app` keeps returning `meta`)
- Modify: `backend/app/adapters/telemetry_http.py` (`class WindowEvicted(RuntimeError)`; `pull` raises it on 404)
- Test: `backend/tests/test_label_backfill.py`

**Interfaces:**
- Produces: `LiveHttpMLAdapter.realize_tick(t: int, expected_sha256: str|None) -> dict[str, RealizedResult]`; raises `WindowEvicted` on 404, `TelemetryIntegrityError` on digest mismatch.
- `label_backfill.run(source_id: str, adapter, *, current_tick: int, lag: int, metric_keys: tuple[str, ...]) -> list[dict]` returns rows written. `lag_from_meta(meta: dict, *, kind: str) -> int` (`label_lag_ticks` for `ml`, `reward_lag_ticks` for `nba`, default 3).

- [ ] **Step 1: Failing tests** (fake producer as in B2; labels for tick 0 become available when `pull_meta` says `latest_tick >= 3`):

```python
def test_backfill_realizes_older_tick_when_labels_land(isolated_db, fake_producer):
    runner = live_runner_module.LiveRunner(churn_url="https://producer.test")
    fake_producer.latest = 2            # ticks 0,1 closed; labels for 0 not yet available
    runner.tick(); runner.tick()
    assert db.get_realized_metric("AICT-L01", 0, "realized_roc_auc")["status"] == "no_labels"
    fake_producer.latest = 4; fake_producer.release_labels(0)
    runner.tick()                        # observes tick 2, then backfills [t-L, t)
    row = db.get_realized_metric("AICT-L01", 0, "realized_roc_auc")
    assert row["status"] == "realized" and 0.0 <= row["value"] <= 1.0

def test_backfill_runs_while_waiting_at_tail(isolated_db, fake_producer):
    runner = live_runner_module.LiveRunner(churn_url="https://producer.test")
    fake_producer.latest = 2; runner.tick(); runner.tick()
    fake_producer.release_labels(0)
    out = runner.tick()                  # waiting: read_tick == latest
    assert out.get("waiting") is True
    assert db.get_realized_metric("AICT-L01", 0, "realized_roc_auc")["status"] == "realized"

def test_realized_row_is_final(isolated_db, fake_producer):
    runner = live_runner_module.LiveRunner(churn_url="https://producer.test")
    fake_producer.latest = 2; runner.tick(); runner.tick()
    fake_producer.release_labels(0); runner.tick()
    fake_producer.evict(0)
    runner.tick()
    assert db.get_realized_metric("AICT-L01", 0, "realized_roc_auc")["status"] == "realized"

def test_evicted_tick_never_holds_cursor(isolated_db, fake_producer):
    runner = live_runner_module.LiveRunner(churn_url="https://producer.test")
    fake_producer.latest = 2; runner.tick(); runner.tick()
    fake_producer.evict(0); fake_producer.latest = 4
    runner.tick()
    assert db.get_realized_metric("AICT-L01", 0, "realized_roc_auc")["status"] == "evicted"
    assert db.get_live_source("AICT-L01")["next_tick"] == 3

def test_observation_payload_unchanged_by_backfill(isolated_db, fake_producer):
    runner = live_runner_module.LiveRunner(churn_url="https://producer.test")
    fake_producer.latest = 2; runner.tick(); runner.tick()
    before = db.list_live_observations("AICT-L01")
    fake_producer.release_labels(0); fake_producer.latest = 4; runner.tick()
    after = {o["observation_id"]: o for o in db.list_live_observations("AICT-L01")}
    for o in before:
        assert after[o["observation_id"]]["payload"] == o["payload"]
        assert after[o["observation_id"]]["content_sha256"] == o["content_sha256"]
```

  The `fake_producer` fixture is a small class with `latest`, `released: set[int]`, `evicted: set[int]`, methods `release_labels(t)`, `evict(t)`, and a `pull` that raises `telemetry_http.WindowEvicted` for evicted ticks and returns `{"records": [], "available_at_tick": t + 3}` for unreleased labels. Put it in `backend/tests/conftest.py` so B2/B4/C tests share it.

- [ ] **Step 2: Run** → fails. **Step 3: Implement**

`telemetry_http.pull`:
```python
    r = httpx.get(...)
    if r.status_code == 404:
        raise WindowEvicted(f"{path} tick={params.get('tick') if params else '?'} not available (404)")
    r.raise_for_status()
```

`live_http.py`:
```python
    def realize_tick(self, t: int, expected_sha256: str | None) -> dict:
        env = pull(self.base_url, self.inferences_path, {"tick": t})
        meta = window_metadata(env, t)
        if expected_sha256 and meta.get("content_sha256") != expected_sha256:
            raise TelemetryIntegrityError(
                f"tick {t} digest changed: {expected_sha256} -> {meta.get('content_sha256')}")
        labels_env = pull(self.base_url, self.labels_path, {"tick": t})
        joined = join_realized(env["records"], labels_env.get("records", []), id_field=self.id_field,
                               label_field=self._label_field, proba_field=self.proba_field)
        if labels_env.get("available_at_tick") is not None and not labels_env.get("records"):
            joined.status, joined.reason = "pending", "label lag"
        return self._realized_signals(joined)

    def _realized_signals(self, joined) -> dict:
        return {"realized_roc_auc": joined}
```
NBA override of `_realized_signals` adds `"acceptance_rate": RealizedResult(float(np.mean(joined.matched)) if joined.coverage and joined.coverage >= 0.5 and joined.matched else None, joined.coverage, "realized" if (joined.coverage or 0) >= 0.5 and joined.matched else joined.status, joined.matched, joined.reason)`.

`label_backfill.py`:
```python
"""Label-lag backfill: realize metrics for recently observed ticks once labels land."""
from __future__ import annotations

import logging

from . import db
from .adapters.telemetry_http import TelemetryIntegrityError, WindowEvicted

log = logging.getLogger(__name__)
DEFAULT_LAG = 3


def lag_from_meta(meta: dict, *, kind: str) -> int:
    key = "reward_lag_ticks" if kind == "nba" else "label_lag_ticks"
    try:
        return max(0, int(meta.get(key)))
    except (TypeError, ValueError):
        return DEFAULT_LAG


def run(source_id: str, adapter, *, current_tick: int, lag: int,
        metric_keys: tuple[str, ...]) -> list[dict]:
    low, high = max(0, current_tick - lag - 1), current_tick
    pending = sorted({t for key in metric_keys
                      for t in db.ticks_needing_realization(source_id, key, low, high)})
    written: list[dict] = []
    for t in pending:
        obs = db.get_live_observation_by_tick(source_id, t)
        expected = obs["content_sha256"] if obs else None
        try:
            results = adapter.realize_tick(t, expected)
        except WindowEvicted as exc:
            for key in metric_keys:
                db.put_realized_metric(source_id, t, key, value=None, coverage=None,
                                       status="evicted", reason=str(exc))
                written.append({"tick": t, "metric_key": key, "status": "evicted"})
            continue
        except TelemetryIntegrityError as exc:
            for key in metric_keys:
                db.put_realized_metric(source_id, t, key, value=None, coverage=None,
                                       status="error", reason=str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 — transient; retry next cycle
            log.warning("backfill %s tick %s failed: %s", source_id, t, exc)
            break
        for key, r in results.items():
            if key not in metric_keys:
                continue
            db.put_realized_metric(source_id, t, key, value=r.value, coverage=r.coverage,
                                   status=r.status, reason=r.reason)
            written.append({"tick": t, "metric_key": key, "status": r.status})
    return written
```
Add `db.get_live_observation_by_tick(source_id, tick) -> dict|None`.

`live_runner.py`: in `_commit_tick` after `runner._read_tick = next_tick`, write the current tick's rows from `payload["signals"]` for keys in `runner.realized_keys` (`("realized_roc_auc",)` for churn, `("realized_roc_auc", "acceptance_rate")` for NBA): status `realized` if value not None else `no_labels`/`pending` from the pending reason. Add to both ML runners:
```python
    realized_keys = ("realized_roc_auc",)        # NBA: + "acceptance_rate"
    lane_kind = "ml"                              # NBA: "nba"

    def _backfill(self, meta: dict, current_tick: int) -> None:
        try:
            label_backfill.run(self.use_case_id, self.ml, current_tick=current_tick,
                               lag=label_backfill.lag_from_meta(meta, kind=self.lane_kind),
                               metric_keys=self.realized_keys)
        except Exception as exc:  # noqa: BLE001 — never fail the tick on backfill
            db.mark_live_warning(self.use_case_id, f"backfill: {exc}")
```
Call `self._backfill(meta, t)` in `tick()` on both the waiting path (before `return waiting`) and after `_commit_tick`. `db.mark_live_warning` is a no-op logger wrapper for now (`log.warning`), so a backfill failure never sets the cursor error state.

- [ ] **Step 4: Run** full fast suite → pass. **Step 5: Commit** `feat: backfill realized metrics once lagged labels arrive`

### Task B5: Grade with the latest realized value

**Files:**
- Modify: `backend/app/scenario/live_runner.py` (each `_grade` adds `"rollup_meta": {"hand_set_lanes": ..., "excluded_lanes": sorted(...), "excluded_keys": sorted(...)}`)
- Create: `backend/app/realized_view.py` with `apply_realized(uc: str, state: dict) -> dict`
- Modify: `backend/app/api/live_portfolio.py` (`detail()` and `portfolio_rows()` call `apply_realized`; realized signals' `history` from `db.realized_history`)
- Test: `backend/tests/test_realized_view.py`

**Interfaces:**
- Produces: `apply_realized` returns a **copy** of state where for each realized key with a `latest_realized` row: `signals[key].value/health` set, `signals[key].pending_reason` removed, `signals[key].as_of_tick` set; for NBA `acceptance_rate` realized also removes `"Feedback & action loop"` from hand-set/excluded lanes; `lanes`/`overall` recomputed via `health.rollup`. Without rows it returns the state unchanged.

- [ ] **Step 1: Failing test** (build a state dict by hand with `rollup_meta`; insert a realized row; assert `overall` flips from Green to Red when realized AUC is 0.6 and `as_of_tick == 2`).
- [ ] **Step 2: Implement** per the interface. **Step 3: Run** → pass.
- [ ] **Step 4: Route test** in `test_strict_live_mode.py` style: `/api/live/use-case/AICT-L01` shows `as_of_tick` on the realized signal.
- [ ] **Step 5: Commit** `feat: grade live use cases with the latest realized metric`
- [ ] **Step 6: Docs** — CHANGELOG `### Fixed` two lines; DEVLOG work-log entry; README Known limitations updated; `docs/STRICT-LIVE.md` gains a "Label-lag backfill" paragraph. Commit `docs: record label-lag backfill and count=0 fix`.
- [ ] **Step 7: PR** `fix: count=0 windows and label-lag realized metrics`; merge after CI.

---

## Slice C — Alerting

Branch from `main`: `feat/live-alerting`.

### Task C1: Pure transition engine

**Files:**
- Create: `backend/app/engines/alerts.py`
- Test: `backend/tests/test_alert_engine.py`

**Interfaces:**
- Produces: `transitions(prev: dict[str, str], curr: dict[str, str], open_keys: set[tuple[str, str]]) -> list[AlertEvent]` where keys are lane names plus `"overall"`; `AlertEvent(kind: "open"|"resolve", lane: str, from_health: str, to_health: str)`; `open_keys` holds `(lane, to_health)` of alerts currently open.

Rules: open on `curr == "Red" and prev != "Red"` and on `prev == "Green" and curr == "Amber"`; skip open if `(lane, curr) in open_keys`; resolve every open key for `lane` when `curr == "Green"`; `Unknown` in either position never opens and never resolves.

- [ ] **Step 1: Failing tests** — `test_green_to_amber_opens`, `test_amber_to_red_opens_red`, `test_red_to_green_resolves`, `test_unknown_never_alerts`, `test_dedupe_while_open`, `test_unknown_gap_does_not_reopen` (Red→Unknown→Red with `("Quality","Red")` open yields no events).
- [ ] **Step 2: Implement** (≈30 lines, dataclass + loop over `set(prev)|set(curr)`). **Step 3: Commit** `feat: alert transition engine`

### Task C2: Persistence and evaluation

**Files:**
- Modify: `backend/app/db.py` — tables `live_alerts`, `live_health_snapshots`; migration 5; functions `get_health_snapshot(source_id)`, `put_health_snapshot(source_id, tick, overall, lanes)`, `open_alert(source_id, lane, from_health, to_health, tick, observation_id) -> alert_id`, `resolve_alerts(source_id, lane, tick) -> list[alert_id]`, `list_alerts(source_id=None, open_only=False, limit=50)`, `open_alert_keys(source_id) -> set[(lane, to_health)]`, `alerts_pending_delivery(limit=100)`, `mark_alert_delivery(alert_id, phase: "open"|"resolve", ok: bool, error: str|None)`.
- Create: `backend/app/alerting.py` with `evaluate(uc: str) -> list[AlertEvent]` (loads `live_runner(uc).state()`, applies `apply_realized`, builds `curr = {**lanes, "overall": overall}`, `prev` from snapshot (empty dict first time → only Red opens), writes alerts and snapshot).
- Modify: `backend/app/live_poller.py` — after `live_runner(uc).tick()` call `alerting.evaluate(uc)` inside the same try; after the loop call `alert_delivery.deliver_pending()`.
- Test: `backend/tests/test_alerting.py`

`live_alerts` columns: `alert_id String PK`, `source_id String idx`, `lane String`, `from_health`, `to_health`, `tick Integer`, `observation_id String`, `opened_at Float`, `resolved_at Float`, `resolved_tick Integer`, `open_delivery_status String default "pending"`, `open_delivery_error Text`, `open_delivered_at Float`, `resolve_delivery_status String default "n/a"`, `resolve_delivery_error Text`, `resolve_delivered_at Float`. `live_health_snapshots`: `source_id PK`, `tick`, `overall`, `lanes_json Text`, `updated_at`.

- [ ] **Step 1: Failing tests** — seed observations via `db.put_live_observation` with lanes Green then Red; `alerting.evaluate("AICT-L01")` twice; assert one open alert with `to_health == "Red"`, second call opens nothing; then a Green observation resolves it (`resolved_at` set).
- [ ] **Step 2: Implement.** **Step 3: Commit** `feat: persist health transitions as live alerts`

### Task C3: Webhook delivery

**Files:**
- Create: `backend/app/alert_delivery.py`
- Modify: `backend/app/config.py` — `LIVE_ALERT_WEBHOOK_URL = os.getenv("LIVE_ALERT_WEBHOOK_URL", "").strip()`, `LIVE_DASHBOARD_URL = os.getenv("LIVE_DASHBOARD_URL", "").strip()`
- Test: `backend/tests/test_alert_delivery.py`

**Interfaces:**
- Produces: `build_payload(alert: dict, phase: str) -> dict` (`{"text": "...", "alert": {...}}` — `text` like `"[RED] AICT-L01 Quality: Amber → Red at tick 12"`), `deliver_pending(post=None) -> int` (count delivered; `post(url, json=..., timeout=10)` injectable; default `httpx.post`). Logs `urlsplit(url).hostname` only.

- [ ] **Step 1: Failing tests** — payload shape (no `features`, no `question`, no URL in `text`); `test_delivery_retries_then_marks_once`: fake `post` returns 500, 500, 200 across three `deliver_pending()` calls → alert `open_delivery_status == "delivered"` and the fake was called exactly three times; `test_no_webhook_configured_marks_skipped`.
- [ ] **Step 2: Implement.** **Step 3: Commit** `feat: webhook delivery for live alerts`

### Task C4: API, bundle guard, UI

**Files:**
- Modify: `backend/app/api/live_routes.py` — `GET /live/alerts` (`uc`, `open`, `limit`) returning `{"contract_version": "1.1", "rows": [...]}` with only the columns above; `backend/app/api/live_portfolio.py` — `detail()` replaces `"actions": []` with `"alerts": db.list_alerts(uc, open_only=True, limit=20)`, `portfolio_summary()` adds `"open_alerts": {"Red": n, "Amber": m}`.
- Modify: `scripts/check-strict-live-bundle.mjs` — add `["LIVE_ALERT_WEBHOOK_URL", "alert webhook secret name"]`.
- Create: `artifacts/control-tower/src/components/alerts.tsx` — `AlertsStrip` (home) and `AlertsPanel({ uc })` (use-case page), both via `useLiveResource`.
- Modify: `artifacts/control-tower/src/pages/home.tsx` (strip after the "Portfolio — observed health" section), `use-case.tsx` (panel after "Observation evidence"), `src/lib/live.tsx` (`LiveAlert` type).
- Test: `backend/tests/test_alert_routes.py` (TestClient in live mode: route lists, filters by `uc`, rejects POST with 404).

- [ ] Steps: failing route test → implement → `pnpm run typecheck && pnpm run build:live && pnpm run check:strict-live` → commit `feat: alerts API and dashboard panels`.

### Task C5: ADR and docs

- Create `docs/adr/README.md` (index table) and `docs/adr/0001-alert-ownership.md` per the playbook ADR template: Decision = RAI team owns triage via webhook channel; producers are not paged; review trigger = contract §18 Q1 resolution.
- CHANGELOG `### Added`; DEVLOG entry; README Known limitations + env vars; `replit.md` secrets list adds `LIVE_ALERT_WEBHOOK_URL` (optional) and `LIVE_DASHBOARD_URL` (optional); `docs/STRICT-LIVE.md` alerting paragraph.
- Commit `docs: alert ownership ADR and alerting docs`; PR `feat: live alerting (state machine, webhook, UI)`; merge after CI.

---

## Slice D — Operational resilience

Branch from `main`: `feat/live-resilience`.

### Task D1: HTTP retry

**Files:**
- Create: `backend/app/http_retry.py`
- Modify: `backend/app/adapters/telemetry_http.py` (`pull`, `pull_meta`, `pull_model`, `push_scores`, `acknowledge_observation` use `request_with_retry`), `backend/app/alert_delivery.py` (default `post`).
- Test: `backend/tests/test_http_retry.py`

**Interfaces:**
- Produces: `request_with_retry(method: str, url: str, *, attempts: int = 3, base_delay: float = 0.5, max_delay: float = 4.0, sleep=time.sleep, rng=random.random, **kwargs) -> httpx.Response`. Retries on `httpx.TransportError` and status in `{429, 502, 503, 504}`; honours integer `Retry-After` (capped at `max_delay`); never retries other 4xx; raises the last error.

- [ ] Tests with `httpx.MockTransport` through `httpx.Client(transport=...)` injected via `client=` kwarg: 429 with `Retry-After: 1` → one sleep of 1.0; 503, 503, 200 → success after two sleeps (0.5±jitter, 1.0±jitter); 404 → raised immediately, one call.
- [ ] Commit `feat: retry producer and webhook requests with backoff`

### Task D2: Structured logging and cycle metrics

**Files:**
- Create: `backend/app/logging_setup.py` (`configure(format: str)`: JSON lines when `LOG_FORMAT=json`, else plain; idempotent)
- Modify: `backend/app/config.py` (`LOG_FORMAT`), `backend/app/main.py` (call `configure`; replace the four `print`s with `log.info`), `backend/app/live_poller.py` (`self.last_cycle: dict|None`, per-source timing, `log.info("cycle", extra=...)`), `/api/readiness` adds `"last_cycle": poller().last_cycle`.
- Test: `backend/tests/test_poller_metrics.py` (`run_once` with a fake runner → `last_cycle["sources"]["AICT-L01"]["outcome"] == "ok"` and `duration_ms >= 0`).
- [ ] Commit `feat: structured logging and poll-cycle metrics`

### Task D3: Operator skip and reset-ack

**Files:**
- Modify: `backend/app/db.py` — `skip_live_tick(source_id, reason) -> dict` (requires `state == "error"` else raises `db.NothingToSkip`; writes stub observation via `put_live_observation` with payload `{"use_case_id", "tick": t, "observed_tick": t, "mode": "live", "skipped": True, "skip_reason": reason, "window_id": f"{source_id}:skipped-t{t}", "record_count": 0, "signals": {}, "lanes": {lane: "Unknown" …}, "overall": "Unknown", "errors": {"skipped": reason}}`, `next_tick=t+1`); `abandon_live_acks(source_id) -> int`.
- Modify: `backend/app/api/live_routes.py` — `POST /live/sources/{uc}/skip` (JSON `{"reason": str}`), `POST /live/sources/{uc}/reset-ack`; both require the worker token (reuse `_worker_token_matches`); 409 on `NothingToSkip`; 404 unknown uc.
- Modify: `backend/app/main.py` middleware: allow `POST` when `path == "/api/live/poll"` or `path.startswith("/api/live/sources/")`.
- Modify: `backend/app/scenario/live_runner.py` — `reset_live_runner(uc)` after skip so the in-memory `_read_tick` reloads.
- Test: `backend/tests/test_operator_routes.py` — `test_skip_requires_held_cursor` (409), `test_skip_advances_and_audits`, `test_skip_requires_worker_token` (401), `test_reset_ack`.
- [ ] Commit `feat: operator skip and reset-ack endpoints`

### Task D4: Persist the NBA baseline offer mix

**Files:**
- Modify: `backend/app/db.py` — table `live_baselines(source_id, kind, model_version, payload Text, captured_at)` unique `(source_id, kind, model_version)`; migration 6; `put_baseline`, `get_baseline`.
- Modify: `backend/app/adapters/ml_monitor/nba_live_http.py` — `__init__(..., source_id="AICT-L03")`; on capture → `db.put_baseline`; `_on_rebaseline` and `__init__` → `db.get_baseline(source_id, "offer_mix", self._version)`.
- Test: `backend/tests/test_nba_baseline.py` — capture in one adapter instance, construct a second, `recommendation_drift` computed on its first window without `recommendation_drift_pending`.
- [ ] Commit `feat: persist NBA baseline offer mix across restarts`
- [ ] Docs: CHANGELOG, DEVLOG, README (env `LOG_FORMAT`, operator endpoints), `docs/STRICT-LIVE.md` runbook section "Unsticking a source". PR `feat: live resilience (retry, logging, operator skip, baseline persistence)`; merge.

---

## Slice E — Hygiene and contract strictness

Branch from `main`: `chore/hygiene-and-contract-strictness`.

### Task E1: Delete dead scaffold

- `git rm -r .migration-backup artifacts/mockup-sandbox lib backend/fly.toml backend/Dockerfile scripts/src/hello.ts artifacts/api-server/src artifacts/api-server/build.mjs artifacts/api-server/tsconfig.json`
- `artifacts/api-server/package.json` → `{"name": "@workspace/api-server", "version": "0.0.0", "private": true}` (keeps the Replit wrapper directory valid).
- `scripts/package.json` → remove `hello` script; `scripts/tsconfig.json` include `["src"]` keeps working with an empty dir only if `src` exists — delete `scripts/tsconfig.json` and the `typecheck` script instead.
- `pnpm-workspace.yaml` packages → `artifacts/*`, `scripts`; remove the `@expo/ngrok-bin` overrides block and `drizzle-orm`/`@tanstack/react-query` catalog entries.
- Root `package.json`: remove `@replit/connectors-sdk` dependency; `typecheck` → `pnpm -r --filter "./artifacts/**" --if-present run typecheck`; drop `typecheck:libs`; root `tsconfig.json` references → remove `lib/*`.
- `artifacts/control-tower/package.json`: remove `@tanstack/react-query`, `@workspace/api-client-react`.
- `.github/workflows/strict-live-frontend.yml` paths: unchanged (lib was excluded already).
- `.gitignore`: drop the `/frontend/` block comment referencing `.migration-backup`.
- Run `pnpm install` (lockfile updates), `pnpm run typecheck && pnpm run build:live && pnpm run check:strict-live`, and `bash scripts/deploy-build.sh` dry logic check: `grep -n "lib/" scripts/*.sh` → none.
- Commit `chore: remove migration backup, TS api stub, lib scaffold, mockup sandbox, Fly config`

### Task E2: Contract strictness

**Files:**
- Modify: `backend/app/adapters/telemetry_http.py` — `SUPPORTED_CONTRACT_VERSIONS = {"1.0", "1.1"}`; `class ContractVersionError(RuntimeError)`; in `pull`, after `data = r.json()`: `v = data.get("contract_version"); if v is None or str(v) not in SUPPORTED_CONTRACT_VERSIONS: raise ContractVersionError(f"contract: unsupported contract_version {v!r} from {path}")`. (Lands in `errors["telemetry"]` and holds the cursor, as the existing degrade path does.)
- Modify: `backend/app/adapters/llm_eval/live_http.py:207,220,267` — `latencies = [float(t["latency_s"]) for t in traces if t.get("latency_s") is not None]`; `res.records`/metadata gets `"latency_missing": sum(1 for t in traces if t.get("latency_s") is None)`; store metadata uses `tr.get("latency_s")` without a default. Fix the docstring at line 14 to say `claude-haiku-4-5` (config default).
- Modify: `backend/app/config.py` `live_configuration_errors` — for each URL, `scheme = urlsplit(value).scheme.lower()`; `if scheme != "https": errors.append(f"{name} must use https in strict live mode")`.
- Tests: `backend/tests/test_contract_strictness.py` — version `"0.9"` rejected, missing rejected, `"1.0"` accepted (mock `httpx.get`); latency missing excluded and counted (call `_aggregate` path or the adapter with a fake judge); `http://` URL produces a configuration error, `https://` does not. Update the CI env? It already uses `https://producer.example/...` — fine.
- Commit `fix: validate contract_version, never default latency_s, require https producers`
- Docs: CHANGELOG (`### Changed`, `### Deprecated or removed`), DEVLOG, README repository guide (remove deleted paths), `docs/LIVE-DEMO.md` (remove 8081 clash note, mockup references). PR `chore: repository hygiene and contract strictness`; merge.

---

## Risks

| Risk | Likelihood | If it happens | Mitigation |
|---|---|---|---|
| Existing tests monkeypatch `telemetry_http.pull` with minimal envelopes and break on `contract_version` validation (E2) | Medium | Suite fails | Validation lives inside `pull`; tests that patch `pull` bypass it. Tests that patch `httpx.get` must include `contract_version`. |
| Backfill re-pulls old inferences on every cycle for ticks that never get labels | Medium | Producer load | Window is bounded to `lag+1` ticks and rows become final after `evicted`; `no_labels` rows for ticks older than `current - lag - 1` fall outside the window. |
| Replit deployment still references deleted paths | Low | Deploy fails | Grep `scripts/*.sh`, `.replit`, `artifact.toml` for `lib/`, `api-server/src`, `mockup` before merging E. |
| `pnpm install` after E changes the lockfile in ways CI's `--frozen-lockfile` rejects | Medium | Frontend CI red | Commit the regenerated `pnpm-lock.yaml` in the same PR. |
| Alert evaluation on a `stale` source flips lanes to Unknown and later back | Medium | Flapping | Engine ignores Unknown both ways; `test_unknown_gap_does_not_reopen` pins it. |

## Proof

```bash
cd backend && .venv/bin/python -m pytest -q -m "not slow"          # per slice: all green, count grows
cd backend && .venv/bin/python -m pytest -q tests/test_docs.py      # slice A
pnpm run typecheck && pnpm run build:live && pnpm run check:strict-live   # slices C, E
gh pr checks <n> --watch                                            # both workflows green before merge
```

## Out of scope

Per-use-case thresholds; LIME in production; §14 sampling policy; Alembic; Prometheus metrics; Slack SDK; paging/escalation; push ingest; skops/ONNX; retention pruning. All remain in `DEVLOG.md` Known gaps.

## Deviations

- A1: the machine-local path check matches an actual home-directory path (a `Users`
  folder followed by a user name) or a `file://` URL with a path, instead of the bare markers. The bare
  check tripped on this plan's own test listing and on the spec's description of the rule.
  `.pytest_cache` was added to the skipped directories (gitignored pytest cache README).
- A1/A3: `backend/tests/test_docs.py` is committed with the A3 docs commit rather than
  before A2, so that every commit on the branch keeps the fast suite green.
- A3: the plan's own Step 2 text linked `CLAUDE.md` relative to `changes/…/`, which the
  link check flagged; the plan link now points to `../../CLAUDE.md`.
- A3: `README.md` and `TESTING.md` record that `pnpm run build:live` and
  `pnpm run check:strict-live` cannot run on macOS (the lockfile's platform overrides
  drop `@rollup/rollup-darwin-arm64`); the strict-live frontend workflow is the proof.
- B2: the `fake_producer` fixture (in `backend/tests/conftest.py`) makes empty ticks
  opt-in (`fake_producer.empty.add(0)`) instead of tick 0 always being `count=0`, because
  the B4 tests need tick 0 to carry records; it also scripts partial label coverage
  (`release_labels(t, coverage=0.4)`) and NBA-shaped windows. `empty_window: True` is
  lifted into the graded payload (and the detail's pass-through keys) so the stored
  observation says so, not only the adapter's `records`.
- B3: the new tests use the shared `isolated_db` fixture rather than
  `isolated_live_db`; `test_live_release_hardening.py` now pins the migration list as
  `[1, 2, 3, 4]` because migration 4 exists. `clear_live_state` also clears
  `live_realized_metrics`.
- B4: the runners share one module-level `_backfill_labels(runner, meta, t)` instead of
  a `_backfill` method on each class, and the current tick's realized row is written by
  `label_backfill.record_current_tick` from `_commit_tick`. `realize_tick` treats an
  undersized (<500) or empty window as `no_labels` rather than realizing a metric the
  live tick would have refused. Backfill is skipped when `/telemetry/meta` itself failed
  (no `meta`), since the producer is unreachable.
- B4 (spec §B.3): realized values are not appended to `live_signal_history` — its unique
  key `(observation_id, signal_key)` is already taken by the pending row stored with the
  observation. Realized sparklines are served from `live_realized_metrics`
  (`db.realized_history`), as the plan's B5 says.
- B5: `apply_realized` also runs in `portfolio_summary()` (spec B.4 names the portfolio
  rollup); detail signals gain `as_of_tick` and `coverage`, and the payload gains
  `realized_as_of_tick` / `acceptance_as_of_tick`. For observations stored before
  `rollup_meta` existed the view reconstructs the hand-set lanes from `lane_reasons`.
- B4 (review fix, spec §B.2 "done" rule): the plan's `realize_tick(t, expected_sha256)`
  gained keyword `current_tick` and `due_tick` (`t + lag`) so an empty labels window with
  `available_at_tick <= current_tick` is final `no_labels`, not `pending`. Finality is a
  new `final` boolean column on `live_realized_metrics` (migration 5, back-filled from
  status) because `no_labels` also names the non-final "empty window" / "insufficient
  sample" outcomes; `REALIZED_FINAL_STATUSES` stays `{realized, evicted}` and
  `ticks_needing_realization` filters on `final`. (Corrected below: finality is judged
  against the producer's source tick `latest_tick - 1`, passed as `source_tick`; the
  monitor's `current_tick` only bounds the backfill window.) A 404 on the
  labels window alone (inferences still served) is `pending` and retried until the due
  tick has passed, then final `no_labels`; only the inference re-pull marks `evicted`.
  `label_backfill.run` also sweeps `pending` rows below the window (left by a producer
  outage) once more. The live tick's "labels pull failed" reason maps to `pending`
  rather than `error`, since it is transient and the backfill retries it.
- B4 (review fix, second pass): the earlier bullet's claim that using the monitor's tick
  "is never earlier than the spec's current source tick" was wrong while waiting at the
  tail, where `read_tick == latest_tick` (the producer's still-open window) and the
  source tick is `latest_tick - 1`. `_backfill_labels` now derives
  `source_tick = latest_tick - 1` from `/telemetry/meta` (falling back to the monitor's
  tick) and `label_backfill.run` forwards it to `realize_tick(..., source_tick=,
  due_tick=)`, whose `available_at_tick <= source_tick` and `due_tick <= source_tick`
  comparisons decide finality; `current_tick` only bounds the window. Tests
  `test_overdue_tick_without_labels_is_final_no_labels`,
  `test_pending_ticks_that_left_the_window_are_swept` and
  `test_labels_404_after_due_tick_is_final_no_labels` were re-timed and now also assert
  that a tick due at `latest_tick` stays `pending` while the monitor waits there.
- B4 (review, minor): `realize_tick` returns `final=True` for `count=0` and undersized
  inference windows (nothing can ever realize them, so they are not re-pulled for L+1
  cycles), and an empty labels window that omits `available_at_tick` follows the 404
  rule — `pending` ("label lag") until the monitor's `t + L` is at or before the source
  tick, then final `no_labels` — instead of staying a non-final `no_labels` forever. The
  `fake_producer` fixture gained `omit_available_at`.
- Spec §B.2/§B.3 amended to match: the backfill window is `[t - L - 1, t)`, realized
  values live only in `live_realized_metrics` (sparklines from `db.realized_history`),
  and the flagged-concern row says so. `rollup_meta` stays in the detail payload's
  pass-through keys (lane names and signal keys only; no raw data).
- C1: the plan's rule "`Unknown` in either position never opens" was narrowed to the
  spec's `* → Red`: a previous Unknown (or no snapshot) followed by Red opens an alert,
  because an unmeasured lane that now reads Red is exactly the alert worth raising; a
  current Unknown still never opens or resolves, and the open-key dedupe still makes
  Red → Unknown → Red a single alert (`test_unknown_gap_does_not_reopen`). A first-ever
  Amber (no snapshot) does not open. Resolve events carry the previous health as
  `from_health` and `"Green"` as `to_health`.
- C2: the alerts tables are migration 6 (slice B already used 4 and 5); the two tests
  that pin the migration list (`test_live_release_hardening.py`,
  `test_realized_store.py`) now read `[1, 2, 3, 4, 5, 6]`. `clear_live_state` also
  clears `live_alerts` and `live_health_snapshots`. A `mark_alerts_delivery_skipped`
  helper was added so a missing webhook marks pending phases `skipped` (not retried
  forever). `alerting.evaluate` runs inside the per-source `try` after `tick()` and the
  poller calls `alert_delivery.deliver_pending()` after the source loop under its own
  `try`, so a webhook outage can never fail a cycle or hold a source.
- C3: `build_payload` also emits Slack `blocks` (section text plus a context link when
  `LIVE_DASHBOARD_URL` is set); the `alert` object carries `use_case_id` (the
  `source_id`), `phase`, `observation_id`, `opened_at`, `resolved_at`, `resolved_tick`
  and `dashboard_url` in addition to lane/health/tick. The resolve text reads
  `[RESOLVED] <uc> <lane>: <to_health> → Green at tick <resolved_tick>`.
- C4: `GET /live/alerts` is also registered on the dev router (`routes.py`) so the Vite
  dashboard works in demo mode; the strict-router test builds `live_routes.router` plus
  `main.strict_live_route_isolation` into its own FastAPI app, because `app.main`
  mounts whichever router matched the mode at first import (demo, in a full run). The
  response carries `"redacted": true` like `/live/observations`. The use-case panel
  fetches `/api/live/alerts?uc=` itself (open plus the last 20 resolved) rather than
  reading only the detail payload's open `alerts`. `pnpm run build:live` and
  `pnpm run check:strict-live` were attempted on this macOS host and fail on the missing
  `@rollup/rollup-darwin-arm64` (the documented limitation); only `pnpm run typecheck`
  ran locally.
- D1: `request_with_retry` takes a `send(method, url, **kwargs)` callable (default
  `client.request` or `httpx.request`) and `telemetry_http` / `alert_delivery` pass one
  that calls their module-level `httpx.get` / `httpx.post`, so the existing
  `monkeypatch.setattr(telemetry_http.httpx, "post", …)` seams still intercept. When the
  retryable statuses are exhausted the LAST RESPONSE is returned (not raised) so callers
  keep their own status handling (`pull`'s 404 → `WindowEvicted`, `raise_for_status`,
  the webhook's `status_code >= 400`); only exhausted transport errors raise. `sleep`
  and `rng` default to `None` and resolve to `time.sleep` / `random.random` per call so
  tests can patch them through the module. A fake response without `status_code` (the
  hardening test's stub) is treated as non-retryable.
- D2: the per-cycle record is `last_cycle = {cycle_id, started_at, finished_at,
  duration_ms, outcome, backlog, sources: {uc: {tick, duration_ms, outcome, backlog,
  error}}}` with outcomes `ok` / `waiting` / `held` / `error` (and `lease_lost` for the
  cycle); readiness nests it under `poller.last_cycle` rather than at the top level.
  `main.py` also replaces the configuration-blocked `print` with `log.error`.
- D3: the skip route marks the skipped tick's realized rows final `no_labels` (via the
  new `live_runner.realized_keys_for(uc)`) so the backfill never re-pulls the poisoned
  window; `skipped` / `skip_reason` were added to the detail view's pass-through keys so
  the stub is auditable from the API; a blank reason is 422; the stub also records the
  held error as `skipped_error` and sets `backlog - 1` / `catching_up|at_tail` so the
  cursor row is consistent until the next contact. `abandon_live_acks` sets
  `ack_status = "abandoned"` (a new value, excluded from `list_live_acks_to_retry`).
- D4: `live_baselines` is migration 7, not 6 (6 was taken by the slice C alert tables).
- D4: `LiveHttpNBAAdapter.__init__` cannot read the baseline (the model version is
  unknown until `_ensure_baseline` runs), so the cold-start read happens lazily in
  `_on_rebaseline` and, once per version, in `_extend` before a capture
  (`_baseline_loaded_for` guards the lookup). Database failures on read or write log a
  warning and degrade to the in-memory mix; `clear_live_state` also clears
  `live_baselines`. The restart test changes the served mix between the two adapter
  instances so a re-capture cannot pass it.
- D3 (review fix): the skip stub is stored `ack_status = "skipped"` (new value, excluded
  from `list_live_acks_to_retry` and from `abandon_live_acks`) via a new
  `acknowledge: bool = True` knob on `put_live_observation`; with a producer configured
  the old `pending` stub would have POSTed a synthetic `window_id` the producer never
  served, failed, and been retried every cycle. `skip_live_tick(source_id, reason,
  realized_keys=())` now writes the stub, the cursor advance and the final `no_labels`
  realized rows in ONE transaction (`_put_live_observation(cx, …)` and
  `_put_realized_metric(cx, …)` cores take the caller's connection) instead of the route
  issuing separate commits, so a failed realized write leaves the cursor held and the
  skip retryable. Making that atomic on SQLite exposed the pysqlite SAVEPOINT caveat
  (`begin_nested` rows survived an outer rollback): `db.engine()` now installs the
  SQLAlchemy-documented `isolation_level=None` + explicit `BEGIN` hooks for `sqlite:`
  URLs (`_enable_sqlite_savepoints`); Postgres is unaffected.
- D4 (review, minor): `_store_baseline` is a no-op while `_version is None` (contract
  1.0 producer without `/model/artifact`), so an un-versioned capture stays in memory
  instead of persisting a row keyed `"None"`.
- D2/D3 (review, note): the strict-live middleware admits POST only to
  `/api/live/poll` and `re.fullmatch(r"/api/live/sources/[^/]+/(skip|reset-ack)")`, so
  other paths under `/api/live/sources/` get the middleware's own 404 body.
