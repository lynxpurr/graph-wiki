from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import networkx as nx

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "wiki_graph_boost_stub", SCRIPTS / "boost-stub.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _graph():
    wiki = nx.DiGraph()
    wiki.add_edges_from([
        ("target-high", "hub"),
        ("hub", "target-high"),
        ("already-linked", "stub"),
        ("already-linked", "hub"),
        ("other-community", "hub"),
    ])
    graph = wiki.copy()
    graph.graph.update({
        "source_nodes": {"wiki": list(wiki.nodes())},
        "entity_mappings": {"wiki": {"stub": "stub-entity"}},
        "_source_graphs": {"wiki": wiki},
    })
    return graph


def _partition():
    return {
        "stub": 3,
        "target-high": 3,
        "already-linked": 3,
        "hub": 3,
        "other-community": 9,
    }


class BoostStubTests(unittest.TestCase):
    def test_single_stub_suggestions(self):
        module = _load_module()
        with patch.object(module, "community_partition", return_value=_partition()):
            suggestions = module.suggest_boost_links(_graph(), "stub", top_k=5)

        self.assertTrue(suggestions)
        self.assertEqual(
            {item["target_page"] for item in suggestions}, {"target-high", "hub"},
        )
        self.assertGreaterEqual(suggestions[0]["target_pr"], suggestions[-1]["target_pr"])

    def test_same_community_filter(self):
        module = _load_module()
        with patch.object(module, "community_partition", return_value=_partition()):
            suggestions = module.suggest_boost_links(_graph(), "stub")

        self.assertTrue(all(item["community"] == 3 for item in suggestions))
        self.assertNotIn(
            "other-community", {item["target_page"] for item in suggestions},
        )

    def test_exclude_existing_links(self):
        module = _load_module()
        with patch.object(module, "community_partition", return_value=_partition()):
            suggestions = module.suggest_boost_links(_graph(), "stub")

        self.assertNotIn(
            "already-linked", {item["target_page"] for item in suggestions},
        )

    def test_estimated_boost_uses_target_out_degree(self):
        module = _load_module()
        graph = _graph()
        pagerank = nx.pagerank(graph.graph["_source_graphs"]["wiki"])
        with patch.object(module, "community_partition", return_value=_partition()):
            suggestions = module.suggest_boost_links(graph, "stub")

        high = next(item for item in suggestions if item["target_page"] == "target-high")
        self.assertAlmostEqual(high["est_boost"], pagerank["target-high"])

    def test_low_pr_detection_and_empty_result(self):
        module = _load_module()
        graph = _graph()

        self.assertEqual(module.find_low_pr_stubs(graph, threshold=1.1), ["stub"])
        self.assertEqual(module.find_low_pr_stubs(graph, threshold=0.0), [])

    def test_spec_format_contains_target_instruction(self):
        module = _load_module()
        graph = _graph()
        with patch.object(module, "community_partition", return_value=_partition()):
            suggestions = module.suggest_boost_links(graph, "stub", top_k=1)
            rendered = module.format_spec(graph, "stub", suggestions)

        self.assertIn("## Boost: stub", rendered)
        self.assertIn("### Target 1:", rendered)
        self.assertIn("[[stub]]", rendered)


if __name__ == "__main__":
    unittest.main()
