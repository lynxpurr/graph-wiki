#!/usr/bin/env python3
"""Export a wiki-graph knowledge graph as GraphML or GEXF."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:
    import networkx as nx
except ImportError:
    print(
        "ERROR: networkx is required for graph export. "
        "Install the wiki-graph runtime dependencies first.",
        file=sys.stderr,
    )
    raise SystemExit(1)


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from graph_loader import build_graph  # noqa: E402


DEFAULT_ONTOLOGY_PATH = SCRIPT_DIR.parents[2] / "memory" / "ontology" / "graph.jsonl"
SOURCE_COLORS = {
    "wiki": "#4A90D9",
    "ontology": "#50B86C",
    "ragflow": "#E8953C",
    "unknown": "#8A94A3",
}
_SIMPLE_TYPES = (str, int, float, bool)


def _as_sources(value: Any) -> list[str]:
    """Return normalized source names from a scalar or collection."""
    if value is None:
        return []
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = [value]

    result: list[str] = []
    for item in values:
        source = str(item).strip().lower()
        if source and source not in result:
            result.append(source)
    return result


def _primary_source(graph: nx.Graph, attrs: Mapping[str, Any]) -> str:
    """Choose the source used for the exported source attribute and color."""
    sources = _as_sources(
        attrs.get("sources")
        or attrs.get("edge_sources")
        or attrs.get("source")
    )
    if not sources:
        sources = _as_sources(graph.graph.get("sources") or graph.graph.get("source"))
    # graph_loader makes ontology data authoritative on fused nodes.
    if "ontology" in sources:
        return "ontology"
    return sources[0] if sources else "unknown"


def _json_safe(value: Any) -> Any:
    """Recursively convert a value into data accepted by json.dumps()."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _normalize_value(value: Any) -> str | int | float | bool:
    """Convert an attribute value to a GraphML/GEXF-safe scalar."""
    if isinstance(value, _SIMPLE_TYPES):
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        return value
    if isinstance(value, (Mapping, list, tuple, set)):
        return json.dumps(
            _json_safe(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return str(value)


def _normalize_attributes(attrs: Mapping[Any, Any]) -> dict[str, Any]:
    """Return scalar-only attributes, omitting values with no information."""
    return {
        str(key): _normalize_value(value)
        for key, value in attrs.items()
        if value is not None
    }


def normalize_graph(graph: nx.Graph) -> nx.Graph:
    """Copy a graph and normalize graph, node, and edge metadata for export."""
    normalized = graph.copy()

    graph_attrs: dict[str, Any] = {}
    for key, value in graph.graph.items():
        if key in {"node_default", "edge_default"} and isinstance(value, Mapping):
            graph_attrs[str(key)] = _normalize_attributes(value)
        elif value is not None:
            graph_attrs[str(key)] = _normalize_value(value)
    normalized.graph.clear()
    normalized.graph.update(graph_attrs)

    for node, original_attrs in graph.nodes(data=True):
        attrs = dict(original_attrs)
        attrs["title"] = attrs.get("title") or str(node)
        attrs["type"] = attrs.get("type") or "concept"
        attrs["source"] = _primary_source(graph, attrs)
        attrs["theorist"] = (
            attrs.get("theorist") or attrs.get("source_theorist") or "unknown"
        )
        normalized.nodes[node].clear()
        normalized.nodes[node].update(_normalize_attributes(attrs))

    for source, target, original_attrs in graph.edges(data=True):
        attrs = dict(original_attrs)
        attrs.setdefault("source", _primary_source(graph, attrs))
        normalized[source][target].clear()
        normalized[source][target].update(_normalize_attributes(attrs))

    return normalized


def _community_subgraph(graph: nx.Graph, community_id: int) -> nx.Graph:
    """Return the induced Louvain community subgraph."""
    try:
        from community import community_louvain
    except ImportError as exc:
        raise RuntimeError(
            "--community requires the existing python-louvain dependency"
        ) from exc

    try:
        # v3.9.0: fixed seed for reproducibility
        partition = community_louvain.best_partition(
            graph.to_undirected(), random_state=42
        )
    except Exception as exc:
        raise ValueError(f"could not compute communities: {exc}") from exc

    selected = [node for node, value in partition.items() if value == community_id]
    if not selected:
        available = ", ".join(str(value) for value in sorted(set(partition.values())))
        suffix = f"; available communities: {available}" if available else ""
        raise ValueError(f"community {community_id} not found{suffix}")

    result = graph.subgraph(selected).copy()
    nx.set_node_attributes(
        result,
        {node: partition[node] for node in result.nodes()},
        "community",
    )
    return result


def _hex_to_gexf_color(color: str) -> dict[str, float | int]:
    """Convert #RRGGBB to NetworkX's GEXF viz:color representation."""
    value = color.removeprefix("#")
    return {
        "r": int(value[0:2], 16),
        "g": int(value[2:4], 16),
        "b": int(value[4:6], 16),
        "a": 1.0,
    }


def _add_gexf_colors(graph: nx.Graph) -> None:
    """Add NetworkX viz metadata, emitted as GEXF viz:color elements."""
    for _node, attrs in graph.nodes(data=True):
        source = str(attrs.get("source", "unknown")).lower()
        color = SOURCE_COLORS.get(source, SOURCE_COLORS["unknown"])
        attrs["viz"] = {"color": _hex_to_gexf_color(color)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export a wiki-graph knowledge graph as GraphML or GEXF."
    )
    parser.add_argument(
        "--source",
        default="wiki",
        help="wiki, ontology, ragflow, or comma-separated sources (default: wiki)",
    )
    parser.add_argument(
        "--format",
        required=True,
        choices=("graphml", "gexf"),
        help="Standard graph format to write",
    )
    parser.add_argument("--output", required=True, help="Output GraphML or GEXF path")
    parser.add_argument(
        "--community",
        type=int,
        metavar="N",
        help="Export only Louvain community N",
    )
    parser.add_argument(
        "--wiki-root",
        default=os.environ.get("WIKI_ROOT", os.getcwd()),
        help="Wiki root (default: WIKI_ROOT or current directory)",
    )
    parser.add_argument(
        "--ontology-path",
        default=os.environ.get("ONTOLOGY_PATH", str(DEFAULT_ONTOLOGY_PATH)),
        help="Ontology graph JSONL path",
    )
    parser.add_argument(
        "--ragflow-path",
        help="Offline ragflow JSONL file or chunk JSON directory",
    )
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()

    try:
        graph, _meta = build_graph(
            source=args.source,
            wiki_root=args.wiki_root,
            ontology_path=args.ontology_path,
            ragflow_path=args.ragflow_path,
        )
        if graph.number_of_nodes() == 0:
            parser.error("graph is empty; nothing to export")
        if args.community is not None:
            graph = _community_subgraph(graph, args.community)
        exported = normalize_graph(graph)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        parser.error(str(exc))

    output = Path(args.output).expanduser()
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        if args.format == "graphml":
            nx.write_graphml(exported, output, encoding="utf-8")
        else:
            _add_gexf_colors(exported)
            nx.write_gexf(exported, output, encoding="utf-8", prettyprint=True)
    except (OSError, nx.NetworkXError, TypeError, ValueError) as exc:
        parser.error(f"could not write {output}: {exc}")

    community = f", community {args.community}" if args.community is not None else ""
    print(
        f"Wrote {output} ({exported.number_of_nodes()} nodes, "
        f"{exported.number_of_edges()} edges{community})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
