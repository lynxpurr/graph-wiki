#!/usr/bin/env python3
"""Suggest same-community wiki pages that can link to low-PR mapped nodes."""

from __future__ import annotations

import argparse
import os
import sys

import networkx as nx

import graph_loader
from _graph_utils import compute_partition, load_graph, normalize_sources


DEFAULT_WIKI_ROOT = os.environ.get("WIKI_ROOT", graph_loader.default_wiki_root())
DEFAULT_ONTOLOGY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "mini-ontology.jsonl",
)


def _wiki_graph(graph):
    source_graph = graph.graph.get("_source_graphs", {}).get("wiki")
    if source_graph is not None:
        return source_graph
    nodes = graph.graph.get("source_nodes", {}).get("wiki", [])
    if nodes:
        return graph.subgraph(node for node in nodes if node in graph).copy()
    return graph


def community_partition(graph) -> dict:
    """Return a deterministic wiki-source Louvain partition."""
    return compute_partition(_wiki_graph(graph))


def find_low_pr_stubs(G, threshold: float = 0.001) -> list[str]:
    """Return mapped wiki slugs whose source-graph PageRank is below threshold."""
    if threshold < 0:
        raise ValueError("threshold must be non-negative")
    wiki_graph = _wiki_graph(G)
    if wiki_graph.number_of_nodes() == 0:
        return []
    pagerank = nx.pagerank(wiki_graph)
    mappings = G.graph.get("entity_mappings", {}).get("wiki", {})
    return sorted(
        (
            slug for slug in mappings
            if slug in wiki_graph and pagerank.get(slug, 0.0) < threshold
        ),
        key=lambda slug: (pagerank.get(slug, 0.0), slug),
    )


def suggest_boost_links(G, stub_slug: str, top_k: int = 5) -> list[dict]:
    """Suggest high-PR pages in the stub's community that do not link to it."""
    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    wiki_graph = _wiki_graph(G)
    if stub_slug not in wiki_graph:
        return []

    pagerank = nx.pagerank(wiki_graph)
    partition = community_partition(G)
    community = partition.get(stub_slug)
    candidates = []
    for target in wiki_graph.nodes():
        if target == stub_slug or partition.get(target) != community:
            continue
        if wiki_graph.has_edge(target, stub_slug):
            continue
        target_pr = pagerank.get(target, 0.0)
        out_degree = wiki_graph.out_degree(target)
        candidates.append({
            "target_page": target,
            "target_pr": target_pr,
            "community": community,
            "est_boost": target_pr / max(1, out_degree),
        })
    candidates.sort(key=lambda item: (-item["target_pr"], item["target_page"]))
    return candidates[:top_k]


def _stub_context(graph, stub_slug: str) -> tuple[float, int | None, str | None]:
    wiki_graph = _wiki_graph(graph)
    pagerank = nx.pagerank(wiki_graph) if wiki_graph.number_of_nodes() else {}
    community = community_partition(graph).get(stub_slug)
    ontology_id = graph.graph.get("entity_mappings", {}).get("wiki", {}).get(stub_slug)
    return pagerank.get(stub_slug, 0.0), community, ontology_id


def format_table(graph, stub_slug: str, suggestions: list[dict]) -> str:
    stub_pr, community, _ontology_id = _stub_context(graph, stub_slug)
    headers = ["Target Page", "Target PR", "Community", "Est. Boost"]
    rows = [[
        item["target_page"],
        f'{item["target_pr"]:.5f}',
        f'#{item["community"]}',
        f'{item["est_boost"]:.3f}',
    ] for item in suggestions]
    widths = [
        max([len(header), *(len(row[index]) for row in rows)])
        for index, header in enumerate(headers)
    ]

    def render(row: list[str]) -> str:
        return "  ".join(
            value.ljust(widths[index]) for index, value in enumerate(row)
        ).rstrip()

    separator = "-" * max(66, sum(widths) + 2 * (len(widths) - 1))
    return "\n".join([
        f"Boost Suggestions for: {stub_slug} "
        f"(PR={stub_pr:.4f}, community=#{community})",
        separator,
        render(headers),
        separator,
        *(render(row) for row in rows),
        separator,
    ])


def format_spec(graph, stub_slug: str, suggestions: list[dict]) -> str:
    stub_pr, community, ontology_id = _stub_context(graph, stub_slug)
    lines = [
        f"## Boost: {stub_slug}",
        f"Current PR: {stub_pr:.4f}, Community: #{community}, "
        f"Ontology: {ontology_id or '-'}",
    ]
    if not suggestions:
        lines.extend(["", "No eligible same-community target pages found."])
        return "\n".join(lines)
    for index, item in enumerate(suggestions, start=1):
        lines.extend([
            "",
            f'### Target {index}: {item["target_page"]} '
            f'(PR={item["target_pr"]:.3f})',
            f'Insert `[[{stub_slug}]]` in the target page\'s "See Also" section.',
            f'Estimated PR boost: {item["est_boost"]:.3f}',
        ])
    return "\n".join(lines)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Suggest wikilinks that boost mapped stubs.")
    parser.add_argument("stub", nargs="?", help="Mapped wiki slug to analyze")
    parser.add_argument("--all", action="store_true", help="Analyze all low-PR mapped nodes")
    parser.add_argument("--source", default="wiki,ontology")
    parser.add_argument("--wiki-root", default=DEFAULT_WIKI_ROOT)
    parser.add_argument("--ontology-path", default=DEFAULT_ONTOLOGY_PATH)
    parser.add_argument("--top", type=_positive_int, default=5)
    parser.add_argument(
        "--pr-threshold", "--threshold", dest="pr_threshold",
        type=_non_negative_float, default=0.001,
    )
    parser.add_argument("--format", choices=("table", "spec"), default="table")
    args = parser.parse_args(argv)
    if args.all == bool(args.stub):
        parser.error("provide either a stub slug or --all")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        sources = normalize_sources(args.source)
        graph, _meta = load_graph(
            sources, args.wiki_root, args.ontology_path,
        )
        mappings = graph.graph.get("entity_mappings", {}).get("wiki", {})
        if args.all:
            stubs = find_low_pr_stubs(graph, args.pr_threshold)
        elif args.stub not in mappings:
            print(f"Error: wiki node is not mapped: {args.stub}", file=sys.stderr)
            return 1
        else:
            stubs = [args.stub]

        if not stubs:
            print("No mapped wiki nodes below the PR threshold.")
            return 0
        rendered = []
        for stub in stubs:
            suggestions = suggest_boost_links(graph, stub, args.top)
            formatter = format_spec if args.format == "spec" else format_table
            rendered.append(formatter(graph, stub, suggestions))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print("\n\n---\n\n".join(rendered))
    return 0


if __name__ == "__main__":
    sys.exit(main())
