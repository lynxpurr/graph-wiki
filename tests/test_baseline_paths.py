"""Regression tests for issue #1 — baseline path asymmetry (假绿).

The baseline writer (wiki_analyzer baseline-save) and the readers
(weekly-health-check.py, canary-gate.py) must resolve the *same* file for the
same --source/--wiki-root/--ontology-path arguments, and a missing baseline
must be reported as missing instead of silently passing.
"""

from __future__ import annotations

import json
import os

import pytest

import baseline_paths
import wiki_analyzer as analyzer

ONTOLOGY_PATH = "/tmp/does-not-need-to-exist/ontology.jsonl"
WIKI_ROOT = "/tmp/does-not-need-to-exist/wiki"


def _resolved_paths(load_script_module, source, **kwargs):
    """Baseline path as seen by all three scripts."""
    weekly = load_script_module("weekly-health-check.py")
    canary = load_script_module("canary-gate.py")
    return {
        "analyzer": analyzer._baseline_path_for_source(source, **kwargs),
        "weekly": weekly._load_baseline(source, **kwargs)[1],
        "canary": canary.resolve_baseline(source, **kwargs).path,
    }


@pytest.mark.parametrize(
    "source,kwargs",
    [
        ("wiki", {"wiki_root": WIKI_ROOT}),
        ("ragflow", {"wiki_root": WIKI_ROOT}),
        ("ontology", {"ontology_path": ONTOLOGY_PATH, "wiki_root": WIKI_ROOT}),
        ("wiki,ontology", {"ontology_path": ONTOLOGY_PATH, "wiki_root": WIKI_ROOT}),
        ("wiki,ragflow", {"wiki_root": WIKI_ROOT}),
        (
            "wiki,ontology,ragflow",
            {"ontology_path": ONTOLOGY_PATH, "wiki_root": WIKI_ROOT},
        ),
    ],
)
def test_all_scripts_resolve_same_baseline_path(load_script_module, source, kwargs):
    paths = _resolved_paths(load_script_module, source, **kwargs)

    assert len(set(paths.values())) == 1, paths


def test_single_wiki_source_uses_wiki_root_not_ontology_dir():
    path = analyzer._baseline_path_for_source("wiki", wiki_root=WIKI_ROOT)

    assert path == os.path.join(WIKI_ROOT, ".graph-baseline.json")


def test_single_ontology_source_uses_ontology_dir():
    path = analyzer._baseline_path_for_source("ontology", ontology_path=ONTOLOGY_PATH)

    assert path == "/tmp/does-not-need-to-exist/.graph-baseline.json"


def test_multi_source_keeps_slug_naming():
    path = analyzer._baseline_path_for_source(
        "wiki,ontology", ontology_path=ONTOLOGY_PATH, wiki_root=WIKI_ROOT,
    )

    assert os.path.basename(path) == ".graph-baseline-wiki-ontology.json"


def test_baseline_save_writes_to_wiki_root(tmp_path):
    result = {"source": "wiki", "scores": {"total": 80}, "diagnosis": []}

    saved = analyzer.baseline_save(result, wiki_root=str(tmp_path))

    path = tmp_path / ".graph-baseline.json"
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == saved
    assert not (tmp_path / ".regression-baseline.json").exists()


def _weekly_baseline(load_script_module, tmp_path, wiki_root):
    """Write a baseline the way baseline-save does, then load it via weekly."""
    analyzer.baseline_save(
        {
            "source": "wiki",
            "scores": {"total": 80},
            "graph_stats": {"nodes": 3, "edges": 2},
            "diagnosis": [{"severity": "P2"}],
            "multi_source": {},
            "diagnosis_summary": {"by_severity": {"P2": 1}},
        },
        wiki_root=str(wiki_root),
    )
    weekly = load_script_module("weekly-health-check.py")
    return weekly, weekly._load_baseline("wiki", wiki_root=str(wiki_root))


def test_writer_and_reader_agree_on_saved_baseline(load_script_module, tmp_path):
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()

    _, (baseline, baseline_path) = _weekly_baseline(
        load_script_module, tmp_path, wiki_root,
    )

    assert baseline_path == str(wiki_root / ".graph-baseline.json")
    assert baseline["scores"]["total"] == 80
    assert baseline["diagnosis_summary"]["by_severity"]["P0"] == 0


def test_legacy_regression_baseline_is_read_with_migration_note(
    load_script_module, tmp_path,
):
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    (_weekly_baseline(load_script_module, tmp_path, wiki_root))
    (wiki_root / ".graph-baseline.json").rename(
        wiki_root / ".regression-baseline.json",
    )

    weekly = load_script_module("weekly-health-check.py")
    baseline, baseline_path = weekly._load_baseline("wiki", wiki_root=str(wiki_root))
    resolution = weekly._baseline_resolution("wiki", wiki_root=str(wiki_root))

    assert baseline["scores"]["total"] == 80
    assert baseline_path == str(wiki_root / ".regression-baseline.json")
    assert resolution.used_legacy is True
    assert "迁移提示" in resolution.migration_note
    assert str(wiki_root / ".graph-baseline.json") in resolution.migration_note


def test_new_baseline_wins_over_legacy(load_script_module, tmp_path):
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    legacy = wiki_root / ".regression-baseline.json"
    legacy.write_text(json.dumps({"scores": {"total": 10}}), encoding="utf-8")
    analyzer.baseline_save(
        {"source": "wiki", "scores": {"total": 80}, "diagnosis": []},
        wiki_root=str(wiki_root),
    )

    weekly = load_script_module("weekly-health-check.py")
    baseline, baseline_path = weekly._load_baseline("wiki", wiki_root=str(wiki_root))

    assert baseline_path == str(wiki_root / ".graph-baseline.json")
    assert baseline["scores"]["total"] == 80


def test_missing_baseline_is_not_a_silent_pass(monkeypatch, load_script_module, tmp_path):
    """Reverse check: no baseline file ⇒ explicit (无基线) and pass=False."""
    weekly = load_script_module("weekly-health-check.py")
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()

    monkeypatch.setattr(
        weekly, "_run_diagnose",
        lambda *args: (
            {"diagnosis": [], "graph_stats": {"nodes": 3, "edges": 2}}, None,
        ),
    )
    monkeypatch.setattr(
        weekly, "_run_test_suite", lambda *args: ({"summary": {"score": 80}}, None),
    )

    result = weekly.check_regression("wiki", wiki_root=str(wiki_root))

    assert result["baseline_found"] is False
    assert result["pass"] is False
    assert any("无基线" in check for check in result["checks"])
    assert any("无基线" in error for error in result["errors"])
    report = weekly.format_report(result)
    assert "(无基线)" in report
    assert "全部指标正常" not in report


def test_present_baseline_reports_real_comparison(monkeypatch, load_script_module, tmp_path):
    weekly = load_script_module("weekly-health-check.py")
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    baseline_path = wiki_root / ".graph-baseline.json"
    baseline_path.write_text(
        json.dumps(
            {
                "scores": {"total": 80},
                "graph_stats": {"nodes": 3, "edges": 2},
                "diagnosis_summary": {"by_severity": {"P0": 0, "P1": 0, "P2": 1}},
            },
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        weekly, "_run_diagnose",
        lambda *args: (
            {
                "diagnosis": [{"severity": "P2"}],
                "graph_stats": {"nodes": 3, "edges": 2},
            },
            None,
        ),
    )
    monkeypatch.setattr(
        weekly, "_run_test_suite", lambda *args: ({"summary": {"score": 78}}, None),
    )

    result = weekly.check_regression("wiki", wiki_root=str(wiki_root))

    assert result["baseline_found"] is True
    assert result["baseline_migration"] is None
    assert "评分: 78 (基线 80, delta=-2)" in result["checks"]
    assert not any("(无基线)" in check for check in result["checks"])
    assert result["pass"] is True


def test_resolver_rejects_unknown_source():
    with pytest.raises(ValueError):
        baseline_paths.resolve_primary_path("nope", wiki_root=WIKI_ROOT)
