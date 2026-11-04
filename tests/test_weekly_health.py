from __future__ import annotations


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
