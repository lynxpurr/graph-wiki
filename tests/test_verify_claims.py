"""Tests for scripts/verify-claims.py — the checker has to be checked too."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "verify-claims.py"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "claims-wiki"


def _run(body: str, tmp_path) -> subprocess.CompletedProcess:
    body_file = tmp_path / "body.md"
    body_file.write_text(body, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--body-file", str(body_file),
         "--wiki-root", str(FIXTURE)],
        capture_output=True, text=True, timeout=300,
    )


def test_missing_block_is_rejected(tmp_path):
    out = _run("no claims here at all\n", tmp_path)
    assert out.returncode == 2, out.stdout + out.stderr
    assert "no claims block" in out.stderr


def test_valid_claims_pass(tmp_path):
    body = (
        "<!-- claims:begin\n"
        "json: linkrot_summary.unique_broken\n"
        "json: diagnosis[].severity\n"
        "title: 代码围栏/行内代码内链接未检查\n"
        "absent: scores.legacy_hardcoded_max\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "4/4 claims hold" in out.stdout


def test_missing_json_path_fails(tmp_path):
    body = (
        "<!-- claims:begin\n"
        "json: linkrot_summary.nonexistent_field\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "not present in output" in out.stdout


def test_absent_claim_fails_when_path_exists(tmp_path):
    body = (
        "<!-- claims:begin\n"
        "absent: linkrot_summary.unique_broken\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "must not" in out.stdout


def test_title_claim_must_match(tmp_path):
    body = (
        "<!-- claims:begin\n"
        "title: 一个根本不存在的标题\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "no diagnosis title contains" in out.stdout


def test_short_no_claims_reason_rejected(tmp_path):
    body = (
        "<!-- claims:begin\n"
        "no-claims: short\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "reason too short" in out.stdout


def test_fixture_reproduces_expected_shape(tmp_path):
    """The fixture must keep producing the values the template claims."""
    body = (
        "<!-- claims:begin\n"
        "json: linkrot_summary.unique_broken\n"
        "json: linkrot_summary.skill_scheme_exempt\n"
        "json: linkrot_summary.code_block_exempt\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "unique_broken = 2" in out.stdout, out.stdout
    assert "skill_scheme_exempt = 1" in out.stdout, out.stdout
    assert "code_block_exempt = 1" in out.stdout, out.stdout