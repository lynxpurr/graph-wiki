from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


nx = pytest.importorskip("networkx")

from graph_loader import build_graph, merge_graphs


def test_d1_to_d7_return_valid_shapes(tmp_wiki: Path, load_script_module):
    diagnose = load_script_module("graph-diagnose.py")
    graph, meta = build_graph(source="wiki", wiki_root=str(tmp_wiki))
    pagerank = nx.pagerank(graph, alpha=0.85)

    dead_ends = diagnose.diagnose_deadends(graph, meta, pagerank)
    isolated = diagnose.diagnose_isolated(graph, meta, pagerank)
    sponges = diagnose.diagnose_sponges(graph, meta, pagerank)
    diffusion = diagnose.diagnose_seed_diffusion(graph, meta, pagerank)
    reciprocity = diagnose.diagnose_reciprocity(graph, meta, pagerank)
    link_rot = diagnose.diagnose_linkrot(graph, meta, wiki_root=str(tmp_wiki))
    staleness = diagnose.diagnose_staleness(graph, meta, pagerank)

    assert isinstance(dead_ends, list)
    assert isinstance(isolated, list)
    assert isinstance(sponges, list)
    assert isinstance(diffusion, list)
    assert isinstance(reciprocity, dict) and "ratio" in reciprocity
    assert isinstance(link_rot, dict) and "total_broken" in link_rot
    assert isinstance(staleness, list)


def _run_diagnose(*args: str) -> subprocess.CompletedProcess:
    script = Path(__file__).resolve().parents[1] / "scripts" / "graph-diagnose.py"
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_wiki_mode_regression_does_not_crash_and_has_provenance(tmp_wiki: Path):
    result = _run_diagnose(
        "--source", "wiki", "--wiki-root", str(tmp_wiki), "--json", "--full",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload["diagnosis"], list)
    assert payload["provenance"]["analyzer_version"]
    assert payload["provenance"]["source_snapshot_hash"] not in {
        "", "(unavailable)", "unavailable",
    }


def test_ontology_mode_skips_link_rot(tmp_ontology_graph: Path):
    result = _run_diagnose(
        "--source", "ontology", "--ontology-path", str(tmp_ontology_graph),
        "--json", "--full",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["source"] == "ontology"
    assert all(item["dimension"] != "D4" for item in payload["diagnosis"])


def test_d8_d10_multi_source_shapes(load_script_module):
    diagnose = load_script_module("graph-diagnose.py")
    wiki = nx.DiGraph(source="wiki")
    wiki.add_node("shared", title="Shared", theorist="School")
    wiki.add_node("wiki_only", title="Wiki Only", theorist="Other")
    wiki.add_edge("shared", "wiki_only")
    ontology = nx.DiGraph(source="ontology")
    ontology.add_node("shared", title="Shared", type="Concept")
    ontology.add_node("ontology_only", title="Ontology Only", type="Person")
    ontology.add_edge("shared", "ontology_only", relation="classifies")
    merged, _meta, _conflicts = merge_graphs([(wiki, {}), (ontology, {})])

    result = diagnose.diagnose_multi_source(merged, ["wiki", "ontology"])

    assert result["status"] == "computed"
    assert result["D8"]["wiki_coverage"] == 0.5
    assert result["D8"]["threshold"] == 0.15
    assert result["D8"]["pass"] is True
    assert result["D8"]["unmapped_wiki_nodes"] == ["wiki_only"]
    assert result["D9"]["compared_nodes"] == 0
    assert result["D9"]["mismatch_count"] == 0
    assert 0 <= result["D10"]["information_gain"] <= 1


def test_multi_source_cli_emits_d8_d10(tmp_wiki: Path, tmp_ontology_graph: Path):
    result = _run_diagnose(
        "--source", "wiki,ontology", "--wiki-root", str(tmp_wiki),
        "--ontology-path", str(tmp_ontology_graph), "--json", "--full",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["source"] == "wiki,ontology"
    assert payload["multi_source"]["status"] == "computed"
    assert set(payload["multi_source"]) >= {"D8", "D9", "D10"}


def test_single_source_marks_d8_d10_skipped(tmp_wiki: Path):
    result = _run_diagnose(
        "--source", "wiki", "--wiki-root", str(tmp_wiki), "--json",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["multi_source"]["status"] == "skipped"
