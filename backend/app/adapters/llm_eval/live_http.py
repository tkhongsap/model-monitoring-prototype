"""LiveHttpLLMAdapter — the LIVE counterpart of the seeded `SeededJudgeAdapter`.

Instead of simulating interactions, it PULLS a real trace window from an external chatbot
app over HTTP (`GET /telemetry/traces?tick=`), runs an LLM-as-judge on each trace, aggregates
to the 5 LLM signals, and persists the traces via the existing `SqliteTraceStore` (so the
drill-down Traces tab works unchanged). ALL judging intelligence stays in the monitor; the
chatbot only answers and records.

Judge:
  - OFFLINE heuristic (default, zero cost, deterministic): groundedness = content-word
    overlap(answer, retrieval_context ∪ tool_outputs) — a refusal is treated as grounded;
    relevance = overlap(question, answer ∪ context); hallucination = non-refusal ∧ low
    groundedness; pii = regex over the answer.
  - REAL Claude (`config.LLM_JUDGE_MODEL`, default `claude-haiku-4-5`; `messages.parse` →
    structured per-trace scores) when `config.ANTHROPIC_API_KEY` is set — a one-line
    flip, same signal contract.

Every step degrades to None (Unknown) on failure, never crashing the tick — matching the
seeded adapter's contract.
"""
from __future__ import annotations

import json
import re

import numpy as np

from ... import config
from ..base import LaneResult, TickContext
from ..telemetry_http import pull, window_metadata
from .stores import LangfuseCloudStore, SqliteTraceStore

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = {
    "the", "a", "an", "is", "are", "do", "i", "my", "me", "of", "to", "on", "in", "for",
    "and", "or", "how", "what", "much", "many", "can", "with", "at", "this", "you", "your",
    "get", "am", "if", "it", "does", "per", "that", "be", "will", "within",
}
# PII in the answer text: Thai mobile number, email, 13-digit national ID.
_PII = re.compile(r"\b0\d{8,9}\b|[\w.+-]+@[\w-]+\.[\w.-]+|\b\d{13}\b")

_GROUNDED_REFUSAL = 0.9   # a correct refusal makes no unsupported claim → grounded
_HALLUCINATION_BAR = 0.5  # non-refusal below this groundedness counts as a hallucination
# A correct refusal or a grounded answer is on-topic by construction; the literal
# token-overlap proxy under-scores relevance (morphology, function words like "current"/
# "which" that a factual answer never echoes), so on-topic responses floor here — matching
# the seeded judge, where relevance stays high and degradation shows in groundedness.
_ONTOPIC_RELEVANCE = 0.9
MIN_LIVE_TRACES = 8


def _content(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS}


def _overlap(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a) if a else 0.0


def _judge_offline(trace: dict) -> dict:
    """Heuristic per-trace scores standing in for the real Claude judge."""
    answer = trace.get("answer", "")
    # the retrieved context = each chunk's title + body (the title carries topic words),
    # plus any tool outputs — this is exactly what a grounded answer should draw from.
    ctx_text = " ".join(c.get("title", "") + " " + c.get("text", "")
                        for c in trace.get("retrieval_context", []))
    ctx_text += " " + " ".join(json.dumps(tc.get("output", {})) for tc in trace.get("tool_calls", []))
    ans, ctx, q = _content(answer), _content(ctx_text), _content(trace.get("question", ""))
    refused = bool(trace.get("refused"))
    if refused:
        grounded, halluc = _GROUNDED_REFUSAL, False
    else:
        grounded = _overlap(ans, ctx)
        halluc = grounded < _HALLUCINATION_BAR
    relevance = _overlap(q, ans | ctx)
    if refused or grounded >= _HALLUCINATION_BAR:   # on-topic response
        relevance = max(relevance, _ONTOPIC_RELEVANCE)
    return {"groundedness": grounded, "relevance": relevance,
            "hallucination": halluc, "pii": bool(_PII.search(answer))}


def _judge_claude(traces: list[dict], model: str, *, allow_fallback: bool = True) -> list[dict]:
    """Real LLM-as-judge: one structured `messages.parse` call per trace, run CONCURRENTLY
    (each call is independent) so a window is judged in ~ceil(n/workers) round-trips rather
    than n sequential ones — the difference between a snappy and a minute-long live tick."""
    import anthropic
    from concurrent.futures import ThreadPoolExecutor
    from pydantic import BaseModel

    class Score(BaseModel):
        groundedness: float
        relevance: float
        hallucination: bool
        pii: bool

    client = anthropic.Anthropic()  # thread-safe; shared across workers
    system = [{
        "type": "text",
        "text": ("You are a strict evaluator of a telecom support chatbot. Given the user "
                 "question, the retrieved policy context, any tool outputs, and the bot's "
                 "answer, score: groundedness (0-1, is every claim supported by the context/"
                 "tools — a correct refusal is fully grounded), relevance (0-1, does the answer "
                 "address the question), hallucination (true if it asserts unsupported facts "
                 "instead of refusing), pii (true if the answer leaks a phone number, email, or "
                 "national ID). Return only the scores."),
        "cache_control": {"type": "ephemeral"},
    }]

    def judge_one(tr: dict) -> dict:
        # isolate each call: a transient failure on ONE trace (rate limit / 529 / blip,
        # likely at 8-way concurrency) must not discard the other ~19 good scores. Fall
        # back to the deterministic heuristic for just that trace so the window still grades.
        try:
            ctx = "\n".join(f"- {c.get('text', '')}" for c in tr.get("retrieval_context", []))
            tools = "\n".join(json.dumps(t.get("output", {})) for t in tr.get("tool_calls", []))
            prompt = (f"Question: {tr.get('question', '')}\n\nRetrieved context:\n{ctx}\n\n"
                      f"Tool outputs:\n{tools}\n\nBot answer: {tr.get('answer', '')}")
            s = client.messages.parse(
                model=model, max_tokens=256,
                system=system, messages=[{"role": "user", "content": prompt}],
                output_format=Score).parsed_output
            return {"groundedness": float(s.groundedness), "relevance": float(s.relevance),
                    "hallucination": bool(s.hallucination), "pii": bool(s.pii)}
        except Exception:  # noqa: BLE001 — strict-live rejects silent heuristic scores
            if allow_fallback:
                return _judge_offline(tr)
            raise

    with ThreadPoolExecutor(max_workers=min(8, len(traces) or 1)) as ex:
        return list(ex.map(judge_one, traces))   # map preserves trace order


def _latency_or_none(trace: dict, ndigits: int) -> float | None:
    """Rounded `latency_s`, or None when the trace does not carry one (never 0.0)."""
    value = trace.get("latency_s")
    return None if value is None else round(float(value), ndigits)


def _aggregate(scores: list[dict], latencies: list[float]) -> dict:
    n = len(scores)
    if not n:
        return {k: None for k in ("hallucination_rate", "groundedness", "relevance",
                                  "pii_exposure_rate", "p95_latency_s")}
    return {
        "hallucination_rate": float(np.mean([s["hallucination"] for s in scores])),
        "groundedness": float(np.mean([s["groundedness"] for s in scores])),
        "relevance": float(np.mean([s["relevance"] for s in scores])),
        "pii_exposure_rate": float(np.mean([s["pii"] for s in scores])),
        "p95_latency_s": float(np.percentile(latencies, 95)) if latencies else None,
    }


class LiveHttpLLMAdapter:
    name = "live_http"

    def __init__(self, base_url: str, scenario_id: str, use_case_id: str,
                 seed: int = 0, judge_model: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.use_case_id = use_case_id
        self.seed = seed
        self.judge_model = judge_model or config.LLM_JUDGE_MODEL
        # push judged traces + scores to Langfuse Cloud when configured (LLM-eval side of
        # the open-source stack: Langfuse for LLM evaluation). The cloud store ALSO writes
        # SQLite, so the local drill-down is unaffected; degrades to SQLite-only if the
        # keys are unset or the Langfuse SDK/host is unavailable (best-effort).
        if config.langfuse_cloud_configured():
            self.store = LangfuseCloudStore(
                scenario_id, use_case_id, config.LANGFUSE_PUBLIC_KEY,
                config.LANGFUSE_SECRET_KEY, config.LANGFUSE_HOST)
        else:
            self.store = SqliteTraceStore(scenario_id, use_case_id)

    def evaluate(self, use_case_id: str, tick: TickContext) -> LaneResult:
        res = LaneResult()
        # the telemetry pull gets its own degrade envelope so the runner can distinguish
        # "window unobserved — hold the cursor and retry" (errors['telemetry']) from a
        # judging/persistence failure (review finding)
        try:
            trace_env = pull(self.base_url, "/telemetry/traces", {"tick": tick.tick})
            traces = trace_env["records"]
            res.metadata.update(window_metadata(trace_env, tick.tick))
        except Exception as e:  # noqa: BLE001
            for k in ("hallucination_rate", "groundedness", "relevance",
                      "pii_exposure_rate", "p95_latency_s"):
                res.signals[k] = None
            res.errors["telemetry"] = f"{type(e).__name__}: {e}"
            return res
        if len(traces) < MIN_LIVE_TRACES:
            for key in ("hallucination_rate", "groundedness", "relevance",
                        "pii_exposure_rate", "p95_latency_s"):
                res.signals[key] = None
            res.records = []
            res.errors["insufficient_sample"] = (
                f"requires {MIN_LIVE_TRACES} traces; observed {len(traces)}")
            return res
        try:
            if config.ANTHROPIC_API_KEY:
                judge = self.judge_model
                # judge-sampling cap (§14): the real judge is one API call per trace, so
                # cap a large window to a uniform sample to keep a live tick responsive.
                cap = config.LLM_JUDGE_MAX_TRACES
                if cap and len(traces) > cap:
                    stride = len(traces) / cap
                    traces = [traces[int(i * stride)] for i in range(cap)]
                scores = _judge_claude(
                    traces, self.judge_model, allow_fallback=not config.strict_live_mode())
            else:
                if config.strict_live_mode():
                    raise RuntimeError(
                        "real Anthropic judge is required when CONTROL_TOWER_MODE=live")
                judge = "heuristic-v1"           # developer/demo mode only
                scores = [_judge_offline(t) for t in traces]

            # contract §5: latency_s is optional per trace.  A missing value is never
            # read as 0.0 — it is excluded from the p95 and counted (spec E.3).
            latencies = [float(t["latency_s"]) for t in traces
                         if t.get("latency_s") is not None]
            res.metadata["latency_missing"] = sum(
                1 for t in traces if t.get("latency_s") is None)
            res.signals.update(_aggregate(scores, latencies))

            # persist traces + scores so the drill-down Traces tab reads them back.
            # Persistence failure (e.g. re-observing a tick -> deterministic trace_id
            # collision) must not blank the already-computed signals (review finding).
            try:
                self.store.set_tick(tick.tick)
                for tr, sc in zip(traces, scores):
                    t = self.store.trace(
                        name="telco_chatbot", input=tr.get("question", ""),
                        output=tr.get("answer", ""),
                        metadata={"topic": tr.get("topic", ""),
                                  "latency_s": _latency_or_none(tr, 3),
                                  "refused": bool(tr.get("refused")), "judge": judge})
                    self.store.score(t, "groundedness", sc["groundedness"])
                    self.store.score(t, "relevance", sc["relevance"])
                    self.store.score(t, "hallucination", 1.0 if sc["hallucination"] else 0.0)
                self.store.flush(block=False)   # SQLite now; Langfuse via background thread
            except Exception as e:  # noqa: BLE001 — signals stand; note the store failure
                res.errors["trace_store"] = f"{type(e).__name__}: {e}"

            # write-back: post judge scores to the chatbot so they appear on Langfuse traces
            # (the chatbot forwards them using the trace_id it published in /telemetry/traces)
            try:
                from ..telemetry_http import push_scores
                score_items = [
                    {"trace_id": tr["trace_id"], "name": name, "value": val, "comment": judge}
                    for tr, sc in zip(traces, scores)
                    if tr.get("trace_id")
                    for name, val in [
                        ("groundedness", sc["groundedness"]),
                        ("relevance", sc["relevance"]),
                        ("hallucination", 1.0 if sc["hallucination"] else 0.0),
                    ]
                ]
                if score_items:
                    push_scores(self.base_url, score_items)
            except Exception as e:  # noqa: BLE001 — write-back failure must not kill the tick
                res.errors["score_writeback"] = f"{type(e).__name__}: {e}"
                if config.strict_live_mode():
                    # A live score that was not durably written back is not presented as
                    # a successful Green evaluation.  Keep the observation for audit, but
                    # make every judged metric explicitly Unknown/error.
                    for key in ("hallucination_rate", "groundedness", "relevance",
                                "pii_exposure_rate", "p95_latency_s"):
                        res.signals[key] = None

            # Public live APIs expose score evidence, never raw questions/answers.  A
            # stable trace identifier supports audit correlation without leaking input.
            seen, sample = set(), []
            for tr, sc in zip(traces, scores):
                trace_id = str(tr.get("trace_id") or "")
                if trace_id not in seen:
                    seen.add(trace_id)
                    sample.append({
                        "trace_id": trace_id,
                        "groundedness": round(sc["groundedness"], 3),
                        "relevance": round(sc["relevance"], 3),
                        "hallucination": sc["hallucination"], "pii": sc["pii"],
                        "latency_s": _latency_or_none(tr, 2),
                        "judge": judge})
            res.records = sample
        except Exception as e:  # noqa: BLE001 — degrade, never crash the tick
            for k in ("hallucination_rate", "groundedness", "relevance",
                      "pii_exposure_rate", "p95_latency_s"):
                res.signals[k] = None
            res.errors["llm_eval"] = f"{type(e).__name__}: {e}"
        return res
