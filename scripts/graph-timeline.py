#!/usr/bin/env python3
"""Timeline trend visualization for wiki-graph data.

Reads history entries and baseline to generate a self-contained D3.js HTML
with health score line chart, per-metric small multiples, and node/edge trends.

Usage:
  python3 graph-timeline.py --source wiki --output timeline.html
  python3 graph-timeline.py --source wiki --json
  python3 graph-timeline.py --source wiki --offline  # inline D3.js
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# ── Configuration ──────────────────────────────────────────────────
D3_CDN_URL = "https://d3js.org/d3.v7.min.js"
D3_OFFLINE_URL = "https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js"
DEFAULT_HISTORY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".graph-history.jsonl",
)
DEFAULT_BASELINE_PATH = os.path.join(os.getcwd(), ".graph-baseline.json")


# ── Data Loading ───────────────────────────────────────────────────

def _load_history(history_path: str, limit: int | None = None) -> list[dict]:
    """Load history entries from JSONL file."""
    entries = []
    if not os.path.exists(history_path):
        return entries
    with open(history_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if limit and len(entries) > limit:
        entries = entries[-limit:]
    return entries


def _load_baseline(baseline_path: str) -> dict | None:
    """Load baseline scorecard file."""
    if not os.path.exists(baseline_path):
        return None
    with open(baseline_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _extract_timeline(
    history: list[dict], baseline: dict | None
) -> dict:
    """Extract timeline data points from history + baseline.

    Each data point:
      {date, health_score, grade, dead_end_ratio, isolated_ratio,
       sponge_ratio, link_rot_ratio, density, outlink_coverage,
       reciprocity, type_diversity, nodes, edges, components}
    """
    points = []

    def _parse_date(ts: str) -> str:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            return ts[:10] if len(ts) >= 10 else ts

    def _safe_float(v, default=0.0):
        if isinstance(v, (int, float)):
            return float(v)
        return default

    def _safe_int(v, default=0):
        if isinstance(v, (int, float)):
            return int(v)
        return default

    for entry in history:
        sc = entry.get("scorecard") or {}
        metrics = sc.get("metrics", {})
        structure = metrics.get("structure", {})
        quality = metrics.get("quality", {})
        health = metrics.get("health", {})
        sc_score = sc.get("health_score")
        if sc_score is None:
            sc_score = health.get("health_score")
        if sc_score is None:
            sc_score = entry.get("total_score", 0)

        points.append({
            "date": _parse_date(entry.get("timestamp", "")),
            "health_score": _safe_int(sc_score),
            "grade": sc.get("grade") or health.get("grade") or "?",
            "dead_end_ratio": _safe_float(structure.get("dead_end_ratio")),
            "isolated_ratio": _safe_float(structure.get("isolated_ratio")),
            "sponge_ratio": _safe_float(structure.get("sponge_ratio")),
            "link_rot_ratio": _safe_float(structure.get("link_rot_ratio")),
            "density": _safe_float(structure.get("density")),
            "outlink_coverage": _safe_float(quality.get("outlink_coverage")),
            "reciprocity": _safe_float(quality.get("reciprocity")),
            "type_diversity": _safe_float(quality.get("type_diversity")),
            "nodes": _safe_int(entry.get("nodes")),
            "edges": _safe_int(entry.get("edges")),
            "components": _safe_int(entry.get("components")),
        })

    # Add baseline as latest point if newer than last history entry
    if baseline:
        b_ts = baseline.get("timestamp", "")
        b_date = _parse_date(b_ts)
        b_metrics = baseline.get("metrics", {})
        b_structure = b_metrics.get("structure", {})
        b_quality = b_metrics.get("quality", {})
        b_health = b_metrics.get("health", {})
        b_legacy = baseline.get("legacy_scores", {})
        b_meta = baseline.get("metadata", {})

        hs = b_health.get("health_score") or (
            baseline.get("health_score")
        )
        if hs is None and b_legacy:
            hs = b_legacy.get("total")

        b_point = {
            "date": b_date,
            "health_score": _safe_int(hs),
            "grade": b_health.get("grade") or "?",
            "dead_end_ratio": _safe_float(b_structure.get("dead_end_ratio")),
            "isolated_ratio": _safe_float(b_structure.get("isolated_ratio")),
            "sponge_ratio": _safe_float(b_structure.get("sponge_ratio")),
            "link_rot_ratio": _safe_float(b_structure.get("link_rot_ratio")),
            "density": _safe_float(b_structure.get("density")),
            "outlink_coverage": _safe_float(b_quality.get("outlink_coverage")),
            "reciprocity": _safe_float(b_quality.get("reciprocity")),
            "type_diversity": _safe_float(b_quality.get("type_diversity")),
            "nodes": _safe_int(b_meta.get("nodes")),
            "edges": _safe_int(b_meta.get("edges")),
            "components": _safe_int(b_meta.get("components", b_legacy.get("components", 0))),
        }
        points.append(b_point)

    # Deduplicate by date
    seen = {}
    for p in points:
        d = p["date"]
        if d not in seen:
            seen[d] = p
    points = sorted(seen.values(), key=lambda x: x["date"])

    return {
        "points": points,
        "entry_count": len(points),
        "metrics": [
            "health_score", "dead_end_ratio", "isolated_ratio",
            "sponge_ratio", "link_rot_ratio",
        ],
        "meta": {
            "source": baseline.get("source", "wiki") if baseline else "wiki",
            "baseline_id": baseline.get("baseline_id", "") if baseline else "",
        },
    }


# ── D3.js Download ─────────────────────────────────────────────────

def _download_d3() -> str:
    scheme = urllib.parse.urlparse(D3_OFFLINE_URL).scheme.lower()
    if scheme not in ("http", "https"):
        raise RuntimeError(
            "D3 source must be served over http/https, got %r" % (scheme,)
        )
    request = urllib.request.Request(
        D3_OFFLINE_URL,
        headers={"User-Agent": "wiki-graph-timeline/1.0"},
    )
    try:
        # Scheme allow-listed immediately above.
        with urllib.request.urlopen(  # nosec B310
            request, timeout=30
        ) as response:
            source = response.read().decode("utf-8")
    except (OSError, UnicodeError, urllib.error.URLError) as exc:
        raise RuntimeError(
            f"Could not download D3.js v7 from {D3_OFFLINE_URL}: {exc}"
        ) from exc
    if len(source) < 100_000 or "d3" not in source[:500].lower():
        raise RuntimeError("Downloaded response did not look like D3.js v7")
    return source


# ── JSON Output ────────────────────────────────────────────────────

def _json_for_script(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


# ── HTML Template ──────────────────────────────────────────────────

TIMELINE_TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__DOCUMENT_TITLE__</title>
  <style>
    :root {
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif;
      color: #20262e;
      background: #f4f6f8;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; padding: 24px;
      background: #f4f6f8;
      min-width: 800px;
    }
    .header {
      max-width: 1280px; margin: 0 auto 24px;
    }
    .header h1 { margin: 0 0 4px; font-size: 22px; font-weight: 700; }
    .header .meta { font-size: 13px; color: #5f6c7a; }
    .notice {
      max-width: 1280px; margin: 0 auto 16px;
      padding: 10px 14px; border-radius: 6px;
      background: #fff4e5; color: #8a6d3b;
      font-size: 13px; border: 1px solid #ffe8c2;
    }
    .grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }
    .card {
      background: #fff; border-radius: 8px;
      box-shadow: 0 1px 4px rgba(24,35,48,0.08);
      padding: 16px;
    }
    .card.full { grid-column: 1 / -1; }
    .card h3 {
      margin: 0 0 12px; font-size: 14px; font-weight: 600;
      color: #3a4552; text-transform: uppercase; letter-spacing: 0.5px;
    }
    .card svg { display: block; width: 100%; }
    .tooltip {
      position: fixed; z-index: 100;
      pointer-events: none;
      background: rgba(24,35,48,0.92);
      color: #fff; padding: 8px 12px;
      border-radius: 6px; font-size: 12px;
      line-height: 1.5; opacity: 0;
      transition: opacity 0.15s;
      white-space: nowrap;
    }
    .tooltip.visible { opacity: 1; }
    .tooltip .label { font-weight: 600; color: #83c5f7; }
    .brush .selection { fill: rgba(42,109,181,0.15); stroke: #2a6db5; }
    .brush .handle { fill: #2a6db5; }
    .axis text { font-size: 11px; fill: #5f6c7a; }
    .axis path, .axis line { stroke: #d0d5dc; }
    .gridline { stroke: #e8ebef; stroke-dasharray: 3 3; }
    .legend { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 8px; }
    .legend-item { display: flex; align-items: center; gap: 6px; font-size: 12px; }
    .legend-swatch { width: 12px; height: 12px; border-radius: 3px; }
  </style>
</head>
<body>
<div class="header">
  <h1>__DOCUMENT_TITLE__</h1>
  <div class="meta">__META_TEXT__</div>
</div>
<div class="notice" id="notice" style="display:__NOTICE_DISPLAY__;">__NOTICE__</div>

<div class="grid" style="max-width:1280px; margin:0 auto;">

  <!-- Health Score -->
  <div class="card full" id="card-health">
    <h3>Graph Health Score</h3>
    <div class="legend" id="legend-health"></div>
    <svg id="chart-health"></svg>
  </div>

  <!-- Per-metric small multiples -->
  <div class="card" id="card-deadend">
    <h3>Dead-End Ratio</h3>
    <svg id="chart-deadend"></svg>
  </div>
  <div class="card" id="card-linkrot">
    <h3>Link-Rot Ratio</h3>
    <svg id="chart-linkrot"></svg>
  </div>
  <div class="card" id="card-isolated">
    <h3>Isolated Ratio</h3>
    <svg id="chart-isolated"></svg>
  </div>
  <div class="card" id="card-sponge">
    <h3>Sponge Ratio</h3>
    <svg id="chart-sponge"></svg>
  </div>

  <!-- Node/Edge dual line -->
  <div class="card full" id="card-size">
    <h3>Graph Size — Nodes &amp; Edges</h3>
    <div class="legend" id="legend-size"></div>
    <svg id="chart-size"></svg>
  </div>

  <!-- Brush timeline -->
  <div class="card full" id="card-brush" style="padding:8px 16px 12px;">
    <svg id="chart-brush"></svg>
  </div>

</div>

<div class="tooltip" id="tooltip"></div>

__D3_SCRIPT__
<script id="timeline-data" type="application/json">__TIMELINE_DATA__</script>
<script>
(function() {
  var data = JSON.parse(document.getElementById("timeline-data").textContent);
  var points = data.points;
  if (!points || points.length < 2) {
    document.getElementById("notice").style.display = "block";
    document.getElementById("notice").textContent = "Need at least 2 data points for timeline visualization.";
    return;
  }
  document.getElementById("notice").style.display = "none";

  var tooltip = d3.select("#tooltip");
  function showTooltip(event, html) {
    tooltip.html(html)
      .style("left", (event.pageX + 14) + "px")
      .style("top", (event.pageY - 10) + "px")
      .classed("visible", true);
  }
  function hideTooltip() { tooltip.classed("visible", false); }

  // Parse dates
  var parseDate = d3.timeParse("%Y-%m-%d");
  points.forEach(function(d) {
    d.parsed = parseDate(d.date);
    d.health_score = +d.health_score;
    d.dead_end_ratio = +d.dead_end_ratio;
    d.isolated_ratio = +d.isolated_ratio;
    d.sponge_ratio = +d.sponge_ratio;
    d.link_rot_ratio = +d.link_rot_ratio;
    d.nodes = +d.nodes;
    d.edges = +d.edges;
  });
  points.sort(function(a,b) { return a.parsed - b.parsed; });

  // Shared x-scale (will be updated by brush)
  var xExtent = d3.extent(points, function(d) { return d.parsed; });
  var fullX = d3.scaleTime().domain(xExtent);
  var activeX = fullX;

  // ── Chart dimensions ─────────────────────────────────────
  function chartDims(id, height, margin) {
    var el = document.getElementById(id);
    if (!el) return null;
    var w = el.parentElement.clientWidth - 32;
    return {
      width: w,
      height: height,
      margin: margin || {top: 8, right: 24, bottom: 28, left: 48}
    };
  }

  function makeAxes(svg, dims, x, y, yLabel, yFormat, gridY) {
    var g = svg.append("g")
      .attr("transform", "translate(" + dims.margin.left + "," + dims.margin.top + ")");
    var xAxis = d3.axisBottom(x).ticks(6).tickFormat(d3.timeFormat("%m/%d"));
    var yAxis = d3.axisLeft(y).ticks(5);
    if (yFormat) yAxis.tickFormat(yFormat);
    g.append("g").attr("class", "axis")
      .attr("transform", "translate(0," + (dims.height - dims.margin.top - dims.margin.bottom) + ")")
      .call(xAxis);
    g.append("g").attr("class", "axis").call(yAxis);
    if (yLabel) {
      g.append("text").attr("transform", "rotate(-90)")
        .attr("y", -38).attr("x", -(dims.height - dims.margin.top - dims.margin.bottom) / 2)
        .attr("dy", "0.35em").style("text-anchor", "middle")
        .style("font-size", "11px").style("fill", "#5f6c7a")
        .text(yLabel);
    }
    if (gridY) {
      g.append("g").attr("class", "gridline")
        .call(d3.axisLeft(y).ticks(5).tickSize(-(dims.width - dims.margin.left - dims.margin.right)).tickFormat(""));
    }
    return g;
  }

  // ── 1. Health Score Line Chart ─────────────────────────────
  (function() {
    var dims = chartDims("chart-health", 300, {top: 8, right: 24, bottom: 28, left: 52});
    if (!dims) return;
    var innerW = dims.width - dims.margin.left - dims.margin.right;
    var innerH = dims.height - dims.margin.top - dims.margin.bottom;
    var svg = d3.select("#chart-health")
      .attr("viewBox", "0 0 " + dims.width + " " + dims.height)
      .attr("preserveAspectRatio", "xMidYMid meet");

    var y = d3.scaleLinear().domain([0, 100]).range([innerH, 0]);
    var gradZone = svg.append("defs").selectAll("rect")
      .data([
        {y0: 0, y1: 40, c: "#f8d7da", label: "F (0-40)"},
        {y0: 40, y1: 60, c: "#ffe8d0", label: "D (40-60)"},
        {y0: 60, y1: 75, c: "#fff4cc", label: "C (60-75)"},
        {y0: 75, y1: 90, c: "#d4edda", label: "B (75-90)"},
        {y0: 90, y1: 100, c: "#cce5ff", label: "A (90-100)"}
      ]);
    var gradeBg = svg.append("g")
      .attr("transform", "translate(" + dims.margin.left + "," + dims.margin.top + ")");
    var gz = [
      {y0: 0, y1: 40, c: "#fce4e6", label: "F (0-40)"},
      {y0: 40, y1: 60, c: "#fff0e0", label: "D (40-60)"},
      {y0: 60, y1: 75, c: "#fffce6", label: "C (60-75)"},
      {y0: 75, y1: 90, c: "#e6f5ea", label: "B (75-90)"},
      {y0: 90, y1: 100, c: "#e0f0ff", label: "A (90-100)"}
    ];
    gz.forEach(function(z) {
      gradeBg.append("rect")
        .attr("x", 0).attr("y", y(z.y1))
        .attr("width", innerW).attr("height", y(z.y0) - y(z.y1))
        .attr("fill", z.c);
    });

    var g = makeAxes(svg, dims, activeX, y, "Health Score", null, true);

    var line = d3.line()
      .x(function(d) { return activeX(d.parsed); })
      .y(function(d) { return y(d.health_score); })
      .curve(d3.curveMonotoneX);

    var path = g.append("path").datum(points)
      .attr("fill", "none").attr("stroke", "#2a6db5")
      .attr("stroke-width", 2.5).attr("d", line);

    // Dots
    var dots = g.selectAll(".dot-health").data(points);
    dots.enter().append("circle").attr("class", "dot-health")
      .attr("cx", function(d) { return activeX(d.parsed); })
      .attr("cy", function(d) { return y(d.health_score); })
      .attr("r", 5).attr("fill", "#2a6db5")
      .attr("stroke", "#fff").attr("stroke-width", 2)
      .on("mouseover", function(event, d) {
        showTooltip(event,
          "<span class='label'>" + d.date + "</span><br>" +
          "Health: <b>" + d.health_score + " (" + d.grade + ")</b>"
        );
      }).on("mouseout", hideTooltip);

    // Phase labels
    var lastPoint = points[points.length - 1];
    g.append("text")
      .attr("x", activeX(lastPoint.parsed) + 6).attr("y", y(lastPoint.health_score) + 4)
      .style("font-size", "11px").style("font-weight", "600")
      .style("fill", "#2a6db5")
      .text(lastPoint.health_score + " " + lastPoint.grade);

    // Legend
    var leg = d3.select("#legend-health").html("");
    gz.reverse().forEach(function(z) {
      leg.append("div").attr("class", "legend-item")
        .html("<span class='legend-swatch' style='background:" + z.c + "'></span>" + z.label);
    });
  })();

  // ── 2. Small Multiples ──────────────────────────────────────
  var smallMetrics = [
    {id: "deadend", key: "dead_end_ratio", label: "Dead-End Ratio", fmt: ".1%", maxDomain: null},
    {id: "linkrot", key: "link_rot_ratio", label: "Link-Rot Ratio", fmt: ".1%", maxDomain: null},
    {id: "isolated", key: "isolated_ratio", label: "Isolated Ratio", fmt: ".1%", maxDomain: null},
    {id: "sponge", key: "sponge_ratio", label: "Sponge Ratio", fmt: ".1%", maxDomain: null},
  ];

  smallMetrics.forEach(function(m) {
    var dims = chartDims("chart-" + m.id, 200);
    if (!dims) return;
    var innerW = dims.width - dims.margin.left - dims.margin.right;
    var innerH = dims.height - dims.margin.top - dims.margin.bottom;
    var svg = d3.select("#chart-" + m.id)
      .attr("viewBox", "0 0 " + dims.width + " " + dims.height)
      .attr("preserveAspectRatio", "xMidYMid meet");

    var vals = points.map(function(d) { return +d[m.key]; });
    var yMin = Math.max(0, d3.min(vals) * 0.5);
    var yMax = m.maxDomain || (d3.max(vals) * 1.2);
    if (yMax - yMin < 0.001) yMax = yMin + 0.01;
    var y = d3.scaleLinear().domain([yMin, yMax]).range([innerH, 0]);
    var g = makeAxes(svg, dims, activeX, y, m.label, d3.format(m.fmt), true);

    var line = d3.line()
      .x(function(d) { return activeX(d.parsed); })
      .y(function(d) { return y(d[m.key]); })
      .curve(d3.curveMonotoneX);

    var colors = ["#e07b3c", "#9b59b6", "#2ecc71", "#e74c3c"];
    var ci = smallMetrics.indexOf(m);

    g.append("path").datum(points)
      .attr("fill", "none").attr("stroke", colors[ci])
      .attr("stroke-width", 2).attr("d", line);

    g.selectAll(".dot-" + m.id).data(points).enter()
      .append("circle").attr("class", "dot-" + m.id)
      .attr("cx", function(d) { return activeX(d.parsed); })
      .attr("cy", function(d) { return y(d[m.key]); })
      .attr("r", 4).attr("fill", colors[ci])
      .attr("stroke", "#fff").attr("stroke-width", 1.5)
      .on("mouseover", function(event, d) {
        showTooltip(event,
          "<span class='label'>" + d.date + "</span><br>" +
          m.label + ": <b>" + d3.format(m.fmt)(d[m.key]) + "</b>"
        );
      }).on("mouseout", hideTooltip);
  });

  // ── 3. Node/Edge Dual Line ──────────────────────────────────
  (function() {
    var dims = chartDims("chart-size", 220, {top: 8, right: 52, bottom: 28, left: 52});
    if (!dims) return;
    var innerW = dims.width - dims.margin.left - dims.margin.right;
    var innerH = dims.height - dims.margin.top - dims.margin.bottom;
    var svg = d3.select("#chart-size")
      .attr("viewBox", "0 0 " + dims.width + " " + dims.height)
      .attr("preserveAspectRatio", "xMidYMid meet");

    var yLeft = d3.scaleLinear()
      .domain([0, d3.max(points, function(d) { return d.nodes; }) * 1.15])
      .range([innerH, 0]);
    var yRight = d3.scaleLinear()
      .domain([0, d3.max(points, function(d) { return d.edges; }) * 1.15])
      .range([innerH, 0]);

    var g = svg.append("g")
      .attr("transform", "translate(" + dims.margin.left + "," + dims.margin.top + ")");
    g.append("g").attr("class", "gridline")
      .call(d3.axisLeft(yLeft).ticks(5).tickSize(-innerW).tickFormat(""));
    g.append("g").attr("class", "axis").call(d3.axisLeft(yLeft).ticks(5));
    g.append("g").attr("class", "axis")
      .attr("transform", "translate(" + innerW + ",0)").call(d3.axisRight(yRight).ticks(5));
    g.append("g").attr("class", "axis")
      .attr("transform", "translate(0," + innerH + ")")
      .call(d3.axisBottom(activeX).ticks(6).tickFormat(d3.timeFormat("%m/%d")));

    g.append("text").attr("transform", "rotate(-90)")
      .attr("y", -42).attr("x", -innerH/2).attr("dy", "0.35em")
      .style("text-anchor", "middle").style("font-size", "11px")
      .style("fill", "#2a6db5").text("Nodes");
    g.append("text").attr("transform", "rotate(-90)")
      .attr("y", innerW + 36).attr("x", -innerH/2).attr("dy", "0.35em")
      .style("text-anchor", "middle").style("font-size", "11px")
      .style("fill", "#e07b3c").text("Edges");

    var lineN = d3.line()
      .x(function(d) { return activeX(d.parsed); })
      .y(function(d) { return yLeft(d.nodes); })
      .curve(d3.curveMonotoneX);
    var lineE = d3.line()
      .x(function(d) { return activeX(d.parsed); })
      .y(function(d) { return yRight(d.edges); })
      .curve(d3.curveMonotoneX);

    g.append("path").datum(points).attr("fill", "none")
      .attr("stroke", "#2a6db5").attr("stroke-width", 2).attr("d", lineN);
    g.append("path").datum(points).attr("fill", "none")
      .attr("stroke", "#e07b3c").attr("stroke-width", 2).attr("d", lineE);

    g.selectAll(".dot-nodes").data(points).enter()
      .append("circle").attr("class", "dot-nodes")
      .attr("cx", function(d) { return activeX(d.parsed); })
      .attr("cy", function(d) { return yLeft(d.nodes); })
      .attr("r", 4).attr("fill", "#2a6db5")
      .on("mouseover", function(event, d) {
        showTooltip(event,
          "<span class='label'>" + d.date + "</span><br>Nodes: <b>" + d.nodes + "</b>"
        );
      }).on("mouseout", hideTooltip);
    g.selectAll(".dot-edges").data(points).enter()
      .append("circle").attr("class", "dot-edges")
      .attr("cx", function(d) { return activeX(d.parsed); })
      .attr("cy", function(d) { return yRight(d.edges); })
      .attr("r", 4).attr("fill", "#e07b3c")
      .on("mouseover", function(event, d) {
        showTooltip(event,
          "<span class='label'>" + d.date + "</span><br>Edges: <b>" + d.edges + "</b>"
        );
      }).on("mouseout", hideTooltip);

    d3.select("#legend-size").html(
      "<div class='legend-item'><span class='legend-swatch' style='background:#2a6db5'></span>Nodes</div>" +
      "<div class='legend-item'><span class='legend-swatch' style='background:#e07b3c'></span>Edges</div>"
    );
  })();

  // ── 4. Brush for Zoom ───────────────────────────────────────
  (function() {
    var dims = chartDims("chart-brush", 60, {top: 4, right: 24, bottom: 20, left: 52});
    if (!dims) return;
    var svg = d3.select("#chart-brush")
      .attr("viewBox", "0 0 " + dims.width + " " + dims.height)
      .attr("preserveAspectRatio", "xMidYMid meet");
    var brushG = svg.append("g")
      .attr("transform", "translate(" + dims.margin.left + "," + dims.margin.top + ")");
    var bw = dims.width - dims.margin.left - dims.margin.right;
    var bh = dims.height - dims.margin.top - dims.margin.bottom;

    brushG.append("g").attr("class", "axis")
      .attr("transform", "translate(0," + bh + ")")
      .call(d3.axisBottom(fullX).ticks(6).tickFormat(d3.timeFormat("%m/%d")));

    // Mini area
    brushG.append("path").datum(points)
      .attr("fill", "#e0e8f0").attr("stroke", "#b0c0d0")
      .attr("stroke-width", 1).attr("opacity", 0.7)
      .attr("d", d3.area()
        .x(function(d) { return fullX(d.parsed); })
        .y0(bh).y1(function(d) { return bh * (1 - d.health_score / 100); })
        .curve(d3.curveMonotoneX)
      );

    var brush = d3.brushX().extent([[0, 0], [bw, bh]])
      .on("brush end", brushed);
    brushG.append("g").attr("class", "brush").call(brush);

    function brushed(event) {
      if (event.selection) {
        activeX = d3.scaleTime()
          .domain(event.selection.map(fullX.invert))
          .range([0, bw]);
      } else {
        activeX = d3.scaleTime().domain(xExtent).range([0, bw]);
      }
      // Re-render all charts (simple approach: reload)
      // For simplicity, we update the fullX.domain and note that the
      // active charts were already rendered. In a real implementation
      // we'd re-render, but for now the brush acts as a visual aid
      // showing the full timeline context.
    }
  })();

})();
</script>
</body>
</html>
'''


# ── Rendering ──────────────────────────────────────────────────────

def _render_html(
    data: dict,
    offline: bool = False,
    title: str = "Wiki-Graph Timeline",
    notice: str = "",
) -> str:
    if offline:
        d3_source = _download_d3().replace("</script", "<\\/script")
        d3_script = f"<script>\n{d3_source}\n</script>"
    else:
        d3_script = f'<script src="{D3_CDN_URL}"></script>'

    meta = data.get("meta", {})
    meta_text = f"Data source: {meta.get('source','?')} · {data.get('entry_count',0)} data points · Baseline: {meta.get('baseline_id','none')}"
    notice_display = "block" if notice else "none"

    return (
        TIMELINE_TEMPLATE.replace("__DOCUMENT_TITLE__", html.escape(title, quote=True))
        .replace("__META_TEXT__", html.escape(meta_text, quote=True))
        .replace("__NOTICE_DISPLAY__", notice_display)
        .replace("__NOTICE__", html.escape(notice, quote=True))
        .replace("__D3_SCRIPT__", d3_script)
        .replace("__TIMELINE_DATA__", _json_for_script(data))
    )


# ── CLI ────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a D3.js timeline visualization of wiki-graph health trends."
    )
    parser.add_argument(
        "--source", default="wiki",
        help="wiki, ontology, ragflow, or comma-separated (default: wiki)",
    )
    parser.add_argument(
        "--output", default="timeline.html",
        help="Output HTML path (default: timeline.html)",
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="Inline D3.js v7 for offline viewing",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output timeline data as JSON (no HTML)",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max history entries to use (default: all)",
    )
    parser.add_argument(
        "--history-path", default=DEFAULT_HISTORY_PATH,
        help=f"Path to .graph-history.jsonl (default: {DEFAULT_HISTORY_PATH})",
    )
    parser.add_argument(
        "--baseline-path", default=DEFAULT_BASELINE_PATH,
        help=f"Path to .graph-baseline.json (default: {DEFAULT_BASELINE_PATH})",
    )
    parser.add_argument("--title", help="Document title (default: Wiki-Graph Timeline)")
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()

    history = _load_history(args.history_path, args.limit)
    baseline = _load_baseline(args.baseline_path)
    data = _extract_timeline(history, baseline)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    notice = ""
    if data["entry_count"] < 2:
        notice = (
            f"Need at least 2 data points for a timeline. "
            f"Currently have {data['entry_count']} point(s). "
            f"Run 'wiki_analyzer.py history-save --source {args.source}' "
            f"to collect more."
        )

    title = args.title or "Wiki-Graph Timeline"
    try:
        rendered = _render_html(data, offline=args.offline, title=title, notice=notice)
    except RuntimeError as exc:
        parser.error(str(exc))

    output = Path(args.output).expanduser()
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    except OSError as exc:
        parser.error(f"Could not write {output}: {exc}")
    print(f"Wrote {output} ({data['entry_count']} data points)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
