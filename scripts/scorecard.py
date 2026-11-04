#!/usr/bin/env python3
"""B3 quality scorecard construction for wiki-graph.

The builder accepts an already-built graph and an already-produced diagnosis.
It never invokes graph-diagnose itself, so callers can reuse pipeline results.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timezone


SCORECARD_SCHEMA_VERSION = "b3-scorecard-v1"

DEFAULT_THRESHOLDS = {
    "dead_end_warn": 0.10,
    "isolated_warn": 0.20,
    "link_rot_crit": 0.05,
    "health_score_crit": 50,
    "reciprocity_warn": 0.10,
    "density_low_warn": 0.01,
    "density_high_warn": 0.25,
}


def _ratio(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return round(float(numerator) / float(denominator), 6)


def _normalised_diversity(values: list[str]) -> float:
    """Return normalised Shannon entropy in the range 0..1."""
    counts = Counter(value for value in values if value and value != "unknown")
    total = sum(counts.values())
    if total == 0 or len(counts) <= 1:
        return 0.0
    entropy = -sum((count / total) * math.log(count / total) for count in counts.values())
    return round(entropy / math.log(len(counts)), 6)


def _diagnosis_counts(diagnosis_data: dict | None) -> tuple[dict[str, int], bool]:
    if not isinstance(diagnosis_data, dict):
        return {}, False
    diagnosis = diagnosis_data.get("diagnosis")
    if not isinstance(diagnosis, list):
        return {}, False
    counts = Counter(
        item.get("dimension") for item in diagnosis if isinstance(item, dict)
    )
    return {str(key): value for key, value in counts.items() if key}, True


def _graph_metrics(graph) -> dict:
    if graph is None:
        return {}
    nodes = graph.number_of_nodes()
    edges = graph.number_of_edges()
    dead_ends = sum(
        1 for node in graph.nodes()
        if graph.out_degree(node) == 0
        and graph.in_degree(node) > 0
        and not str(node).startswith("audit-logs/")
    )
    isolated = sum(1 for node in graph.nodes() if graph.in_degree(node) == 0)
    sponges = sum(
        1 for node in graph.nodes()
        if graph.in_degree(node) + graph.out_degree(node) >= 5
        and graph.in_degree(node) /
        (graph.in_degree(node) + graph.out_degree(node)) >= 0.75
    )
    outlink_nodes = sum(1 for node in graph.nodes() if graph.out_degree(node) > 0)
    reciprocal_edges = sum(
        1 for source, target in graph.edges() if graph.has_edge(target, source)
    )
    node_kinds = []
    for node in graph.nodes():
        attrs = graph.nodes[node]
        node_kinds.append(attrs.get("type") or attrs.get("theorist") or "unknown")

    density = 0.0
    if nodes > 1:
        density = edges / (nodes * (nodes - 1))

    return {
        "nodes": nodes,
        "edges": edges,
        "dead_ends": dead_ends,
        "isolated": isolated,
        "sponges": sponges,
        "density": round(density, 6),
        "outlink_coverage": _ratio(outlink_nodes, nodes),
        "reciprocity": _ratio(reciprocal_edges, edges),
        "type_diversity": _normalised_diversity(node_kinds),
    }


def _excess_penalty(value: float, threshold: float, weight: float,
                    cap: float) -> float:
    """Scale a penalty by threshold multiples above the threshold."""
    if threshold <= 0 or value <= threshold:
        return 0.0
    return min(cap, weight * ((value - threshold) / threshold))


def composite_health_score(structure: dict, quality: dict,
                           thresholds: dict | None = None) -> dict:
    """Calculate the 0..100 health score and A-F grade.

    Penalties:
      - dead ends: 20 points per threshold-multiple above dead_end_warn, cap 40
      - link rot: 15 points per threshold-multiple above link_rot_crit, cap 30
      - isolated nodes: ratio * 10, cap 10
      - reciprocity: 5 points when below reciprocity_warn
      - density: 5 points outside the configured low/high band
    """
    configured = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    dead_end_penalty = _excess_penalty(
        structure["dead_end_ratio"], configured["dead_end_warn"], 20, 40,
    )
    link_rot_penalty = _excess_penalty(
        structure["link_rot_ratio"], configured["link_rot_crit"], 15, 30,
    )
    isolated_penalty = min(10.0, structure["isolated_ratio"] * 10)
    reciprocity_penalty = (
        5.0 if quality["reciprocity"] < configured["reciprocity_warn"] else 0.0
    )
    density_penalty = 5.0 if (
        structure["density"] < configured["density_low_warn"]
        or structure["density"] > configured["density_high_warn"]
    ) else 0.0

    penalties = {
        "dead_end": round(dead_end_penalty, 2),
        "link_rot": round(link_rot_penalty, 2),
        "isolated": round(isolated_penalty, 2),
        "reciprocity": round(reciprocity_penalty, 2),
        "density": round(density_penalty, 2),
    }
    health_score = int(round(max(0.0, min(100.0, 100 - sum(penalties.values())))))
    if health_score >= 90:
        grade = "A"
    elif health_score >= 75:
        grade = "B"
    elif health_score >= 60:
        grade = "C"
    elif health_score >= 40:
        grade = "D"
    else:
        grade = "F"
    return {"health_score": health_score, "grade": grade, "penalties": penalties}


def evaluate_thresholds(structure: dict, quality: dict, composite: dict,
                        thresholds: dict, diagnosis_available: bool) -> dict:
    """Evaluate scorecard thresholds and derive canary risk."""
    critical = []
    warnings = []

    # dead_end_warn is a release gate: the development plan explicitly requires
    # dead_end_ratio > 0.10 to make canary eligibility high risk.
    if structure["dead_end_ratio"] > thresholds["dead_end_warn"]:
        critical.append("dead_end_ratio")
    if structure["link_rot_ratio"] > thresholds["link_rot_crit"]:
        critical.append("link_rot_ratio")
    if composite["health_score"] < thresholds["health_score_crit"]:
        critical.append("health_score")

    if structure["isolated_ratio"] > thresholds["isolated_warn"]:
        warnings.append("isolated_ratio")
    if quality["reciprocity"] < thresholds["reciprocity_warn"]:
        warnings.append("reciprocity")
    if (
        structure["density"] < thresholds["density_low_warn"]
        or structure["density"] > thresholds["density_high_warn"]
    ):
        warnings.append("density")
    if not diagnosis_available:
        warnings.append("diagnosis_unavailable")

    if critical:
        status = "critical"
        eligibility = "high_risk"
    elif warnings:
        status = "warning"
        eligibility = "moderate_risk"
    else:
        status = "green"
        eligibility = "low_risk"
    return {
        "status": status,
        "critical": critical,
        "warnings": warnings,
        "canary_eligibility": eligibility,
    }


def build_scorecard(graph=None, diagnosis_data: dict | None = None,
                    source: str | None = None, thresholds: dict | None = None,
                    legacy_scores: dict | None = None,
                    timestamp: str | None = None,
                    multi_source_scores: dict | None = None) -> dict:
    """Build a structured B3 scorecard from graph + existing diagnosis data."""
    configured = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    graph_values = _graph_metrics(graph)
    diagnosis_counts, diagnosis_available = _diagnosis_counts(diagnosis_data)
    graph_stats = (diagnosis_data or {}).get("graph_stats", {})

    nodes = graph_values.get("nodes", graph_stats.get("nodes", 0))
    edges = graph_values.get("edges", graph_stats.get("edges", 0))
    dead_ends = diagnosis_counts.get("D1", graph_values.get("dead_ends", 0))
    isolated = diagnosis_counts.get("D2", graph_values.get("isolated", 0))
    sponges = diagnosis_counts.get("D3", graph_values.get("sponges", 0))
    broken_links = diagnosis_counts.get("D4", 0)

    structure = {
        "dead_end_ratio": _ratio(dead_ends, nodes),
        "isolated_ratio": _ratio(isolated, nodes),
        "sponge_ratio": _ratio(sponges, nodes),
        "link_rot_ratio": _ratio(broken_links, edges + broken_links),
        "density": graph_values.get("density", graph_stats.get("density", 0) or 0),
    }

    reciprocity = graph_values.get("reciprocity")
    if reciprocity is None and diagnosis_available:
        d6 = next(
            (item for item in diagnosis_data.get("diagnosis", [])
             if item.get("dimension") == "D6"),
            {},
        )
        reciprocity = d6.get("evidence", {}).get("ratio", 0)
    quality = {
        "outlink_coverage": graph_values.get(
            "outlink_coverage", round(max(0.0, 1.0 - structure["dead_end_ratio"]), 6)
        ),
        "reciprocity": round(float(reciprocity or 0), 6),
        "type_diversity": graph_values.get("type_diversity", 0.0),
    }
    if multi_source_scores is None:
        fusion = (diagnosis_data or {}).get("multi_source", {})
        if fusion.get("status") == "computed":
            multi_source_scores = {
                "coverage_score": round(
                    float(fusion.get("D8", {}).get("wiki_coverage", 0)) * 10,
                ),
                "consistency_score": round(
                    float(fusion.get("D9", {}).get("consistency_ratio", 0)) * 10,
                ),
                "gain_score": round(
                    float(fusion.get("D10", {}).get("information_gain", 0)) * 10,
                ),
            }
    multi_source_scores = multi_source_scores or {
        "coverage_score": None,
        "consistency_score": None,
        "gain_score": None,
    }
    quality.update({
        key: multi_source_scores.get(key)
        for key in ("coverage_score", "consistency_score", "gain_score")
    })
    composite = composite_health_score(structure, quality, configured)
    evaluation = evaluate_thresholds(
        structure, quality, composite, configured, diagnosis_available,
    )

    generated_at = timestamp or datetime.now(timezone.utc).isoformat()
    date_part = generated_at[:10]
    return {
        "schema_version": SCORECARD_SCHEMA_VERSION,
        "baseline_id": f"baseline_{date_part}",
        "timestamp": generated_at,
        "source": source or (diagnosis_data or {}).get("source", "wiki"),
        "metrics": {
            "structure": structure,
            "quality": quality,
            "composite": composite,
        },
        "thresholds": configured,
        "threshold_evaluation": {
            "status": evaluation["status"],
            "critical": evaluation["critical"],
            "warnings": evaluation["warnings"],
        },
        "canary_eligibility": evaluation["canary_eligibility"],
        "legacy_scores": legacy_scores or {},
        "metadata": {
            "nodes": nodes,
            "edges": edges,
            "dead_ends": dead_ends,
            "isolated": isolated,
            "sponges": sponges,
            "broken_links": broken_links,
            "diagnosis_available": diagnosis_available,
        },
    }
