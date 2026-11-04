#!/usr/bin/env python3
"""Classify high-PageRank wiki nodes without ontology mappings."""

from __future__ import annotations

import argparse
import os
import sys

import networkx as nx

import graph_loader
from _graph_utils import compute_partition, load_graph, normalize_sources
from _matching import suggest_mappings


DEFAULT_WIKI_ROOT = os.environ.get("WIKI_ROOT", graph_loader.default_wiki_root())
DEFAULT_ONTOLOGY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "mini-ontology.jsonl",
)
CATEGORIES = ("mappable", "needs_entity", "ambiguous")


def _wiki_graph(graph):
    source_graph = graph.graph.get("_source_graphs", {}).get("wiki")
    if source_graph is not None:
        return source_graph
    source_nodes = graph.graph.get("source_nodes", {}).get("wiki", [])
    if source_nodes:
        return graph.subgraph(node for node in source_nodes if node in graph).copy()
    return graph


def _ontology_entities(graph) -> dict[str, dict]:
    metadata = graph.graph.get("_source_meta", {}).get("ontology", {})
    source_graph = graph.graph.get("_source_graphs", {}).get("ontology")
    source_nodes = graph.graph.get("source_nodes", {}).get("ontology")
    if source_nodes is None and source_graph is not None:
        source_nodes = list(source_graph.nodes())
    if source_nodes is None:
        source_nodes = list(metadata) if isinstance(metadata, dict) else []

    entities = {}
    for entity_id in source_nodes:
        entity = metadata.get(entity_id, {}) if isinstance(metadata, dict) else {}
        if not entity and source_graph is not None and entity_id in source_graph:
            entity = dict(source_graph.nodes[entity_id])
        entities[entity_id] = entity
    return entities


def _community_partition(graph) -> dict:
    return compute_partition(graph)


def _page_path(graph, wiki_node: str) -> str:
    metadata = graph.graph.get("_source_meta", {}).get("wiki", {})
    if isinstance(metadata, dict):
        path = metadata.get(wiki_node, {}).get("path")
        if path:
            return str(path)
    return wiki_node if wiki_node.endswith(".md") else f"{wiki_node}.md"


def analyze_orphans(
    G,
    top_k: int = 100,
    mapping_threshold: float = 0.5,
    ambiguous_threshold: float = 0.3,
) -> dict[str, list[dict]]:
    """Classify Top-K PageRank wiki nodes that have no ontology mapping."""
    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    if not 0 <= ambiguous_threshold <= mapping_threshold <= 1:
        raise ValueError("thresholds must satisfy 0 <= ambiguous <= mapping <= 1")

    report = {category: [] for category in CATEGORIES}
    wiki_graph = _wiki_graph(G)
    if wiki_graph.number_of_nodes() == 0:
        return report

    pagerank = nx.pagerank(wiki_graph)
    top_nodes = [
        node for node, _score in sorted(
            pagerank.items(), key=lambda item: (-item[1], str(item[0])),
        )[:top_k]
    ]
    mappings = G.graph.get("entity_mappings", {}).get("wiki", {})
    unmapped = [node for node in top_nodes if node not in mappings]
    if not unmapped:
        return report

    partition = _community_partition(G)
    matching_graph = G.copy()
    matching_graph.graph["partition"] = partition
    suggestions = suggest_mappings(
        unmapped,
        _ontology_entities(G),
        matching_graph,
        top_k=1,
        threshold=0.0,
    )
    best_by_node = {}
    for suggestion in suggestions:
        previous = best_by_node.get(suggestion["wiki_node"])
        if previous is None or suggestion["score"] > previous["score"]:
            best_by_node[suggestion["wiki_node"]] = suggestion

    for wiki_node in unmapped:
        best = best_by_node.get(wiki_node)
        score = float(best["score"]) if best else None
        if score is not None and score >= mapping_threshold:
            category = "mappable"
        elif score is not None and score >= ambiguous_threshold:
            category = "ambiguous"
        else:
            category = "needs_entity"

        node_pr = pagerank.get(wiki_node, 0.0)
        report[category].append({
            "wiki_node": wiki_node,
            "page": _page_path(G, wiki_node),
            "pr": node_pr,
            "pagerank": node_pr,
            "in_degree": wiki_graph.in_degree(wiki_node),
            "out_degree": wiki_graph.out_degree(wiki_node),
            "community": partition.get(wiki_node),
            "category": category,
            "best_candidate": best["ontology_entity"] if best else None,
            "score": score,
            "layers": list(best.get("layers", [])) if best else [],
        })
    return report


def _all_items(report: dict[str, list[dict]]) -> list[dict]:
    return sorted(
        (item for category in CATEGORIES for item in report.get(category, [])),
        key=lambda item: (-item["pagerank"], item["wiki_node"]),
    )


def format_table(report: dict[str, list[dict]], top_k: int = 100) -> str:
    items = _all_items(report)
    headers = ["Wiki Node", "PR", "In/Out", "Category", "Best Candidate", "Score"]
    rows = [[
        item["wiki_node"],
        f'{item["pagerank"]:.5f}',
        f'{item["in_degree"]}/{item["out_degree"]}',
        item["category"],
        item["best_candidate"] or "-",
        f'{item["score"]:.2f}' if item["score"] is not None else "-",
    ] for item in items]
    widths = [
        max([len(header), *(len(row[index]) for row in rows)])
        for index, header in enumerate(headers)
    ]

    def render(row: list[str]) -> str:
        return "  ".join(
            value.ljust(widths[index]) for index, value in enumerate(row)
        ).rstrip()

    counts = {category: len(report.get(category, [])) for category in CATEGORIES}
    denominator = top_k
    impact = counts["mappable"] / denominator * 10 if denominator else 0.0
    separator = "-" * max(76, sum(widths) + 2 * (len(widths) - 1))
    return "\n".join([
        f"Orphan Analysis \N{EM DASH} Top-{top_k} PR Unmapped Wiki Nodes ({len(items)} nodes)",
        separator,
        render(headers),
        separator,
        *(render(row) for row in rows),
        separator,
        "Summary: " + "  ".join(
            f"{category}={counts[category]}" for category in CATEGORIES
        ),
        f"Estimated D8 impact after applying mappable: +{impact:.1f} pts",
    ])


def _reason(item: dict) -> str:
    reasons = []
    if "str" in item["layers"]:
        reasons.append("string similarity")
    if "comm" in item["layers"]:
        reasons.append(f'same community cluster #{item["community"]}')
    return " + ".join(reasons) or "no strong matching signal"


def format_spec(report: dict[str, list[dict]]) -> str:
    sections = []
    for item in _all_items(report):
        candidate = item["best_candidate"]
        score = item["score"]
        if item["category"] == "mappable":
            sections.append("\n".join([
                f'## MAPPABLE: {item["wiki_node"]} \N{RIGHTWARDS ARROW} {candidate} ({score:.2f})',
                f'Page: `{item["page"]}`',
                f'Reason: {_reason(item)}',
                "Action:",
                f'  1. Edit `{item["page"]}` frontmatter',
                f'  2. Add: `ontology_mappings: [{candidate}]`',
            ]))
        elif item["category"] == "needs_entity":
            entity_id = os.path.basename(item["wiki_node"]).replace("_", "-")
            best = (
                f'Best match: {candidate} ({score:.2f})'
                if candidate is not None and score is not None
                else "No ontology candidate found"
            )
            community = item["community"]
            sections.append("\n".join([
                f'## NEEDS_ENTITY: {item["wiki_node"]}',
                f'Page: `{item["page"]}`',
                f'PR={item["pagerank"]:.5f}, in={item["in_degree"]}, '
                f'out={item["out_degree"]}, community=#{community}',
                best,
                f'Recommended action: create new ontology entity `{entity_id}`',
                "  - type: concept",
                "  - theorist: (to be determined)",
                "Template:",
                f'  {{"id": "{entity_id}", "type": "concept", '
                '"properties": {"theorist": ""}}',
            ]))
        else:
            sections.append("\n".join([
                f'## AMBIGUOUS: {item["wiki_node"]} ({score:.2f})',
                f'Page: `{item["page"]}`',
                f'Best candidate: {candidate} ({score:.2f} - {_reason(item)})',
                "Manual review needed:",
                f'  A. Accept mapping \N{RIGHTWARDS ARROW} add `ontology_mappings: [{candidate}]`',
                f'  B. Create new entity \N{RIGHTWARDS ARROW} `{os.path.basename(item["wiki_node"])}`',
                "  C. Accept as wiki-only \N{RIGHTWARDS ARROW} no action",
            ]))
    return "\n\n---\n\n".join(sections) if sections else "No orphan nodes found."


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _threshold(value: str) -> float:
    parsed = float(value)
    if not 0 <= parsed <= 1:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze high-PR unmapped wiki nodes.")
    parser.add_argument("--source", default="wiki,ontology")
    parser.add_argument("--wiki-root", default=DEFAULT_WIKI_ROOT)
    parser.add_argument("--ontology-path", default=DEFAULT_ONTOLOGY_PATH)
    parser.add_argument("--top", type=_positive_int, default=100)
    parser.add_argument("--mapping-threshold", type=_threshold, default=0.5)
    parser.add_argument("--ambiguous-threshold", type=_threshold, default=0.3)
    parser.add_argument("--format", choices=("table", "spec"), default="table")
    args = parser.parse_args(argv)
    if args.ambiguous_threshold > args.mapping_threshold:
        parser.error("--ambiguous-threshold cannot exceed --mapping-threshold")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        sources = normalize_sources(args.source)
        graph, _meta = load_graph(
            sources, args.wiki_root, args.ontology_path,
        )
        report = analyze_orphans(
            graph,
            top_k=args.top,
            mapping_threshold=args.mapping_threshold,
            ambiguous_threshold=args.ambiguous_threshold,
        )
        output = format_spec(report) if args.format == "spec" else format_table(
            report, args.top,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
