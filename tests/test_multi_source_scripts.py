from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest


pytest.importorskip("networkx")
pytest.importorskip("community")

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True, text=True, timeout=60,
    )


def test_graph_test_suite_adds_fusion_scores(
    tmp_wiki: Path, tmp_ontology_graph: Path,
):
    common = (
        "--source", "wiki,ontology",
        "--wiki-root", str(tmp_wiki),
        "--ontology-path", str(tmp_ontology_graph),
    )
    result = _run("graph-test-suite.py", *common, "--json")
    scorecard_result = _run("graph-test-suite.py", *common, "--scorecard")
    terminal_result = _run("graph-test-suite.py", *common)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    scores = payload["multi_source_scores"]
    assert scores["status"] == "computed"
    assert all(
        isinstance(scores[name], int)
        for name in ("coverage_score", "consistency_score", "gain_score")
    )
    assert payload["summary"]["max_score"] == 130
    assert scores["D11"]["ontology_alignment"] == {
        "total_relations": 2,
        "verified_relations": 0,
        "alignment_ratio": 0.0,
        "score": 0.0,
    }
    assert scores["d8_detail"]["top_n"] == 100
    assert scores["d8_detail"]["mapped_in_top"] <= scores["d8_detail"][
        "total_wiki_nodes"
    ]
    assert scores["d11_detail"] == scores["D11"]["ontology_alignment"]
    expected_bonus = round(
        (scores["coverage_score"] + scores["consistency_score"]
         + scores["gain_score"]) / 3
    )
    assert payload["multi_source_bonus"] == expected_bonus
    assert payload["summary"]["score"] == (
        payload["summary"]["raw_score"] + expected_bonus
    )
    assert scorecard_result.returncode == 1, scorecard_result.stderr
    assert "Failing dimensions" in scorecard_result.stderr
    scorecard = json.loads(scorecard_result.stdout)
    quality = scorecard["metrics"]["quality"]
    assert quality["coverage_score"] == scores["coverage_score"]
    assert quality["consistency_score"] == scores["consistency_score"]
    assert quality["gain_score"] == scores["gain_score"]
    assert scorecard["legacy_scores"]["multi_source"]["D11"] == scores["D11"]
    assert scorecard["weights"]["multi_source_bonus"] == {
        "D8_coverage": f'bonus @ 1/3× (coverage={scores["coverage_score"]}/10)',
        "D9_consistency": f'bonus @ 1/3× (consistency={scores["consistency_score"]}/10)',
        "D10_gain": f'bonus @ 1/3× (gain={scores["gain_score"]}/10)',
    }
    assert scorecard["weights"]["formula"] == (
        "total = sum(D1..D12) + round((D8 + D9 + D10) / 3)"
    )
    assert terminal_result.returncode == 0, terminal_result.stderr
    assert (
        f"D8={scores['coverage_score']} + "
        f"D9={scores['consistency_score']} + "
        f"D10={scores['gain_score']} → /3"
    ) in terminal_result.stdout


def test_d9_uses_declared_theorist_and_debug_modes_work(
    tmp_wiki: Path, tmp_path: Path,
):
    ontology = tmp_path / "ontology.jsonl"
    records = [
        {
            "op": "create",
            "entity": {
                "id": "home", "type": "concept",
                "properties": {"title": "Home"},
            },
        },
        {
            "op": "update",
            "entity": {
                "id": "home", "properties": {"theorist": "systems"},
            },
        },
    ]
    ontology.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    common = (
        "--source", "wiki,ontology", "--wiki-root", str(tmp_wiki),
        "--ontology-path", str(ontology),
    )

    result = _run("graph-test-suite.py", *common, "--json")
    d8_debug = _run("graph-test-suite.py", *common, "--debug", "D8")
    d9_debug = _run("graph-test-suite.py", *common, "--debug", "D9")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["multi_source_scores"]["consistency_score"] == 10
    assert d8_debug.returncode == 0, d8_debug.stderr
    assert "D8 Debug: Top-100 wiki nodes by PageRank" in d8_debug.stdout
    assert d9_debug.returncode == 0, d9_debug.stderr
    assert "home" in d9_debug.stdout
    assert "systems" in d9_debug.stdout
    assert "Matched: 1/1" in d9_debug.stdout


def test_d11_counts_only_relations_verified_by_wiki_edges(
    tmp_wiki: Path, tmp_path: Path,
):
    ontology = tmp_path / "ontology.jsonl"
    records = [
        {
            "op": "create",
            "entity": {
                "id": node_id,
                "type": "理论框架",
                "properties": {"title": node_id},
            },
        }
        for node_id in ("home", "concepts/alpha", "archive")
    ]
    records.extend([
        {
            "op": "relate",
            "entity": {
                "from": "home", "to": "concepts/alpha", "relation": "引用",
            },
        },
        {
            "op": "relate",
            "entity": {
                "from": "archive", "to": "concepts/alpha", "relation": "引用",
            },
        },
        {
            "op": "relate",
            "entity": {
                "from": "concepts/alpha", "to": "archive", "relation": "反驳",
            },
        },
    ])
    ontology.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )

    result = _run(
        "graph-test-suite.py", "--source", "wiki,ontology",
        "--wiki-root", str(tmp_wiki), "--ontology-path", str(ontology), "--json",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    alignment = payload["multi_source_scores"]["D11"]["ontology_alignment"]
    assert payload["summary"]["max_score"] == 130
    assert alignment == {
        "total_relations": 3,
        "verified_relations": 2,
        "alignment_ratio": 0.6667,
        "score": 6.67,
    }


def test_graph_test_suite_single_source_marks_fusion_na(tmp_wiki: Path):
    result = _run(
        "graph-test-suite.py", "--source", "wiki",
        "--wiki-root", str(tmp_wiki), "--json",
    )

    assert result.returncode == 0, result.stderr
    scores = json.loads(result.stdout)["multi_source_scores"]
    assert scores == {
        "status": "N/A",
        "coverage_score": None,
        "consistency_score": None,
        "gain_score": None,
    }
    assert json.loads(result.stdout)["summary"]["max_score"] == 120


def test_graph_test_suite_quiet_emits_only_scorecard_json(tmp_wiki: Path):
    result = _run(
        "graph-test-suite.py", "--source", "wiki",
        "--wiki-root", str(tmp_wiki), "--quiet",
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "b3-scorecard-v1"
    assert "Failing dimensions" in result.stderr
    assert "Knowledge Graph" not in result.stdout


def test_graph_compute_multi_source_query_modes(
    tmp_wiki: Path, tmp_ontology_graph: Path,
):
    common = (
        "--source", "wiki,ontology", "--wiki-root", str(tmp_wiki),
        "--ontology-path", str(tmp_ontology_graph),
    )
    path_result = _run(
        "graph-compute.py", *common,
        "--path-from", "home", "--path-to", "concepts/alpha",
    )
    subgraph_result = _run(
        "graph-compute.py", *common,
        "--subgraph", "home", "--radius", "1", "--json",
    )

    assert path_result.returncode == 0, path_result.stderr
    assert "home" in path_result.stdout and "concepts/alpha" in path_result.stdout
    assert subgraph_result.returncode == 0, subgraph_result.stderr
    assert json.loads(subgraph_result.stdout)["center"] == "home"


def test_graph_compute_community_all_exports_full_partition(
    tmp_wiki: Path, tmp_ontology_graph: Path,
):
    common = (
        "--source", "wiki,ontology", "--wiki-root", str(tmp_wiki),
        "--ontology-path", str(tmp_ontology_graph),
    )
    all_result = _run(
        "graph-compute.py", *common, "--community", "all", "--json",
    )

    assert all_result.returncode == 0, all_result.stderr
    partition = json.loads(all_result.stdout)
    assert isinstance(partition, dict)
    assert partition
    assert all(isinstance(community_id, int) for community_id in partition.values())
    assert len(set(partition.values())) > 1

    community_id = next(iter(partition.values()))
    one_result = _run(
        "graph-compute.py", *common,
        "--community", str(community_id), "--json",
    )

    assert one_result.returncode == 0, one_result.stderr
    one_community = json.loads(one_result.stdout)
    assert one_community["community_id"] == community_id
    assert {node["id"] for node in one_community["nodes"]} == {
        node for node, node_community in partition.items()
        if node_community == community_id
    }


def test_graph_test_suite_no_write_supports_read_only_wiki(
    tmp_wiki: Path, tmp_ontology_graph: Path,
):
    original_mode = stat.S_IMODE(tmp_wiki.stat().st_mode)
    tmp_wiki.chmod(0o555)
    try:
        result = _run(
            "graph-test-suite.py", "--source", "wiki,ontology",
            "--wiki-root", str(tmp_wiki),
            "--ontology-path", str(tmp_ontology_graph),
            "--no-write", "--json",
        )
    finally:
        tmp_wiki.chmod(original_mode)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["multi_source_scores"]["status"] == "computed"
    assert not (tmp_wiki / ".merge-conflicts.json").exists()
