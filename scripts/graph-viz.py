#!/usr/bin/env python3
"""Generate a self-contained interactive D3 visualization of a knowledge graph."""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import math
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping

import networkx as nx


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from graph_loader import build_graph  # noqa: E402


D3_CDN_URL = "https://d3js.org/d3.v7.min.js"
D3_OFFLINE_URL = "https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js"
DEFAULT_ONTOLOGY_PATH = SCRIPT_DIR.parents[2] / "memory" / "ontology" / "graph.jsonl"


def _load_compute_module() -> ModuleType:
    """Load graph-compute.py despite its non-importable filename."""
    module_path = SCRIPT_DIR / "graph-compute.py"
    spec = importlib.util.spec_from_file_location("wiki_graph_compute", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load compute module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_COMPUTE = _load_compute_module()
compute_pagerank = _COMPUTE.compute_pagerank
compute_communities = _COMPUTE.compute_communities


def _as_text(value: Any, default: str = "", limit: int | None = None) -> str:
    if value is None:
        text = default
    elif isinstance(value, str):
        text = value
    elif isinstance(value, (list, tuple, set)):
        text = ", ".join(str(item) for item in value)
    elif isinstance(value, Mapping):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)
    text = text.strip()
    if limit is not None and len(text) > limit:
        return f"{text[: max(0, limit - 3)].rstrip()}..."
    return text


def _as_list(value: Any) -> list[str]:
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
        name = str(item).strip().lower()
        if name and name not in result:
            result.append(name)
    return result


def _node_sources(graph: nx.DiGraph, attrs: Mapping[str, Any]) -> list[str]:
    sources = _as_list(attrs.get("sources") or attrs.get("source"))
    if not sources:
        sources = _as_list(graph.graph.get("sources") or graph.graph.get("source"))
    return sources or ["unknown"]


def _primary_source(sources: list[str]) -> str:
    # Merged ontology attributes are authoritative in graph_loader.
    if "ontology" in sources:
        return "ontology"
    return sources[0] if sources else "unknown"


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _graph_to_d3_json(
    graph: nx.DiGraph,
    meta: Mapping[Any, Any],
    pagerank: Mapping[Any, float] | None = None,
    partition: Mapping[Any, Any] | None = None,
) -> dict[str, Any]:
    """Convert a NetworkX graph into JSON-safe data for the D3 renderer."""
    pagerank = pagerank or {}
    partition = partition or {}
    nodes: list[dict[str, Any]] = []

    for node, graph_attrs in graph.nodes(data=True):
        meta_attrs = meta.get(node, {})
        attrs: dict[str, Any] = {}
        if isinstance(meta_attrs, Mapping):
            attrs.update(meta_attrs)
        attrs.update(graph_attrs)
        sources = _node_sources(graph, attrs)
        description = (
            attrs.get("description")
            or attrs.get("definition_text")
            or attrs.get("content")
            or ""
        )
        community = partition.get(node)
        if community is not None:
            try:
                community = int(community)
            except (TypeError, ValueError):
                community = _as_text(community)

        nodes.append({
            "id": str(node),
            "title": _as_text(attrs.get("title"), default=str(node)),
            "type": _as_text(attrs.get("type"), default="concept") or "concept",
            "source": _primary_source(sources),
            "sources": sources,
            "theorist": _as_text(
                attrs.get("theorist") or attrs.get("source_theorist"),
                default="unknown",
            ) or "unknown",
            "description": _as_text(description, limit=200),
            "pagerank": _finite_float(pagerank.get(node)),
            "community": community,
            "in_degree": int(graph.in_degree(node)),
            "out_degree": int(graph.out_degree(node)),
        })

    links: list[dict[str, Any]] = []
    for source, target, attrs in graph.edges(data=True):
        edge_sources = _as_list(
            attrs.get("edge_sources") or attrs.get("sources") or attrs.get("source")
        )
        if not edge_sources:
            edge_sources = _as_list(
                graph.graph.get("sources") or graph.graph.get("source")
            )
        relation = attrs.get("relation")
        if not relation:
            relation = "references" if "wiki" in edge_sources else "unspecified"
        links.append({
            "source": str(source),
            "target": str(target),
            "relation": _as_text(relation, default="unspecified"),
            "sources": edge_sources or ["unknown"],
        })

    communities = {
        item["community"] for item in nodes if item["community"] is not None
    }
    return {
        "nodes": nodes,
        "links": links,
        "meta": {
            "node_count": len(nodes),
            "edge_count": len(links),
            "community_count": len(communities),
        },
    }


def _compute_metrics(
    graph: nx.DiGraph,
) -> tuple[dict[Any, float], dict[Any, Any], int]:
    pagerank: dict[Any, float] = {}
    partition: dict[Any, Any] = {}
    community_count = 0
    if not graph:
        return pagerank, partition, community_count

    try:
        pagerank = dict(compute_pagerank(graph))
    except Exception as exc:  # Metrics are optional visualization enhancements.
        print(f"WARN: PageRank unavailable: {exc}", file=sys.stderr)
    try:
        computed_partition, communities = compute_communities(graph)
        partition = dict(computed_partition)
        community_count = len(communities)
    except Exception as exc:  # Louvain can reject empty or degenerate graphs.
        print(f"WARN: community detection unavailable: {exc}", file=sys.stderr)
    return pagerank, partition, community_count


def _resolve_node(graph: nx.DiGraph, requested: str) -> Any | None:
    if requested in graph:
        return requested
    return next((node for node in graph if str(node) == requested), None)


def _focus_graph(
    graph: nx.DiGraph, focus: Any, radius: int,
) -> nx.DiGraph:
    neighborhood = nx.ego_graph(graph.to_undirected(), focus, radius=radius)
    return graph.subgraph(neighborhood.nodes()).copy()


def _degrade_graph(
    graph: nx.DiGraph,
    pagerank: Mapping[Any, float],
    max_nodes: int,
    required_nodes: set[Any] | None = None,
) -> tuple[nx.DiGraph, bool]:
    if max_nodes == 0 or graph.number_of_nodes() <= max_nodes:
        return graph, False

    ranked = sorted(
        graph.nodes(),
        key=lambda node: (
            pagerank.get(node, -1.0),
            graph.in_degree(node) + graph.out_degree(node),
            str(node),
        ),
        reverse=True,
    )
    selected = set(ranked[:max_nodes])
    selected.update(node for node in (required_nodes or set()) if node in graph)
    for node in tuple(selected):
        selected.update(graph.predecessors(node))
        selected.update(graph.successors(node))
    return graph.subgraph(selected).copy(), True


def _json_for_script(data: Mapping[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return (
        payload.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _download_d3() -> str:
    scheme = urllib.parse.urlparse(D3_OFFLINE_URL).scheme.lower()
    if scheme not in ("http", "https"):
        raise RuntimeError(
            "D3 source must be served over http/https, got %r" % (scheme,)
        )
    request = urllib.request.Request(
        D3_OFFLINE_URL,
        headers={"User-Agent": "wiki-graph-viz/1.0"},
    )
    try:
        # Scheme allow-listed immediately above.
        with urllib.request.urlopen(  # nosec B310
            request, timeout=30
        ) as response:
            source = response.read().decode("utf-8")
    except (OSError, UnicodeError, urllib.error.URLError) as exc:
        raise RuntimeError(
            f"Could not download D3.js v7 for --offline from {D3_OFFLINE_URL}: {exc}"
        ) from exc
    if len(source) < 100_000 or "d3" not in source[:500].lower():
        raise RuntimeError("Downloaded D3.js response did not look like the v7 library")
    return source


HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__DOCUMENT_TITLE__</title>
  <style>
    :root {
      color-scheme: light;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
      color: #20262e;
      background: #f4f6f8;
    }
    * { box-sizing: border-box; }
    html, body, #container { width: 100%; height: 100%; margin: 0; overflow: hidden; }
    body { background: #f4f6f8; }
    button, input { font: inherit; letter-spacing: 0; }
    button:focus-visible, input:focus-visible { outline: 3px solid rgba(42, 109, 181, 0.28); outline-offset: 2px; }
    #container { position: relative; min-width: 280px; }
    #graph { display: block; width: 100%; height: 100%; background: #f8fafb; cursor: grab; }
    #graph:active { cursor: grabbing; }
    #controls {
      position: absolute;
      inset: 16px 16px auto 16px;
      z-index: 5;
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      pointer-events: none;
    }
    #controls > * { pointer-events: auto; }
    #export-png, #search-input {
      min-height: 38px;
      border: 1px solid #c6cdd5;
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.96);
      color: #20262e;
      box-shadow: 0 2px 8px rgba(24, 35, 48, 0.10);
    }
    #export-png {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 0 12px;
      font-weight: 650;
      cursor: pointer;
    }
    #export-png:hover { background: #eef3f7; }
    #search-input { width: min(280px, 43vw); padding: 8px 12px 8px 34px; }
    #search-wrap { position: relative; }
    #search-wrap::before {
      content: "";
      position: absolute;
      z-index: 1;
      left: 12px;
      top: 11px;
      width: 11px;
      height: 11px;
      border: 2px solid #617080;
      border-radius: 50%;
    }
    #search-wrap::after {
      content: "";
      position: absolute;
      z-index: 1;
      left: 23px;
      top: 23px;
      width: 7px;
      height: 2px;
      background: #617080;
      transform: rotate(45deg);
      transform-origin: left center;
    }
    .sr-only {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }
    #notice {
      position: absolute;
      z-index: 4;
      top: 64px;
      left: 50%;
      max-width: min(680px, calc(100vw - 32px));
      transform: translateX(-50%);
      padding: 7px 11px;
      border: 1px solid #dfc57f;
      border-radius: 6px;
      background: rgba(255, 248, 224, 0.96);
      color: #614b15;
      font-size: 13px;
      box-shadow: 0 2px 8px rgba(55, 45, 15, 0.08);
    }
    #notice:empty { display: none; }
    .link { stroke: #98a2ad; stroke-opacity: 0.48; stroke-width: 1.2; }
    .node {
      stroke: #ffffff;
      stroke-width: 1.5;
      cursor: pointer;
      transition: opacity 120ms ease, stroke-width 120ms ease;
    }
    .node:hover { stroke: #1d2732; stroke-width: 2.5; }
    #sidebar {
      position: absolute;
      z-index: 7;
      top: 0;
      right: 0;
      width: min(300px, calc(100vw - 28px));
      height: 100%;
      padding: 20px;
      overflow-y: auto;
      border-left: 1px solid #cbd2d9;
      background: rgba(255, 255, 255, 0.98);
      box-shadow: -8px 0 24px rgba(24, 35, 48, 0.13);
      transform: translateX(105%);
      transition: transform 180ms ease;
    }
    #sidebar.open { transform: translateX(0); }
    #sidebar-header { display: flex; align-items: flex-start; gap: 12px; }
    #detail-title {
      flex: 1;
      margin: 0;
      overflow-wrap: anywhere;
      font-size: 20px;
      line-height: 1.25;
      font-weight: 720;
    }
    #close-sidebar {
      width: 32px;
      height: 32px;
      flex: 0 0 32px;
      padding: 0;
      border: 1px solid transparent;
      border-radius: 5px;
      background: transparent;
      color: #4b5662;
      font-size: 24px;
      line-height: 28px;
      cursor: pointer;
    }
    #close-sidebar:hover { border-color: #cbd2d9; background: #eef2f5; }
    #details { margin: 22px 0 0; }
    .detail-row { margin: 0 0 16px; }
    .detail-row dt {
      margin: 0 0 3px;
      color: #697583;
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
    }
    .detail-row dd { margin: 0; overflow-wrap: anywhere; font-size: 14px; line-height: 1.45; }
    #legend {
      position: absolute;
      z-index: 4;
      left: 16px;
      bottom: 16px;
      min-width: 148px;
      max-height: min(45vh, 320px);
      padding: 10px 12px;
      overflow-y: auto;
      border: 1px solid #cbd2d9;
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.94);
      box-shadow: 0 2px 8px rgba(24, 35, 48, 0.09);
      font-size: 12px;
    }
    #legend[hidden] { display: none; }
    #legend-title { margin-bottom: 7px; color: #4c5865; font-weight: 750; }
    .legend-item { display: flex; align-items: center; gap: 8px; margin-top: 5px; }
    .legend-swatch { width: 11px; height: 11px; flex: 0 0 11px; border-radius: 2px; }
    .legend-label { max-width: 180px; overflow-wrap: anywhere; }
    @media (max-width: 560px) {
      #controls { inset: 10px 10px auto 10px; }
      #export-png { width: 42px; padding: 0; justify-content: center; }
      #export-label { display: none; }
      #search-input { width: min(245px, calc(100vw - 72px)); }
      #notice { top: 58px; }
      #legend { left: 10px; bottom: 10px; }
    }
  </style>
</head>
<body>
  <div id="container">
    <div id="controls">
      <button id="export-png" type="button" title="Export graph as PNG">
        <span aria-hidden="true">📷</span><span id="export-label">Export PNG</span>
      </button>
      <label id="search-wrap">
        <span class="sr-only">Search nodes</span>
        <input id="search-input" type="search" placeholder="Search title or ID" autocomplete="off">
      </label>
    </div>
    <div id="notice" role="status">__NOTICE__</div>
    <svg id="graph" role="img" aria-label="Interactive knowledge graph"></svg>
    <aside id="sidebar" aria-hidden="true" aria-label="Node details">
      <div id="sidebar-header">
        <h2 id="detail-title">Node details</h2>
        <button id="close-sidebar" type="button" title="Close details" aria-label="Close details">&times;</button>
      </div>
      <dl id="details"></dl>
    </aside>
    <div id="legend" hidden><div id="legend-title"></div><div id="legend-items"></div></div>
  </div>
  __D3_SCRIPT__
  <script id="graph-data" type="application/json">__GRAPH_DATA__</script>
  <script>
  (() => {
    "use strict";

    const data = JSON.parse(document.getElementById("graph-data").textContent);
    const container = document.getElementById("container");
    const svg = d3.select("#graph");
    const sidebar = document.getElementById("sidebar");
    const details = document.getElementById("details");
    const searchInput = document.getElementById("search-input");
    const notice = document.getElementById("notice");
    const sourceColors = {
      wiki: "#4A90D9",
      ontology: "#50B86C",
      ragflow: "#E8953C",
      unknown: "#7D8792"
    };
    const categoryColors = d3.schemeCategory10;
    let width = Math.max(280, container.clientWidth);
    let height = Math.max(240, container.clientHeight);
    let currentQuery = "";

    svg.attr("viewBox", `0 0 ${width} ${height}`);
    const defs = svg.append("defs");
    defs.append("marker")
      .attr("id", "arrowhead")
      .attr("viewBox", "0 -4 8 8")
      .attr("refX", 8)
      .attr("refY", 0)
      .attr("markerWidth", 8)
      .attr("markerHeight", 8)
      .attr("orient", "auto")
      .append("path")
      .attr("d", "M0,-4L8,0L0,4Z")
      .attr("fill", "#98a2ad")
      .attr("fill-opacity", 0.72);

    const viewport = svg.append("g").attr("class", "viewport");
    const zoomBehavior = d3.zoom()
      .scaleExtent([0.08, 8])
      .on("zoom", event => viewport.attr("transform", event.transform));
    svg.call(zoomBehavior);

    const rankValues = data.nodes
      .map(node => node.pagerank)
      .filter(value => Number.isFinite(value));
    const rankExtent = rankValues.length ? d3.extent(rankValues) : null;
    const rankScale = rankExtent && rankExtent[0] !== rankExtent[1]
      ? d3.scaleSqrt().domain(rankExtent).range([4, 24])
      : null;
    const radius = node => rankScale && Number.isFinite(node.pagerank)
      ? rankScale(node.pagerank)
      : 8;

    const communityDomain = Array.from(new Set(
      data.nodes.filter(node => node.community !== null).map(node => String(node.community))
    )).sort(d3.ascending);
    const typeDomain = Array.from(new Set(data.nodes.map(node => node.type || "concept")))
      .sort(d3.ascending);
    const communityScale = d3.scaleOrdinal(communityDomain, categoryColors);
    const typeScale = d3.scaleOrdinal(typeDomain, categoryColors);
    const nodeColor = node => {
      if (data.meta.color_by === "source") {
        return sourceColors[node.source] || sourceColors.unknown;
      }
      if (data.meta.color_by === "type") {
        return typeScale(node.type || "concept");
      }
      return node.community === null
        ? sourceColors.unknown
        : communityScale(String(node.community));
    };

    const link = viewport.append("g")
      .attr("aria-hidden", "true")
      .selectAll("line")
      .data(data.links)
      .join("line")
      .attr("class", "link")
      .attr("marker-end", "url(#arrowhead)");

    const node = viewport.append("g")
      .selectAll("circle")
      .data(data.nodes)
      .join("circle")
      .attr("class", "node")
      .attr("r", radius)
      .attr("fill", nodeColor)
      .on("mouseenter", highlightCommunity)
      .on("mouseleave", applySearchFilter)
      .on("click", (event, datum) => {
        event.stopPropagation();
        openDetails(datum);
      });

    node.append("title").text(datum => datum.title || datum.id);

    const simulation = d3.forceSimulation(data.nodes)
      .force("link", d3.forceLink(data.links).id(datum => datum.id).distance(78).strength(0.32))
      .force("charge", d3.forceManyBody().strength(-105))
      .force("center", d3.forceCenter(width / 2, height / 2))
      .force("collision", d3.forceCollide().radius(datum => radius(datum) + 3).iterations(2))
      .on("tick", ticked);

    node.call(d3.drag()
      .on("start", dragStarted)
      .on("drag", dragged)
      .on("end", dragEnded));

    function ticked() {
      link
        .attr("x1", datum => datum.source.x)
        .attr("y1", datum => datum.source.y)
        .attr("x2", datum => {
          const dx = datum.target.x - datum.source.x;
          const dy = datum.target.y - datum.source.y;
          const distance = Math.hypot(dx, dy) || 1;
          return datum.target.x - (dx / distance) * (radius(datum.target) + 7);
        })
        .attr("y2", datum => {
          const dx = datum.target.x - datum.source.x;
          const dy = datum.target.y - datum.source.y;
          const distance = Math.hypot(dx, dy) || 1;
          return datum.target.y - (dy / distance) * (radius(datum.target) + 7);
        });
      node.attr("cx", datum => datum.x).attr("cy", datum => datum.y);
    }

    function dragStarted(event, datum) {
      if (!event.active) simulation.alphaTarget(0.28).restart();
      datum.fx = datum.x;
      datum.fy = datum.y;
    }

    function dragged(event, datum) {
      datum.fx = event.x;
      datum.fy = event.y;
    }

    function dragEnded(event, datum) {
      if (!event.active) simulation.alphaTarget(0);
      datum.fx = null;
      datum.fy = null;
    }

    function highlightCommunity(_event, datum) {
      if (datum.community === null || datum.community === undefined) return;
      node.style("opacity", candidate => candidate.community === datum.community ? 1 : 0.15);
      link.style("opacity", edge => {
        const sameSource = edge.source.community === datum.community;
        const sameTarget = edge.target.community === datum.community;
        return sameSource && sameTarget ? 0.72 : 0.08;
      });
    }

    function applySearchFilter() {
      const query = currentQuery;
      if (!query) {
        node.style("opacity", 1);
        link.style("opacity", null);
        return;
      }
      const matches = candidate =>
        String(candidate.id).toLowerCase().includes(query)
        || String(candidate.title || "").toLowerCase().includes(query);
      node.style("opacity", candidate => matches(candidate) ? 1 : 0.12);
      link.style("opacity", edge => matches(edge.source) && matches(edge.target) ? 0.62 : 0.04);
    }

    function pulse(target) {
      node.filter(candidate => candidate === target)
        .interrupt()
        .raise()
        .transition().duration(180).attr("r", radius(target) * 3)
        .transition().duration(420).ease(d3.easeBackOut).attr("r", radius(target));
    }

    function recenter(target) {
      const scale = Math.max(1.6, d3.zoomTransform(svg.node()).k);
      const transform = d3.zoomIdentity
        .translate(width / 2 - target.x * scale, height / 2 - target.y * scale)
        .scale(scale);
      svg.transition().duration(500).call(zoomBehavior.transform, transform);
    }

    searchInput.addEventListener("input", () => {
      currentQuery = searchInput.value.trim().toLowerCase();
      applySearchFilter();
      if (!currentQuery) return;
      const match = data.nodes.find(candidate =>
        String(candidate.id).toLowerCase().includes(currentQuery)
        || String(candidate.title || "").toLowerCase().includes(currentQuery)
      );
      if (match) {
        recenter(match);
        pulse(match);
      }
    });

    function detailRow(label, value) {
      const wrapper = document.createElement("div");
      wrapper.className = "detail-row";
      const term = document.createElement("dt");
      term.textContent = label;
      const description = document.createElement("dd");
      description.textContent = value === null || value === undefined || value === "" ? "-" : String(value);
      wrapper.append(term, description);
      return wrapper;
    }

    function openDetails(datum) {
      document.getElementById("detail-title").textContent = datum.title || datum.id;
      const score = Number.isFinite(datum.pagerank) ? datum.pagerank.toExponential(4) : "Unavailable";
      details.replaceChildren(
        detailRow("ID", datum.id),
        detailRow("Type", datum.type),
        detailRow("Source", (datum.sources || [datum.source]).join(", ")),
        detailRow("Theorist", datum.theorist),
        detailRow("Description", datum.description),
        detailRow("In degree", datum.in_degree),
        detailRow("Out degree", datum.out_degree),
        detailRow("PageRank", score)
      );
      sidebar.classList.add("open");
      sidebar.setAttribute("aria-hidden", "false");
    }

    document.getElementById("close-sidebar").addEventListener("click", () => {
      sidebar.classList.remove("open");
      sidebar.setAttribute("aria-hidden", "true");
    });

    function renderLegend() {
      const legend = document.getElementById("legend");
      let title;
      let items;
      if (data.meta.color_by === "source") {
        title = "Sources";
        items = (data.meta.sources || []).map(value => ({
          label: value,
          color: sourceColors[value] || sourceColors.unknown
        }));
      } else if (data.meta.color_by === "type") {
        title = "Types";
        items = typeDomain.map(value => ({label: value, color: typeScale(value)}));
      } else {
        title = "Communities";
        items = communityDomain.map(value => ({
          label: `Community ${value}`,
          color: communityScale(value)
        }));
      }
      if (items.length <= 1) return;
      document.getElementById("legend-title").textContent = title;
      const list = document.getElementById("legend-items");
      items.forEach(item => {
        const row = document.createElement("div");
        row.className = "legend-item";
        const swatch = document.createElement("span");
        swatch.className = "legend-swatch";
        swatch.style.backgroundColor = item.color;
        const label = document.createElement("span");
        label.className = "legend-label";
        label.textContent = item.label;
        row.append(swatch, label);
        list.append(row);
      });
      legend.hidden = false;
    }
    renderLegend();

    async function exportPng() {
      const button = document.getElementById("export-png");
      button.disabled = true;
      try {
        const clone = svg.node().cloneNode(true);
        clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
        clone.setAttribute("width", width);
        clone.setAttribute("height", height);
        const style = document.createElementNS("http://www.w3.org/2000/svg", "style");
        style.textContent = ".link{stroke:#98a2ad;stroke-opacity:.48;stroke-width:1.2}.node{stroke:#fff;stroke-width:1.5}";
        clone.insertBefore(style, clone.firstChild);
        const source = new XMLSerializer().serializeToString(clone);
        const url = URL.createObjectURL(new Blob([source], {type: "image/svg+xml;charset=utf-8"}));
        const image = new Image();
        await new Promise((resolve, reject) => {
          image.onload = resolve;
          image.onerror = reject;
          image.src = url;
        });
        const ratio = Math.max(1, window.devicePixelRatio || 1);
        const canvas = document.createElement("canvas");
        canvas.width = Math.round(width * ratio);
        canvas.height = Math.round(height * ratio);
        const context = canvas.getContext("2d");
        context.scale(ratio, ratio);
        context.fillStyle = "#f8fafb";
        context.fillRect(0, 0, width, height);
        context.drawImage(image, 0, 0, width, height);
        URL.revokeObjectURL(url);
        const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/png"));
        if (!blob) throw new Error("PNG encoding failed");
        const downloadUrl = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
        anchor.href = downloadUrl;
        anchor.download = `graph-viz-export-${timestamp}.png`;
        anchor.click();
        setTimeout(() => URL.revokeObjectURL(downloadUrl), 1000);
      } catch (error) {
        notice.textContent = `PNG export failed: ${error.message || error}`;
      } finally {
        button.disabled = false;
      }
    }
    document.getElementById("export-png").addEventListener("click", exportPng);

    new ResizeObserver(() => {
      width = Math.max(280, container.clientWidth);
      height = Math.max(240, container.clientHeight);
      svg.attr("viewBox", `0 0 ${width} ${height}`);
      simulation.force("center", d3.forceCenter(width / 2, height / 2));
      simulation.alpha(0.2).restart();
    }).observe(container);
  })();
  </script>
</body>
</html>
'''


def _render_html(
    data: Mapping[str, Any],
    offline: bool = False,
    title: str = "Knowledge Graph",
) -> str:
    if offline:
        d3_source = _download_d3().replace("</script", "<\\/script")
        d3_script = f"<script>\n{d3_source}\n</script>"
    else:
        d3_script = f'<script src="{D3_CDN_URL}"></script>'
    notice = _as_text(data.get("meta", {}).get("notice", ""))
    return (
        HTML_TEMPLATE.replace("__DOCUMENT_TITLE__", html.escape(title, quote=True))
        .replace("__NOTICE__", html.escape(notice, quote=True))
        .replace("__D3_SCRIPT__", d3_script)
        .replace("__GRAPH_DATA__", _json_for_script(data))
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate an interactive D3.js knowledge graph as a single HTML file."
    )
    parser.add_argument(
        "--source", default="wiki",
        help="wiki, ontology, ragflow, or comma-separated sources (default: wiki)",
    )
    parser.add_argument(
        "--output", default="graph-viz.html",
        help="Output HTML path (default: graph-viz.html)",
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="Download and inline D3.js v7 so the generated HTML works offline",
    )
    parser.add_argument("--focus", metavar="NODE_ID", help="Render a node's k-hop neighborhood")
    parser.add_argument("--radius", type=int, default=2, help="Focus radius (default: 2)")
    parser.add_argument(
        "--max-nodes", type=int, default=500,
        help="PageRank Top N before adding neighbors; 0 disables the limit (default: 500)",
    )
    parser.add_argument(
        "--wiki-root", default=os.environ.get("WIKI_ROOT", os.getcwd()),
        help="Wiki root (default: WIKI_ROOT or current directory)",
    )
    parser.add_argument(
        "--ontology-path",
        default=os.environ.get("ONTOLOGY_PATH", str(DEFAULT_ONTOLOGY_PATH)),
        help="Ontology graph JSONL path",
    )
    parser.add_argument("--ragflow-path", help="Offline ragflow JSONL file or JSON directory")
    parser.add_argument("--title", help="Document title (default: Knowledge Graph)")
    parser.add_argument(
        "--color-by", choices=("source", "community", "type"),
        help="Color nodes by source, Louvain community, or node type",
    )
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.radius < 0:
        parser.error("--radius must be 0 or greater")
    if args.max_nodes < 0:
        parser.error("--max-nodes must be 0 or greater")

    try:
        graph, graph_meta = build_graph(
            source=args.source,
            wiki_root=args.wiki_root,
            ontology_path=args.ontology_path,
            ragflow_path=args.ragflow_path,
        )
    except (OSError, TypeError, ValueError) as exc:
        parser.error(str(exc))

    full_node_count = graph.number_of_nodes()
    full_edge_count = graph.number_of_edges()
    pagerank, partition, full_community_count = _compute_metrics(graph)

    focus_id: str | None = None
    focus_node: Any | None = None
    view = graph
    if args.focus:
        focus_node = _resolve_node(graph, args.focus)
        if focus_node is None:
            parser.error(f"focus node not found: {args.focus}")
        focus_id = str(focus_node)
        view = _focus_graph(graph, focus_node, args.radius)

    required_nodes = {focus_node} if focus_node is not None else None
    view, degraded = _degrade_graph(view, pagerank, args.max_nodes, required_nodes)
    source_names = _as_list(args.source)
    color_by = args.color_by or ("source" if len(source_names) > 1 else "community")
    document_title = args.title or "Knowledge Graph"
    if focus_id is not None:
        document_title = f"{document_title} - Focus: {focus_id} (radius {args.radius})"

    data = _graph_to_d3_json(view, graph_meta, pagerank, partition)
    notice = ""
    if degraded:
        notice = (
            f"Showing Top {args.max_nodes} + neighbors ({view.number_of_nodes()} total). "
            "Full graph: --max-nodes 0"
        )
    data["meta"].update({
        "title": document_title,
        "sources": source_names,
        "source_count": len(source_names),
        "focus": focus_id,
        "radius": args.radius if focus_id is not None else None,
        "degraded": degraded,
        "notice": notice,
        "full_node_count": full_node_count,
        "full_edge_count": full_edge_count,
        "full_community_count": full_community_count,
        "color_by": color_by,
    })

    try:
        rendered = _render_html(data, offline=args.offline, title=document_title)
    except RuntimeError as exc:
        parser.error(str(exc))

    output = Path(args.output).expanduser()
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    except OSError as exc:
        parser.error(f"Could not write {output}: {exc}")
    print(
        f"Wrote {output} ({data['meta']['node_count']} nodes, "
        f"{data['meta']['edge_count']} edges)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
