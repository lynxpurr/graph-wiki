#!/usr/bin/env python3
"""Canary automated gate — compare scorecard against baseline for CI/cron gating.

v3.4.0 Wave 7A: Compares current scorecard vs .graph-baseline.json and produces
a gate decision (PASS/WARN/REVIEW/BLOCK) for automated deployment pipelines.

Reuses (zero changes):
  - graph_loader.build_graph()      — graph construction
  - scorecard.build_scorecard()     — scorecard + canary eligibility
  - wiki_analyzer.scorecard_delta() — per-metric deltas
  - baseline_paths.resolve_baseline() — baseline path resolution (shared with
    wiki_analyzer baseline-save and weekly-health-check, issue #1)

Exit codes: 0=PASS, 1=WARN, 2=REVIEW, 3=BLOCK
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

# ── Bootstrap script directory for local imports ──────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from graph_loader import build_graph, normalize_sources
from baseline_paths import resolve_baseline
from scorecard import build_scorecard
from wiki_analyzer import scorecard_delta

# ── Default paths ─────────────────────────────────────────────────────────
WIKI_ROOT = os.environ.get(
    "WIKI_ROOT",
    os.getcwd(),
)
ONTOLOGY_PATH = os.environ.get(
    "ONTOLOGY_PATH",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "examples", "mini-ontology.jsonl",
    ),
)

# ── Gate decision severity levels ─────────────────────────────────────────
PASS = "PASS"
WARN = "WARN"
REVIEW = "REVIEW"
BLOCK = "BLOCK"
_SEVERITY_ORDER = {PASS: 0, WARN: 1, REVIEW: 2, BLOCK: 3}
_DECISION_EMOJI = {PASS: "✅", WARN: "⚠️", REVIEW: "🔍", BLOCK: "🚫"}
_EXIT_CODES = {PASS: 0, WARN: 1, REVIEW: 2, BLOCK: 3}
_MISSING_DIAGNOSIS_MESSAGE = (
    "Missing diagnosis data — run graph-test-suite --export-canary-data first"
)


# ── Core logic ────────────────────────────────────────────────────────────

def _flatten_metrics(scorecard: dict | None) -> dict[str, float]:
    """Flatten structure + quality + composite into a single dict."""
    flat: dict[str, float] = {}
    if not scorecard:
        return flat
    metrics = scorecard.get("metrics", {})
    for group in ("structure", "quality", "composite"):
        group_data = metrics.get(group, {})
        if isinstance(group_data, dict):
            for key, val in group_data.items():
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    flat[f"{group}.{key}"] = float(val)
    return flat


def compute_delta(baseline: dict, current: dict) -> dict:
    """Compute per-metric deltas between baseline and current scorecards."""
    base_flat = _flatten_metrics(baseline)
    curr_flat = _flatten_metrics(current)
    all_keys = sorted(set(base_flat.keys()) | set(curr_flat.keys()))
    deltas: dict[str, dict] = {}
    for key in all_keys:
        before = base_flat.get(key, 0.0)
        after = curr_flat.get(key, 0.0)
        delta = round(after - before, 6)
        deltas[key] = {
            "baseline": before,
            "current": after,
            "delta": delta,
            "direction": "↑" if delta > 0 else "↓" if delta < 0 else "→",
        }
    return deltas


def _ontology_alignment_score(scorecard: dict | None) -> float | None:
    """Read D11 from its canonical diagnosis path.

    Falls back to the scorecard's own D11 score when the diagnosis overlay is
    absent, so a run without --canary-data can still evaluate alignment
    instead of being hard-blocked (P0-3 fix).
    """
    diagnosis = (scorecard or {}).get("diagnosis")
    if isinstance(diagnosis, dict):
        alignment = diagnosis.get("ontology_alignment")
        if isinstance(alignment, dict):
            score = alignment.get("score")
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                return float(score)
    # Fallback: any D11 score computed by build_scorecard itself.
    for key in ("d11", "D11", "ontology_alignment"):
        value = (scorecard or {}).get(key)
        if isinstance(value, dict):
            score = value.get("score")
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                return float(score)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    scores = (scorecard or {}).get("scores")
    if isinstance(scores, dict):
        for key in ("d11", "D11", "ontology_alignment"):
            value = scores.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    return None


def decide_gate(
    delta: dict,
    current_scorecard: dict | None = None,
    baseline_scorecard: dict | None = None,
) -> dict:
    """Apply the gate decision matrix and return structured result.

    Decision matrix (escalating, final = max severity):
       health_score ↓ >= baseline   → PASS
       health_score ↓ <5%           → WARN
       health_score ↓ 5-10%         → REVIEW
       health_score ↓ >10%          → BLOCK
       dead_end ↑ >5                → WARN  (overlay)
       link_rot ↑ >10               → REVIEW (overlay)
       isolated ↑ >20%              → WARN  (overlay)
       D8 coverage <3               → BLOCK (overlay)
       D8 coverage <5               → WARN  (overlay)
       D11 alignment drop >0.5      → BLOCK (overlay)
       D11 alignment drop >0.2      → WARN  (overlay)
       missing diagnosis data       → BLOCK (overlay)
       unmapped entities >0         → WARN  (overlay)
    """
    violations: list[dict] = []
    decision = PASS

    curr = current_scorecard or {}
    base = baseline_scorecard or {}

    # ── Health score degradation ──────────────────────────────────────
    hs_key = "composite.health_score"
    hs = delta.get(hs_key, {})
    if hs:
        hs_baseline = hs["baseline"]
        hs_current = hs["current"]
        hs_delta = hs["delta"]
        if hs_baseline > 0:
            pct_change = abs(hs_delta) / hs_baseline * 100
        else:
            pct_change = 0

        if hs_delta < 0:
            violation = {
                "metric": hs_key,
                "baseline": hs_baseline,
                "current": hs_current,
                "delta": hs_delta,
                "pct_change": round(pct_change, 2),
            }
            if pct_change > 10:
                violation["severity"] = BLOCK
                decision = _max_severity(decision, BLOCK)
            elif pct_change > 5:
                violation["severity"] = REVIEW
                decision = _max_severity(decision, REVIEW)
            else:
                violation["severity"] = WARN
                decision = _max_severity(decision, WARN)
            violations.append(violation)
    else:
        # No baseline health_score → assume pass
        pass

    # ── Dead end increase ─────────────────────────────────────────────
    de_key = "structure.dead_end_ratio"
    de = delta.get(de_key, {})
    if de:
        de_baseline = de.get("baseline", 0)
        de_current = de.get("current", 0)
        if de_current > de_baseline + 0.05:
            violations.append({
                "metric": de_key,
                "baseline": de_baseline,
                "current": de_current,
                "delta": de["delta"],
                "severity": WARN,
            })
            decision = _max_severity(decision, WARN)

    # ── Link rot increase ─────────────────────────────────────────────
    lr_key = "structure.link_rot_ratio"
    lr = delta.get(lr_key, {})
    if lr:
        lr_baseline = lr.get("baseline", 0)
        lr_current = lr.get("current", 0)
        if lr_current > lr_baseline + 0.10:
            violations.append({
                "metric": lr_key,
                "baseline": lr_baseline,
                "current": lr_current,
                "delta": lr["delta"],
                "severity": REVIEW,
            })
            decision = _max_severity(decision, REVIEW)

    # ── Isolated increase ─────────────────────────────────────────────
    iso_key = "structure.isolated_ratio"
    iso = delta.get(iso_key, {})
    if iso:
        iso_baseline = iso.get("baseline", 0)
        iso_current = iso.get("current", 0)
        if iso_current > iso_baseline + 0.20:
            violations.append({
                "metric": iso_key,
                "baseline": iso_baseline,
                "current": iso_current,
                "delta": iso["delta"],
                "severity": WARN,
            })
            decision = _max_severity(decision, WARN)

    # ── D8 coverage degradation ──────────────────────────────────────
    d8_key = "quality.coverage_score"
    d8 = delta.get(d8_key, {})
    if d8:
        d8_current = d8.get("current", 10)
        if d8_current < 3:
            violations.append({
                "metric": d8_key,
                "baseline": d8.get("baseline", 0),
                "current": d8_current,
                "delta": d8.get("delta", 0),
                "severity": BLOCK,
            })
            decision = _max_severity(decision, BLOCK)
        elif d8_current < 5:
            violations.append({
                "metric": d8_key,
                "baseline": d8.get("baseline", 0),
                "current": d8_current,
                "delta": d8.get("delta", 0),
                "severity": WARN,
            })
            decision = _max_severity(decision, WARN)

    # ── D11 alignment degradation ───────────────────────────────────
    d11_current = _ontology_alignment_score(curr)
    d11_baseline = _ontology_alignment_score(base)
    missing_diagnosis = d11_current is None
    if missing_diagnosis:
        # P0-3 fix: degrade to WARN (was BLOCK) so the documented exit-0
        # path is reachable when no diagnosis overlay is supplied.
        violations.append({
            "metric": "diagnosis.ontology_alignment.score",
            "baseline": d11_baseline or 0,
            "current": 0,
            "delta": 0,
            "severity": WARN,
            "message": _MISSING_DIAGNOSIS_MESSAGE,
        })
        decision = _max_severity(decision, WARN)
    elif d11_baseline is not None:
        d11_delta = round(d11_current - d11_baseline, 6)
        if d11_delta < -0.5:
            violations.append({
                "metric": "ontology_alignment",
                "baseline": d11_baseline,
                "current": d11_current,
                "delta": d11_delta,
                "severity": BLOCK,
            })
            decision = _max_severity(decision, BLOCK)
        elif d11_delta < -0.2:
            violations.append({
                "metric": "ontology_alignment",
                "baseline": d11_baseline,
                "current": d11_current,
                "delta": d11_delta,
                "severity": WARN,
            })
            decision = _max_severity(decision, WARN)

    # ── New unmapped ontology entities ───────────────────────────────
    unmapped = curr.get("metadata", {}).get("unmapped_entities", 0)
    if unmapped > 0:
        violations.append({
            "metric": "unmapped_entities",
            "current": unmapped,
            "delta": unmapped,
            "severity": WARN,
        })
        decision = _max_severity(decision, WARN)

    # ── Canary eligibility overlay ────────────────────────────────────
    canary = curr.get("canary_eligibility", "")
    if canary == "high_risk":
        decision = _max_severity(decision, REVIEW)
    elif canary == "moderate_risk" and decision == PASS:
        decision = WARN

    result = {
        "decision": decision,
        "severity_score": _SEVERITY_ORDER[decision],
        "violations": violations,
        "violation_count": len(violations),
    }
    if missing_diagnosis:
        result["message"] = _MISSING_DIAGNOSIS_MESSAGE
    return result


def _max_severity(current: str, candidate: str) -> str:
    return candidate if _SEVERITY_ORDER[candidate] > _SEVERITY_ORDER[current] else current


# ── Report rendering ──────────────────────────────────────────────────────

def render_json(
    gate: dict, delta: dict, baseline: dict, current: dict,
    baseline_path: str | None = None,
    migration_note: str | None = None,
) -> str:
    """Render structured JSON output."""
    output = {
        "status": gate["decision"],
        "gate_decision": gate["decision"],
        "severity_score": gate["severity_score"],
        "violation_count": gate["violation_count"],
        "violations": gate["violations"],
        "delta": delta,
        "baseline": {
            "baseline_id": baseline.get("baseline_id", ""),
            "timestamp": baseline.get("timestamp", ""),
            "source": baseline.get("source", ""),
            "health_score": (
                baseline.get("metrics", {}).get("composite", {}).get("health_score")
            ),
            "grade": (
                baseline.get("metrics", {}).get("composite", {}).get("grade")
            ),
        },
        "current": {
            "timestamp": current.get("timestamp", ""),
            "source": current.get("source", ""),
            "health_score": (
                current.get("metrics", {}).get("composite", {}).get("health_score")
            ),
            "grade": (
                current.get("metrics", {}).get("composite", {}).get("grade")
            ),
        },
        "risk_eligibility": {
            "canary": current.get("canary_eligibility", "unknown"),
            "threshold_status": (
                current.get("threshold_evaluation", {}).get("status", "unknown")
            ),
        },
    }
    if baseline_path:
        output["baseline_path"] = baseline_path
    if migration_note:
        output["baseline_migration"] = migration_note
    if gate.get("message"):
        output["message"] = gate["message"]
    return json.dumps(output, ensure_ascii=False, indent=2)


def render_report(
    gate: dict, delta: dict, baseline: dict, current: dict, source: str,
    baseline_path: str | None = None,
    migration_note: str | None = None,
) -> str:
    """Render a Markdown canary report."""
    emoji = _DECISION_EMOJI.get(gate["decision"], "❓")
    ts = datetime.now(timezone.utc).isoformat()

    lines = [
        f"# Canary Report — {ts[:16].replace('T', ' ')}",
        "",
        f"## Gate Decision: {emoji} {gate['decision']}",
        "",
        "## Scorecard Delta",
        "",
        "| Metric | Baseline | Current | Δ | Direction |",
        "|--------|----------|---------|---|-----------|",
    ]

    # Show key metrics first, then others
    priority_keys = [
        "composite.health_score", "structure.dead_end_ratio",
        "structure.link_rot_ratio", "structure.isolated_ratio",
        "structure.sponge_ratio", "quality.reciprocity",
        "quality.coverage_score", "quality.consistency_score",
        "structure.density", "composite.grade",
    ]
    shown = set()
    for key in priority_keys:
        if key in delta:
            d = delta[key]
            grade_key = "composite.grade"
            if key == grade_key:
                lines.append(
                    f"| {key} | {d.get('baseline','')} | {d.get('current','')} "
                    f"| — | — |"
                )
            else:
                lines.append(
                    f"| {key} | {d.get('baseline',0):.4f} | {d.get('current',0):.4f} "
                    f"| {d.get('delta',0):+.4f} | {d.get('direction','→')} |"
                )
            shown.add(key)
    for key in sorted(delta):
        if key not in shown:
            d = delta[key]
            lines.append(
                f"| {key} | {d.get('baseline',0):.4f} | {d.get('current',0):.4f} "
                f"| {d.get('delta',0):+.4f} | {d.get('direction','→')} |"
            )

    # Violations
    if gate["violations"]:
        lines.extend([
            "",
            "## Violations",
            "",
        ])
        for i, v in enumerate(gate["violations"], 1):
            lines.append(
                f"{i}. **{v['metric']}** — "
                f"baseline={v.get('baseline',0):.4f} → "
                f"current={v.get('current',0):.4f} "
                f"(Δ={v.get('delta',0):+.4f}) "
                f"[{v.get('severity','?')}]"
            )
            if v.get("message"):
                lines.append(f"   - {v['message']}")

    # Risk eligibility
    canary = current.get("canary_eligibility", "unknown")
    threshold = current.get("threshold_evaluation", {})
    lines.extend([
        "",
        "## Risk Eligibility",
        "",
        f"- **Canary eligibility**: `{canary}`",
        f"- **Threshold status**: `{threshold.get('status', 'unknown')}`",
    ])
    if threshold.get("critical"):
        lines.append(f"- **Critical**: {', '.join(threshold['critical'])}")
    if threshold.get("warnings"):
        lines.append(f"- **Warnings**: {', '.join(threshold['warnings'])}")

    # Baseline info
    meta = baseline.get("metadata", {})
    lines.extend([
        "",
        "## Baseline Info",
        "",
        f"- **Baseline ID**: {baseline.get('baseline_id', 'unknown')}",
        f"- **Baseline saved**: {baseline.get('timestamp', baseline.get('saved_at', 'unknown'))}",
        f"- **Source**: {baseline.get('source', source)}",
        f"- **Nodes**: {meta.get('nodes', '?')}",
        f"- **Edges**: {meta.get('edges', '?')}",
        f"- **Current source**: {source}",
    ])
    if baseline_path:
        lines.append(f"- **Baseline path**: {baseline_path}")
    if migration_note:
        lines.extend(["", "## Baseline Migration", "", migration_note])

    return "\n".join(lines) + "\n"


# ── CLI ───────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Canary gate — automated scorecard-to-deployment gating",
    )
    parser.add_argument(
        "--source", default="wiki",
        help="Data source(s): wiki, ontology, or comma-separated (default: wiki)",
    )
    parser.add_argument(
        "--baseline",
        help="Path to baseline scorecard JSON (default: .graph-baseline.json in wiki root)",
    )
    parser.add_argument(
        "--canary-data",
        help="Path to canary data exported by graph-test-suite.py",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output structured JSON instead of Markdown report",
    )
    parser.add_argument(
        "--report",
        help="Write Markdown report to this file path",
    )
    parser.add_argument(
        "--wiki-root", default=WIKI_ROOT,
        help="Wiki root directory",
    )
    parser.add_argument(
        "--ontology-path", default=ONTOLOGY_PATH,
        help="Ontology JSONL path",
    )
    args = parser.parse_args()

    source_names = normalize_sources(args.source)
    source_str = ",".join(source_names)

    # ── Load baseline ─────────────────────────────────────────────────
    resolution = None
    if args.baseline:
        baseline_path = args.baseline
    else:
        resolution = resolve_baseline(
            source_names,
            ontology_path=args.ontology_path,
            wiki_root=args.wiki_root,
            default_dir=os.path.dirname(ONTOLOGY_PATH),
        )
        baseline_path = resolution.path
    migration_note = resolution.migration_note if resolution else None
    if migration_note:
        print(f"⚠️ {migration_note}", file=sys.stderr)
    if not os.path.exists(baseline_path):
        hint = ""
        if resolution is not None and resolution.legacy_path:
            hint = (
                "\nLegacy baseline also missing: "
                f"{resolution.legacy_path}"
            )
        print(
            f"Error: Baseline not found at {baseline_path}{hint}\n"
            f"Run wiki_analyzer baseline-save --source {source_str} "
            f"--wiki-root {args.wiki_root} first.", file=sys.stderr,
        )
        return 1

    try:
        with open(baseline_path, "r", encoding="utf-8") as f:
            baseline = json.load(f)
    except (json.JSONDecodeError, PermissionError) as exc:
        print(f"Error reading baseline: {exc}", file=sys.stderr)
        return 1

    # ── Load full-pipeline canary metrics ─────────────────────────────
    canary_data = None
    if args.canary_data:
        try:
            with open(args.canary_data, "r", encoding="utf-8") as f:
                canary_data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Error reading canary data: {exc}", file=sys.stderr)
            return 1
        if not isinstance(canary_data, dict):
            print("Error reading canary data: expected a JSON object", file=sys.stderr)
            return 1

    # ── Build current graph + scorecard ───────────────────────────────
    try:
        graph, meta = build_graph(
            source=source_str,
            wiki_root=args.wiki_root,
            ontology_path=args.ontology_path,
        )
    except Exception as exc:
        print(f"Error building graph: {exc}", file=sys.stderr)
        return 1

    fusion_values = None
    if canary_data is not None:
        candidate = canary_data.get("fusion_values")
        if isinstance(candidate, dict):
            fusion_values = candidate

    current = build_scorecard(
        graph=graph,
        source=source_str,
        timestamp=datetime.now(timezone.utc).isoformat(),
        multi_source_scores=fusion_values,
    )
    if canary_data is not None:
        current["multi_source"] = fusion_values
        current["diagnosis"] = {
            "ontology_alignment": canary_data.get("ontology_alignment"),
        }
        if "ragflow_coverage" in canary_data:
            current["diagnosis"]["ragflow_coverage"] = canary_data[
                "ragflow_coverage"
            ]

    # ── Delta + gate decision ─────────────────────────────────────────
    delta = compute_delta(baseline, current)
    gate = decide_gate(delta, current, baseline)

    # ── Output ────────────────────────────────────────────────────────
    if args.json:
        output = render_json(gate, delta, baseline, current,
                             baseline_path, migration_note)
        print(output)
    else:
        output = render_report(gate, delta, baseline, current, source_str,
                               baseline_path, migration_note)
        print(output)

    if args.report:
        try:
            with open(args.report, "w", encoding="utf-8") as f:
                f.write(render_report(gate, delta, baseline, current, source_str,
                                      baseline_path, migration_note))
        except OSError as exc:
            print(f"Error writing report: {exc}", file=sys.stderr)
            return 1

    return _EXIT_CODES.get(gate["decision"], 0)


if __name__ == "__main__":
    sys.exit(main())
