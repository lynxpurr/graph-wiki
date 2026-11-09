#!/usr/bin/env python3
"""wiki-graph 分析 CLI — wiki_analyzer.py

用法:
  python3 wiki_analyzer.py analyze        # 完整分析 → JSON to stdout
  python3 wiki_analyzer.py baseline-save  # 保存评分基线
  python3 wiki_analyzer.py trend-check    # 对比基线，检测退化
  python3 wiki_analyzer.py specs          # 诊断 → 结构化任务清单
  python3 wiki_analyzer.py report-weekly  # 人类可读周报 Markdown
  python3 wiki_analyzer.py history-save   # 追加时间序列记录
  python3 wiki_analyzer.py history-trend  # 评分趋势分析
"""

import os, sys, json, subprocess
from pathlib import Path
from datetime import datetime, timezone

from baseline_paths import resolve_baseline, resolve_primary_path
from scorecard import build_scorecard
from utils import atomic_append_jsonl, atomic_write

__version__ = "3.2.0"

SCRIPTS_DIR = Path(__file__).resolve().parent

# Python 解释器路径：优先用 WIKI_GRAPH_PYTHON 环境变量（适配多 venv 场景）
_PYTHON_BIN = os.environ.get("WIKI_GRAPH_PYTHON", sys.executable)
_PYTHON_BIN = os.path.expanduser(_PYTHON_BIN)

ONTOLOGY_PATH = os.environ.get(
    "ONTOLOGY_PATH",
    str(Path(__file__).resolve().parent.parent / "examples" / "mini-ontology.jsonl"),
)
WIKI_ROOT = os.environ.get(
    "WIKI_ROOT",
    os.getcwd(),
)
BASELINE_PATH = os.path.join(os.path.dirname(ONTOLOGY_PATH), ".graph-baseline.json")
HISTORY_PATH = os.path.join(os.path.dirname(ONTOLOGY_PATH), ".graph-history.jsonl")


def _source_names(source: str | list[str]) -> list[str]:
    raw = source.split(',') if isinstance(source, str) else source
    names = []
    for item in raw:
        name = item.strip().lower()
        if name not in {"wiki", "ontology", "ragflow"}:
            raise ValueError(f"Unknown source: {name}")
        if name not in names:
            names.append(name)
    if not names:
        raise ValueError("At least one source is required")
    return names


def _baseline_path_for_source(source: str | list[str], ontology_path: str = None,
                              wiki_root: str = None) -> str:
    """基线路径唯一解析入口（见 baseline_paths.py，issue #1）。

    单源 wiki/ragflow → <wiki_root>/.graph-baseline.json
    仅 ontology      → dirname(ontology_path)/.graph-baseline.json
    多源             → <base_dir>/.graph-baseline-<slug>.json

    未显式给出路径参数时回退到 BASELINE_PATH 所在目录（保持既有默认行为，
    也让测试可以通过 monkeypatch BASELINE_PATH 控制落点）。
    """
    return resolve_primary_path(
        source,
        ontology_path=ontology_path,
        wiki_root=wiki_root,
        default_dir=os.path.dirname(BASELINE_PATH),
    )


def _baseline_resolution(source: str | list[str], ontology_path: str = None,
                         wiki_root: str = None):
    """读取用解析（含旧 .regression-baseline.json 回退 + 迁移提示）。"""
    return resolve_baseline(
        source,
        ontology_path=ontology_path,
        wiki_root=wiki_root,
        default_dir=os.path.dirname(BASELINE_PATH),
    )


def is_available() -> bool:
    """依赖检查：networkx 是否可用（community/louvain 已不再强制要求）。"""
    try:
        import networkx  # noqa: F401
        return True
    except ImportError:
        return False


def _run_script(script_name: str, *args) -> dict:
    """(deprecated) Generic subprocess runner — use typed runners instead."""
    return run_diagnose(*args) if script_name == "graph-diagnose.py" else run_test_suite(*args)


def run_diagnose(source: str = "ontology", ontology_path: str = None,
                  wiki_root: str = None, ragflow_path: str = None) -> dict:
    """Run graph-diagnose.py with explicit source/path routing."""
    script = SCRIPTS_DIR / "graph-diagnose.py"
    cmd = [_PYTHON_BIN, str(script), "--source", source, "--json", "--full"]
    sources = _source_names(source)
    if "ontology" in sources and ontology_path:
        cmd += ["--ontology-path", ontology_path]
    if "wiki" in sources and wiki_root:
        cmd += ["--wiki-root", wiki_root]
    if "ragflow" in sources and ragflow_path:
        cmd += ["--ragflow-path", ragflow_path]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        error_msg = result.stderr[:500] if result.stderr else "unknown error"
        raise RuntimeError(f"graph-diagnose.py failed: {error_msg}")
    return json.loads(result.stdout)


def run_test_suite(source: str = "ontology", ontology_path: str = None,
                    wiki_root: str = None, scorecard: bool = False,
                    diagnosis: dict = None,
                    ragflow_path: str = None) -> dict:
    """Run graph-test-suite.py with explicit source/path routing.

    scorecard mode passes the already-collected diagnosis over stdin. The test
    suite therefore never starts a second graph-diagnose process.
    """
    script = SCRIPTS_DIR / "graph-test-suite.py"
    cmd = [_PYTHON_BIN, str(script), "--source", source]
    input_payload = None
    if scorecard:
        cmd += ["--scorecard"]
        if diagnosis is not None:
            cmd += ["--diagnosis-json", "-"]
            input_payload = json.dumps(diagnosis, ensure_ascii=False)
    else:
        cmd += ["--json"]
    sources = _source_names(source)
    if "ontology" in sources and ontology_path:
        cmd += ["--ontology-path", ontology_path]
    if "wiki" in sources and wiki_root:
        cmd += ["--wiki-root", wiki_root]
    if "ragflow" in sources and ragflow_path:
        cmd += ["--ragflow-path", ragflow_path]
    result = subprocess.run(
        cmd, input=input_payload, capture_output=True, text=True, timeout=120,
    )
    payload = None
    if result.stdout.strip():
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = None
    if result.returncode != 0:
        # rc=1 且 stdout 是完整 scorecard = graph-test-suite 的「有低分维度」
        # 判定（它已经算完并输出了结果），不是崩溃。不能让这份 payload 被丢
        # 掉：丢弃会让基线落成 scores.total=null，周检于是永远显示「无基线」
        # 假绿（issue #1）。
        if result.returncode == 1 and isinstance(payload, dict):
            return payload
        error_msg = result.stderr[:500] if result.stderr else "unknown error"
        raise RuntimeError(f"graph-test-suite.py failed: {error_msg}")
    if payload is None:
        raise RuntimeError(
            "graph-test-suite.py returned invalid JSON: "
            f"{result.stdout[:200]}"
        )
    return payload


def analyze(source: str = "ontology", ontology_path: str = None,
            wiki_root: str = None, ragflow_path: str = None) -> dict:
    """运行 graph-diagnose + graph-test-suite，返回结构化结果。

    v3.1.1: 显式传递 source/path，不再硬编码 ontology 模式。
    """
    if not is_available():
        return {"error": "wiki-graph dependencies unavailable", "schema_version": "3.2.0",
                "success": False}

    sources = _source_names(source)
    source = ','.join(sources)
    if "ontology" in sources and ontology_path is None:
        ontology_path = ONTOLOGY_PATH

    diagnostic = run_diagnose(
        source=source, ontology_path=ontology_path, wiki_root=wiki_root,
        ragflow_path=ragflow_path,
    )
    diagnostic["success"] = True

    # Collect scores from graph-test-suite
    try:
        scorecard = run_test_suite(
            source=source,
            ontology_path=ontology_path,
            wiki_root=wiki_root,
            scorecard=True,
            diagnosis=diagnostic,
            ragflow_path=ragflow_path,
        )
        diagnostic["scorecard"] = scorecard
        legacy_scores = scorecard.get("legacy_scores", {})
        if legacy_scores:
            diagnostic["scores"] = legacy_scores
    except Exception as e:
        diagnostic["_test_suite_error"] = str(e)[:200]
        diagnostic["success"] = "partial"

    # Trend check
    try:
        trend = trend_check(diagnostic, ontology_path=ontology_path,
                            wiki_root=wiki_root)
        if trend:
            diagnostic["trend"] = trend
    except Exception as e:
        diagnostic["_trend_error"] = str(e)[:200]

    return diagnostic


def diagnostics_to_specs(result: dict) -> list[dict]:
    """诊断结果 → 结构化任务清单。

    P0 链接腐烂 → 标记自动处理
    P1 死胡同 → 标记自动处理
    P2 孤立 → 标记自动处理
    扩散/互惠/陈旧 → 只报告

    返回结构化任务 dict 列表（供下游工具消费）。
    """
    specs = []
    provenance = dict(result.get("provenance", {}))
    for item in result.get("diagnosis", []):
        dimension = item.get("dimension", "")
        is_fusion_finding = dimension in {"D8", "D9", "D10"}
        if not item.get("auto_queue", False) and not is_fusion_finding:
            continue

        severity = item.get("severity", "P2")
        task_type = item.get("task_type", "bugfix")

        specs.append({
            "title": item.get("title", f"wiki-graph {item.get('dimension', '?')}"),
            "priority": severity,
            "type": task_type,
            "risk": "low",
            "mode": "auto",
            "source": "wiki-graph",
            "diagnosis_dimension": dimension,
            "auto_queue": bool(item.get("auto_queue", False)),
            "description": item.get("recommendation", "No recommendation"),
            "evidence": item.get("evidence", {}),
            "acceptance_criteria": [
                f"{item.get('dimension','')}: 修复 {item.get('title','')}",
                "运行 graph-diagnose 确认诊断项消失",
            ],
            "tags": ["wiki-graph", "auto-diag", task_type],
            "provenance": provenance,
        })
    return specs


def baseline_save(result: dict, ontology_path: str = None,
                  wiki_root: str = None) -> dict:
    """Save a B3 scorecard baseline while retaining legacy score fields."""
    saved_at = datetime.now(timezone.utc).isoformat()
    scorecard = dict(result.get("scorecard") or build_scorecard(
        graph=None,
        diagnosis_data=result,
        source=result.get("source", "ontology"),
        legacy_scores=result.get("scores", {}),
        timestamp=result.get("timestamp") or saved_at,
    ))
    legacy_scores = result.get("scores") or scorecard.get("legacy_scores", {})
    scorecard["legacy_scores"] = legacy_scores
    baseline = {
        **scorecard,
        "baseline_schema_version": "3.2.0",
        "saved_at": saved_at,
        "graph_stats": result.get("graph_stats", {}),
        "multi_source": result.get("multi_source", {
            "status": "skipped", "note": "D8-D10 require multiple sources",
        }),
        # Compatibility contract for v3.0-v3.1 trend consumers.
        "scores": legacy_scores,
        "diagnosis_summary": {
            "total": len(result.get("diagnosis", [])),
            # 零计数 severity 也要落盘：否则读取方（weekly-health-check）会把
            # 缺失的 P0/P1 当成「无基线」（issue #1 同一类假绿）。
            "by_severity": _normalized_severity_counts(result),
        },
    }
    baseline_path = _baseline_path_for_source(
        result.get("source", "ontology"),
        ontology_path=ontology_path,
        wiki_root=wiki_root,
    )
    atomic_write(baseline_path, json.dumps(baseline, ensure_ascii=False, indent=2))
    return baseline


def _flatten_scorecard_metrics(scorecard: dict | None) -> dict[str, float]:
    flattened = {}
    metrics = (scorecard or {}).get("metrics", {})
    for group, values in metrics.items():
        if not isinstance(values, dict):
            continue
        for metric, value in values.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                flattened[f"{group}.{metric}"] = float(value)
    return flattened


def scorecard_delta(previous: dict | None, current: dict | None) -> dict:
    """Return per-metric numeric deltas with direction arrows."""
    previous_metrics = _flatten_scorecard_metrics(previous)
    current_metrics = _flatten_scorecard_metrics(current)
    deltas = {}
    for metric in sorted(previous_metrics.keys() & current_metrics.keys()):
        before = previous_metrics[metric]
        after = current_metrics[metric]
        delta = round(after - before, 6)
        deltas[metric] = {
            "previous": before,
            "current": after,
            "delta": delta,
            "direction": "↑" if delta > 0 else "↓" if delta < 0 else "→",
        }
    return deltas


def trend_check(result: dict, ontology_path: str = None,
                wiki_root: str = None) -> dict | None:
    """对比基线，检测知识健康度退化。

    返回: {previous_score, current_score, delta, alert, alert_threshold}
    评分下降 ≥ 10 分 → alert=True
    """
    resolution = _baseline_resolution(
        result.get("source", "ontology"),
        ontology_path=ontology_path,
        wiki_root=wiki_root,
    )
    baseline_path = resolution.path
    if not os.path.exists(baseline_path):
        return None

    try:
        with open(baseline_path, "r", encoding="utf-8") as f:
            baseline = json.load(f)
    except (json.JSONDecodeError, PermissionError):
        return None

    prev_scores = baseline.get("scores") or baseline.get("legacy_scores", {})
    curr_scores = result.get("scores") or result.get("scorecard", {}).get(
        "legacy_scores", {}
    )
    prev_total = prev_scores.get("total", 0)
    curr_total = curr_scores.get("total", 0)

    if prev_total == 0:
        return None

    delta = curr_total - prev_total
    trend = {
        "previous_score": prev_total,
        "current_score": curr_total,
        "delta": delta,
        "alert": delta <= -10,
        "alert_threshold": -10,
        "baseline_saved_at": baseline.get("saved_at", ""),
        "baseline_path": baseline_path,
    }
    if resolution.migration_note:
        trend["baseline_migration"] = resolution.migration_note
    metric_delta = scorecard_delta(baseline, result.get("scorecard"))
    if metric_delta:
        trend["scorecard_delta"] = metric_delta
    sources = _source_names(result.get("source", "ontology"))
    if len(sources) > 1:
        previous_multi = baseline.get("multi_source", {})
        current_multi = result.get("multi_source", {})
        previous_d8 = previous_multi.get("D8", {})
        current_d8 = current_multi.get("D8", {})
        previous_coverage = previous_d8.get("wiki_coverage")
        current_coverage = current_d8.get("wiki_coverage")
        previous_conflicts = previous_d8.get("conflicts", 0)
        current_conflicts = current_d8.get("conflicts", 0)
        fusion_alerts = []
        if isinstance(previous_coverage, (int, float)) and isinstance(
            current_coverage, (int, float),
        ):
            coverage_delta = current_coverage - previous_coverage
            trend["coverage"] = {
                "previous": previous_coverage,
                "current": current_coverage,
                "delta": round(coverage_delta, 4),
            }
            if coverage_delta < -0.10:
                fusion_alerts.append("coverage_drop")
        conflict_delta = current_conflicts - previous_conflicts
        trend["merge_conflicts"] = {
            "previous": previous_conflicts,
            "current": current_conflicts,
            "delta": conflict_delta,
        }
        if conflict_delta > 0:
            fusion_alerts.append("new_conflicts")
        if fusion_alerts:
            trend["fusion_alerts"] = fusion_alerts
            trend["alert"] = True
    return trend


def report_weekly(result: dict) -> str:
    """生成人类可读周报 Markdown。"""
    gs = result.get("graph_stats", {})
    scores = result.get("scores", {})
    diags = result.get("diagnosis", [])
    trend = result.get("trend", {})

    by_sev = _count_by_severity(result)

    lines = [
        "# wiki-graph 周报",
        f"生成时间: {result.get('timestamp', '?')}",
        f"数据源: {result.get('source', '?')}",
        "",
        "## 图统计",
        f"- 节点: {gs.get('nodes', '?')}",
        f"- 边: {gs.get('edges', '?')}",
        f"- 孤立节点: {gs.get('isolated', '?')}",
        f"- 密度: {gs.get('density', '?')}",
        f"- 连通分量: {gs.get('components', '?')}",
        "",
        "## 健康评分",
        f"- 总分: {scores.get('total', '?')} / {scores.get('max', '?')}",
    ]

    if trend:
        delta = trend.get("delta", 0)
        arrow = "↑" if delta > 0 else "↓" if delta < 0 else "→"
        alert = " ⚠️ ALERT" if trend.get("alert") else ""
        lines.append(f"- 趋势: {trend.get('previous_score','?')} → {trend.get('current_score','?')} ({arrow}{abs(delta)}){alert}")

    multi_source = result.get("multi_source", {})
    if multi_source.get("status") == "computed":
        lines.extend([
            "",
            "## 多源融合",
            f"- D8 跨源覆盖: {multi_source.get('D8', {}).get('wiki_coverage', 0):.1%}",
            f"- D9 类型一致: {multi_source.get('D9', {}).get('consistency_ratio', 0):.1%}",
            f"- D10 信息增益: {multi_source.get('D10', {}).get('information_gain', 0):.1%}",
            f"- 合并冲突: {multi_source.get('D8', {}).get('conflicts', 0)}",
        ])

    lines.extend([
        "",
        "## 诊断摘要",
        f"总诊断: {len(diags)}",
        f"- P0: {by_sev.get('P0', 0)}",
        f"- P1: {by_sev.get('P1', 0)}",
        f"- P2: {by_sev.get('P2', 0)}",
        "",
        "## 可自动处理",
        f"{sum(1 for d in diags if d.get('auto_queue'))} / {len(diags)}",
    ])

    if trend and trend.get("alert"):
        lines.extend([
            "",
            "## ⚠️ 退化告警",
            f"评分从 {trend.get('previous_score')} 降到 {trend.get('current_score')} (delta={trend.get('delta')})",
            "建议手动 review 知识结构变更。",
        ])

    return "\n".join(lines)


def history_save(result: dict) -> str:
    """追加一条评分记录到时间序列文件。

    返回写入的 JSONL 路径。
    """
    scores = result.get("scores", {})
    gs = result.get("graph_stats", {})
    by_sev = _count_by_severity(result)

    record = {
        "timestamp": result.get("timestamp", datetime.now(timezone.utc).isoformat()),
        "total_score": scores.get("total", 0),
        "nodes": gs.get("nodes", 0),
        "edges": gs.get("edges", 0),
        "isolated": gs.get("isolated", 0),
        "density": gs.get("density", 0),
        "components": gs.get("components", 0),
        "p0_count": by_sev.get("P0", 0),
        "p1_count": by_sev.get("P1", 0),
        "p2_count": by_sev.get("P2", 0),
        "scorecard": result.get("scorecard"),
        "multi_source": result.get("multi_source"),
    }
    atomic_append_jsonl(HISTORY_PATH, record)
    return HISTORY_PATH


def history_trend(weeks: int = 4) -> dict:
    """最近 N 周的评分趋势分析。

    返回:
      {
        "records": int,
        "weeks": int,
        "trend": [{"week": "2026-W28", "avg_score": 78.5, "records": 2}, ...],
        "slope": float (线性回归斜率，>0 改善，<0 退化),
        "current_week": {"avg_score": 80, "records": 1},
      }
    """
    if not os.path.exists(HISTORY_PATH):
        return {"records": 0, "weeks": weeks, "trend": [], "error": "No history file found"}

    from datetime import datetime as dt_cls
    from collections import defaultdict

    records = []
    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts_str = rec.get("timestamp", "")
                if ts_str:
                    t = dt_cls.fromisoformat(ts_str.replace("Z", "+00:00"))
                    week_key = t.strftime("%G-W%V")
                    records.append({
                        "week": week_key,
                        "total_score": rec.get("total_score", 0),
                        "nodes": rec.get("nodes", 0),
                        "edges": rec.get("edges", 0),
                        "scorecard": rec.get("scorecard") or (
                            rec if isinstance(rec.get("metrics"), dict) else None
                        ),
                    })
            except (json.JSONDecodeError, ValueError):
                continue

    if not records:
        return {"records": 0, "weeks": weeks, "trend": []}

    # Aggregate by week
    by_week = defaultdict(list)
    for r in records:
        by_week[r["week"]].append(r["total_score"])

    sorted_weeks = sorted(by_week.keys())[-weeks:]
    trend = []
    for wk in sorted_weeks:
        sv = by_week[wk]
        trend.append({
            "week": wk,
            "avg_score": round(sum(sv) / len(sv), 1),
            "min": min(sv),
            "max": max(sv),
            "records": len(sv),
        })

    # Linear regression slope
    slope = 0.0
    if len(trend) >= 3:
        xs = list(range(len(trend)))
        ys = [t["avg_score"] for t in trend]
        n = len(xs)
        sum_x = sum(xs)
        sum_y = sum(ys)
        sum_xy = sum(x * y for x, y in zip(xs, ys))
        sum_xx = sum(x * x for x in xs)
        denom = n * sum_xx - sum_x * sum_x
        if denom != 0:
            slope = round((n * sum_xy - sum_x * sum_y) / denom, 2)

    result = {
        "records": len(records),
        "weeks": weeks,
        "trend": trend,
        "slope": slope,
        "slope_label": "improving" if slope > 0 else "declining" if slope < 0 else "stable",
        "current_week": trend[-1] if trend else None,
    }
    scorecard_records = [r["scorecard"] for r in records if r.get("scorecard")]
    if len(scorecard_records) >= 2:
        result["scorecard_delta"] = scorecard_delta(
            scorecard_records[-2], scorecard_records[-1],
        )
        result["scorecard_baselines"] = [
            scorecard_records[-2].get("baseline_id"),
            scorecard_records[-1].get("baseline_id"),
        ]
    return result


def _count_by_severity(result: dict) -> dict:
    counts = {}
    for item in result.get("diagnosis", []):
        sev = item.get("severity", "?")
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def _normalized_severity_counts(result: dict) -> dict:
    """基线落盘用：P0/P1/P2 始终存在（缺省=0）。"""
    counts = _count_by_severity(result)
    for severity in ("P0", "P1", "P2"):
        counts.setdefault(severity, 0)
    return counts


# ══════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="wiki-graph analyzer CLI")
    parser.add_argument("action", choices=["analyze", "baseline-save", "trend-check", "specs",
                                              "report-weekly",
                                              "history-save", "history-trend"])
    parser.add_argument("--source", default="ontology",
                        help="wiki, ontology, ragflow, or comma-separated sources")
    parser.add_argument("--ontology-path", default=ONTOLOGY_PATH)
    parser.add_argument("--wiki-root", default=WIKI_ROOT)
    parser.add_argument("--ragflow-path", default=None,
                        help="Offline ragflow JSONL file or chunk JSON directory")
    parser.add_argument("--print-report", action="store_true", help="Print report-weekly to stdout")
    parser.add_argument("--weeks", type=int, default=4, help="history-trend: 回溯周数 (default: 4)")
    args = parser.parse_args()

    if not is_available():
        print(json.dumps({"error": "wiki-graph dependencies unavailable"}))
        sys.exit(1)

    def analyze_from_args():
        sources = _source_names(args.source)
        return analyze(
            source=','.join(sources),
            ontology_path=args.ontology_path if "ontology" in sources else None,
            wiki_root=args.wiki_root if "wiki" in sources else None,
            ragflow_path=args.ragflow_path if "ragflow" in sources else None,
        )

    if args.action == "analyze":
        result = analyze_from_args()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif args.action == "baseline-save":
        result = analyze_from_args()
        baseline_save(result, ontology_path=args.ontology_path,
                      wiki_root=args.wiki_root)
        baseline_path = _baseline_path_for_source(
            result.get('source', args.source),
            ontology_path=args.ontology_path,
            wiki_root=args.wiki_root,
        )
        print(f"Baseline saved to {baseline_path}")
        legacy = _baseline_resolution(
            result.get('source', args.source),
            ontology_path=args.ontology_path,
            wiki_root=args.wiki_root,
        ).legacy_path
        if legacy and os.path.exists(legacy):
            print(
                f"迁移提示: 旧基线 {legacy} 仍存在（已不再读取）。"
                f"请确认新基线可用后删除旧文件。"
            )

    elif args.action == "trend-check":
        result = analyze_from_args()
        trend = trend_check(result, ontology_path=args.ontology_path,
                            wiki_root=args.wiki_root)
        print(json.dumps(trend, ensure_ascii=False, indent=2))

    elif args.action == "specs":
        result = analyze_from_args()
        specs = diagnostics_to_specs(result)
        print(json.dumps(specs, ensure_ascii=False, indent=2))

    elif args.action == "report-weekly":
        result = analyze_from_args()
        report = report_weekly(result)
        if args.print_report:
            print(report)

    elif args.action == "history-save":
        result = analyze_from_args()
        path = history_save(result)
        print(f"History record saved to {path}: score={result.get('scores', {}).get('total', '?')}")

    elif args.action == "history-trend":
        trend = history_trend(weeks=args.weeks)
        print(json.dumps(trend, ensure_ascii=False, indent=2))
