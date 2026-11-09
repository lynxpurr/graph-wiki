"""Tests for scripts/verify-claims.py — the checker has to be checked too.

Round 3 added adversarial cases: the gate was found bypassable by a second claims
block, by copying the PR template verbatim, by an empty title substring, and by a
perfunctory no-claims excuse (#14, #15).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "verify-claims.py"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "claims-wiki"
TEMPLATE = REPO_ROOT / ".github" / "pull_request_template.md"


def _run(body: str, tmp_path, changed: list[str] | None = None):
    body_file = tmp_path / "body.md"
    body_file.write_text(body, encoding="utf-8")
    cmd = [sys.executable, str(SCRIPT), "--body-file", str(body_file),
           "--wiki-root", str(FIXTURE)]
    if changed is not None:
        changed_file = tmp_path / "changed.txt"
        changed_file.write_text("\n".join(changed), encoding="utf-8")
        cmd += ["--changed-files", str(changed_file)]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


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
    out = _run(body, tmp_path, changed=["docs/readme.md"])
    assert out.returncode == 1, out.stdout + out.stderr
    assert "reason too short" in out.stdout


def test_fixture_reproduces_expected_shape(tmp_path):
    """The fixture must keep producing the values the docs claim."""
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


def test_multiple_claims_blocks_rejected(tmp_path):
    """Issue #14: a second block used to be dropped silently — a free bypass."""
    body = (
        "<!-- claims:begin\n"
        "json: linkrot_summary.unique_broken\n"
        "claims:end -->\n\n"
        "<!-- claims:begin\n"
        "json: this.path.is.completely.fabricated\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 2, out.stdout + out.stderr
    assert "2 claims blocks" in out.stderr


def test_empty_title_substring_rejected(tmp_path):
    """Issue #15: empty substring made every item match."""
    body = (
        "<!-- claims:begin\n"
        "title:\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "would match anything" in out.stdout


def test_template_placeholder_does_not_pass(tmp_path):
    """Issue #15: pasting the template verbatim must not satisfy the gate."""
    body = TEMPLATE.read_text(encoding="utf-8")
    out = _run(body, tmp_path)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "REPLACE_WITH_A_REAL_CLAIM" in out.stdout


def test_json_eq_passes_on_matching_value(tmp_path):
    body = (
        "<!-- claims:begin\n"
        "json-eq: linkrot_summary.unique_broken == 2\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "json-eq" in out.stdout


def test_json_eq_fails_on_wrong_value(tmp_path):
    """The value check kills the existence-only limitation for real numbers."""
    body = (
        "<!-- claims:begin\n"
        "json-eq: linkrot_summary.unique_broken == 99\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "expected 99" in out.stdout


def test_no_claims_requires_changed_files(tmp_path):
    """Issue #15: skipping without a diff to inspect is not allowed."""
    body = (
        "<!-- claims:begin\n"
        "no-claims: 只是调整了文档排版而已\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path, changed=None)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "cannot validate a skip" in out.stdout


def test_no_claims_rejected_when_code_changed(tmp_path):
    """Issue #15: a code change cannot escape checking through prose."""
    body = (
        "<!-- claims:begin\n"
        "no-claims: 这个改动纯粹是文档排版调整而已\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path, changed=["scripts/graph-diagnose.py"])
    assert out.returncode == 1, out.stdout + out.stderr
    assert "a skip is not allowed" in out.stdout


def test_no_claims_allowed_for_docs_only(tmp_path):
    body = (
        "<!-- claims:begin\n"
        "no-claims: 只改了 README 的措辞，无脚本行为变化\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path, changed=["README.md"])
    assert out.returncode == 0, out.stdout + out.stderr
    assert "no-claims" in out.stdout

def test_no_claims_rejected_when_workflow_changed(tmp_path):
    """Round-3 hole: a CI workflow edit is behaviour, not prose."""
    body = (
        "<!-- claims:begin\n"
        "no-claims: 只调整了 CI 工作流的注释排版\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path, changed=[".github/workflows/claims.yml"])
    assert out.returncode == 1, out.stdout + out.stderr
    assert "a skip is not allowed" in out.stdout


def test_no_claims_rejected_when_dependency_changed(tmp_path):
    """Round-3 hole: dependency pins change behaviour too."""
    body = (
        "<!-- claims:begin\n"
        "no-claims: 只是升级了依赖版本号而已\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path, changed=["requirements.txt"])
    assert out.returncode == 1, out.stdout + out.stderr
    assert "a skip is not allowed" in out.stdout


def test_no_claims_rejected_on_empty_changed_list(tmp_path):
    """Round-3 hole: an empty diff list made every skip look safe."""
    body = (
        "<!-- claims:begin\n"
        "no-claims: 这次什么都没改，放心\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path, changed=[])
    assert out.returncode == 1, out.stdout + out.stderr
    assert "changed-file list is empty" in out.stdout


def test_json_eq_rejects_float_for_int(tmp_path):
    """Round-3 hole: 2 == 2.0 in Python, so a float could assert an int."""
    body = (
        "<!-- claims:begin\n"
        "json-eq: linkrot_summary.unique_broken == 2.0\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "expected 2.0" in out.stdout


def test_json_eq_rejects_string_for_int(tmp_path):
    """A quoted number is a different claim than a number."""
    body = (
        "<!-- claims:begin\n"
        "json-eq: linkrot_summary.unique_broken == \"2\"\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 1, out.stdout + out.stderr


def test_json_eq_accepts_matching_int(tmp_path):
    """Control case: the fix must not break honest integer claims."""
    body = (
        "<!-- claims:begin\n"
        "json-eq: linkrot_summary.unique_broken == 2\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr
