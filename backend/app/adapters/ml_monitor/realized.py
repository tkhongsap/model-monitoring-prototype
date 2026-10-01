"""Pure label-to-inference join used by the live tick and the label-lag backfill.

Contract v1.1 §7: labels arrive later than inferences and are partial by design. The
realized metric is computed on the matched subset only when coverage is at least 50 %
and both classes are present; every other outcome is a named, non-numeric status so the
caller (and the persisted `live_realized_metrics` row) can say *why* there is no value.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sklearn.metrics import roc_auc_score

MIN_COVERAGE = 0.5

# Statuses a join can produce. `pending`, `evicted` and `error` are assigned by callers
# (backfill) and never by this function.
REALIZED = "realized"
NO_LABELS = "no_labels"
INSUFFICIENT_COVERAGE = "insufficient_coverage"
SINGLE_CLASS = "single_class"


@dataclass
class RealizedResult:
    value: float | None
    coverage: float | None
    status: str
    matched: list[int] = field(default_factory=list)
    reason: str | None = None
    # Set by the backfill caller when a non-value status is nevertheless final (labels
    # overdue per the producer's `available_at_tick`); never set by `join_realized`.
    final: bool = False


def join_realized(inferences: list[dict], labels: list[dict], *, id_field: str,
                  label_field: str, proba_field: str) -> RealizedResult:
    """Join `labels` onto `inferences` by `id_field` and score the matched subset.

    `coverage` is matched / inferences (labels with no inference never count). An empty
    inference window has no coverage at all (`None`); a non-empty window with no labels
    has coverage 0.0.
    """
    if not inferences:
        return RealizedResult(None, None, NO_LABELS, reason="empty window")
    if not labels:
        return RealizedResult(None, 0.0, NO_LABELS, reason="label lag")
    lab = {label[id_field]: int(label[label_field]) for label in labels}
    pairs = [(lab[r[id_field]], float(r[proba_field])) for r in inferences
             if r[id_field] in lab]
    coverage = len(pairs) / len(inferences)
    matched = [y for y, _ in pairs]
    if coverage < MIN_COVERAGE:
        return RealizedResult(None, coverage, INSUFFICIENT_COVERAGE, matched,
                              f"label coverage {coverage:.0%} below 50%")
    if len(set(matched)) < 2:
        return RealizedResult(None, coverage, SINGLE_CLASS, matched,
                              "single class in matched labels")
    return RealizedResult(float(roc_auc_score(matched, [p for _, p in pairs])),
                          coverage, REALIZED, matched)
