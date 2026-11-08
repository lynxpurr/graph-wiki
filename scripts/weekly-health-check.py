#!/usr/bin/env python3
"""Wiki/Ontology 质量周检 — 对比回归基线，检测退化、产生告警

支持单源与逗号分隔多源模式。

用法:
  python3 weekly-health-check.py --source wiki --wiki-root ~/wiki
  python3 weekly-health-check.py --source ontology --ontology-path <path>
  python3 weekly-health-check.py --source ontology --json
  python3 weekly-health-check.py --source ontology --print-report
  python3 weekly-health-check.py --source ontology --exit-zero  # 兼容旧行为

退出码: 0=通过, 1=未通过（退化告警 / 无基线 / 基线损坏 / 内部执行错误）。
`--exit-zero` 无条件返回 0（旧调用方的逃生开关），不影响判定结果本身。
"""

import subprocess, json, os, re, sys, argparse
from datetime import datetime

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from baseline_paths import resolve_baseline  # noqa: E402  (shared resolver)

PYTHON = os.environ.get("WIKI_GRAPH_PYTHON", sys.executable)

WIKI_ROOT = os.environ.get("WIKI_ROOT", os.path.expanduser("~/wiki"))
ONTOLOGY_PATH = os.environ.get(
    "ONTOLOGY_PATH",
    os.path.join(os.path.dirname(SCRIPTS_DIR), "..", "..", "..", "memory", "ontology", "graph.jsonl"),
)


def run(cmd, cwd=SCRIPTS_DIR):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
    return r.stdout, r.stderr, r.returncode


def _source_names(source):
    names = []
    for item in (source.split(',') if isinstance(source, str) else source):
        name = item.strip().lower()
        if name and name not in names:
            names.append(name)
    return names


def _count_by_severity(diagnosis):
    """Count current diagnostic issues using the baseline severity schema."""
    return {
        severity: sum(
            1 for item in diagnosis if item.get("severity") == severity
        )
        for severity in ("P0", "P1", "P2")
    }


def _baseline_severity(baseline):
    """取出基线里的 per-severity 计数；无法比较时返回 None。

    三种输入语义必须区分（issue #1 假绿第二分支）:

    * 空对象 ``{}`` —— **有信息量**：写入方会规范化写入全键，空对象等于
      「各严重度均为 0」，比较必须照常发生（P0 增加仍要告警）。
    * 缺 ``by_severity`` / 缺 ``diagnosis_summary`` / 值为 ``None`` ——
      **无信息量**：无法比较，不能静默当成 0（那正是假绿通道）。
    * 正常字典 —— 逐 severity 比较，缺某个键时回退 0（旧写入方行为）。
    """
    if not isinstance(baseline, dict):
        return None
    summary = baseline.get("diagnosis_summary")
    if not isinstance(summary, dict):
        return None
    by_severity = summary.get("by_severity")
    if not isinstance(by_severity, dict):
        return None
    return by_severity


def _baseline_resolution(source, ontology_path=None, wiki_root=None):
    """解析基线路径（与 baseline-save / canary-gate 共用同一规则）。"""
    return resolve_baseline(
        source,
        ontology_path=ontology_path or ONTOLOGY_PATH,
        wiki_root=os.path.expanduser(wiki_root) if wiki_root else WIKI_ROOT,
        default_dir=os.path.dirname(os.path.abspath(ONTOLOGY_PATH)),
    )


def _load_baseline(source, ontology_path=None, wiki_root=None):
    """Find regression baseline file based on source."""
    baseline_path = _baseline_resolution(source, ontology_path, wiki_root).path

    if not os.path.exists(baseline_path):
        return {}, baseline_path
    try:
        with open(baseline_path, "r", encoding="utf-8") as f:
            return json.load(f), baseline_path
    except (json.JSONDecodeError, PermissionError):
        return {}, baseline_path


def _run_diagnose(source, ontology_path=None, wiki_root=None, ragflow_path=None):
    """Run graph-diagnose --json and return parsed result."""
    cmd = [
        PYTHON, os.path.join(SCRIPTS_DIR, "graph-diagnose.py"),
        "--source", source, "--json", "--full",
    ]
    sources = _source_names(source)
    if "ontology" in sources and ontology_path:
        cmd.extend(["--ontology-path", ontology_path])
    if "wiki" in sources and wiki_root:
        cmd.extend(["--wiki-root", wiki_root])
    if "ragflow" in sources and ragflow_path:
        cmd.extend(["--ragflow-path", ragflow_path])

    stdout, stderr, rc = run(cmd)
    if rc != 0:
        return {"error": stderr[:500]}, stderr
    try:
        return json.loads(stdout), None
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse failed: {e}"}, stdout[:500]


def _run_test_suite(source, ontology_path=None, wiki_root=None, ragflow_path=None):
    """Run graph-test-suite --json and return parsed result."""
    cmd = [
        PYTHON, os.path.join(SCRIPTS_DIR, "graph-test-suite.py"),
        "--source", source, "--json",
    ]
    sources = _source_names(source)
    if "ontology" in sources and ontology_path:
        cmd.extend(["--ontology-path", ontology_path])
    if "wiki" in sources and wiki_root:
        cmd.extend(["--wiki-root", wiki_root])
    if "ragflow" in sources and ragflow_path:
        cmd.extend(["--ragflow-path", ragflow_path])

    stdout, stderr, rc = run(cmd)
    if rc != 0:
        return {}, stderr
    try:
        return json.loads(stdout), None
    except json.JSONDecodeError:
        return {}, stdout[:500]


def check_regression(source, ontology_path=None, wiki_root=None,
                     ragflow_path=None):
    """Run all checks and return structured result."""
    baseline, baseline_path = _load_baseline(source, ontology_path, wiki_root)
    resolution = _baseline_resolution(source, ontology_path, wiki_root)
    diag_result, diag_err = _run_diagnose(
        source, ontology_path, wiki_root, ragflow_path,
    )
    test_result, test_err = _run_test_suite(
        source, ontology_path, wiki_root, ragflow_path,
    )

    alerts = []
    checks = []

    # ── Score (read from test-suite, not diagnose) ──
    cur_total = test_result.get("summary", {}).get("score", 0)
    bl_total = baseline.get("scores", {}).get("total", 0)
    if not bl_total:
        bl_total = baseline.get("test_suite", {}).get("total_score", 0)

    if isinstance(cur_total, (int, float)) and isinstance(bl_total, (int, float)) and bl_total > 0 and cur_total > 0:
        delta = cur_total - bl_total
        if delta < -2:
            alerts.append(f"⚠️ 评分退化: {bl_total} → {cur_total} ({delta})")
        checks.append(f"评分: {cur_total} (基线 {bl_total}, delta={delta})")
    else:
        checks.append(f"评分: {cur_total} (无基线)")

    # ── Diagnostic issue counts by severity ──
    diagnosis = diag_result.get("diagnosis", [])
    current_severity = _count_by_severity(diagnosis)
    baseline_severity = _baseline_severity(baseline)
    severity_errors = []

    if baseline_severity is None:
        # 键缺失 / None ⇒ 无法比较。无基线文件本身已由下面的「无基线」错误
        # 覆盖，这里只在基线存在却缺严重度数据时补一条明确错误。
        if baseline:
            severity_errors.append(
                "基线缺少严重度数据，无法比较: "
                "diagnosis_summary.by_severity 不可用 — P0/P1/P2 回归对比未执行；"
                f"请重新运行 wiki_analyzer.py baseline-save --source {source}"
            )
        for severity in ("P0", "P1", "P2"):
            checks.append(f"{severity} 问题: {current_severity[severity]} (无基线)")
    else:
        for severity in ("P0", "P1", "P2"):
            current_count = current_severity[severity]
            # 空对象 {} 与「旧基线缺某个键」都回退 0（{} = 全零，有信息量）。
            baseline_count = baseline_severity.get(severity)
            if baseline_count is None:
                baseline_count = 0
            if not isinstance(baseline_count, int) or isinstance(baseline_count, bool):
                severity_errors.append(
                    f"基线严重度数据无效 ({severity}={baseline_count!r})，无法比较"
                )
                checks.append(f"{severity} 问题: {current_count} (无基线)")
                continue
            if current_count > baseline_count:
                alerts.append(
                    f"⚠️ {severity} 问题增加: {baseline_count} → {current_count}"
                )
            checks.append(
                f"{severity} 问题: {current_count} (基线 {baseline_count})"
            )

    # ── Graph stats (unified path for both sources) ──
    gs = diag_result.get("graph_stats", {})
    cur_nodes = gs.get("nodes", 0)
    cur_edges = gs.get("edges", 0)
    bl_nodes = baseline.get("graph_stats", {}).get("nodes", 0)
    bl_edges = baseline.get("graph_stats", {}).get("edges", 0)
    if bl_nodes > 0 and cur_nodes < bl_nodes - 3:
        alerts.append(f"⚠️ 节点丢失: {bl_nodes} → {cur_nodes}")
    checks.append(f"节点: {cur_nodes} (基线 {bl_nodes})")
    if bl_edges > 0 and cur_edges < bl_edges - 5:
        alerts.append(f"⚠️ 边减少: {bl_edges} → {cur_edges}")
    checks.append(f"边: {cur_edges} (基线 {bl_edges})")

    # ── Multi-source fusion regression (D8-D10) ──
    multi_source = diag_result.get("multi_source", {})
    if len(_source_names(source)) > 1:
        current_d8 = multi_source.get("D8", {})
        baseline_multi = baseline.get("multi_source", {})
        baseline_d8 = baseline_multi.get("D8", {})
        current_coverage = current_d8.get("wiki_coverage")
        baseline_coverage = baseline_d8.get("wiki_coverage")
        if isinstance(current_coverage, (int, float)):
            if isinstance(baseline_coverage, (int, float)):
                coverage_delta = current_coverage - baseline_coverage
                if coverage_delta < -0.10:
                    alerts.append(
                        f"⚠️ 跨源覆盖下降: {baseline_coverage:.1%} → "
                        f"{current_coverage:.1%} ({coverage_delta:.1%})"
                    )
                checks.append(
                    f"跨源覆盖: {current_coverage:.1%} "
                    f"(基线 {baseline_coverage:.1%}, delta={coverage_delta:.1%})"
                )
            else:
                checks.append(f"跨源覆盖: {current_coverage:.1%} (无多源基线)")
        current_conflicts = current_d8.get("conflicts", 0)
        baseline_conflicts = baseline_d8.get("conflicts", 0)
        if current_conflicts > baseline_conflicts:
            alerts.append(
                f"⚠️ 新增合并冲突: {baseline_conflicts} → {current_conflicts}"
            )
        checks.append(f"合并冲突: {current_conflicts} (基线 {baseline_conflicts})")

    errors = []
    if diag_err:
        errors.append(f"diagnose error: {diag_err[:200]}")
    if test_err:
        errors.append(f"test-suite error: {test_err[:200]}")

    # 无基线时必须显式失败，不能静默空转成 pass=true（issue #1 假绿护栏）。
    if not baseline:
        errors.append(
            f"无基线: {baseline_path} 不存在或无法解析 — 回归对比未执行；"
            f"请先运行 wiki_analyzer.py baseline-save --source {source}"
        )
    errors.extend(severity_errors)

    # v3.1.1 (C5): pass = no alerts AND no errors (false-green fix)
    passed = len(alerts) == 0 and len(errors) == 0

    return {
        "timestamp": datetime.now().isoformat(),
        "source": source,
        "multi_source": multi_source,
        "diagnosis_summary": {"by_severity": current_severity},
        "baseline_path": baseline_path,
        "baseline_found": bool(baseline),
        "baseline_migration": resolution.migration_note,
        "checks": checks,
        "alerts": alerts,
        "errors": errors,
        "pass": passed,
    }


def format_report(result):
    """Generate human-readable report."""
    lines = [
        f"📊 周检 — {result['source']} — {result['timestamp'][:19]}",
        f"基线: {result['baseline_path']}",
    ]
    if result.get("baseline_migration"):
        lines.append(f"⚠️ {result['baseline_migration']}")
    if not result.get("baseline_found", True):
        lines.append("⚠️ (无基线) 回归对比未执行")
    lines.append("")
    for c in result["checks"]:
        lines.append(f"  {c}")
    if result["errors"]:
        lines.append("")
        lines.append("⚠️ 执行错误:")
        for e in result["errors"]:
            lines.append(f"  {e}")
    if result["alerts"]:
        lines.append("")
        lines.append("🚨 告警:")
        for a in result["alerts"]:
            lines.append(f"  {a}")
    if not result.get("pass", True):
        lines.append("")
        lines.append("❌ 周检未通过（见上方告警/执行错误）")
    elif not result["alerts"]:
        lines.append("")
        lines.append("✅ 全部指标正常，无退化")
    return "\n".join(lines)


# ══════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════

EXIT_PASS = 0
EXIT_FAIL = 1


def _build_parser():
    parser = argparse.ArgumentParser(description="Wiki/Ontology 质量周检")
    parser.add_argument("--source", default="wiki",
                        help="wiki, ontology, ragflow, or comma-separated sources")
    parser.add_argument("--ontology-path", default=ONTOLOGY_PATH,
                        help="Ontology JSONL 路径")
    parser.add_argument("--wiki-root", default=WIKI_ROOT,
                        help="Wiki 根目录")
    parser.add_argument("--ragflow-path", default=None,
                        help="Offline ragflow JSONL file or chunk JSON directory")
    parser.add_argument("--json", action="store_true",
                        help="JSON 输出")
    parser.add_argument("--print-report", action="store_true",
                        help="打印人类可读报告到 stdout")
    parser.add_argument("--exit-zero", action="store_true",
                        help="无论判定结果如何都以 0 退出（旧调用方逃生开关）")
    return parser


def main(argv=None):
    """Run the weekly check and return an exit code (0=pass, 1=fail)."""
    args = _build_parser().parse_args(argv)

    result = check_regression(
        source=args.source,
        ontology_path=args.ontology_path,
        wiki_root=args.wiki_root,
        ragflow_path=args.ragflow_path,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_report(result))

    if args.exit_zero:
        return EXIT_PASS
    return EXIT_PASS if result.get("pass") else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
