from __future__ import annotations

import json

import pytest


def _baseline():
    return {
        "scores": {"total": 80},
        "graph_stats": {"nodes": 4, "edges": 4},
        "diagnosis_summary": {"by_severity": {"P0": 0, "P1": 0, "P2": 0}},
    }


def _diagnosis():
    return {
        "diagnosis": [],
        "graph_stats": {"nodes": 4, "edges": 4},
    }


def test_weekly_health_ontology_path_does_not_typeerror(monkeypatch, load_script_module):
    weekly = load_script_module("weekly-health-check.py")
    monkeypatch.setattr(weekly, "_load_baseline", lambda *args: (_baseline(), "/tmp/base.json"))
    monkeypatch.setattr(weekly, "_run_diagnose", lambda *args: (_diagnosis(), None))
    monkeypatch.setattr(
        weekly, "_run_test_suite", lambda *args: ({"summary": {"score": 80}}, None),
    )

    result = weekly.check_regression("ontology", ontology_path="/tmp/graph.jsonl")

    assert result["pass"] is True
    assert result["errors"] == []


def test_weekly_health_pass_and_fail_logic(monkeypatch, load_script_module):
    weekly = load_script_module("weekly-health-check.py")
    monkeypatch.setattr(weekly, "_load_baseline", lambda *args: (_baseline(), "/tmp/base.json"))
    monkeypatch.setattr(weekly, "_run_diagnose", lambda *args: (_diagnosis(), None))
    monkeypatch.setattr(
        weekly,
        "_run_test_suite",
        lambda *args: ({"summary": {"score": 65}}, "test-suite failed"),
    )

    result = weekly.check_regression("wiki", wiki_root="/tmp/wiki")

    assert result["pass"] is False
    assert result["alerts"]
    assert result["errors"] == ["test-suite error: test-suite failed"]


def test_weekly_health_alerts_on_multi_source_coverage_and_conflicts(
    monkeypatch, load_script_module,
):
    weekly = load_script_module("weekly-health-check.py")
    baseline = _baseline()
    baseline["multi_source"] = {
        "status": "computed",
        "D8": {"wiki_coverage": 0.8, "conflicts": 1},
    }
    diagnosis = _diagnosis()
    diagnosis["multi_source"] = {
        "status": "computed",
        "D8": {"wiki_coverage": 0.65, "conflicts": 2},
    }
    monkeypatch.setattr(weekly, "_load_baseline", lambda *args: (baseline, "/tmp/base.json"))
    monkeypatch.setattr(weekly, "_run_diagnose", lambda *args: (diagnosis, None))
    monkeypatch.setattr(
        weekly, "_run_test_suite", lambda *args: ({"summary": {"score": 80}}, None),
    )

    result = weekly.check_regression(
        "wiki,ontology", ontology_path="/tmp/graph.jsonl", wiki_root="/tmp/wiki",
    )

    assert result["pass"] is False
    assert any("跨源覆盖下降" in alert for alert in result["alerts"])
    assert any("新增合并冲突" in alert for alert in result["alerts"])


# ── Severity baseline semantics (issue #1, second false-green branch) ──────
# `{}` means "all severities are zero" (the writer normalises every key), so
# comparison must still happen. A missing/None by_severity carries no
# information and must fail the check instead of silently skipping severity
# regression detection.

def _write_baseline(wiki_root, by_severity=_baseline()["diagnosis_summary"]["by_severity"],
                    *, drop_summary=False, null_value=False, drop_key=False):
    baseline = {
        "scores": {"total": 80},
        "graph_stats": {"nodes": 4, "edges": 4},
        "diagnosis_summary": {"by_severity": by_severity},
    }
    if drop_summary:
        baseline.pop("diagnosis_summary")
    elif drop_key:
        baseline["diagnosis_summary"].pop("by_severity")
    elif null_value:
        baseline["diagnosis_summary"]["by_severity"] = None
    wiki_root.mkdir(parents=True, exist_ok=True)
    (wiki_root / ".graph-baseline.json").write_text(
        json.dumps(baseline, ensure_ascii=False), encoding="utf-8",
    )
    return wiki_root


def _diagnosis_with(p0=0, p1=0, p2=0):
    diagnosis = [{"severity": "P0"} for _ in range(p0)]
    diagnosis += [{"severity": "P1"} for _ in range(p1)]
    diagnosis += [{"severity": "P2"} for _ in range(p2)]
    return {"diagnosis": diagnosis, "graph_stats": {"nodes": 4, "edges": 4}}


def _run_check(monkeypatch, weekly, wiki_root, diagnosis):
    monkeypatch.setattr(weekly, "_run_diagnose", lambda *args: (diagnosis, None))
    monkeypatch.setattr(
        weekly, "_run_test_suite", lambda *args: ({"summary": {"score": 80}}, None),
    )
    return weekly.check_regression("wiki", wiki_root=str(wiki_root))


def test_empty_by_severity_zero_baseline_still_alerts_on_p0(monkeypatch,
                                                           load_script_module,
                                                           tmp_path):
    """`{}` baseline = all zeros ⇒ P0 0→6 must alert and fail."""
    weekly = load_script_module("weekly-health-check.py")
    wiki_root = _write_baseline(tmp_path / "wiki", by_severity={})

    result = _run_check(monkeypatch, weekly, wiki_root, _diagnosis_with(p0=6))

    assert result["pass"] is False
    assert result["errors"] == []
    assert any("P0 问题增加: 0 → 6" in alert for alert in result["alerts"])
    assert "P0 问题: 6 (基线 0)" in result["checks"]
    assert not any("(无基线)" in check for check in result["checks"])


@pytest.mark.parametrize(
    "shape", ["missing_summary", "missing_key", "null_value"],
)
def test_missing_severity_data_fails_instead_of_skipping(monkeypatch,
                                                        load_script_module,
                                                        tmp_path,
                                                        shape):
    """No severity data ⇒ explicit error, never a silent pass."""
    weekly = load_script_module("weekly-health-check.py")
    if shape == "missing_summary":
        wiki_root = _write_baseline(tmp_path / "wiki", drop_summary=True)
    elif shape == "missing_key":
        wiki_root = _write_baseline(tmp_path / "wiki", drop_key=True)
    else:
        wiki_root = _write_baseline(tmp_path / "wiki", null_value=True)

    result = _run_check(monkeypatch, weekly, wiki_root, _diagnosis_with(p0=6))

    assert result["pass"] is False
    assert any("基线缺少严重度数据，无法比较" in error for error in result["errors"])
    assert any("(无基线)" in check for check in result["checks"])


def test_normal_by_severity_baseline_behaviour_is_unchanged(monkeypatch,
                                                           load_script_module,
                                                           tmp_path):
    weekly = load_script_module("weekly-health-check.py")
    wiki_root = _write_baseline(
        tmp_path / "wiki", by_severity={"P0": 0, "P1": 0, "P2": 2},
    )

    result = _run_check(monkeypatch, weekly, wiki_root, _diagnosis_with(p2=2))

    assert result["pass"] is True
    assert result["errors"] == []
    assert result["alerts"] == []
    assert "P2 问题: 2 (基线 2)" in result["checks"]


def test_partial_severity_baseline_falls_back_to_zero(monkeypatch,
                                                      load_script_module,
                                                      tmp_path):
    """A non-empty dict missing a key keeps the legacy "assume 0" fallback."""
    weekly = load_script_module("weekly-health-check.py")
    wiki_root = _write_baseline(tmp_path / "wiki", by_severity={"P2": 2})

    result = _run_check(monkeypatch, weekly, wiki_root, _diagnosis_with(p2=2))

    assert result["pass"] is True
    assert "P0 问题: 0 (基线 0)" in result["checks"]


def test_empty_by_severity_with_no_change_passes(monkeypatch,
                                                 load_script_module,
                                                 tmp_path):
    """`{}` baseline + no issues at all ⇒ nothing to alert, check passes."""
    weekly = load_script_module("weekly-health-check.py")
    wiki_root = _write_baseline(tmp_path / "wiki", by_severity={})

    result = _run_check(monkeypatch, weekly, wiki_root, _diagnosis_with())

    assert result["pass"] is True
    assert result["alerts"] == []
    assert result["errors"] == []
    assert "P0 问题: 0 (基线 0)" in result["checks"]
