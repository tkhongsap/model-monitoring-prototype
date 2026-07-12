# AI Monitoring & Evaluation Contract — v1.1

**Classification: CPG Confidential — internal True Corporation use only.**

This document defines the **common monitoring and evaluation framework** by which AI use
cases — existing and new, LLM and classical ML — plug into the RAI monitoring control tower
consistently. It is written so another True Corp IT team can implement against it **without
reading our code**. The reference implementations live in two private repos:

- **Monitor (control tower)**: `model-monitoring-prototype` — pulls telemetry, runs ALL
  monitoring intelligence (Evidently drift, NannyML CBPE, LLM-as-judge, LIME/SHAP), grades
  health, serves the dashboard.
- **Reference model services**: `ai-use-cases` — a churn predictor (ML), a support chatbot
  (LLM), and an NBA recommender (NBA), each emitting this contract.

Obligations are staged: **[NOW]** = required of the prototype and any pilot integration;
**[JULY]** = required for the production rollout alongside the Amity intelligence layer.

---

## 1. Purpose, scope and status

v1.1 covers **binary-classification ML** use cases and **RAG-chat LLM** use cases; the
**NBA/recommender lane is DRAFT** (§10). Regression, multiclass, and ranking are roadmap
(§18). A use case that implements the REQUIRED tier of this contract gets, with zero
monitor-side code changes: drift detection, label-free performance estimation, realized
performance with label lag, LLM quality/safety/latency evaluation, explainability, and a
graded Green/Amber/Red health rollup on the control tower.

## 2. Architecture overview

**Pull-only control tower.** The monitor initiates HTTPS requests to each model service's
telemetry endpoints on a poll schedule. Model services never import, call, or depend on the
monitor. All monitoring intelligence runs monitor-side; services emit **raw** telemetry
(feature windows, labels, traces, optionally a model artifact). This keeps the dependency
direction correct for observability and means a new use case needs no monitoring code.

Prerequisite stated plainly: **the monitor must have network reach into each service's
telemetry port** (§17). Where cross-zone pull is refused, push-mode ingest
(`POST /api/ingest/{use_case_id}/{window}`) is a **reserved, committed roadmap surface** —
the Window envelope (§5) is transport-symmetric by design.

## 3. Contract versioning and evolution

- Every Window envelope carries `contract_version` (currently `"1.1"`). **[NOW]**
- Additive optional fields do NOT increment the version; renamed/removed/retyped fields DO.
- The normative schema artifact is `ai-use-cases/shared/telco_shared/telemetry.py`
  (pydantic v2). An OpenAPI 3.1 export is **[JULY]**.
- Consumers must ignore unknown fields (tolerant reader).

## 4. Endpoint tiers per use-case type

| Tier | ML use case | LLM use case | NBA use case (DRAFT) | All |
|---|---|---|---|---|
| **REQUIRED** | `GET /telemetry/inferences?tick=` · `GET /telemetry/labels?tick=` · `GET /telemetry/reference` | `GET /telemetry/traces?tick=` | `GET /telemetry/recommendations?tick=` · `GET /telemetry/rewards?tick=` · `GET /telemetry/reference` | `GET /health` · `GET /telemetry/meta` |
| **OPTIONAL** | `GET /model/artifact` | — | `GET /model/artifact` | — |
| **DEMO-ONLY — do not implement in production** | `POST /admin/tick` · `POST /admin/drift` · `POST /admin/retrain` · `POST /predict` | same + `POST /chat` | same + `POST /recommend` | — |

`/admin/*` endpoints exist in the reference apps solely to script degradation demos (a
drift knob, a retrain trigger). They are **outside the production contract**: a production
service's traffic, drift, and retraining are real, not injected.

`GET /telemetry/meta` returns:

```json
{
  "contract_version": "1.1",
  "model_name": "telco-churn",
  "use_case_type": "ml",            // "ml" | "llm" | "nba"
  "latest_tick": 17,
  "label_lag_ticks": 3,             // ml; "reward_lag_ticks" for nba; omitted for llm
  "feature_order": ["tenure_months", "..."],   // ml/nba
  "categorical_features": ["contract_type", "..."]  // ml/nba
}
```

## 5. The Window envelope (normative)

Every telemetry pull returns:

```json
{
  "contract_version": "1.1",
  "window": "inferences",           // inferences|labels|reference|traces|recommendations|rewards
  "from_tick": 12, "to_tick": 12,   // v1 invariant: from_tick == to_tick == requested tick
  "count": 500,                     // == len(records)
  "available_at_tick": null,        // labels/rewards only: tick at which they become available
  "window_id": "telco-churn:v1:t12:synthetic_demo",
  "source_instance_id": "producer-01",
  "opened_at": "2026-07-12T00:00:00Z",
  "closed_at": "2026-07-12T00:05:00Z",
  "content_sha256": "...",          // SHA-256 of the immutable canonical record content
  "first_record_id": "inf-6001",
  "last_record_id": "inf-6500",
  "model_version": "1",
  "provenance_counts": {"synthetic_demo": 500},
  "batch_id": "poc-20260712T120000p0700-ab12cd34ef56", // optional scheduled-run correlation
  "records": [ ... ]
}
```

Content type `application/json; charset=utf-8`. `NaN`/`Infinity` are forbidden — emit
`null`. `tick` omitted on the query string ⇒ the latest window. A `window_id` is immutable:
the producer must never return a different `content_sha256` for an already-observed id.

## 6. Time and windowing semantics

- `tick` is a **monotonically increasing logical window index owned by the model service**
  (e.g. one tick = one weekly scoring batch, or one hour of chat traffic). The wall-clock
  duration of a tick is declared per use case in the registry entry (§15).
- The monitor discovers `latest_tick` from `/telemetry/meta` and **syncs its read cursor to
  it** — it never blind-increments past the service's clock, and it can replay/catch up
  after downtime because windows stay pullable. **[NOW]**
- Retention guarantee: telemetry windows remain pullable for **≥ 72 h** across service
  restarts. **[NOW]** (producer and monitor cursors/observations are durable).
- `404 unknown_tick` = the service never had (or evicted) that window — including
  negative ticks. `count=0` = the window exists and genuinely had zero traffic. These
  are different statements.
- **The latest window is OPEN** (interactive traffic may still append to it); windows
  are final once `latest_tick` has moved past them. The monitor reads only CLOSED
  windows (`tick < latest_tick`), and holds its cursor (retries rather than skips) when
  a window could not be observed at all.
- `opened_at`/`closed_at` timestamps are required in v1.1 and anchor source-to-monitor lag.
- `batch_id` is optional and identifies the genuine execution batch that produced the
  window. The monitor persists and displays it unchanged beside `window_id` and the
  matching digest; it is never used to infer health.

After inserting an observation durably, the monitor best-effort posts
`POST /api/sync/observed` to the producer gateway with `window_id`, `observation_id`, and
`content_sha256`. A failed acknowledgement is stored separately and never rolls back the
observation. `GET /api/live/sync` exposes `source_tick`, `observed_tick`, `backlog`, state,
last-success time, observation id, and matching digest.

## 7. ML lane

**Records** (normative fields — see `telemetry.py` for types):

- `InferenceRecord`: `inference_id` (stable, unique), `model_name`, `served_version`,
  `tick`, `customer_id` (pseudonymous, §13), `features` (dict of **pre-encoded finite
  floats**; categoricals declared in `/telemetry/meta`), `churn_proba` (alias — the
  canonical name is `y_pred_proba`, §18), `predicted_label`.
- `LabelRecord`: `inference_id`, `customer_id`, `tick` (= the inference tick), `label`,
  `served_version`. Delivered **lagged**: labels for tick *t* become available at
  *t + label_lag_ticks*; the Window carries `available_at_tick`.
- `ReferenceRow`: `features`, `label` — the training/reference distribution; re-emitted
  after each retrain (it is the drift baseline).

**Labels are partial by design.** The monitor joins labels to inferences on `inference_id`
and computes realized metrics on the matched **subset** once coverage ≥ 50 % and both
classes are present, reporting `realized_label_coverage` alongside the metric. Pending
(lag not yet reached) is distinguished from "no labels" via `available_at_tick`.

**What the monitor computes**: reference vs current features → Evidently →
`data_drift_share`; current features + probabilities (+ artifact) → NannyML CBPE →
`estimated_roc_auc` (label-free early warning); matched labels → `realized_roc_auc`.

## 8. Model artifact endpoint (OPTIONAL)

`GET /model/artifact` → serialized model bytes with headers `X-Model-Version`,
`X-Feature-Order` (comma-joined), `X-Categorical-Features` (comma-joined),
`X-Sklearn-Version`, `X-Numpy-Version`.

- **Feature order is owned by the service.** The monitor reads it from the header/meta and
  must never carry a per-use-case feature list in its own source. **[NOW — implemented]**
  (the reference monitor keeps the original churn list only as a documented last-resort
  fallback for header-less legacy pulls; new use cases never rely on it)
- A version change triggers the monitor to re-pull the reference and refit its CBPE
  baseline and explainers.
- **Trust boundary**: the current format is joblib (pickle) — permitted **only within the
  named first-party trust zone** (our own services, operator-configured URLs).
  **[JULY]**: skops (sklearn), native JSON (XGBoost), or ONNX, plus artifact signing, for
  any cross-team exchange.
- **Degraded-mode matrix** — a use case that cannot ship an artifact (vendor model,
  SageMaker endpoint) still gets: Evidently drift ✓, realized metrics ✓. It loses: CBPE
  estimation ✗, monitor-side LIME/SHAP ✗ (alternative: the use case emits its own
  explanation telemetry — roadmap §18).

## 9. LLM lane

**`Trace` record** (normative semantics):

- `trace_id` stable+unique; `tick`; `model` (the serving model id); `customer_id`
  pseudonymous or null.
- `question`, `answer` — producer must redact per §13 before emitting.
- `retrieval_context[]`: `{doc_id, title, text, score}` — exactly what the generator was
  grounded on. `tool_calls[]`: `{name, input, output, latency_s}` — tool outputs count as
  grounding context for the judge.
- `refused` (bool): the assistant **declined to answer** (policy/out-of-scope). A helpful
  answer that contains a negative fact ("you have no prepaid balance") is NOT a refusal.
- `latency_s`: **producer-measured end-to-end wall time. REQUIRED — never default it to
  0**; `p95_latency_s` is a graded Reliability signal computed from it.
- `prompt_tokens`/`completion_tokens`: provider-reported. `topic`: optional.

**What the monitor computes** (LLM-as-judge over each trace):
`hallucination_rate`, `groundedness`, `relevance`, `pii_exposure_rate`, `p95_latency_s`.
The judge identity (heuristic vs Claude model id) is recorded with every score (§14).

## 10. NBA lane (DRAFT)

Mirrors the ML lane with serving telemetry:

- `Recommendation`: `rec_id` (stable — rewards join on it), `tick`, `customer_id`,
  `offer_id`, `features` (customer ⊕ offer context), `accept_proba`, `served_version`,
  `offer_mix` (the served-offer distribution of this window).
- `Reward`: `rec_id`, `customer_id`, `tick`, `accepted` (0/1), `served_version` — same lag
  protocol as labels (`reward_lag_ticks`, `available_at_tick`, partial by design).

**Signals**: the three ML signals on the accept-propensity core (drift / estimated AUC /
realized AUC), plus `acceptance_rate` (Feedback lane — the realized business feedback
loop) and `recommendation_drift` (Drift lane — total-variation distance between the
current `offer_mix` and the baseline mix captured at the first window after each
(re)baseline).

## 11. Signal vocabulary and health rollup

Canonical signals (bands are **org defaults owned by the AI Council**; per-use-case
overrides live in the registry entry §15):

| Signal | Lane | Direction | Green | Red |
|---|---|---|---|---|
| `hallucination_rate` | Quality | lower | < 0.02 | ≥ 0.02 |
| `groundedness` | Quality | higher | ≥ 0.85 | < 0.70 |
| `relevance` | Quality | higher | ≥ 0.85 | < 0.70 |
| `pii_exposure_rate` | Safety & security | lower | 0.0 | ≥ 0.01 |
| `p95_latency_s` | Reliability | lower | ≤ 4.0 | ≥ 8.0 |
| `data_drift_share` | Drift & degradation | lower | ≤ 0.30 | ≥ 0.50 |
| `estimated_roc_auc` | Quality | higher | ≥ 0.80 | < 0.72 |
| `realized_roc_auc` | Quality | higher | ≥ 0.80 | < 0.72 |
| `acceptance_rate` (NBA) | Feedback & action loop | higher | ≥ 0.15 | < 0.05 |
| `recommendation_drift` (NBA) | Drift & degradation | lower | ≤ 0.30 | ≥ 0.50 |

Grading: Red checked first (a value on both bars — e.g. hallucination exactly 0.02 — is Red); missing value → **Unknown**. Rollup is worst-of
signal → lane → overall across five lanes (Quality, Safety & security, Reliability,
Drift & degradation, Feedback & action loop). Two refinements:

- **Reasoned exclusions**: a signal that is Unknown *for a declared reason* (label lag not
  reached) is excluded from the rollup rather than degrading it.
- **Hand-set lanes**: lanes a use case genuinely doesn't produce (e.g. Feedback for a
  chatbot) are declared Unknown-with-reason and excluded.

## 12. Error semantics and resilience

- Error envelope: `{"error": "<code>", "detail": "<human text>"}`.
- Status codes: `404` unknown/evicted tick · `401/403` auth · `429/503` backpressure
  (monitor backs off and retries next poll).
- Timeouts: 30 s telemetry pulls, 60 s artifact pull.
- **Monitor guarantee**: any failure — network, schema, engine — degrades the affected
  signal(s) to Unknown with an error entry in the graded payload. **A tick never crashes.**

## 13. Security and data handling

- **[NOW]** Static per-use-case bearer token on all `/telemetry/*` and `/model/artifact`
  (`Authorization: Bearer …`), HTTPS mandatory outside localhost. Tokens via env/config,
  never in git.
- **[JULY]** mTLS / service-mesh identities; secrets from platform vaults (Key Vault /
  Secrets Manager / Vault); no `.env` files in production.
- **PII: the emitting service redacts/pseudonymizes BEFORE the telemetry endpoint.**
  `customer_id` must be a pseudonymous hash — never MSISDN or national ID. Free-text
  fields (`question`, `answer`, tool outputs) must be masked per the service's data
  classification. The monitor persists: traces + judge scores (trace store), signal
  values, artifacts (drift reports, plots). Retention period is a registry field.
- For use cases that cannot ship raw text at all: **precomputed-scores mode** — the
  service runs its own evaluation and emits scores instead of traces (roadmap, §18).

## 14. Volume, sampling and judge policy

- v1 is one-tick-per-call with a recommended max records per per-tick window (1 000;
  enforcement is **[JULY]** — the prototype does not enforce it). `/telemetry/reference`
  is exempt: its size is bounded by the reference-window size the service declares.
  Range pulls (`from_tick`/`to_tick`) + `next_cursor` pagination and `sample_n` are
  reserved for a future version.
- **Judge cost model**: real judging is one LLM call per judged trace.
  Recommended policy: judge ≤ ~200 traces/window — 100 % of refusals and
  an approved sample plus a small random baseline. If the real judge or score write-back
  fails, strict live mode records Unknown/error; it never substitutes heuristic scores.
  Default judge tier: Haiku-class.
- **The offline heuristic judge is an English-only dev stand-in.** Thai or mixed-language
  production traffic **requires the real Claude judge** (the token-overlap heuristic
  mis-scores Thai as hallucination). Every judged trace records its judge identity
  (`heuristic-v1` or the model id) in its stored metadata, and the graded LLM payload
  carries a top-level `judge` field — so trend lines survive judge switches.

## 15. Onboarding: use-case registry and conformance

**[JULY]** New use cases onboard **declaratively** — a PR to a registry repo, not a code
change to the monitor:

```yaml
use_case_id: AICT-L07          # allocated by the AI CoE
owner: team-fraud@truecorp
type: ml                        # ml | llm | nba
base_url: https://fraud-scoring.zone-b.internal:8443
auth_secret_ref: vault://monitoring/fraud-scoring-token
poll: { every: 1h }
window_duration: PT1H
label_lag_ticks: 24
data_classification: internal
threshold_overrides:            # AI Council sign-off required
  realized_roc_auc: { green: 0.75, red: 0.68 }
```

**Conformance checklist** a team runs before the monitor polls it: meta endpoint returns
the declared type/lag/features · every REQUIRED endpoint returns the envelope ·
unknown tick → 404 · auth enforced · labels/rewards honor the lag protocol · IDs stable
across re-pulls · no raw PII in any field. (The reference apps are the executable
examples of all of the above.)

Prototype status: the three reference use cases (AICT-L01 churn, AICT-L02 chatbot,
AICT-L03 NBA) are registered in monitor config; the YAML registry replaces this in July.

## 16. Amity integration surface

The monitor's **graded health payload** is the read API Amity (or any portal) consumes —
per use case: `{use_case_id, tick, signals{value, health, lane, bars…}, lanes, overall,
artifacts, errors}` via `GET /api/live/state?uc=…`, plus lane-specific fields — `judge`
and `judge_sample` on LLM use cases; `lime_top`, `reference_auc`, coverage and offer-mix
fields on ML/NBA use cases. Consumers must treat lane-specific fields as optional. Two integration directions,
both supported by design:

1. **Amity as consumer**: Amity's dashboard reads the graded payloads / receives Red
   transitions (alerting ownership = open question §18).
2. **Amity use cases as producers**: any model Amity hosts implements the REQUIRED tier
   and registers like every other use case.

## 17. Deployment topology and operations

- Monitor control plane co-located with Amity in the on-prem landing zone; AWS/Azure
  placement is a config change (base URLs + secrets), not a code change.
- Strict live mode requires managed Postgres for observations, signal history, source
  cursors, and the renewable single-worker lease. SQLite remains test/developer-only.
- Autoscale instances use a database lease with an in-flight heartbeat so only one worker
  polls and expensive 15–60 second model/judge windows cannot outlive ownership.
- `CONTROL_TOWER_MODE=live` makes baked scenario, simulation, export, and legacy artifact
  APIs return 404. It also refuses readiness when Postgres, external producer URLs, shared
  telemetry token, real Anthropic judge, or Langfuse credentials are missing.

## 18. Known limitations and roadmap

Push-mode ingest · range/paginated pulls · per-use-case thresholds UI ·
lag-replay version skew (a monitor catching up across a retrain grades old windows
against the CURRENT model's CBPE/drift baselines; realized metrics use recorded
probabilities and stay correct) ·
Thai-capable offline judge · regression/multiclass/ranking support · skops/ONNX +
artifact signing · precomputed-scores mode · `y_pred_proba` canonical naming
(`churn_proba` is a v1 alias) · use-case-emitted explanations.

**Open questions for the AI Council** (unresolved by this doc, decisions needed):

1. Amity topology: can the monitor get network ingress into each serving zone, or do we
   need push ingest sooner? Who owns alerting when a use case goes Red?
2. Judge economics: which Claude access path (direct API vs Bedrock/Azure), what judge
   tier and per-use-case budget, and how is cost charged back?
3. Threshold governance: does the Council formally own the default bands? What sign-off
   for per-use-case overrides?
4. Langfuse: is self-hosted Langfuse (Postgres + ClickHouse + Redis + S3) approved for
   the landing zone, and who operates it?
5. PII ruling: is "pseudonymized id + raw chat text with defined retention" acceptable,
   or must some use cases use precomputed-scores mode? What retention period?
6. Onboarding mechanics: who allocates `use_case_id`s; is a PR-based YAML registry
   acceptable for July?
7. Windowing migration: standard wall-clock window duration for v1.1, and when to migrate
   relative to the first external integration?
8. Degraded mode: is drift + realized-only (no CBPE, no monitor-side explainability)
   acceptable for artifact-less models, per the RAI acceptance criteria?

## 19. Appendix — normative artifacts & examples

- Normative schemas: `ai-use-cases/shared/telco_shared/telemetry.py` (pydantic v2).
- Reference implementations: `ai-use-cases/apps/{churn-predictor, chatbot, nba-recommender}`.
- Reference consumer: `model-monitoring-prototype/backend/app/adapters/*/live_http.py`.
- Live demo: start the apps, then `POST /api/live/tick?uc=AICT-L01|L02|L03` and
  `GET /api/live/state?uc=…` on the monitor.
