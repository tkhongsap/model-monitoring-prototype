# Live Control Tower — demo runbook

**Classification: CPG Confidential — internal True Corporation use only.**

The dashboard shows **three real AI models being monitored live** — not a simulation:

| Use case | Lane | What it demonstrates |
|---|---|---|
| **AICT-L01 — Customer Churn Prediction** (`telco-churn`, :8083) | ML | Evidently drift, NannyML label-free AUC estimate, realized AUC (label lag), LIME/SHAP |
| **AICT-L02 — Support Chatbot** (`telco-support-chatbot`, :8082) | LLM | LLM-as-judge: groundedness / relevance / hallucination / PII / latency, traces pushed to Langfuse |
| **AICT-L03 — Next-Best-Action Recommender** (`telco-nba`, :8084) | ML+NBA | drift, estimated/realized accept-AUC, **acceptance rate**, **recommendation-mix drift**, offer-mix panel |

Each model implements the same small **telemetry contract** (`docs/MONITORING-CONTRACT.md`); the monitor
reaches OUT to their `/telemetry/*` endpoints, runs the shared health engine, and grades every lane
Green / Amber / Red. **Onboarding a 4th model = one seed row + one URL** (see "Plug in a new model" below).

The baked 130-row scenario exists only for the separate developer command. A strict-live
deployment does not register baked routes, does not build demo artifacts, and serves only
persisted producer observations. Telecom inputs are synthetic-safe and labelled by provenance;
they are not claimed to be customer production data.

---

## 1. One-time setup

```powershell
# Python side (monitor + the 3 model apps share the monitor venv):
#   the venv at backend/.venv already has fastapi, evidently, nannyml, lime, shap, rank-bm25, anthropic, langfuse
# Model apps also need the shared package on PYTHONPATH: …/ai-use-cases/shared

# Frontend (once):  from the monitor repo root
corepack pnpm install            # installs the control-tower React app
```

`.env` (monitor repo root, gitignored) should hold your keys — already configured this session:
`ANTHROPIC_API_KEY` (real judge), `LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST` (JP region), and the
`LIVE_CHURN_URL` / `LIVE_CHATBOT_URL` / `LIVE_NBA_URL` app URLs. Judge cost is bounded by
`LLM_JUDGE_MAX_TRACES` (default 20 sampled traces/tick) so a live tick stays ~10-15 s.

## 2. Start everything

Ports: model apps 8081-8084, monitor API 8000, dashboard 5000.

```powershell
$py  = "…/model-monitoring-prototype/backend/.venv/Scripts/python.exe"
$uc  = "…/ai-use-cases"
$be  = "…/model-monitoring-prototype/backend"
$ct  = "…/model-monitoring-prototype/artifacts/control-tower"

# (a) the 3 model apps + account API
$env:PYTHONPATH = "$uc\shared"
uvicorn app.main:app --app-dir "$uc\apps\telecom-account-api" --port 8081   # tool target for the chatbot
uvicorn app.main:app --app-dir "$uc\apps\chatbot"             --port 8082
uvicorn app.main:app --app-dir "$uc\apps\churn-predictor"     --port 8083
uvicorn app.main:app --app-dir "$uc\apps\nba-recommender"     --port 8084

# (b) the monitor backend (reads .env for keys + LIVE_*_URL)
uvicorn app.main:app --app-dir "$be" --port 8000

# (c) the dashboard (Vite dev server, proxies /api -> :8000)
$env:PORT = "5000"; $env:BASE_PATH = "/"
node "$ct\node_modules\vite\bin\vite.js" --port 5000
```

Open **http://localhost:5000**.

## 3. Make it move

The monitor observes **closed** telemetry windows, so the model apps need real traffic first. Two ways:

- **Developer traffic driver** (local-only; drives real inference over labelled synthetic-safe input):
  ```powershell
  python "$uc\tools\traffic.py" --churn http://127.0.0.1:8083 --chatbot http://127.0.0.1:8082 --nba http://127.0.0.1:8084
  ```
- **Production:** use the producer dashboard's authenticated, explicit **Run demo window** action.
  The control tower is read-only; its lease worker observes closed producer windows. There is
  no hidden background traffic generator and no browser-driven monitor cursor.

Watch the **Heatmap** and **At A Glance** pages turn Green → Amber → Red as drift ramps, then recover after a
retrain. The chatbot's judged traces appear in your Langfuse project in real time.

## 4. Deploy online (producer Reserved VM + monitor Autoscale)

Everything above also runs on the web — **two deployments, joined only by URLs** (the
same shape as production: the monitor is one service; the models live elsewhere).

### 4a. Models VM (`ai-use-cases` repo)
Replit exposes one external port per deployment, so `apps/portfolio-server` runs the
four apps as subprocesses and reverse-proxies path prefixes (`/churn`, `/chatbot`,
`/nba`, `/account`) — each model is still an independent service; the gateway is demo
hosting convenience only.

1. Import the `ai-use-cases` repo into Replit (keep it **private** — CPG Confidential).
2. Deploy → **Reserved VM** (`.replit` is pre-configured: `deploy/build.sh` +
   `deploy/run.sh`).
3. Secrets: `RAI_TELEMETRY_TOKEN` (pick a strong shared secret), optionally
   `ANTHROPIC_API_KEY` (real Claude on the chatbot's `/chat`), `RAI_CHAT_REPS=4`.
4. Note the URL, e.g. `https://telco-models.<user>.replit.app` — check `/health`
   (aggregates all four services).

### 4b. Autoscale monitor (`model-monitoring-prototype` repo)
`.replit` is pre-configured for **Autoscale**. Monitor state and artifact bytes live in
a monitor-owned managed PostgreSQL database. A renewable database lease prevents duplicate
observations across instances; `.github/workflows/autoscale-poll.yml` wakes a scaled-to-zero
deployment every five minutes and waits for one authenticated poll cycle.

1. Add a separate managed PostgreSQL database, then deploy → **Autoscale**.
2. Secrets:
   - `LIVE_CHURN_URL   = https://telco-models.<user>.replit.app/churn`
   - `LIVE_CHATBOT_URL = https://telco-models.<user>.replit.app/chatbot`
   - `LIVE_NBA_URL     = https://telco-models.<user>.replit.app/nba`
   - `LIVE_TELEMETRY_TOKEN` = the same shared secret as 4a
   - `LIVE_PRODUCER_URL` = the producer gateway URL
   - `LIVE_WORKER_TOKEN` = a separate strong secret
   - `ANTHROPIC_API_KEY` (real judge), `LLM_JUDGE_MODEL=claude-haiku-4-5`,
     `LLM_JUDGE_MAX_TRACES=20`
   - `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` (trace push)
3. Set GitHub Actions secret `MONITOR_WORKER_TOKEN` to the same value as
   `LIVE_WORKER_TOKEN`; optionally set repository variable `MONITOR_URL`. Open the monitor
   URL for a public, redacted, read-only dashboard. Generate production demo traffic only
   from the producer's authenticated controls.

**Deferred (enterprise)**: when models sit on-prem behind firewalls, the pull model
needs either network reach or the push-ingest roadmap item — a Council/architecture
decision (`MONITORING-CONTRACT.md` §2, §17), not a code change. For the enterprise
rollout everything lands inside True's ecosystem and this becomes moot.

## 5. Plug in a new model (the template pitch)

This is the story for other IT teams — onboarding a model is **config, not code**:

1. **Implement the contract** on your model service (`docs/MONITORING-CONTRACT.md`): `GET /telemetry/meta`,
   the per-window telemetry pull for your lane (ML: `/telemetry/inferences|labels|reference`; LLM:
   `/telemetry/traces`), and optionally `GET /model/artifact`.
2. **Register it**: add one row to `backend/seeds/live_registry_seed.json` (name / owner / risk tier /
   platform), add its id to `LIVE_UCS` + a `lane_kind` (`"ml"` or `"llm"`) in
   `backend/app/api/live_portfolio.py`, and set its base-URL env var.
3. **Watch it appear** — the shared health engine grades it with the same rubric, and it renders as a new
   Heatmap row + At-A-Glance card + full drill-down. No frontend change.

> "These three are not screenshots — they're real models graded by the same governance rubric. Onboarding a
> fourth is a seed row and a URL, and it renders identically."
