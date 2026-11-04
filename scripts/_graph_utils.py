"""Shared graph loading and community helpers for wiki-graph scripts."""

from __future__ import annotations

import os
import tempfile

import networkx as nx

import graph_loader


def normalize_sources(source_str: str | list[str]) -> list[str]:
    """Return normalized, de-duplicated source names in input order."""
    return graph_loader.normalize_sources(source_str)


def load_graph(
    source_str: str | list[str],
    wiki_root: str,
    ontology_path: str,
    ragflow_path: str | None = None,
    read_only: bool = False,
):
    """Build a graph without persisting merge-conflict output in the wiki."""
    sources = normalize_sources(source_str)
    graph_source = sources if len(sources) > 1 else sources[0]
    build_kwargs = {
        "source": graph_source,
        "wiki_root": wiki_root,
        "ontology_path": ontology_path,
        "ragflow_path": ragflow_path,
    }
    if read_only:
        result = graph_loader.build_graph(
            **build_kwargs, read_only=True,
        )
    else:
        with tempfile.TemporaryDirectory(prefix="wiki-graph-") as scratch:
            result = graph_loader.build_graph(
                **build_kwargs,
                conflict_path=os.path.join(scratch, "merge-conflicts.json"),
            )

    if isinstance(result, tuple):
        graph, meta = result
    else:
        graph, meta = result, {}
    if not hasattr(graph, "graph") or not hasattr(graph, "nodes"):
        raise TypeError("graph_loader did not return a NetworkX graph")
    return graph, meta


def compute_partition(graph) -> dict:
    """Compute a deterministic Louvain partition, including edgeless graphs."""
    nodes = list(graph.nodes())
    if not nodes:
        return {}
    undirected = graph.to_undirected()
    if undirected.number_of_edges() == 0:
        return {node: index for index, node in enumerate(nodes)}
    try:
        communities = nx.algorithms.community.louvain_communities(
            undirected, seed=42,
        )
    except AttributeError:  # Compatibility with older NetworkX installations.
        communities = nx.algorithms.community.greedy_modularity_communities(
            undirected,
        )
    return {
        node: community_id
        for community_id, members in enumerate(communities)
        for node in members
    }
