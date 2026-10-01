"""Pure label-to-inference join (shared by the live tick and the label-lag backfill)."""
from __future__ import annotations

from app.adapters.ml_monitor.realized import join_realized


def _inf(n):
    return [{"inference_id": f"i{i}", "churn_proba": i / n} for i in range(n)]


def test_realized_when_coverage_and_both_classes():
    inf = _inf(10)
    labels = [{"inference_id": f"i{i}", "label": int(i >= 5)} for i in range(10)]
    r = join_realized(inf, labels, id_field="inference_id", label_field="label",
                      proba_field="churn_proba")
    assert r.status == "realized" and r.value == 1.0 and r.coverage == 1.0
    assert r.reason is None


def test_no_labels():
    r = join_realized(_inf(4), [], id_field="inference_id", label_field="label",
                      proba_field="churn_proba")
    assert r.status == "no_labels" and r.value is None and r.coverage == 0.0


def test_empty_window_is_no_labels_with_reason():
    r = join_realized([], [{"inference_id": "i0", "label": 1}], id_field="inference_id",
                      label_field="label", proba_field="churn_proba")
    assert r.status == "no_labels" and r.value is None and r.coverage is None
    assert r.reason == "empty window"


def test_insufficient_coverage():
    labels = [{"inference_id": "i0", "label": 1}, {"inference_id": "i1", "label": 0}]
    r = join_realized(_inf(10), labels, id_field="inference_id", label_field="label",
                      proba_field="churn_proba")
    assert r.status == "insufficient_coverage" and r.coverage == 0.2
    assert r.value is None and r.matched == [1, 0]


def test_single_class():
    labels = [{"inference_id": f"i{i}", "label": 1} for i in range(10)]
    r = join_realized(_inf(10), labels, id_field="inference_id", label_field="label",
                      proba_field="churn_proba")
    assert r.status == "single_class" and r.value is None and r.coverage == 1.0


def test_unmatched_labels_do_not_count_toward_coverage():
    labels = [{"inference_id": f"zzz{i}", "label": i % 2} for i in range(10)]
    r = join_realized(_inf(10), labels, id_field="inference_id", label_field="label",
                      proba_field="churn_proba")
    assert r.status == "insufficient_coverage" and r.coverage == 0.0
