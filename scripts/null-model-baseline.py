#!/usr/bin/env python3
"""Build a directed Erdos-Renyi null baseline for graph metrics."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections.abc import Sequence
from typing import Any

import networkx as nx
from scipy.stats import percentileofscore, spearmanr, t


BASE_SEED = 42


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate directed Erdos-Renyi G(n,m) graphs and summarize "
            "PageRank/in-degree Spearman correlation and reciprocity."
        )
    )
    parser.add_argument("--nodes", type=_positive_int, required=True)
    parser.add_argument("--edges", type=_non_negative_int, required=True)
    parser.add_argument("--runs", type=_positive_int, default=100)
    parser.add_argument(
        "--observed-rho",
        type=float,
        help="Observed PageRank/in-degree Spearman rho to percentile-rank",
    )
    return parser


def _spearman_pagerank_indegree(graph: nx.DiGraph) -> float | None:
    nodes = list(graph.nodes())
    pagerank = nx.pagerank(graph)
    pagerank_values = [pagerank[node] for node in nodes]
    in_degree_values = [graph.in_degree(node) for node in nodes]
    if len(nodes) < 2 or len(set(pagerank_values)) < 2 \
            or len(set(in_degree_values)) < 2:
        return None
    rho, _pvalue = spearmanr(pagerank_values, in_degree_values)
    rho = float(rho)
    return rho if math.isfinite(rho) else None


def _reciprocity_ratio(graph: nx.DiGraph) -> float:
    if graph.number_of_edges() == 0:
        return 0.0
    reciprocity = nx.reciprocity(graph)
    return float(reciprocity) if reciprocity is not None else 0.0


def _summary(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "mean": None,
            "std": None,
            "ci_95": [None, None],
        }

    sample_mean = statistics.fmean(values)
    sample_std = statistics.stdev(values) if len(values) > 1 else 0.0
    if len(values) > 1 and sample_std:
        critical = float(t.ppf(0.975, df=len(values) - 1))
        margin = critical * sample_std / math.sqrt(len(values))
    else:
        margin = 0.0

    return {
        "count": len(values),
        "mean": round(sample_mean, 6),
        "std": round(sample_std, 6),
        "ci_95": [
            round(sample_mean - margin, 6),
            round(sample_mean + margin, 6),
        ],
    }


def run_baseline(
    nodes: int,
    edges: int,
    runs: int,
    observed_rho: float | None = None,
) -> dict[str, Any]:
    rho_values: list[float] = []
    reciprocity_values: list[float] = []

    for run_index in range(runs):
        graph = nx.gnm_random_graph(
            nodes,
            edges,
            seed=BASE_SEED + run_index,
            directed=True,
        )
        rho = _spearman_pagerank_indegree(graph)
        if rho is not None:
            rho_values.append(rho)
        reciprocity_values.append(_reciprocity_ratio(graph))

    observed = None
    if observed_rho is not None:
        percentile = None
        if rho_values:
            percentile = float(percentileofscore(rho_values, observed_rho, kind="mean"))
        observed = {
            "rho": observed_rho,
            "percentile": round(percentile, 2) if percentile is not None else None,
        }

    return {
        "model": "directed_erdos_renyi_gnm",
        "parameters": {
            "nodes": nodes,
            "edges": edges,
            "runs": runs,
            "base_seed": BASE_SEED,
        },
        "pagerank_in_degree_spearman": _summary(rho_values),
        "reciprocity_ratio": _summary(reciprocity_values),
        "undefined_spearman_runs": runs - len(rho_values),
        "observed_rho": observed,
    }


def _format_metric(name: str, summary: dict[str, Any], percent: bool = False) -> str:
    if summary["mean"] is None:
        return f"{name}: undefined"
    scale = 100 if percent else 1
    suffix = "%" if percent else ""
    lower, upper = summary["ci_95"]
    return (
        f"{name}: {summary['mean'] * scale:.4f}{suffix} +/- "
        f"{summary['std'] * scale:.4f}{suffix} "
        f"(95% CI {lower * scale:.4f}{suffix} to {upper * scale:.4f}{suffix})"
    )


def _print_human_summary(result: dict[str, Any]) -> None:
    parameters = result["parameters"]
    lines = [
        "Null model baseline: directed Erdos-Renyi G(n,m)",
        (
            f"Graphs: {parameters['runs']} runs, {parameters['nodes']} nodes, "
            f"{parameters['edges']} edges, base seed {parameters['base_seed']}"
        ),
        _format_metric(
            "PageRank/in-degree Spearman rho",
            result["pagerank_in_degree_spearman"],
        ),
        _format_metric(
            "Reciprocity ratio",
            result["reciprocity_ratio"],
            percent=True,
        ),
    ]
    observed = result["observed_rho"]
    if observed is not None:
        percentile = observed["percentile"]
        percentile_text = f"{percentile:.2f}" if percentile is not None else "undefined"
        lines.append(
            f"Observed rho {observed['rho']:.4f}: {percentile_text}th percentile"
        )
    print("\n".join(lines), file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    max_edges = args.nodes * (args.nodes - 1)
    if args.edges > max_edges:
        parser.error(
            f"--edges must not exceed {max_edges} for a directed simple graph "
            f"with {args.nodes} nodes"
        )
    if args.observed_rho is not None and not -1 <= args.observed_rho <= 1:
        parser.error("--observed-rho must be between -1 and 1")

    result = run_baseline(
        nodes=args.nodes,
        edges=args.edges,
        runs=args.runs,
        observed_rho=args.observed_rho,
    )
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    _print_human_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
