from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

try:
    import pytest
except ImportError:
    print("pytest unavailable; skipping mapping suggestion tests")
    raise SystemExit(0)

nx = pytest.importorskip("networkx")

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _matching import (  # noqa: E402
    community_affinity,
    normalize_slug,
    string_similarity,
    suggest_mappings,
)


def _load_cli_module():
    spec = importlib.util.spec_from_file_location(
        "wiki_graph_suggest_mappings", SCRIPTS / "suggest-mappings.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_normalize_slug():
    assert normalize_slug("  Mixed-Case_Value  ") == "mixed case value"


def test_string_similarity_exact():
    assert string_similarity("Power-Concentration", "power_concentration") == 1.0


def test_string_similarity_substring_bidirectional():
    assert string_similarity("control", "controlled-information") >= 0.75
    assert string_similarity("controlled-information", "control") >= 0.75


def test_string_similarity_no_match():
    assert string_similarity("abc", "xyz") < 0.3


def test_string_similarity_empty_values():
    assert string_similarity("", "anything") == 0.0
    assert string_similarity("", "") == 1.0


def test_community_affinity_same():
    partition = {"wiki": 3, "entity": 3}
    assert community_affinity("wiki", "entity", partition) == 1.0


def test_community_affinity_different_or_missing():
    partition = {"wiki": 3, "entity": 4}
    assert community_affinity("wiki", "entity", partition) == 0.0
    assert community_affinity("missing-a", "missing-b", partition) == 0.0


def test_suggest_mappings_pipeline():
    graph = nx.DiGraph()
    wiki_node = "concepts/controlled-information"
    graph.add_nodes_from([wiki_node, "controlled-information", "unrelated"])
    graph.graph["partition"] = {
        wiki_node: 7,
        "controlled-information": 7,
        "unrelated": 8,
    }

    results = suggest_mappings(
        [wiki_node],
        {"controlled-information": {}, "unrelated": {}},
        graph,
        threshold=0.5,
    )

    assert len(results) == 1
    assert results[0]["ontology_entity"] == "controlled-information"
    assert results[0]["score"] == pytest.approx(0.825)
    assert results[0]["layers"] == ["str", "comm"]


def test_suggest_mappings_uses_entity_title_metadata():
    graph = nx.DiGraph()
    graph.add_node("concepts/collective-power", title="Collective Power")

    results = suggest_mappings(
        ["concepts/collective-power"],
        {"entity-42": {"title": "Collective Power"}},
        graph,
        threshold=0.6,
    )

    assert results[0]["ontology_entity"] == "entity-42"
    assert results[0]["score"] == pytest.approx(0.7)


def test_suggest_mappings_threshold_filter():
    graph = nx.DiGraph()
    graph.add_nodes_from(["alpha", "omega"])
    graph.graph["partition"] = {"alpha": 1, "omega": 2}

    assert suggest_mappings(
        ["alpha"], {"omega": {}}, graph, threshold=0.3,
    ) == []


def test_suggest_mappings_top_k_is_per_wiki_node():
    graph = nx.DiGraph()
    results = suggest_mappings(
        ["alpha", "beta"],
        {"alpha": {}, "alpha-more": {}, "beta": {}, "beta-more": {}},
        graph,
        top_k=1,
        threshold=0.5,
    )

    assert [(item["wiki_node"], item["ontology_entity"]) for item in results] == [
        ("alpha", "alpha"),
        ("beta", "beta"),
    ]


def test_empty_unmapped():
    assert suggest_mappings([], {"entity": {}}, nx.DiGraph()) == []


def test_cli_extracts_unmapped_and_ontology_metadata_from_mock_graph():
    cli = _load_cli_module()
    graph = nx.DiGraph()
    graph.graph.update({
        "source_nodes": {
            "wiki": ["mapped", "unmapped"],
            "ontology": ["entity-a"],
        },
        "entity_mappings": {"wiki": {"mapped": "entity-a"}},
        "_source_meta": {"ontology": {"entity-a": {"title": "Entity A"}}},
    })

    assert cli.find_unmapped(graph) == ["unmapped"]
    assert cli.ontology_entities(graph) == {
        "entity-a": {"title": "Entity A"},
    }


def test_cli_table_format_handles_no_suggestions():
    cli = _load_cli_module()
    rendered = cli.format_table([])

    assert "Wiki Node" in rendered
    assert "Candidate Ontology" in rendered


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
