#!/usr/bin/env python3
"""Baseline diff comparison for wiki-graph.

Compares two baselines (or baseline vs current state) and outputs
structural, metric, and PageRank diffs in text, JSON, or HTML format.

Usage:
  python3 graph-diff.py --source wiki --baseline-a 2026-07-10 --baseline-b 2026-07-16
  python3 graph-diff.py --source wiki --baseline 2026-07-10          # vs current
  python3 graph-diff.py --source wiki --baseline-a E1 --baseline-b E7 --json
  python3 graph-diff.py --source wiki --baseline-a E1 --baseline-b E7 --html diff.html
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

# ── Configuration ──────────────────────────────────────────────────
DEFAULT_HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".graph-history.jsonl",
)
DEFAULT_BASELINE_PATH = os.path.join(os.getcwd(), ".graph-baseline.json")
DEFAULT_WIKI_ROOT = os.getcwd()
DEFAULT_ONTOLOGY_PATH = str(PROJECT_DIR / "examples" / "mini-ontology.jsonl")

D3_CDN_URL = "https://d3js.org/d3.v7.min.js"
D3_OFFLINE_URL = "https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js"

# Maps POSITIVE direction: higher is better
SCORECARD_METRICS = {
    "health_score": ("Health Score", True, "num", 0, 100),
    "grade": ("Grade", None, "grade", None, None),
    "dead_end_ratio": ("Dead-End Ratio", False, "pct", 0, 1),
    "isolated_ratio": ("Isolated Ratio", False, "pct", 0, 1),
    "sponge_ratio": ("Sponge Ratio", False, "pct", 0, 1),
    "link_rot_ratio": ("Link-Rot Ratio", False, "pct", 0, 1),
    "density": ("Density", True, "pct", 0, 1),
    "outlink_coverage": ("Outlink Coverage", True, "pct", 0, 1),
    "reciprocity": ("Reciprocity", True, "pct", 0, 1),
    "type_diversity": ("Type Diversity", True, "pct", 0, 1),
    "nodes": ("Node Count", True, "int", 0, None),
    "edges": ("Edge Count", True, "int", 0, None),
    "components": ("Components", True, "int", 0, None),
}


# ── Data Loading ───────────────────────────────────────────────────

def _load_history(history_path: str) -> list[dict]:
    entries = []
    if not os.path.exists(history_path):
        return entries
    with open(history_path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                e["_index"] = i + 1  # 1-indexed for reference
                entries.append(e)
            except json.JSONDecodeError:
                continue
    return entries


def _load_baseline(baseline_path: str) -> dict | None:
    if not os.path.exists(baseline_path):
        return None
    with open(baseline_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_entry(
    ref: str,
    history: list[dict],
    baseline: dict | None,
    source: str,
) -> dict | None:
    """Resolve a reference string to a data entry.

    Supported formats:
      - "E1", "E7" → history entries by 1-indexed position
      - "2026-07-10" → history entry by date match in timestamp
      - "baseline", "current" → current baseline file
    """
    ref_lower = ref.lower().strip()

    # History entry by index: E1, E2, ...
    if ref_lower.startswith("e") and ref_lower[1:].isdigit():
        idx = int(ref_lower[1:]) - 1
        if 0 <= idx < len(history):
            return history[idx]
        return None

    # Baseline / current
    if ref_lower in ("baseline", "current", "latest"):
        if baseline:
            baseline["_index"] = 0
            baseline["_label"] = "current"
            return baseline
        return None

    # Date match in history
    for e in history:
        ts = e.get("timestamp", "")
        if ts[:10] == ref[:10]:
            return e

    # Try as date match in baseline
    if baseline:
        bts = baseline.get("timestamp", "")
        if bts[:10] == ref[:10]:
            baseline["_index"] = 0
            baseline["_label"] = "current"
            return baseline

    return None


# ── Graph Reconstruction ───────────────────────────────────────────

def _build_current_graph(source: str) -> "tuple[Any, dict] | None":
    """Build current live graph from source files.

    Only works for current state (not historical entries).
    """
    try:
        import graph_loader
    except ImportError:
        return None

    try:
        G, meta = graph_loader.build_graph(
            source=source,
            wiki_root=DEFAULT_WIKI_ROOT,
            ontology_path=DEFAULT_ONTOLOGY_PATH,
        )
        return G, meta
    except Exception:
        return None


def _compute_pagerank(G: Any) -> dict[str, float]:
    """Compute PageRank (pure Python, avoids numpy/scipy ABI mismatch)."""
    nodes = list(G.nodes())
    n = len(nodes)
    if n == 0:
        return {}

    alpha = 0.85
    max_iter = 100
    tol = 1e-6

    out_degree = {node: max(1, G.out_degree(node)) for node in nodes}
    pr = {node: 1.0 / n for node in nodes}

    for _ in range(max_iter):
        new_pr: dict = {}
        for node in nodes:
            rank = (1 - alpha) / n
            for pred in G.predecessors(node):
                rank += alpha * pr[pred] / out_degree[pred]
            new_pr[node] = rank
        diff = sum(abs(new_pr[node] - pr[node]) for node in nodes)
        pr = new_pr
        if diff < tol:
            break
    return {str(k): float(v) for k, v in pr.items()}


def _compute_communities(G: Any) -> dict[str, int]:
    """Compute communities (greedy modularity, numpy-free)."""
    try:
        from networkx.algorithms.community import greedy_modularity_communities
        ug = G.to_undirected() if hasattr(G, "to_undirected") else G
        comms = list(greedy_modularity_communities(ug))
        part = {}
        for cid, nodeset in enumerate(comms):
            for node in nodeset:
                part[str(node)] = cid
        return part
    except Exception:
        return {}


# ── Metric Extraction ──────────────────────────────────────────────

def _extract_metrics(entry: dict) -> dict[str, Any]:
    """Extract a flat dict of all comparable metrics from an entry."""
    m: dict[str, Any] = {}

    # Health score
    sc = entry.get("scorecard") or {}
    hs = sc.get("health_score")
    if hs is None:
        hs = entry.get("health_score")
    if hs is None:
        hs = entry.get("total_score")
    m["health_score"] = hs

    # Grade
    grade = sc.get("grade")
    if grade is None:
        grade = (entry.get("metrics") or {}).get("health", {}).get("grade")
    m["grade"] = grade

    # Metric breakdown
    met = entry.get("metrics") or {}

    structure = met.get("structure", {})
    m["dead_end_ratio"] = structure.get("dead_end_ratio")
    m["isolated_ratio"] = structure.get("isolated_ratio")
    m["sponge_ratio"] = structure.get("sponge_ratio")
    m["link_rot_ratio"] = structure.get("link_rot_ratio")
    m["density"] = structure.get("density")

    quality = met.get("quality", {})
    m["outlink_coverage"] = quality.get("outlink_coverage")
    m["reciprocity"] = quality.get("reciprocity")
    m["type_diversity"] = quality.get("type_diversity")

    # Graph size
    md = entry.get("metadata") or {}
    m["nodes"] = entry.get("nodes") or md.get("nodes")
    m["edges"] = entry.get("edges") or md.get("edges")
    m["components"] = entry.get("components") or md.get("components", entry.get("component_count"))

    return m


def _entry_label(entry: dict) -> str:
    """Human-readable label for an entry."""
    if "_label" in entry:
        return entry["_label"]
    idx = entry.get("_index")
    ts = entry.get("timestamp", "")
    date = ts[:10] if len(ts) >= 10 else ts
    if idx:
        return f"E{idx} ({date})"
    return date or "unknown"


# ── Diff Computation ───────────────────────────────────────────────

def _diff_metrics(a: dict[str, Any], b: dict[str, Any]) -> list[dict]:
    """Compare metrics between two entries, return list of MetricDelta dicts."""
    deltas = []
    for key, (label, higher_better, fmt, rmin, rmax) in SCORECARD_METRICS.items():
        va = a.get(key)
        vb = b.get(key)
        if va is None or vb is None:
            continue
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            abs_delta = vb - va
            pct_delta = (abs_delta / abs(va)) * 100 if va != 0 else (100 if vb != 0 else 0)
            deltas.append({
                "key": key,
                "label": label,
                "a": va,
                "b": vb,
                "delta": abs_delta,
                "delta_pct": pct_delta,
                "direction": "+" if abs_delta > 0 else ("-" if abs_delta < 0 else "="),
                "improved": (
                    (abs_delta > 0 and higher_better) or (abs_delta < 0 and not higher_better)
                ) if higher_better is not None else None,
                "higher_better": higher_better,
                "fmt": fmt,
            })
        elif key == "grade":
            deltas.append({
                "key": "grade",
                "label": label,
                "a": va,
                "b": vb,
                "delta": None,
                "delta_pct": None,
                "direction": "=" if va == vb else "→",
                "improved": None,
                "higher_better": None,
                "fmt": "grade",
            })
    return deltas


def _diff_nodes(G_a: Any, G_b: Any) -> dict:
    """Compute node-level differences between two graphs."""
    nodes_a = set(str(n) for n in G_a.nodes())
    nodes_b = set(str(n) for n in G_b.nodes())

    added = sorted(nodes_b - nodes_a)
    removed = sorted(nodes_a - nodes_b)
    common = sorted(nodes_a & nodes_b)

    return {
        "added": added,
        "removed": removed,
        "common_count": len(common),
        "added_count": len(added),
        "removed_count": len(removed),
    }


def _diff_edges(G_a: Any, G_b: Any) -> dict:
    """Compute edge-level differences between two graphs."""
    edges_a = set((str(u), str(v)) for u, v in G_a.edges())
    edges_b = set((str(u), str(v)) for u, v in G_b.edges())

    added = sorted(str(e) for e in (edges_b - edges_a))
    removed = sorted(str(e) for e in (edges_a - edges_b))

    return {
        "added": added,
        "removed": removed,
        "added_count": len(added),
        "removed_count": len(removed),
    }


def _diff_pagerank(
    pr_a: dict[str, float],
    pr_b: dict[str, float],
    top_n: int = 10,
) -> dict:
    """Compute PageRank changes between two snapshots."""
    all_nodes = set(pr_a.keys()) | set(pr_b.keys())
    changes = []
    for node in all_nodes:
        va = pr_a.get(node, 0)
        vb = pr_b.get(node, 0)
        delta = vb - va
        pct = (delta / va * 100) if va > 0 else (100 if vb > 0 else 0)
        changes.append({
            "node": node,
            "pr_a": va,
            "pr_b": vb,
            "delta": delta,
            "delta_pct": pct,
        })

    changes.sort(key=lambda x: abs(x["delta"]), reverse=True)

    rising = [c for c in changes if c["delta"] > 0][:top_n]
    falling = [c for c in changes if c["delta"] < 0][:top_n]
    falling.sort(key=lambda x: x["delta"])  # most negative first

    return {
        "rising": rising,
        "falling": falling,
        "total_nodes_compared": len(all_nodes),
    }


def _diff_communities(comm_a: dict[str, int], comm_b: dict[str, int]) -> dict:
    """Compute community membership changes."""
    changed = []
    new_nodes = set(comm_b.keys()) - set(comm_a.keys())
    removed_nodes = set(comm_a.keys()) - set(comm_b.keys())
    common = set(comm_a.keys()) & set(comm_b.keys())

    for node in sorted(common):
        if comm_a[node] != comm_b[node]:
            changed.append({
                "node": node,
                "from_community": comm_a[node],
                "to_community": comm_b[node],
            })

    return {
        "changed_nodes": changed,
        "changed_count": len(changed),
        "new_nodes": sorted(new_nodes),
        "removed_nodes": sorted(removed_nodes),
        "new_count": len(new_nodes),
        "removed_count": len(removed_nodes),
    }


# ── Report Rendering ───────────────────────────────────────────────

def _grade_to_rank(grade: str) -> int:
    """Convert letter grade to numeric rank (higher = better)."""
    grade = str(grade).upper().strip()
    mapping = {
        "A+": 100, "A": 95, "A-": 90,
        "B+": 85, "B": 80, "B-": 75,
        "C+": 70, "C": 65, "C-": 60,
        "D+": 55, "D": 50, "D-": 45,
        "F": 30, "?": 0,
    }
    return mapping.get(grade, 0)


def _fmt_val(value: Any, fmt: str) -> str:
    """Format a metric value for display."""
    if fmt == "pct":
        return f"{value * 100:.2f}%" if isinstance(value, (int, float)) else str(value)
    elif fmt == "num":
        return f"{value:.1f}" if isinstance(value, float) else str(value)
    elif fmt == "int":
        return str(int(value)) if isinstance(value, (int, float)) else str(value)
    elif fmt == "grade":
        return str(value)
    return str(value)


def _render_text(
    a_label: str,
    b_label: str,
    metric_deltas: list[dict],
    node_diff: dict | None,
    edge_diff: dict | None,
    pr_diff: dict | None,
    comm_diff: dict | None,
) -> str:
    lines = []
    lines.append(f"Graph Diff: {a_label} → {b_label}")
    lines.append("=" * 60)

    # Summary
    health = next((d for d in metric_deltas if d["key"] == "health_score"), None)
    if health:
        arrow = "↑" if (health["direction"] == "+") else ("↓" if health["direction"] == "-" else "→")
        improved = " (improved)" if health.get("improved") is True else (" (declined)" if health.get("improved") is False else "")
        lines.append(f"Health: {health['a']} → {health['b']} {arrow}{health['delta']:+d}{improved}")

    grade = next((d for d in metric_deltas if d["key"] == "grade"), None)
    if grade:
        lines.append(f"Grade:  {grade['a']} → {grade['b']}")

    lines.append("")

    # Metric table
    lines.append("Metric Changes")
    lines.append("-" * 60)
    lines.append(f"{'Metric':<20} {'A':>10} {'B':>10} {'Delta':>10} {'%':>8} {'Trend':>6}")
    lines.append("-" * 60)

    for d in metric_deltas:
        if d["key"] == "grade":
            continue
        a_str = _fmt_val(d["a"], d["fmt"])
        b_str = _fmt_val(d["b"], d["fmt"])
        d_str = _fmt_val(d["delta"], d["fmt"])
        pct_str = f"{d['delta_pct']:+.1f}%" if d["delta_pct"] is not None else "—"
        trend = "▲" if d["direction"] == "+" else ("▼" if d["direction"] == "-" else "─")
        lines.append(f"{d['label']:<20} {a_str:>10} {b_str:>10} {d_str:>10} {pct_str:>8} {trend:>6}")

    lines.append("")

    # Structural
    if node_diff:
        lines.append(f"Node Diff: +{node_diff['added_count']} added, -{node_diff['removed_count']} removed")
        if node_diff["added"]:
            lines.append(f"  Added ({node_diff['added_count']}):")
            for n in node_diff["added"][:15]:
                lines.append(f"    + {n}")
            if node_diff["added_count"] > 15:
                lines.append(f"    ... and {node_diff['added_count'] - 15} more")
        if node_diff["removed"]:
            lines.append(f"  Removed ({node_diff['removed_count']}):")
            for n in node_diff["removed"][:15]:
                lines.append(f"    - {n}")
            if node_diff["removed_count"] > 15:
                lines.append(f"    ... and {node_diff['removed_count'] - 15} more")
        lines.append("")

    if edge_diff:
        lines.append(f"Edge Diff: +{edge_diff['added_count']} added, -{edge_diff['removed_count']} removed")
        lines.append("")

    # PageRank
    if pr_diff:
        lines.append(f"PageRank Changes (compared {pr_diff['total_nodes_compared']} nodes)")
        lines.append("-" * 60)
        if pr_diff["rising"]:
            lines.append("Top Rising:")
            for i, r in enumerate(pr_diff["rising"][:5], 1):
                lines.append(f"  {i}. {r['node']:<40} +{r['delta_pct']:+.1f}% ({r['pr_a']:.4f}→{r['pr_b']:.4f})")
        if pr_diff["falling"]:
            lines.append("Top Falling:")
            for i, f in enumerate(pr_diff["falling"][:5], 1):
                lines.append(f"  {i}. {f['node']:<40} {f['delta_pct']:+.1f}% ({f['pr_a']:.4f}→{f['pr_b']:.4f})")
        lines.append("")

    # Communities
    if comm_diff:
        lines.append(f"Community Changes: {comm_diff['changed_count']} nodes switched community")
        if comm_diff["changed_nodes"]:
            for c in comm_diff["changed_nodes"][:10]:
                lines.append(f"  {c['node']}: community {c['from_community']} → {c['to_community']}")
            if comm_diff["changed_count"] > 10:
                lines.append(f"  ... and {comm_diff['changed_count'] - 10} more")
        lines.append("")

    return "\n".join(lines)


# ── HTML Rendering ─────────────────────────────────────────────────

DIFF_HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Graph Diff: __TITLE__</title>
  <style>
    :root { font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif; color: #20262e; background: #f4f6f8; }
    * { box-sizing: border-box; }
    body { margin: 0; padding: 24px; background: #f4f6f8; min-width: 800px; }
    .header { max-width: 1280px; margin: 0 auto 24px; }
    .header h1 { margin: 0 0 4px; font-size: 22px; font-weight: 700; }
    .header .meta { font-size: 13px; color: #5f6c7a; }
    .card { background: #fff; border-radius: 8px; box-shadow: 0 1px 4px rgba(24,35,48,0.08); padding: 20px; margin-bottom: 16px; max-width: 1280px; margin-left: auto; margin-right: auto; }
    .card h3 { margin: 0 0 12px; font-size: 14px; font-weight: 600; color: #3a4552; text-transform: uppercase; letter-spacing: 0.5px; }
    .summary { display: flex; gap: 24px; align-items: center; }
    .score-box { text-align: center; padding: 16px 24px; border-radius: 8px; min-width: 120px; }
    .score-box.a { background: #2a6db5; color: #fff; }
    .score-box.b { background: #e07b3c; color: #fff; }
    .score-box .value { font-size: 32px; font-weight: 700; }
    .score-box .label { font-size: 12px; opacity: 0.85; margin-top: 2px; }
    .arrow { font-size: 24px; color: #5f6c7a; font-weight: 700; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { text-align: right; padding: 6px 10px; border-bottom: 1px solid #e8ebef; }
    th { font-weight: 600; color: #5f6c7a; font-size: 11px; text-transform: uppercase; }
    td.name { text-align: left; font-weight: 500; }
    td.improved { color: #27ae60; }
    td.declined { color: #e74c3c; }
    td.neutral { color: #5f6c7a; }
    .node-list { display: flex; flex-wrap: wrap; gap: 4px; }
    .node-tag { padding: 2px 8px; border-radius: 3px; font-size: 12px; font-weight: 500; }
    .node-tag.added { background: #e6f5ea; color: #27ae60; }
    .node-tag.removed { background: #fce4e6; color: #e74c3c; }
    .node-tag.changed { background: #fff4e5; color: #e07b3c; }
    .pr-row { display: flex; align-items: center; gap: 8px; margin: 3px 0; font-size: 13px; }
    .pr-bar-bg { flex: 1; height: 6px; background: #e8ebef; border-radius: 3px; overflow: hidden; }
    .pr-bar { height: 100%; border-radius: 3px; }
    .pr-bar.up { background: #27ae60; }
    .pr-bar.down { background: #e74c3c; }
    .pr-node { font-weight: 500; min-width: 180px; }
    .pr-val { min-width: 100px; text-align: right; font-size: 12px; color: #5f6c7a; }
    .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    .section-title { font-size: 13px; font-weight: 600; color: #3a4552; margin-bottom: 8px; }
    .empty { color: #9aa3b0; font-style: italic; font-size: 13px; padding: 12px 0; }
  </style>
</head>
<body>
<div class="header">
  <h1>Graph Diff: __TITLE__</h1>
  <div class="meta">__META__</div>
</div>

<!-- Summary Card -->
<div class="card">
  <h3>Health Summary</h3>
  <div class="summary">
    <div class="score-box a">
      <div class="value">__SCORE_A__</div>
      <div class="label">__GRADE_A__ · __LABEL_A__</div>
    </div>
    <div class="arrow">→</div>
    <div class="score-box b">
      <div class="value">__SCORE_B__</div>
      <div class="label">__GRADE_B__ · __LABEL_B__</div>
    </div>
    <div style="margin-left:16px">
      <span style="font-size:22px;font-weight:700;color:__TREND_COLOR__">__TREND_ARROW__ __TREND_PCT__</span>
      <div style="font-size:12px;color:#5f6c7a">health score change</div>
    </div>
  </div>
</div>

<!-- Metric Table -->
<div class="card">
  <h3>Metric Changes</h3>
  __METRIC_TABLE__
</div>

<!-- Structural Changes -->
__STRUCTURAL_CARD__

<!-- PageRank -->
__PR_CARD__

<!-- Communities -->
__COMM_CARD__

</body>
</html>
'''


def _render_html(
    a_label: str,
    b_label: str,
    metric_deltas: list[dict],
    node_diff: dict | None,
    edge_diff: dict | None,
    pr_diff: dict | None,
    comm_diff: dict | None,
    title: str = "Baseline Comparison",
) -> str:
    health = next((d for d in metric_deltas if d["key"] == "health_score"), None)
    grade = next((d for d in metric_deltas if d["key"] == "grade"), None)

    # Health scores
    score_a = str(int(health["a"])) if health else "?"
    score_b = str(int(health["b"])) if health else "?"
    grade_a = str(grade["a"]) if grade else "?"
    grade_b = str(grade["b"]) if grade else "?"

    # Trend
    if health and health["direction"] != "=":
        trend_arrow = "↑" if health["direction"] == "+" else "↓"
        trend_color = "#27ae60" if health.get("improved") else "#e74c3c"
        trend_pct = f"{'+' if health['direction'] == '+' else ''}{health['delta']}"
    else:
        trend_arrow = "→"
        trend_color = "#5f6c7a"
        trend_pct = "0"

    # Metric table rows
    metric_rows = []
    for d in metric_deltas:
        a_str = _fmt_val(d["a"], d["fmt"])
        b_str = _fmt_val(d["b"], d["fmt"])
        d_str = _fmt_val(d["delta"], d["fmt"])

        if d.get("improved") is True:
            td_class = "improved"
            arrow = "▲"
        elif d.get("improved") is False:
            td_class = "declined"
            arrow = "▼"
        else:
            td_class = "neutral"
            arrow = "─"

        pct_str = f"{d['delta_pct']:+.1f}%" if d["delta_pct"] is not None else "—"

        metric_rows.append(
            f"<tr>"
            f"<td class='name'>{html_mod.escape(d['label'])}</td>"
            f"<td>{html_mod.escape(a_str)}</td>"
            f"<td>{html_mod.escape(b_str)}</td>"
            f"<td class='{td_class}'>{arrow} {html_mod.escape(d_str)}</td>"
            f"<td class='{td_class}'>{pct_str}</td>"
            f"</tr>"
        )

    metric_table = (
        "<table>\n"
        "<tr><th>Metric</th><th>A</th><th>B</th><th>Delta</th><th>%</th></tr>\n"
        + "\n".join(metric_rows)
        + "\n</table>"
    )

    # Structural card
    structural_card = ""
    if node_diff or edge_diff:
        parts = []
        if node_diff:
            added_tags = " ".join(
                f"<span class='node-tag added'>+{html_mod.escape(n)}</span>"
                for n in node_diff["added"][:30]
            )
            removed_tags = " ".join(
                f"<span class='node-tag removed'>-{html_mod.escape(n)}</span>"
                for n in node_diff["removed"][:30]
            )
            more_added = (
                f" <span style='font-size:12px;color:#9aa3b0'>+{node_diff['added_count'] - 30} more</span>"
                if node_diff["added_count"] > 30 else ""
            )
            more_removed = (
                f" <span style='font-size:12px;color:#9aa3b0'>+{node_diff['removed_count'] - 30} more</span>"
                if node_diff["removed_count"] > 30 else ""
            )

            parts.append(
                "<div class='card'>"
                f"<h3>Node Changes (+{node_diff['added_count']} added, -{node_diff['removed_count']} removed)</h3>"
                + (f"<p><strong>Added:</strong></p><div class='node-list'>{added_tags}{more_added}</div>" if node_diff["added"] else "")
                + (f"<p><strong>Removed:</strong></p><div class='node-list'>{removed_tags}{more_removed}</div>" if node_diff["removed"] else "")
                + "</div>"
            )
        if edge_diff:
            parts.append(
                "<div class='card'>"
                f"<h3>Edge Changes (+{edge_diff['added_count']} added, -{edge_diff['removed_count']} removed)</h3>"
                + (f"<p>Added: {edge_diff['added_count']} edges</p>" if edge_diff["added_count"] else "")
                + (f"<p>Removed: {edge_diff['removed_count']} edges</p>" if edge_diff["removed_count"] else "")
                + "</div>"
            )
        structural_card = "\n".join(parts)

    # PageRank
    pr_card = ""
    if pr_diff and (pr_diff["rising"] or pr_diff["falling"]):
        def _pr_rows(items, cls, label, max_width=0.1):
            if max_width == 0:
                max_width = max((abs(i["delta"]) for i in items), default=1)
            rows = []
            for r in items:
                w = min(100, abs(r["delta_pct"]) * 3)
                rows.append(
                    f"<div class='pr-row'>"
                    f"<span class='pr-node'>{html_mod.escape(str(r['node'])[:40])}</span>"
                    f"<div class='pr-bar-bg'><div class='pr-bar {cls}' style='width:{w}%'></div></div>"
                    f"<span class='pr-val'>{r['delta_pct']:+.1f}% ({r['pr_a']:.4f}→{r['pr_b']:.4f})</span>"
                    f"</div>"
                )
            return f"<div class='section-title'>{label}</div>" + "\n".join(rows)

        pr_content = (
            _pr_rows(pr_diff["rising"], "up", "Top Rising") +
            _pr_rows(pr_diff["falling"], "down", "Top Falling")
        )
        pr_card = f"<div class='card'><h3>PageRank Changes</h3>{pr_content}</div>"

    # Communities
    comm_card = ""
    if comm_diff:
        parts = []
        if comm_diff["changed_nodes"]:
            tags = " ".join(
                f"<span class='node-tag changed'>{html_mod.escape(c['node'])} (c{c['from_community']}→c{c['to_community']})</span>"
                for c in comm_diff["changed_nodes"][:20]
            )
            more = (
                f" <span style='font-size:12px;color:#9aa3b0'>+{comm_diff['changed_count'] - 20} more</span>"
                if comm_diff['changed_count'] > 20 else ""
            )
            parts.append(f"<div class='node-list'>{tags}{more}</div>")
        if comm_diff["new_nodes"]:
            parts.append(f"<p><strong>New nodes:</strong> {', '.join(html_mod.escape(n) for n in comm_diff['new_nodes'][:20])}</p>")
        if comm_diff["removed_nodes"]:
            parts.append(f"<p><strong>Removed nodes:</strong> {', '.join(html_mod.escape(n) for n in comm_diff['removed_nodes'][:20])}</p>")
        if parts:
            comm_card = (
                f"<div class='card'><h3>Community Changes ({comm_diff['changed_count']} nodes switched)</h3>"
                + "\n".join(parts)
                + "</div>"
            )

    meta = f"{a_label} → {b_label}"

    return (
        DIFF_HTML_TEMPLATE.replace("__TITLE__", html_mod.escape(title, quote=True))
        .replace("__META__", html_mod.escape(meta))
        .replace("__SCORE_A__", score_a)
        .replace("__SCORE_B__", score_b)
        .replace("__GRADE_A__", grade_a)
        .replace("__GRADE_B__", grade_b)
        .replace("__LABEL_A__", html_mod.escape(a_label, quote=True))
        .replace("__LABEL_B__", html_mod.escape(b_label, quote=True))
        .replace("__TREND_ARROW__", trend_arrow)
        .replace("__TREND_COLOR__", trend_color)
        .replace("__TREND_PCT__", trend_pct)
        .replace("__METRIC_TABLE__", metric_table)
        .replace("__STRUCTURAL_CARD__", structural_card or '<div class="card"><p class="empty">No structural diff available (insufficient graph data in baselines).</p></div>')
        .replace("__PR_CARD__", pr_card or "")
        .replace("__COMM_CARD__", comm_card or "")
    )


# ── CLI ────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare two wiki-graph baselines and report differences."
    )
    parser.add_argument(
        "--source", default="wiki",
        help="Data source: wiki, ontology, ragflow, or comma-separated (default: wiki)",
    )
    parser.add_argument(
        "--baseline-a",
        help="Reference to first baseline (E1..EN, date, 'baseline')",
    )
    parser.add_argument(
        "--baseline-b",
        help="Reference to second baseline (E1..EN, date, 'baseline', default: current)",
    )
    parser.add_argument(
        "--baseline",
        help="Single baseline ref → compare with current state",
    )
    parser.add_argument(
        "--history-path", default=DEFAULT_HISTORY_PATH,
        help=f"Path to .graph-history.jsonl (default: {DEFAULT_HISTORY_PATH})",
    )
    parser.add_argument(
        "--baseline-path", default=DEFAULT_BASELINE_PATH,
        help=f"Path to .graph-baseline.json (default: {DEFAULT_BASELINE_PATH})",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output diff as JSON",
    )
    parser.add_argument(
        "--html",
        help="Output diff as HTML report (specify filename)",
    )
    parser.add_argument("--title", help="Document title")
    parser.add_argument(
        "--no-structural", action="store_true",
        help="Skip structural diff (faster, only compare metrics)",
    )
    parser.add_argument(
        "--top-n", type=int, default=10,
        help="Number of top PageRank changes to report (default: 10)",
    )
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()

    # Resolve entries
    history = _load_history(args.history_path)
    baseline = _load_baseline(args.baseline_path)

    if args.baseline and not args.baseline_a:
        entry_a = _resolve_entry(args.baseline, history, baseline, args.source)
        entry_b = _resolve_entry("current", history, baseline, args.source)
        if not entry_a:
            parser.error(f"Could not resolve baseline reference: {args.baseline}")
    elif args.baseline_a and args.baseline_b:
        entry_a = _resolve_entry(args.baseline_a, history, baseline, args.source)
        entry_b = _resolve_entry(args.baseline_b, history, baseline, args.source)
    elif args.baseline_a:
        entry_a = _resolve_entry(args.baseline_a, history, baseline, args.source)
        entry_b = _resolve_entry("current", history, baseline, args.source)
    else:
        # Default: E1 vs E2
        if len(history) >= 2:
            entry_a = history[0]
            entry_b = history[1]
        elif len(history) == 1:
            entry_a = history[0]
            entry_b = _resolve_entry("current", history, baseline, args.source)
        else:
            parser.error("No history entries found. Provide --baseline-a and --baseline-b.")

    if not entry_a:
        parser.error(f"Could not resolve baseline A reference: {args.baseline_a}")
    if not entry_b:
        parser.error(f"Could not resolve baseline B reference: {args.baseline_b or 'current'}")

    a_label = _entry_label(entry_a)
    b_label = _entry_label(entry_b)

    # Extract metrics
    metrics_a = _extract_metrics(entry_a)
    metrics_b = _extract_metrics(entry_b)
    metric_deltas = _diff_metrics(metrics_a, metrics_b)

    # Structural diff
    node_diff = None
    edge_diff = None
    pr_diff = None
    comm_diff = None

    if not args.no_structural:
        G_a = None
        G_b = None

        # Try to reconstruct graphs
        if entry_a.get("_label") == "current" or str(entry_a.get("_index")) == "0":
            result = _build_current_graph(args.source)
            if result:
                G_a, _ = result

        if entry_b.get("_label") == "current" or str(entry_b.get("_index")) == "0":
            result = _build_current_graph(args.source)
            if result:
                G_b, _ = result
                # If a is also current, reuse
                if G_a is None and G_b is not None:
                    pass  # can only diff if both available

        if G_a is not None and G_b is not None:
            node_diff = _diff_nodes(G_a, G_b)
            edge_diff = _diff_edges(G_a, G_b)
            pr_a = _compute_pagerank(G_a)
            pr_b = _compute_pagerank(G_b)
            if pr_a and pr_b:
                pr_diff = _diff_pagerank(pr_a, pr_b, args.top_n)
            comm_a = _compute_communities(G_a)
            comm_b = _compute_communities(G_b)
            if comm_a and comm_b:
                comm_diff = _diff_communities(comm_a, comm_b)

    # Output
    if args.json:
        output = {
            "a": {"label": a_label, "metrics": metrics_a},
            "b": {"label": b_label, "metrics": metrics_b},
            "deltas": metric_deltas,
            "structural": {
                "available": node_diff is not None,
                "nodes": node_diff,
                "edges": edge_diff,
                "pagerank": pr_diff,
                "communities": comm_diff,
            },
        }
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.html:
        title = args.title or f"{a_label} → {b_label}"
        rendered = _render_html(
            a_label, b_label, metric_deltas,
            node_diff, edge_diff, pr_diff, comm_diff,
            title=title,
        )
        out = Path(args.html).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")
        struct_note = " (structural diff available)" if node_diff else ""
        print(f"Wrote {out}{struct_note}")
        return 0

    # Text report
    print(_render_text(a_label, b_label, metric_deltas, node_diff, edge_diff, pr_diff, comm_diff))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
