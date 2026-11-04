#!/usr/bin/env python3
"""Recommend ontology mappings for unmapped wiki nodes."""

from __future__ import annotations

import argparse
import json
import os
import sys

import graph_loader
from _graph_utils import compute_partition, load_graph, normalize_sources
from _matching import suggest_mappings


DEFAULT_WIKI_ROOT = os.environ.get("WIKI_ROOT", graph_loader.default_wiki_root())
DEFAULT_ONTOLOGY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "mini-ontology.jsonl",
)


def _wiki_nodes(graph) -> list[str]:
    source_nodes = graph.graph.get("source_nodes", {})
    if isinstance(source_nodes, dict) and "wiki" in source_nodes:
        return list(source_nodes["wiki"])
    source_meta = graph.graph.get("_source_meta", {}).get("wiki", {})
    if isinstance(source_meta, dict):
        return list(source_meta)
    source_graph = graph.graph.get("_source_graphs", {}).get("wiki")
    if source_graph is not None:
        return list(source_graph.nodes())
    return [
        node for node, attrs in graph.nodes(data=True)
        if "wiki" in attrs.get("sources", [])
    ]


def find_unmapped(graph) -> list[str]:
    """Return wiki source nodes absent from the wiki entity mapping keys."""
    mapped = graph.graph.get("entity_mappings", {}).get("wiki", {})
    return [node for node in _wiki_nodes(graph) if node not in mapped]


def ontology_entities(graph) -> dict[str, dict]:
    """Extract ontology entity metadata from graph-loader source metadata."""
    metadata = graph.graph.get("_source_meta", {}).get("ontology", {})
    if not isinstance(metadata, dict):
        return {}
    source_nodes = graph.graph.get("source_nodes", {}).get("ontology")
    entity_ids = source_nodes if source_nodes is not None else metadata.keys()
    return {
        entity_id: metadata.get(entity_id, {})
        for entity_id in entity_ids
        if isinstance(entity_id, str)
    }


community_partition = compute_partition


def format_table(suggestions: list[dict]) -> str:
    headers = ["Wiki Node", "Candidate Ontology", "Score", "Layers"]
    rows = [
        [
            item["wiki_node"], item["ontology_entity"],
            f'{item["score"]:.2f}', "+".join(item["layers"]),
        ]
        for item in suggestions
    ]
    widths = [
        max([len(headers[index]), *(len(row[index]) for row in rows)])
        for index in range(len(headers))
    ]

    def render(row: list[str]) -> str:
        return "  ".join(
            value.ljust(widths[index]) for index, value in enumerate(row)
        ).rstrip()

    return "\n".join([
        render(headers),
        "  ".join("-" * width for width in widths),
        *(render(row) for row in rows),
    ])


def _page_path(graph, wiki_node: str) -> str:
    metadata = graph.graph.get("_source_meta", {}).get("wiki", {})
    if isinstance(metadata, dict):
        path = metadata.get(wiki_node, {}).get("path", "")
        if path:
            return path
    return wiki_node if wiki_node.endswith(".md") else f"{wiki_node}.md"


def format_spec(suggestions: list[dict], graph, partition: dict) -> str:
    sections = []
    for item in suggestions:
        layers = item["layers"]
        community_id = partition.get(item["wiki_node"])
        reasons = ["string similarity match"] if "str" in layers else []
        if "comm" in layers:
            reasons.append(f"same community cluster #{community_id}")
        sections.append("\n".join([
            f'## {item["wiki_node"]} \u2192 {item["ontology_entity"]} '
            f'({item["score"]:.2f})',
            f'Page: `{_page_path(graph, item["wiki_node"])}`',
            f'Suggested ontology mapping: `{item["ontology_entity"]}`',
            f'Reason: {" + ".join(reasons)}',
            f'Action: add `ontology_mappings: [{item["ontology_entity"]}]` to frontmatter',
        ]))
    return "\n\n---\n\n".join(sections)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _threshold(value: str) -> float:
    parsed = float(value)
    if not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recommend ontology candidates for unmapped wiki nodes.",
    )
    parser.add_argument("--source", required=True, help="Comma-separated source names")
    parser.add_argument("--wiki-root", default=DEFAULT_WIKI_ROOT, help="Wiki Markdown root")
    parser.add_argument(
        "--ontology-path", default=DEFAULT_ONTOLOGY_PATH, help="Ontology JSONL path",
    )
    parser.add_argument("--top", type=_positive_int, default=20, help="Candidates per wiki node")
    parser.add_argument(
        "--threshold", type=_threshold, default=0.5, help="Minimum combined score",
    )
    parser.add_argument(
        "--format", choices=("table", "json", "spec"), default="table",
        help="Output format",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        sources = normalize_sources(args.source)
        graph, _meta = load_graph(
            sources, args.wiki_root, args.ontology_path,
        )
    except Exception as exc:
        print(f"Error loading graph: {exc}", file=sys.stderr)
        return 1

    unmapped = find_unmapped(graph)
    if not unmapped:
        print("No unmapped nodes found")
        return 0

    entities = ontology_entities(graph)
    if not entities:
        print("Error: ontology contains no entities", file=sys.stderr)
        return 1

    partition = community_partition(graph)
    graph.graph["community_partition"] = partition
    suggestions = suggest_mappings(
        unmapped, entities, graph, top_k=args.top, threshold=args.threshold,
    )

    if args.format == "json":
        output = json.dumps(suggestions, ensure_ascii=False, indent=2)
    elif args.format == "spec":
        output = format_spec(suggestions, graph, partition)
    else:
        output = format_table(suggestions)
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
