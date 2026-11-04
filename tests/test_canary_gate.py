"""Tests for canary-gate.py — Wave 7A automated gate decisions."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

# ── Bootstrap — import canary-gate.py (hyphenated name) ───────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

_spec = importlib.util.spec_from_file_location(
    "canary_gate",
    os.path.join(REPO_ROOT, "scripts", "canary-gate.py"),
)
canary_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(canary_gate)


def _make_scorecard(health_score=90, dead_end_ratio=0.05, link_rot=0.02,
                    isolated_ratio=0.10, canary="low_risk",
                    coverage_score=None, consistency_score=None,
                    ontology_alignment_score=8,
                    unmapped_entities=0):
    """Create a minimal scorecard dict for testing."""
    return {
        "schema_version": "b3-scorecard-v1",
        "baseline_id": "test_baseline",
        "timestamp": "2026-07-24T12:00:00+00:00",
        "source": "wiki",
        "metrics": {
            "structure": {
                "dead_end_ratio": dead_end_ratio,
                "link_rot_ratio": link_rot,
                "isolated_ratio": isolated_ratio,
                "sponge_ratio": 0.05,
                "density": 0.10,
            },
            "quality": {
                "outlink_coverage": 0.95,
                "reciprocity": 0.10,
                "type_diversity": 0.50,
                "coverage_score": coverage_score,
                "consistency_score": consistency_score,
                "gain_score": None,
            },
            "composite": {
                "health_score": health_score,
                "grade": "A" if health_score >= 90 else "B",
                "penalties": {"dead_end": 0, "link_rot": 0, "isolated": 5,
                              "reciprocity": 0, "density": 0},
            },
        },
        "canary_eligibility": canary,
        "diagnosis": {
            "ontology_alignment": {
                "score": ontology_alignment_score,
            },
        },
        "threshold_evaluation": {
            "status": "green" if canary == "low_risk" else "warning",
            "critical": [],
            "warnings": [],
        },
        "metadata": {"nodes": 100, "edges": 200, "dead_ends": 5,
                     "isolated": 10, "sponges": 5, "broken_links": 3,
                     "diagnosis_available": ontology_alignment_score is not None,
                     "unmapped_entities": unmapped_entities},
    }


class TestComputeDelta:
    def test_same_scorecards_zero_delta(self):
        sc = _make_scorecard()
        delta = canary_gate.compute_delta(sc, sc)
        for key, d in delta.items():
            if key != "composite.grade":  # grade is not numeric
                assert d["delta"] == 0, f"{key} should be 0 delta, got {d['delta']}"

    def test_health_score_degraded(self):
        base = _make_scorecard(health_score=90)
        curr = _make_scorecard(health_score=80)
        delta = canary_gate.compute_delta(base, curr)
        assert delta["composite.health_score"]["delta"] == -10
        assert delta["composite.health_score"]["direction"] == "↓"

    def test_health_score_improved(self):
        base = _make_scorecard(health_score=80)
        curr = _make_scorecard(health_score=90)
        delta = canary_gate.compute_delta(base, curr)
        assert delta["composite.health_score"]["delta"] == 10
        assert delta["composite.health_score"]["direction"] == "↑"


class TestDecideGate:
    def test_pass_when_same(self):
        sc = _make_scorecard(health_score=90)
        delta = canary_gate.compute_delta(sc, sc)
        gate = canary_gate.decide_gate(delta, sc, sc)
        assert gate["decision"] == "PASS"
        assert gate["violation_count"] == 0

    def test_warn_small_drop(self):
        base = _make_scorecard(health_score=90)
        curr = _make_scorecard(health_score=87)  # ~3.3% drop < 5%
        delta = canary_gate.compute_delta(base, curr)
        gate = canary_gate.decide_gate(delta, curr, base)
        assert gate["decision"] == "WARN"

    def test_review_moderate_drop(self):
        base = _make_scorecard(health_score=90)
        curr = _make_scorecard(health_score=84)  # 6.67% drop → in 5-10%
        delta = canary_gate.compute_delta(base, curr)
        gate = canary_gate.decide_gate(delta, curr, base)
        assert gate["decision"] == "REVIEW"

    def test_block_large_drop(self):
        base = _make_scorecard(health_score=90)
        curr = _make_scorecard(health_score=75)  # 16.7% drop > 10%
        delta = canary_gate.compute_delta(base, curr)
        gate = canary_gate.decide_gate(delta, curr, base)
        assert gate["decision"] == "BLOCK"

    def test_dead_end_increase_triggers_warn(self):
        base = _make_scorecard(dead_end_ratio=0.05)
        curr = _make_scorecard(dead_end_ratio=0.15)  # +0.10 > 0.05 threshold
        delta = canary_gate.compute_delta(base, curr)
        # health_score same (90→90), so base decision is PASS
        # dead_end increase should overlay WARN
        base_pass = _make_scorecard(dead_end_ratio=0.05, health_score=90)
        curr_pass = _make_scorecard(dead_end_ratio=0.15, health_score=90)
        delta2 = canary_gate.compute_delta(base_pass, curr_pass)
        gate = canary_gate.decide_gate(delta2, curr_pass, base_pass)
        assert gate["decision"] == "WARN"

    def test_link_rot_increase_triggers_review(self):
        base = _make_scorecard(link_rot=0.02)
        curr = _make_scorecard(link_rot=0.15)  # +0.13 > 0.10 threshold
        base2 = _make_scorecard(link_rot=0.02, health_score=90)
        curr2 = _make_scorecard(link_rot=0.15, health_score=90)
        delta = canary_gate.compute_delta(base2, curr2)
        gate = canary_gate.decide_gate(delta, curr2, base2)
        assert gate["decision"] == "REVIEW"

    def test_isolated_increase_triggers_warn(self):
        base = _make_scorecard(isolated_ratio=0.05)
        curr = _make_scorecard(isolated_ratio=0.30)  # +0.25 > 0.20 threshold
        base2 = _make_scorecard(isolated_ratio=0.05, health_score=90)
        curr2 = _make_scorecard(isolated_ratio=0.30, health_score=90)
        delta = canary_gate.compute_delta(base2, curr2)
        gate = canary_gate.decide_gate(delta, curr2, base2)
        assert gate["decision"] == "WARN"

    def test_high_risk_canary_overlay(self):
        base = _make_scorecard(health_score=90, canary="low_risk")
        curr = _make_scorecard(health_score=90, canary="high_risk")
        delta = canary_gate.compute_delta(base, curr)
        gate = canary_gate.decide_gate(delta, curr, base)
        assert gate["decision"] == "REVIEW"

    def test_moderate_risk_overlay_on_pass(self):
        base = _make_scorecard(health_score=90, canary="low_risk")
        curr = _make_scorecard(health_score=90, canary="moderate_risk")
        delta = canary_gate.compute_delta(base, curr)
        gate = canary_gate.decide_gate(delta, curr, base)
        assert gate["decision"] == "WARN"

    def test_escalation_keeps_highest(self):
        # health_drop → WARN + dead_end → WARN + link_rot → REVIEW
        # final should be REVIEW (max severity wins)
        base = _make_scorecard(health_score=90, dead_end_ratio=0.05, link_rot=0.02)
        curr = _make_scorecard(health_score=87, dead_end_ratio=0.12, link_rot=0.15)
        delta = canary_gate.compute_delta(base, curr)
        gate = canary_gate.decide_gate(delta, curr, base)
        assert gate["decision"] == "REVIEW"

    def test_d8_coverage_thresholds(self):
        base = _make_scorecard(coverage_score=8)

        warning = _make_scorecard(coverage_score=4)
        warning_gate = canary_gate.decide_gate(
            canary_gate.compute_delta(base, warning), warning, base,
        )
        assert warning_gate["decision"] == "WARN"
        assert warning_gate["violations"][0]["metric"] == "quality.coverage_score"

        blocked = _make_scorecard(coverage_score=2)
        blocked_gate = canary_gate.decide_gate(
            canary_gate.compute_delta(base, blocked), blocked, base,
        )
        assert blocked_gate["decision"] == "BLOCK"

    def test_d11_alignment_drop_thresholds(self):
        base = _make_scorecard(
            consistency_score=2, ontology_alignment_score=8,
        )

        warning = _make_scorecard(
            consistency_score=10, ontology_alignment_score=7.7,
        )
        warning_gate = canary_gate.decide_gate(
            canary_gate.compute_delta(base, warning), warning, base,
        )
        assert warning_gate["decision"] == "WARN"
        assert warning_gate["violations"][0]["metric"] == "ontology_alignment"

        blocked = _make_scorecard(
            consistency_score=10, ontology_alignment_score=7.4,
        )
        blocked_gate = canary_gate.decide_gate(
            canary_gate.compute_delta(base, blocked), blocked, base,
        )
        assert blocked_gate["decision"] == "BLOCK"

    def test_missing_diagnosis_warns_with_pipeline_instruction(self):
        """P0-3 regression: missing diagnosis degrades to WARN, not BLOCK,
        so the documented exit-0 path is reachable without --canary-data."""
        base = _make_scorecard()
        current = _make_scorecard(ontology_alignment_score=None)
        gate = canary_gate.decide_gate(
            canary_gate.compute_delta(base, current), current, base,
        )

        assert gate["decision"] == "WARN"
        assert gate["message"] == (
            "Missing diagnosis data — run graph-test-suite "
            "--export-canary-data first"
        )

    def test_unmapped_entities_trigger_warn(self):
        base = _make_scorecard()
        curr = _make_scorecard(unmapped_entities=1)
        gate = canary_gate.decide_gate(
            canary_gate.compute_delta(base, curr), curr, base,
        )
        assert gate["decision"] == "WARN"
        assert gate["violations"] == [{
            "metric": "unmapped_entities",
            "current": 1,
            "delta": 1,
            "severity": "WARN",
        }]


class TestRenderJson:
    def test_json_output_contains_required_fields(self):
        sc = _make_scorecard()
        delta = canary_gate.compute_delta(sc, sc)
        gate = canary_gate.decide_gate(delta, sc, sc)
        output = canary_gate.render_json(gate, delta, sc, sc)
        data = json.loads(output)
        assert data["status"] == "PASS"
        assert "gate_decision" in data
        assert "violations" in data
        assert "delta" in data
        assert "baseline" in data
        assert "current" in data
        assert "risk_eligibility" in data


class TestRenderReport:
    def test_report_contains_gate_decision(self):
        sc = _make_scorecard()
        delta = canary_gate.compute_delta(sc, sc)
        gate = canary_gate.decide_gate(delta, sc, sc)
        report = canary_gate.render_report(gate, delta, sc, sc, "wiki")
        assert "Gate Decision" in report
        assert "PASS" in report
        assert "Scorecard Delta" in report
        assert "Risk Eligibility" in report
        assert "Baseline Info" in report


class TestSeverityHelpers:
    def test_max_severity(self):
        assert canary_gate._max_severity("PASS", "WARN") == "WARN"
        assert canary_gate._max_severity("WARN", "PASS") == "WARN"
        assert canary_gate._max_severity("REVIEW", "BLOCK") == "BLOCK"
        assert canary_gate._max_severity("PASS", "PASS") == "PASS"


class TestFlattenMetrics:
    def test_flatten_nonempty(self):
        sc = _make_scorecard(health_score=90)
        flat = canary_gate._flatten_metrics(sc)
        assert flat.get("composite.health_score") == 90
        assert flat.get("structure.dead_end_ratio") == 0.05

    def test_flatten_none(self):
        assert canary_gate._flatten_metrics(None) == {}


class TestCliIntegration:
    def test_exported_pipeline_data_reaches_canary_gate(self, tmp_path: Path):
        baseline_path = tmp_path / "test-baseline.json"
        canary_data_path = tmp_path / "test-canary-data.json"
        graph_suite = os.path.join(REPO_ROOT, "scripts", "graph-test-suite.py")
        canary_script = os.path.join(REPO_ROOT, "scripts", "canary-gate.py")
        wiki_root = os.path.join(REPO_ROOT, "examples", "mini-wiki")
        ontology_path = os.path.join(
            REPO_ROOT, "examples", "mini-ontology.jsonl",
        )

        suite_result = subprocess.run(
            [
                sys.executable, graph_suite,
                "--source", "wiki,ontology",
                "--wiki-root", wiki_root,
                "--ontology-path", ontology_path,
                "--scorecard",
                "--export-canary-data", str(canary_data_path),
            ],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        assert suite_result.returncode in (0, 1), suite_result.stderr
        baseline_path.write_text(suite_result.stdout, encoding="utf-8")
        exported = json.loads(canary_data_path.read_text(encoding="utf-8"))
        assert exported["fusion_values"]["coverage_score"] is not None
        assert exported["ontology_alignment"]["score"] is not None

        gate_result = subprocess.run(
            [
                sys.executable, canary_script,
                "--canary-data", str(canary_data_path),
                "--baseline", str(baseline_path),
                "--source", "wiki,ontology",
                "--wiki-root", wiki_root,
                "--ontology-path", ontology_path,
                "--json",
            ],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        assert gate_result.returncode in (0, 1, 2), gate_result.stderr
        gate = json.loads(gate_result.stdout)
        assert gate["gate_decision"] in {"PASS", "WARN", "REVIEW"}
        assert gate.get("message") != canary_gate._MISSING_DIAGNOSIS_MESSAGE
