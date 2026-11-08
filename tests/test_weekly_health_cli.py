"""Behaviour-level tests for `weekly-health-check.py` exit codes (issue #1).

The check used to always exit 0 — the verdict lived only in the text output,
so cron/CI reading `$?` saw a false green. These tests drive the real CLI:
they assert the process exit code and the JSON/text verdict, not internal
variables.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
MINI_WIKI = REPO_ROOT / "examples" / "mini-wiki"
WEEKLY = SCRIPTS / "weekly-health-check.py"
ANALYZER = SCRIPTS / "wiki_analyzer.py"

# Pages that are linked from elsewhere: removing them creates broken links
# and drops edges, i.e. a genuine regression.
DEGRADE_PAGES = ("concepts/hashing.md", "concepts/merge-sort.md")


def _copy_wiki(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    shutil.copytree(MINI_WIKI, wiki)
    return wiki


def _save_baseline(wiki: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(ANALYZER), "baseline-save",
         "--source", "wiki", "--wiki-root", str(wiki)],
        capture_output=True, text=True, timeout=180, cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Baseline saved to" in proc.stdout


def _degrade(wiki: Path) -> None:
    for relative in DEGRADE_PAGES:
        (wiki / relative).unlink()


def _weekly(wiki: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(WEEKLY), "--source", "wiki",
         "--wiki-root", str(wiki), *extra],
        capture_output=True, text=True, timeout=180, cwd=str(REPO_ROOT),
    )


@pytest.fixture
def baselined_wiki(tmp_path: Path) -> Path:
    wiki = _copy_wiki(tmp_path)
    _save_baseline(wiki)
    return wiki


def test_exits_zero_when_check_passes(baselined_wiki: Path):
    proc = _weekly(baselined_wiki)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "✅ 全部指标正常" in proc.stdout


def test_exits_one_on_real_regression(baselined_wiki: Path):
    _degrade(baselined_wiki)

    proc = _weekly(baselined_wiki)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "🚨 告警" in proc.stdout
    assert "❌ 周检未通过" in proc.stdout


def test_exits_one_without_baseline(tmp_path: Path):
    wiki = _copy_wiki(tmp_path)  # no baseline saved

    proc = _weekly(wiki)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "(无基线)" in proc.stdout
    assert "❌ 周检未通过" in proc.stdout


def test_exits_one_on_corrupt_baseline(tmp_path: Path):
    wiki = _copy_wiki(tmp_path)
    (wiki / ".graph-baseline.json").write_text("{not json", encoding="utf-8")

    proc = _weekly(wiki)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "(无基线)" in proc.stdout


def test_exit_zero_flag_forces_success_status(baselined_wiki: Path):
    _degrade(baselined_wiki)

    proc = _weekly(baselined_wiki, "--exit-zero")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    # The verdict itself must not be softened by the escape hatch.
    assert "❌ 周检未通过" in proc.stdout


def test_json_mode_exit_code_reflects_verdict(baselined_wiki: Path):
    healthy = _weekly(baselined_wiki, "--json")
    assert healthy.returncode == 0, healthy.stdout + healthy.stderr
    assert json.loads(healthy.stdout)["pass"] is True

    _degrade(baselined_wiki)
    regressed = _weekly(baselined_wiki, "--json")
    payload = json.loads(regressed.stdout)
    assert regressed.returncode == 1, regressed.stdout + regressed.stderr
    assert payload["pass"] is False
    assert payload["alerts"]


def test_json_mode_exit_zero_flag_forces_success_status(baselined_wiki: Path):
    _degrade(baselined_wiki)

    proc = _weekly(baselined_wiki, "--json", "--exit-zero")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["pass"] is False


def test_empty_by_severity_baseline_does_not_hide_new_p0(baselined_wiki: Path):
    """End-to-end repro: `by_severity: {}` must not skip the P0 comparison.

    真正的「零问题」基线是 {by_severity: {}, total: 0}（旧写入方不补零键时的
    真实产物），此时按全零比较，P0 增加必须告警。
    """
    baseline_path = baselined_wiki / ".graph-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline["diagnosis_summary"]["by_severity"] = {}
    baseline["diagnosis_summary"]["total"] = 0
    baseline_path.write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (baselined_wiki / "concepts" / "rot.md").write_text(
        """---
title: "Rot"
source_theorist: systems
date_created: 2026-07-20
date_modified: 2026-07-20
---
# Rot

[[nope-a]] [[nope-b]] [[nope-c]] [[nope-d]] [[nope-e]] [[nope-f]]
""",
        encoding="utf-8",
    )

    proc = _weekly(baselined_wiki, "--json")

    payload = json.loads(proc.stdout)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert payload["pass"] is False
    assert any("P0 问题增加" in alert for alert in payload["alerts"])


def test_inconsistent_empty_by_severity_fails_without_fabricating_baseline(
    baselined_wiki: Path,
):
    """End-to-end: `{}` + total>0 ⇒ 判失败，且不得伪造「0 → N」的假增量。"""
    baseline_path = baselined_wiki / ".graph-baseline.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline["diagnosis_summary"]["by_severity"] = {}
    baseline["diagnosis_summary"]["total"] = 7  # 声称有问题却说不出分级
    baseline_path.write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (baselined_wiki / "concepts" / "rot.md").write_text(
        """---
title: "Rot"
source_theorist: systems
date_created: 2026-07-20
date_modified: 2026-07-20
---
# Rot

[[nope-a]] [[nope-b]] [[nope-c]] [[nope-d]] [[nope-e]] [[nope-f]]
""",
        encoding="utf-8",
    )

    proc = _weekly(baselined_wiki, "--json")

    payload = json.loads(proc.stdout)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert payload["pass"] is False
    assert any("不自洽" in error for error in payload["errors"])
    assert not any("P0 问题增加" in alert for alert in payload["alerts"])
