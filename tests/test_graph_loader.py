from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest


nx = pytest.importorskip("networkx")

from graph_loader import (
    _build_ontology,
    _build_ragflow,
    _canonical_id,
    build_graph,
    iter_wikilinks,
    merge_graphs,
    normalize_sources,
)


def test_iter_wikilinks_parses_alias_anchor_and_code():
    content = """[[basic]] [[target|Label]] [[page#anchor]]
`[[inline-code]]`
```markdown
[[fenced-code]]
```
"""
    links = list(iter_wikilinks(content))

    assert links[0] == ("basic", None, False)
    assert links[1] == ("target", "Label", False)
    assert links[2] == ("page", None, False)
    assert links[3][2] is True
    assert links[4][2] is True
    assert [target for target, _, in_code in links if not in_code] == [
        "basic", "target", "page",
    ]


def test_canonical_id_is_root_relative_and_handles_index(tmp_path: Path):
    root = tmp_path / "wiki"
    nested = root / "topics" / "page.md"
    index = root / "topics" / "index.md"

    assert _canonical_id(str(nested), str(root)) == "topics/page"
    assert _canonical_id(str(index), str(root)) == "topics"
    assert not _canonical_id(str(nested), str(root)).endswith(".md")


def test_build_graph_wiki_nodes_edges_and_meta(tmp_wiki: Path):
    graph, meta = build_graph(source="wiki", wiki_root=str(tmp_wiki))

    assert set(graph.nodes()) == {"home", "concepts/alpha", "concepts/beta", "archive"}
    assert graph.has_edge("home", "concepts/alpha")
    assert graph.has_edge("concepts/alpha", "concepts/beta")
    assert "ignored-code-link" not in graph
    assert meta["home"]["title"] == "Home"
    assert meta["home"]["path"] == "home.md"


def test_alias_resolution_uses_unique_basename(tmp_wiki: Path):
    graph, _ = build_graph(source="wiki", wiki_root=str(tmp_wiki))

    assert graph.has_edge("home", "concepts/beta")
    assert graph.has_edge("archive", "concepts/alpha")


def test_ontology_relation_types_and_symmetric_edges(tmp_path: Path):
    ontology = tmp_path / "ontology.jsonl"
    node_ids = list("abcdefgh")
    relations = [
        ("a", "b", "引用", "references"),
        ("b", "c", "实例化", "instantiates"),
        ("c", "d", "反驳", "refutes"),
        ("d", "e", "先决条件", "prerequisite_of"),
        ("e", "f", "属于", "belongs_to"),
        ("f", "g", "发生于", "occurred_in"),
        ("g", "h", "对称", "mutual_reinforcement"),
    ]
    records = [
        {
            "op": "create",
            "entity": {
                "id": node_id,
                "type": "理论框架",
                "properties": {"title": node_id.upper()},
            },
        }
        for node_id in node_ids
    ]
    records.extend(
        {
            "op": "relate",
            "entity": {"from": source, "to": target, "relation": relation},
        }
        for source, target, relation, _canonical in relations
    )
    ontology.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )

    graph, _meta = build_graph(source="ontology", ontology_path=str(ontology))

    for source, target, relation, canonical in relations:
        assert graph[source][target]["relation"] == relation
        assert graph[source][target]["relation_type"] == canonical
    assert graph.has_edge("h", "g")
    assert graph["h"]["g"] == graph["g"]["h"]


def test_ontology_update_merges_properties_into_graph_and_meta(tmp_path: Path):
    ontology = tmp_path / "ontology.jsonl"
    records = [
        {
            "op": "create",
            "entity": {
                "id": "alpha", "type": "concept",
                "properties": {"title": "Alpha"},
            },
        },
        {
            "op": "update",
            "entity": {
                "id": "alpha", "properties": {"theorist": "systems"},
            },
        },
    ]
    ontology.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    graph, meta = _build_ontology(str(ontology))

    assert graph.nodes["alpha"]["theorist"] == "systems"
    assert meta["alpha"]["properties"]["theorist"] == "systems"


def test_ambiguous_slug_is_reported(tmp_path: Path, capsys):
    wiki = tmp_path / "wiki"
    (wiki / "a").mkdir(parents=True)
    (wiki / "b").mkdir()
    (wiki / "source.md").write_text("[[page]]\n", encoding="utf-8")
    (wiki / "a" / "page.md").write_text("# A\n", encoding="utf-8")
    (wiki / "b" / "page.md").write_text("# B\n", encoding="utf-8")

    graph, _ = build_graph(source="wiki", wiki_root=str(wiki))
    captured = capsys.readouterr()

    assert "WARN: ambiguous link [[page]]" in captured.err
    assert graph.out_degree("source") == 1
    assert next(graph.successors("source")) in {"a/page", "b/page"}


def test_multi_source_exact_merge_fuses_attributes_and_edges(tmp_path: Path):
    wiki = nx.DiGraph(source="wiki")
    wiki.add_node(
        "entity_a", title="Wiki A", theorist="systems", tags=["wiki"],
        description="Wiki description",
    )
    wiki.add_node("entity_b", title="B")
    wiki.add_edge("entity_a", "entity_b")
    wiki_meta = {
        "entity_a": {"path": "entity_a.md"},
        "entity_b": {"path": "entity_b.md"},
    }

    ontology = nx.DiGraph(source="ontology")
    ontology.add_node(
        "entity_a", title="Ontology A", type="Concept",
        tags=["ontology", "wiki"], description="Ontology description",
    )
    ontology.add_node("entity_b", title="B", type="Concept")
    ontology.add_edge("entity_a", "entity_b", relation="supports")

    merged, meta, conflicts = merge_graphs([
        (wiki, wiki_meta), (ontology, {}),
    ])

    assert conflicts == []
    assert set(merged.nodes) == {"entity_a", "entity_b"}
    assert merged.nodes["entity_a"]["title"] == "Ontology A"
    assert merged.nodes["entity_a"]["theorist"] == "systems"
    assert merged.nodes["entity_a"]["tags"] == ["wiki", "ontology"]
    assert merged.nodes["entity_a"]["description"] == "Ontology description"
    assert merged.nodes["entity_a"]["wiki_description"] == "Wiki description"
    assert merged.nodes["entity_a"]["sources"] == ["wiki", "ontology"]
    assert merged["entity_a"]["entity_b"]["relation"] == "supports"
    assert merged["entity_a"]["entity_b"]["edge_sources"] == ["wiki", "ontology"]
    assert meta["_merge"]["entity_mappings"]["wiki"]["entity_a"] == "entity_a"


def test_exact_title_match_wins_over_fuzzy_neighbor():
    wiki = nx.DiGraph(source="wiki")
    wiki.add_node("concepts/social-proof", title="concepts/social-proof")
    wiki.add_node(
        "concepts/social-proof-cascade", title="concepts/social-proof-cascade",
    )
    ontology = nx.DiGraph(source="ontology")
    ontology.add_node("social-proof", title="concepts/social-proof", type="理论框架")

    merged, meta, conflicts = merge_graphs([(wiki, {}), (ontology, {})])

    assert conflicts == []
    assert "social-proof" in merged
    assert "concepts/social-proof-cascade" in merged
    assert meta["_merge"]["entity_mappings"]["wiki"] == {
        "concepts/social-proof": "social-proof",
    }


def test_basename_slug_match_precedes_different_titles():
    wiki = nx.DiGraph(source="wiki")
    wiki.add_node("concepts/alpha", title="Wiki Alpha")
    ontology = nx.DiGraph(source="ontology")
    ontology.add_node("alpha", title="Ontology Alpha", type="concept")

    merged, meta, conflicts = merge_graphs([(wiki, {}), (ontology, {})])

    assert conflicts == []
    assert set(merged.nodes) == {"alpha"}
    assert meta["_merge"]["entity_mappings"]["wiki"] == {
        "concepts/alpha": "alpha",
    }


def test_unmapped_ontology_entities_emit_warning(capsys):
    wiki = nx.DiGraph(source="wiki")
    wiki.add_node("concepts/alpha", title="Alpha")
    ontology = nx.DiGraph(source="ontology")
    ontology.add_node("alpha", title="Alpha", type="concept")
    ontology.add_node("orphan", title="Orphan", type="concept")

    merge_graphs([(wiki, {}), (ontology, {})])
    captured = capsys.readouterr()

    assert "[WARN] 1 ontology entities not mapped" in captured.err
    assert "  - orphan" in captured.err


def test_fusion_created_self_loop_emits_warning(capsys):
    wiki = nx.DiGraph(source="wiki")
    wiki.add_nodes_from(["page", "target"])
    wiki.add_edge("page", "target")
    wiki_meta = {
        "page": {"ontology_mappings": ["target"]},
        "target": {},
    }
    ontology = nx.DiGraph(source="ontology")
    ontology.add_node("target", type="concept")

    merged, _meta, _conflicts = merge_graphs([
        (wiki, wiki_meta), (ontology, {}),
    ])
    captured = capsys.readouterr()

    assert list(nx.selfloop_edges(wiki)) == []
    assert list(nx.selfloop_edges(ontology)) == []
    assert list(nx.selfloop_edges(merged)) == [("target", "target")]
    assert "[WARN] fused graph contains 1 self-loop(s)" in captured.err
    assert "  - target" in captured.err


def test_fuzzy_ambiguity_stays_unmerged_and_is_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("ONTOLOGY_OUTPUT", "sanitized")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "first.md").write_text('---\ntitle: "Shared Concept"\n---\n', encoding="utf-8")
    (wiki / "second.md").write_text('---\ntitle: "Shared Concept"\n---\n', encoding="utf-8")
    ontology = tmp_path / "graph.jsonl"
    ontology.write_text(
        '{"op":"create","entity":{"id":"onto_shared","type":"Concept",'
        '"properties":{"title":"Shared Concept"}}}\n',
        encoding="utf-8",
    )

    graph, meta = build_graph(
        source="wiki,ontology", wiki_root=str(wiki),
        ontology_path=str(ontology),
    )

    assert set(graph.nodes) == {"first", "second", "onto_shared"}
    conflicts = meta["_merge"]["conflicts"]
    assert len(conflicts) == 2
    assert all(item["reason"] == "ambiguous_title" for item in conflicts)
    conflict_file = wiki / ".merge-conflicts.json"
    assert conflict_file.exists()
    assert json.loads(conflict_file.read_text(encoding="utf-8")) == conflicts


def test_read_only_merge_keeps_conflicts_in_memory_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("ONTOLOGY_OUTPUT", "sanitized")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "first.md").write_text(
        '---\ntitle: "Shared Concept"\n---\n', encoding="utf-8",
    )
    (wiki / "second.md").write_text(
        '---\ntitle: "Shared Concept"\n---\n', encoding="utf-8",
    )
    ontology = tmp_path / "graph.jsonl"
    ontology.write_text(
        '{"op":"create","entity":{"id":"onto_shared","type":"Concept",'
        '"properties":{"title":"Shared Concept"}}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "graph_loader._write_merge_conflicts",
        lambda *_args, **_kwargs: pytest.fail("read-only build attempted a write"),
    )
    original_mode = stat.S_IMODE(wiki.stat().st_mode)
    wiki.chmod(0o555)
    try:
        graph, meta = build_graph(
            source="wiki,ontology", wiki_root=str(wiki),
            ontology_path=str(ontology), read_only=True,
        )
    finally:
        wiki.chmod(original_mode)

    assert set(graph.nodes) == {"first", "second", "onto_shared"}
    assert len(meta["_merge"]["conflicts"]) == 2
    assert graph.graph["merge_conflicts_path"] is None
    assert meta["_merge"]["conflict_path"] is None
    assert not (wiki / ".merge-conflicts.json").exists()


def test_ragflow_offline_dump_and_graceful_skip(tmp_path: Path):
    skipped_graph, skipped_meta = _build_ragflow(tmp_path / "missing.jsonl")
    assert skipped_graph.number_of_nodes() == 0
    assert skipped_meta["ragflow_status"] == "skipped"

    dump = tmp_path / "chunks.jsonl"
    dump.write_text(
        '\n'.join([
            '{"chunk_id":"c1","kb_name":"kb","document_id":"doc",'
            '"content":"first","entities":["Entity A"]}',
            '{"chunk_id":"c2","kb_name":"kb","document_id":"doc",'
            '"content":"second","entities":[]}',
        ]) + '\n',
        encoding="utf-8",
    )
    graph, meta = build_graph(source="ragflow", ragflow_path=str(dump))

    assert meta["ragflow_status"] == "loaded"
    assert set(graph.nodes) == {"c1", "c2"}
    assert graph["c1"]["c2"]["relation"] == "next"
    assert normalize_sources(["wiki", "ontology", "wiki"]) == ["wiki", "ontology"]
