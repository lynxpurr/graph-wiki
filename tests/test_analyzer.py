from __future__ import annotations

import json
from types import SimpleNamespace

import wiki_analyzer as analyzer
from scorecard import build_scorecard


def _analysis_result(score=80, dead_ends=1, density=0.08, timestamp=None):
    diagnosis = [
        {
            "dimension": "D1",
            "title": f"Dead end {index}",
            "severity": "P1",
            "task_type": "bugfix",
            "auto_queue": True,
            "recommendation": "Add an outbound link.",
            "evidence": {"slug": f"node-{index}"},
        }
        for index in range(dead_ends)
    ]
    diagnosis.append({
        "dimension": "D6",
        "title": "Reciprocity",
        "severity": "P2",
        "task_type": "docs",
        "auto_queue": False,
        "recommendation": "Review reciprocal links.",
        "evidence": {"ratio": 0.12},
    })
    result = {
        "source": "wiki",
        "timestamp": timestamp or "2026-07-14T00:00:00+00:00",
        "graph_stats": {
            "nodes": 20,
            "edges": 30,
            "isolated": 0,
            "density": density,
            "components": 1,
        },
        "scores": {"total": score, "max": 110, "percentage": 73, "grade": "A"},
        "diagnosis": diagnosis,
        "provenance": {
            "analyzer_version": "3.1.2",
            "analyzer_sha256": "a" * 64,
            "source_snapshot_hash": "b" * 64,
            "threshold_version": "3.1.2",
        },
    }
    result["scorecard"] = build_scorecard(
        diagnosis_data=result,
        source="wiki",
        legacy_scores=result["scores"],
        timestamp=result["timestamp"],
    )
    result["scorecard"]["metrics"]["structure"]["density"] = density
    return result


def test_diagnostics_to_specs_produces_valid_specs():
    specs = analyzer.diagnostics_to_specs(_analysis_result())

    assert len(specs) == 1
    spec = specs[0]
    for field in ("title", "priority", "type", "risk", "description",
                  "evidence", "acceptance_criteria", "tags"):
        assert field in spec, f"missing field: {field}"
    assert spec["diagnosis_dimension"] == "D1"


def test_baseline_save_creates_valid_scorecard_json(tmp_path, monkeypatch):
    baseline_path = tmp_path / ".graph-baseline.json"
    monkeypatch.setattr(analyzer, "BASELINE_PATH", str(baseline_path))

    saved = analyzer.baseline_save(_analysis_result())
    payload = json.loads(baseline_path.read_text(encoding="utf-8"))

    assert payload == saved
    assert payload["schema_version"] == "b3-scorecard-v1"
    assert payload["baseline_id"].startswith("baseline_")
    assert payload["metrics"]["composite"]["health_score"] >= 0
    assert payload["scores"]["total"] == 80
    assert payload["canary_eligibility"] in {
        "low_risk", "moderate_risk", "high_risk",
    }


def test_trend_check_calculates_legacy_and_metric_delta(tmp_path, monkeypatch):
    baseline_path = tmp_path / ".graph-baseline.json"
    monkeypatch.setattr(analyzer, "BASELINE_PATH", str(baseline_path))
    analyzer.baseline_save(_analysis_result(score=80, density=0.08))

    current = _analysis_result(score=67, density=0.12)
    trend = analyzer.trend_check(current)

    assert trend["delta"] == -13
    assert trend["alert"] is True
    assert trend["scorecard_delta"]["structure.density"]["direction"] == "↑"


def test_history_trend_includes_scorecard_metric_arrows(tmp_path, monkeypatch):
    history_path = tmp_path / ".graph-history.jsonl"
    monkeypatch.setattr(analyzer, "HISTORY_PATH", str(history_path))
    analyzer.history_save(
        _analysis_result(score=78, density=0.08, timestamp="2026-07-07T00:00:00+00:00")
    )
    analyzer.history_save(
        _analysis_result(score=82, density=0.10, timestamp="2026-07-14T00:00:00+00:00")
    )

    trend = analyzer.history_trend(weeks=4)

    assert trend["records"] == 2
    assert trend["scorecard_delta"]["structure.density"] == {
        "previous": 0.08,
        "current": 0.1,
        "delta": 0.02,
        "direction": "↑",
    }


def test_run_test_suite_passes_existing_diagnosis_via_stdin(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(analyzer.subprocess, "run", fake_run)
    diagnosis = {"source": "wiki", "diagnosis": []}

    analyzer.run_test_suite(
        source="wiki", wiki_root="/tmp/wiki", scorecard=True, diagnosis=diagnosis,
    )

    assert "--scorecard" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--diagnosis-json") + 1] == "-"
    assert json.loads(captured["input"]) == diagnosis


def test_dead_end_threshold_makes_canary_high_risk():
    result = _analysis_result(dead_ends=3)

    assert result["scorecard"]["metrics"]["structure"]["dead_end_ratio"] == 0.15
    assert result["scorecard"]["canary_eligibility"] == "high_risk"


def _fusion_result(coverage=0.8, conflicts=0):
    result = _analysis_result()
    result["source"] = "wiki,ontology"
    result["multi_source"] = {
        "status": "computed",
        "sources": ["wiki", "ontology"],
        "D8": {
            "wiki_coverage": coverage,
            "conflicts": conflicts,
            "pass": coverage > 0.6,
        },
        "D9": {"consistency_ratio": 0.5},
        "D10": {"information_gain": 0.3},
    }
    return result


def test_scorecard_quality_marks_single_source_fusion_scores_na():
    quality = _analysis_result()["scorecard"]["metrics"]["quality"]

    assert quality["coverage_score"] is None
    assert quality["consistency_score"] is None
    assert quality["gain_score"] is None


def test_scorecard_quality_includes_multi_source_scores():
    result = _fusion_result()
    quality = build_scorecard(
        diagnosis_data=result, source=result["source"],
    )["metrics"]["quality"]

    assert quality["coverage_score"] == 8
    assert quality["consistency_score"] == 5
    assert quality["gain_score"] == 3


def test_diagnostics_to_specs_includes_d8_d10_but_only_flags_queueable():
    result = _fusion_result()
    result["diagnosis"].extend([
        {
            "dimension": "D8", "title": "Coverage", "severity": "P1",
            "task_type": "enhancement", "auto_queue": True,
            "recommendation": "Map nodes.", "evidence": {"wiki_coverage": 0.4},
        },
        {
            "dimension": "D9", "title": "Type mismatch", "severity": "P1",
            "task_type": "refactor", "auto_queue": True,
            "recommendation": "Align types.", "evidence": {"node": "a"},
        },
        {
            "dimension": "D10", "title": "Low gain", "severity": "P2",
            "task_type": "enhancement", "auto_queue": False,
            "recommendation": "Review mappings.", "evidence": {"gain": 0.0},
        },
    ])

    specs = analyzer.diagnostics_to_specs(result)
    fusion_specs = {
        spec["diagnosis_dimension"]: spec for spec in specs
        if spec["diagnosis_dimension"] in {"D8", "D9", "D10"}
    }

    assert set(fusion_specs) == {"D8", "D9", "D10"}
    assert fusion_specs["D8"]["auto_queue"] is True
    assert fusion_specs["D9"]["auto_queue"] is True
    assert fusion_specs["D10"]["auto_queue"] is False


def test_multi_source_baseline_and_trend_use_source_specific_file(tmp_path, monkeypatch):
    default_path = tmp_path / ".graph-baseline.json"
    monkeypatch.setattr(analyzer, "BASELINE_PATH", str(default_path))
    analyzer.baseline_save(_fusion_result(coverage=0.8, conflicts=1))
    multi_path = tmp_path / ".graph-baseline-wiki-ontology.json"

    assert multi_path.exists()
    current = _fusion_result(coverage=0.65, conflicts=2)
    trend = analyzer.trend_check(current)
    assert trend["coverage"]["delta"] == -0.15
    assert trend["merge_conflicts"]["delta"] == 1
    assert set(trend["fusion_alerts"]) == {"coverage_drop", "new_conflicts"}
    assert trend["alert"] is True


def test_run_diagnose_routes_all_multi_source_paths(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(analyzer.subprocess, "run", fake_run)
    analyzer.run_diagnose(
        source="wiki,ontology,ragflow", ontology_path="/tmp/graph.jsonl",
        wiki_root="/tmp/wiki", ragflow_path="/tmp/chunks.jsonl",
    )

    assert captured["cmd"][captured["cmd"].index("--source") + 1] == "wiki,ontology,ragflow"
    assert "--ontology-path" in captured["cmd"]
    assert "--wiki-root" in captured["cmd"]
    assert "--ragflow-path" in captured["cmd"]
