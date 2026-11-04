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
        "wiki_graph_orphan_report", SCRIPTS / "orphan-report.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _graph(mapped: bool = False):
    wiki = nx.DiGraph()
    wiki.add_node("concepts/power-consolidation")
    ontology = nx.DiGraph()
    ontology.add_node("power-concentration", title="Power Concentration")
    graph = nx.compose(wiki, ontology)
    graph.graph.update({
        "source_nodes": {
            "wiki": list(wiki.nodes()),
            "ontology": list(ontology.nodes()),
        },
        "entity_mappings": {
            "wiki": {
                "concepts/power-consolidation": "power-concentration",
            } if mapped else {},
        },
        "_source_graphs": {"wiki": wiki, "ontology": ontology},
        "_source_meta": {
            "wiki": {
                "concepts/power-consolidation": {
                    "path": "concepts/power-consolidation.md",
                },
            },
            "ontology": {"power-concentration": {}},
        },
    })
    return graph


def _suggestion(score: float) -> list[dict]:
    return [{
        "wiki_node": "concepts/power-consolidation",
        "ontology_entity": "power-concentration",
        "score": score,
        "layers": ["str"],
    }]


class OrphanReportTests(unittest.TestCase):
    def test_classify_mappable(self):
        module = _load_module()
        with patch.object(module, "suggest_mappings", return_value=_suggestion(0.68)):
            report = module.analyze_orphans(_graph())

        self.assertEqual(len(report["mappable"]), 1)
        self.assertEqual(
            report["mappable"][0]["best_candidate"], "power-concentration",
        )

    def test_classify_needs_entity(self):
        module = _load_module()
        with patch.object(module, "suggest_mappings", return_value=[]):
            report = module.analyze_orphans(_graph())

        self.assertEqual(len(report["needs_entity"]), 1)
        self.assertIsNone(report["needs_entity"][0]["score"])

    def test_classify_ambiguous(self):
        module = _load_module()
        with patch.object(module, "suggest_mappings", return_value=_suggestion(0.42)):
            report = module.analyze_orphans(_graph())

        self.assertEqual(len(report["ambiguous"]), 1)
        self.assertAlmostEqual(report["ambiguous"][0]["score"], 0.42)

    def test_empty_unmapped(self):
        module = _load_module()
        with patch.object(module, "suggest_mappings") as matcher:
            report = module.analyze_orphans(_graph(mapped=True))

        self.assertEqual(
            report, {"mappable": [], "needs_entity": [], "ambiguous": []},
        )
        matcher.assert_not_called()

    def test_spec_format_contains_category_and_action(self):
        module = _load_module()
        with patch.object(module, "suggest_mappings", return_value=_suggestion(0.68)):
            report = module.analyze_orphans(_graph())

        rendered = module.format_spec(report)
        self.assertIn("## MAPPABLE: concepts/power-consolidation", rendered)
        self.assertIn("ontology_mappings: [power-concentration]", rendered)
        self.assertIn("concepts/power-consolidation.md", rendered)


if __name__ == "__main__":
    unittest.main()
