from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _privacy_filter import classify, filter_ontology
from graph_loader import build_graph, get_ontology_path


class PrivacyFilterTests(unittest.TestCase):
    def test_network_effects_is_public(self):
        self.assertEqual(classify("network-effects"), 1)

    def test_sample_scholar_is_public(self):
        self.assertEqual(classify("sample-scholar"), 1)

    def test_soft_power_is_public(self):
        self.assertEqual(classify("soft-claim"), 1)

    def test_existing_single_token_slug_is_public(self):
        self.assertEqual(classify("batna"), 1)

    def test_standard_wiki_paths_are_public(self):
        for name in ("concepts/power", "frameworks/model", "people/sample-theorist"):
            with self.subTest(name=name):
                self.assertEqual(classify(name), 1)

    def test_non_whitelisted_name_is_sanitized(self):
        self.assertEqual(classify("Internal Research Note"), 2)

    def test_empty_name_is_sanitized(self):
        self.assertEqual(classify(""), 2)

    def test_chinese_company_name_is_confidential(self):
        self.assertEqual(classify("某公司CTO"), 3)

    def test_employee_name_is_confidential(self):
        self.assertEqual(classify("employee-retention"), 3)

    def test_chinese_competitor_name_is_confidential(self):
        self.assertEqual(classify("竞品分析"), 3)

    def test_english_confidential_matching_is_case_insensitive(self):
        for term in ("Salary-Bands", "Revenue-Model", "Competitor-Map", "Layoff-Plan"):
            with self.subTest(term=term):
                self.assertEqual(classify(term), 3)

    def test_confidential_pattern_precedes_public_path(self):
        self.assertEqual(classify("concepts/employee-retention"), 3)

    def test_filter_ontology_mixed_names(self):
        records = [
            {"name": "network-effects"},
            {"name": "Internal Research Note"},
            {"name": "employee-retention"},
        ]
        path = self._write_jsonl(records)

        self.assertEqual(filter_ontology(str(path), tier=1), records[:1])
        self.assertEqual(filter_ontology(str(path), tier=2), records[:2])
        self.assertEqual(filter_ontology(str(path), tier=3), records)

    def test_filter_ontology_filters_relations_by_both_endpoints(self):
        records = [
            {"op": "create", "entity": {"id": "soft-claim"}},
            {"op": "create", "entity": {"id": "network-effects"}},
            {
                "op": "relate",
                "entity": {"from": "soft-claim", "to": "network-effects"},
            },
            {
                "op": "relate",
                "entity": {"from": "soft-claim", "to": "某公司CTO"},
            },
        ]
        path = self._write_jsonl(records)

        self.assertEqual(filter_ontology(str(path), tier=1), records[:3])

    def test_filter_ontology_omits_malformed_and_non_object_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ontology.jsonl"
            path.write_text(
                '{"name": "sample-claim"}\nnot-json\n["sample-claim"]\n',
                encoding="utf-8",
            )
            self.assertEqual(
                filter_ontology(str(path), tier=3),
                [{"name": "sample-claim"}],
            )

    def test_public_filter_warns_on_dropped_records(self):
        """P0-1 regression: dropped records must be surfaced, not silent."""
        import io
        from contextlib import redirect_stderr
        from graph_loader import build_graph

        records = [
            {"name": "sample-claim"},
            {"name": "Internal Research Note"},
        ]
        path = self._write_jsonl(records)
        with patch.dict(os.environ, {"ONTOLOGY_OUTPUT": "public"}, clear=False):
            buf = io.StringIO()
            with redirect_stderr(buf):
                G, meta = build_graph("ontology", ontology_path=str(path))
            self.assertIn("dropped", buf.getvalue().lower())

    def test_filter_ontology_rejects_invalid_tier(self):
        path = self._write_jsonl([])
        with self.assertRaisesRegex(ValueError, "tier must be"):
            filter_ontology(str(path), tier=0)

    def test_default_output_path_is_public(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                get_ontology_path(),
                str(ROOT / "examples" / "mini-ontology.jsonl"),
            )

    def test_public_output_path_is_public(self):
        with patch.dict(os.environ, {"ONTOLOGY_OUTPUT": "public"}, clear=False):
            self.assertEqual(get_ontology_path(), str(ROOT / "examples" / "mini-ontology.jsonl"))

    def test_private_output_path_requires_explicit_env(self):
        with patch.dict(os.environ, {"ONTOLOGY_OUTPUT": "private"}, clear=False):
            os.environ.pop("ONTOLOGY_PRIVATE_PATH", None)
            with self.assertRaisesRegex(ValueError, "ONTOLOGY_PRIVATE_PATH"):
                get_ontology_path()

    def test_private_output_path_can_produce_file(self):
        with tempfile.TemporaryDirectory() as home:
            env = {
                "ONTOLOGY_OUTPUT": "private",
                "ONTOLOGY_PRIVATE_PATH": str(Path(home) / "ontology-private-output.jsonl"),
            }
            with patch.dict(os.environ, env, clear=False):
                output = Path(get_ontology_path())
                output.parent.mkdir(parents=True, exist_ok=True)
                record = {
                    "op": "create",
                    "entity": {
                        "id": "employee-retention",
                        "type": "concept",
                        "properties": {"title": "Employee Retention"},
                    },
                }
                output.write_text(json.dumps(record) + "\n", encoding="utf-8")
                graph, _ = build_graph(source="ontology")
                self.assertTrue(output.is_file())
                self.assertIn("employee-retention", graph)

    def test_public_loader_applies_tier_one_filter(self):
        records = [
            {
                "op": "create",
                "entity": {"id": "sample-claim", "type": "concept"},
            },
            {
                "op": "create",
                "entity": {"id": "employee-retention", "type": "concept"},
            },
        ]
        path = self._write_jsonl(records)
        with patch.dict(os.environ, {"ONTOLOGY_OUTPUT": "public"}, clear=False):
            graph, _ = build_graph(source="ontology", ontology_path=str(path))

        self.assertEqual(set(graph), {"sample-claim"})

    def test_unknown_output_mode_is_rejected(self):
        with patch.dict(os.environ, {"ONTOLOGY_OUTPUT": "external"}, clear=False):
            with self.assertRaisesRegex(ValueError, "ONTOLOGY_OUTPUT"):
                get_ontology_path()

    def test_all_example_entities_are_public(self):
        ontology = ROOT / "examples" / "mini-ontology.jsonl"
        entity_ids = []
        for line in ontology.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("op") == "create":
                entity_ids.append(record["entity"]["id"])

        self.assertTrue(entity_ids)
        self.assertTrue(all(classify(entity_id) == 1 for entity_id in entity_ids))

    def _write_jsonl(self, records: list[dict]) -> Path:
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        path = Path(directory) / "ontology.jsonl"
        path.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
            encoding="utf-8",
        )
        return path


if __name__ == "__main__":
    unittest.main()
