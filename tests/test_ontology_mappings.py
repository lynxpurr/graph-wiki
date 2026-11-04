from __future__ import annotations

import json
from pathlib import Path

from graph_loader import _frontmatter_ontology_mappings, build_graph


def test_frontmatter_ontology_mappings_parses_block_list_until_next_key():
    content = """---
ontology_mappings:
  - first-entity
  - "second-entity"
tags:
  - ignored
---
"""

    assert _frontmatter_ontology_mappings(content) == [
        "first-entity", "second-entity",
    ]


def test_inline_ontology_mapping_drives_d8_entity_mapping(tmp_path: Path):
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "unrelated-page.md").write_text(
        """---
title: Unrelated Wiki Title
ontology_mappings: [test-entity]
---

# Unrelated Wiki Title
""",
        encoding="utf-8",
    )
    ontology = tmp_path / "ontology.jsonl"
    record = {
        "op": "create",
        "entity": {
            "id": "test-entity",
            "type": "concept",
            "properties": {"title": "Different Ontology Title"},
        },
    }
    ontology.write_text(json.dumps(record) + "\n", encoding="utf-8")

    graph, meta = build_graph(
        source="wiki,ontology",
        wiki_root=str(wiki),
        ontology_path=str(ontology),
    )

    assert graph.graph["_source_meta"]["wiki"]["unrelated-page"][
        "ontology_mappings"
    ] == ["test-entity"]
    assert meta["_merge"]["entity_mappings"]["wiki"] == {
        "unrelated-page": "test-entity",
    }
    assert set(graph.nodes["test-entity"]["sources"]) == {"wiki", "ontology"}
