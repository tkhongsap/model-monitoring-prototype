"""Adapter protocols — the swap seam (PRD Appendix E §E.1, names fixed).

Exactly three engine protocols: LLMEvalAdapter (judge + tracing; TraceStore is a
sub-interface inside its implementations), MLMonitorAdapter (Evidently drift +
NannyML performance), ExplainAdapter (LIME + SHAP). ScenarioSource (scenario/source.py)
is a separate NON-engine input interface, excluded from the Azure-swap claim.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class TickContext:
    tick: int
    seed: int
    scenario_id: str | None = None
    inject: dict[str, Any] = field(default_factory=dict)  # churn_alpha, n_halluc, ...


@dataclass
class LaneResult:
    signals: dict[str, float | None] = field(default_factory=dict)  # SignalSpec keys (C.2)
    records: list = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)          # v1.1 window envelope
    artifacts: dict[str, str] = field(default_factory=dict)         # kind -> artifact_id
    errors: dict[str, str] = field(default_factory=dict)


@dataclass
class ExplainResult:
    lime_top: list = field(default_factory=list)
    instance: dict = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


class LLMEvalAdapter(Protocol):
    name: str
    def evaluate(self, use_case_id: str, tick: TickContext) -> LaneResult: ...


class MLMonitorAdapter(Protocol):
    name: str
    def monitor(self, use_case_id: str, tick: TickContext) -> LaneResult: ...


class ExplainAdapter(Protocol):
    name: str
    def explain(self, use_case_id: str, tick: TickContext) -> ExplainResult: ...


class TraceStore(Protocol):
    """Sub-interface inside LLMEvalAdapter implementations — not a fourth engine.
    Mirrors the Langfuse SDK v2 surface: trace() / score() / flush()."""
    def trace(self, name: str, input: str, output: str, metadata: dict) -> Any: ...
    def score(self, trace: Any, name: str, value: float) -> None: ...
    def flush(self) -> None: ...
