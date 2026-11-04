"""Test scorecard JSON schema and health score computation."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import networkx as nx

from scripts.scorecard import build_scorecard


def test_empty_graph_scorecard():
    graph = nx.DiGraph()
    scorecard = build_scorecard(
        graph=graph,
        source="test",
        timestamp="2026-01-01T00:00:00Z",
    )

    assert scorecard["schema_version"] == "b3-scorecard-v1"
    assert scorecard["source"] == "test"
    assert "metrics" in scorecard
    assert "structure" in scorecard["metrics"]
    assert scorecard["metadata"]["nodes"] == 0


def test_small_graph_health_score():
    graph = nx.DiGraph()
    graph.add_edges_from([(1, 2), (2, 3), (3, 1), (1, 3)])

    scorecard = build_scorecard(graph=graph, source="test")
    health_score = scorecard["metrics"]["composite"]["health_score"]

    assert 0 <= health_score <= 100
    assert scorecard["metrics"]["composite"]["grade"] in {
        "A", "B", "C", "D", "F",
    }


def test_graph_suite_grade_uses_percentage_of_max_score(tmp_path):
    root = Path(__file__).resolve().parents[1]
    wiki_root = shutil.copytree(
        root / "examples" / "mini-wiki",
        tmp_path / "mini-wiki",
    )
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "graph-test-suite.py"),
            "--source", "wiki,ontology,ragflow",
            "--wiki-root", str(wiki_root),
            "--ontology-path", str(root / "examples" / "mini-ontology.jsonl"),
            "--ragflow-path", str(root / "examples" / "mini-ragflow.jsonl"),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)["summary"]
    pct = summary["score"] / summary["max_score"] * 100

    assert summary["max_score"] == 140
    assert 75 <= pct < 90
    assert summary["grade"] == "A  图谱计算表现优秀"


def test_multi_source_scores_passthrough():
    graph = nx.DiGraph()
    graph.add_edge("a", "b")
    multi_source = {
        "coverage_score": 5,
        "consistency_score": 8,
        "gain_score": 3,
    }

    scorecard = build_scorecard(
        graph=graph,
        source="wiki,ontology",
        multi_source_scores=multi_source,
    )

    assert scorecard["metrics"]["quality"]["coverage_score"] == 5
    assert scorecard["metrics"]["quality"]["consistency_score"] == 8
    assert scorecard["metrics"]["quality"]["gain_score"] == 3


def test_canary_eligibility_green():
    graph = nx.DiGraph()
    for source in range(10):
        for target in range(10):
            if source != target:
                graph.add_edge(source, target)

    scorecard = build_scorecard(graph=graph, source="test")

    assert scorecard["canary_eligibility"] in (
        "low_risk", "moderate_risk", "high_risk",
    )


def test_thresholds_configurable():
    graph = nx.DiGraph()

    scorecard = build_scorecard(
        graph=graph,
        source="test",
        thresholds={"dead_end_warn": 0.50},
    )

    assert scorecard["thresholds"]["dead_end_warn"] == 0.50
