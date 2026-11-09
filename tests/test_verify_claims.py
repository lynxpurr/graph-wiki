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

def test_zero_width_disguised_second_block_is_detected(tmp_path):
    """Round 4 S1: zero-width chars hid a second block from the block count."""
    body = (
        "<!-- claims:begin\n"
        "json: linkrot_summary.unique_broken\n"
        "claims:end -->\n\n"
        "<!-- cl\u200baims:begin\n"
        "json: fabricated.path\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 2, out.stdout + out.stderr
    assert "claims blocks found" in out.stderr


def test_inline_marker_mention_is_prose_not_block(tmp_path):
    """Round 4 S6: prose mentioning the marker used to count as an extra block."""
    body = (
        "Add a '<!-- claims:begin ... claims:end -->' block to your PR.\n\n"
        "<!-- claims:begin\n"
        "json: linkrot_summary.unique_broken\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr


def test_terminator_inside_a_value_does_not_truncate(tmp_path):
    """Round 4 S5: an inline terminator used to cut the block mid-value."""
    body = (
        "<!-- claims:begin\n"
        "json-eq: linkrot_summary.dedupe_key == claims:end -->\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    # The whole value is compared, not a truncated fragment: the claim simply fails.
    assert out.returncode == 1, out.stdout + out.stderr
    assert "dedupe_key" in out.stdout


def test_no_claims_rejected_for_extensionless_file(tmp_path):
    """Round 4 E2: a Makefile changes behaviour but has no extension."""
    body = (
        "<!-- claims:begin\n"
        "no-claims: 只是小改动没有行为变化\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path, changed=["Makefile"])
    assert out.returncode == 1, out.stdout + out.stderr
    assert "non-document change" in out.stdout


def test_no_claims_rejected_for_lock_file(tmp_path):
    """Round 4 E3: dependency locks pin behaviour."""
    body = (
        "<!-- claims:begin\n"
        "no-claims: 只是刷新了锁文件而已\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path, changed=["requirements.lock"])
    assert out.returncode == 1, out.stdout + out.stderr


def test_no_claims_rejected_for_quoted_path(tmp_path):
    """Round 4 E7: git can emit quoted paths; the quote must not defeat the check."""
    body = (
        "<!-- claims:begin\n"
        "no-claims: 只改了脚本注释\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path, changed=['"scripts/x.py"'])
    assert out.returncode == 1, out.stdout + out.stderr


def test_no_claims_rejected_for_dot_slash_and_absolute(tmp_path):
    """Round 4 E4/E5: path prefixes must not change the classification."""
    body = (
        "<!-- claims:begin\n"
        "no-claims: 只是调整脚本内部顺序\n"
        "claims:end -->\n"
    )
    for variant in ("./scripts/foo.py", "/abs/path/to/repo/scripts/foo.py"):
        out = _run(body, tmp_path, changed=[variant])
        assert out.returncode == 1, variant + out.stdout + out.stderr


def test_json_eq_rejects_array_path(tmp_path):
    """Round 4 J8: json: takes any element while json-eq took the first."""
    body = (
        "<!-- claims:begin\n"
        "json-eq: diagnosis[].severity == P0\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "does not accept [] paths" in out.stdout


def test_json_eq_rejects_bool_for_int(tmp_path):
    """Round 4 J1: True == 1 in Python; the gate must not conflate them."""
    body = (
        "<!-- claims:begin\n"
        "json-eq: linkrot_summary.unique_broken == true\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "expected True (bool)" in out.stdout


def test_invisible_hidden_block_beyond_the_original_set(tmp_path):
    """Round 5 self-check: U+200B-F was a list, not a principle. Any Cf will do."""
    body = (
        "<!-- claims:begin\n"
        "json-eq: linkrot_summary.unique_broken == 2\n"
        "claims:end -->\n"
        "\n"
        "<!-- cl{c}aims:begin\n"
        "json: nonexistent.path\n"
        "claims:end -->\n"
    )
    for ch in ("\u2060", "\u00ad", "\u202e", "\u180e"):
        out = _run(body.format(c=ch), tmp_path)
        assert out.returncode == 2, repr(ch) + out.stdout + out.stderr
        assert "claims blocks found" in (out.stdout + out.stderr)


def test_fenced_example_is_not_a_second_block(tmp_path):
    """Round 5 self-check: quoting the template inside a fence is help, not smuggling."""
    body = (
        "<!-- claims:begin\n"
        "json-eq: linkrot_summary.total_broken == 3\n"
        "claims:end -->\n"
        "\n"
        "This is what the block looks like:\n"
        "\n"
        "```\n"
        "<!-- claims:begin\n"
        "json: <path>\n"
        "claims:end -->\n"
        "```\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr


def test_fenced_tilde_block_is_ignored_too(tmp_path):
    """Same rule for ~~~ fences; the marker set must not depend on one syntax."""
    body = (
        "<!-- claims:begin\n"
        "json-eq: linkrot_summary.total_broken == 3\n"
        "claims:end -->\n"
        "\n"
        "~~~\n"
        "<!-- claims:begin\n"
        "json: <path>\n"
        "claims:end -->\n"
        "~~~\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr


def test_hidden_marker_beyond_cf_category(tmp_path):
    """Round 5 W2: stripping Cf alone was still an enumeration. Cc/Zs/Zl/Mn escaped."""
    body = (
        "<!-- claims:begin\n"
        "json-eq: linkrot_summary.unique_broken == 2\n"
        "claims:end -->\n"
        "\n"
        "{variant}\n"
        "json: fabricated.path\n"
        "claims:end -->\n"
    )
    variants = {
        "zl": "<!-- claims:begin\u2028",
        "nul": "<!-- claims:begin\x00",
        "vs16": "<!-- claims:begin\ufe0f",
        "combining": "<!-- clai\u0301ms:begin",
        "nbsp": "<!-- claims:begin\u00a0",
    }
    for name, marker in variants.items():
        out = _run(body.format(variant=marker), tmp_path)
        assert out.returncode == 2, name + out.stdout + out.stderr
        assert "claims blocks found" in (out.stdout + out.stderr)


def test_trailing_nbsp_on_marker_line_still_counts(tmp_path):
    """Round 5 X3: rich-text paste adds NBSP; the honest block must stay visible."""
    body = (
        "<!-- claims:begin\n"
        "json-eq: linkrot_summary.total_broken == 3\n"
        "claims:end -->\u00a0\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr


def test_four_backtick_fence_quotation_is_not_a_block(tmp_path):
    """Round 5 X1: a four-backtick run closes a three-backtick quotation."""
    body = (
        "<!-- claims:begin\n"
        "json-eq: linkrot_summary.total_broken == 3\n"
        "claims:end -->\n"
        "\n"
        "````\n"
        "<!-- claims:begin\n"
        "json: <path>\n"
        "claims:end -->\n"
        "````\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr


def test_block_inside_a_fence_is_named_as_such(tmp_path):
    """Round 5 F4b/X2: a swallowed block must say why, not just say not found."""
    body = (
        "``` do not write it like this\n"
        "<!-- claims:begin\n"
        "json-eq: linkrot_summary.total_broken == 3\n"
        "claims:end -->\n"
        "```\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 2, out.stdout + out.stderr
    assert "inside a code fence" in (out.stdout + out.stderr)


def test_blockquoted_example_is_not_a_second_block(tmp_path):
    """X4: a block quote is a quotation container, like a fence — every level.

    A quoted example of the block must not count as a second claims block."""
    body = (
        "<!-- claims:begin\n"
        "json-eq: linkrot_summary.total_broken == 3\n"
        "claims:end -->\n"
        "\n"
        "Quoting the template inside a block quote is help, not smuggling:\n"
        "\n"
        "{q}<!-- claims:begin\n"
        "{q}json: <path>\n"
        "{q}claims:end -->\n"
    )
    for q in ("> ", ">> ", "> > "):
        out = _run(body.format(q=q), tmp_path)
        assert out.returncode == 0, q + out.stdout + out.stderr


def test_block_inside_a_block_quote_is_named_as_such(tmp_path):
    """X4: a block swallowed by a block quote must say why, not just say not found."""
    for q in ("> ", ">> ", "> > "):
        body = (
            "{q}<!-- claims:begin\n"
            "{q}json-eq: linkrot_summary.total_broken == 3\n"
            "{q}claims:end -->\n"
        ).format(q=q)
        out = _run(body, tmp_path)
        assert out.returncode == 2, q + out.stdout + out.stderr
        assert "block quote" in (out.stdout + out.stderr)


def test_code_change_requires_a_value_assertion(tmp_path):
    """Round 5 W1: a hollow block of existence-only claims looked green."""
    body = (
        "<!-- claims:begin\n"
        "json: linkrot_summary.total_broken\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path, changed=["scripts/graph-diagnose.py"])
    assert out.returncode == 1, out.stdout + out.stderr
    assert "asserts no value" in (out.stdout + out.stderr)


def test_doc_change_allows_existence_only(tmp_path):
    """The same body on a documentation-only change stays legal."""
    body = (
        "<!-- claims:begin\n"
        "json: linkrot_summary.total_broken\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path, changed=["README.md"])
    assert out.returncode == 0, out.stdout + out.stderr


def test_name_status_rename_reports_both_paths(tmp_path):
    """Round 5 P7: --name-only hides the old name; a rename must not read as docs."""
    body = (
        "<!-- claims:begin\n"
        "no-claims: 只是重命名了文件\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path, changed=["R100\tevil.py\tnotes.md"])
    assert out.returncode == 1, out.stdout + out.stderr


def test_lazy_continuation_marker_inside_quote_is_quoted(tmp_path):
    """Round 6 M1: a line after "> prose" stays in the quote when padded with an invisible char."""
    body = (
        "> quoted prose\n"
        "\u200b<!-- claims:begin\n"
        "json-eq: linkrot_summary.total_broken == 3\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 2, out.stdout + out.stderr
    assert "block quote" in (out.stdout + out.stderr)


def test_lazy_continuation_with_combining_mark_is_quoted(tmp_path):
    """Same rule for a combining mark: it cannot open an HTML block either."""
    body = (
        "> quoted prose\n"
        "\u0301<!-- claims:begin\n"
        "json-eq: linkrot_summary.total_broken == 3\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 2, out.stdout + out.stderr
    assert "block quote" in (out.stdout + out.stderr)


def test_bare_marker_after_quote_line_escapes_the_quote(tmp_path):
    """L1 control: an HTML block can interrupt a paragraph, so this one is real."""
    body = (
        "> quoted prose\n"
        "<!-- claims:begin\n"
        "json-eq: linkrot_summary.total_broken == 3\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr


def test_blank_line_closes_the_quote(tmp_path):
    """A blank line ends the block quote, so the block after it is a real claim."""
    body = (
        "> quoted prose\n"
        "\n"
        "<!-- claims:begin\n"
        "json-eq: linkrot_summary.total_broken == 3\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr


def test_invisible_only_line_is_not_a_blank_line(tmp_path):
    """Round 7 W7-1: CommonMark blank = spaces/tabs only. NBSP/ZWSP lines keep the quote open."""
    body = (
        "> quoted prose\n"
        "{pad}\n"
        "\u200b<!-- claims:begin\n"
        "json-eq: linkrot_summary.total_broken == 3\n"
        "claims:end -->\n"
    )
    for name, pad in (("nbsp", "\u00a0"), ("zwsp", "\u200b"), ("shy", "\u00ad"), ("combining", "\u0301")):
        out = _run(body.format(pad=pad), tmp_path)
        assert out.returncode == 2, name + out.stdout + out.stderr
        assert "block quote" in (out.stdout + out.stderr)


def test_phantom_quote_prefix_cannot_hide_a_second_block(tmp_path):
    """Round 7 W7-2: a ZWSP before ">" must not open a quote that swallows block two."""
    body = (
        "<!-- claims:begin\n"
        "json-eq: linkrot_summary.total_broken == 3\n"
        "claims:end -->\n"
        "\n"
        "\u200b> note\n"
        "\u200b<!-- claims:begin\n"
        "json: fabricated.path\n"
        "claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 2, out.stdout + out.stderr
    assert "claims blocks found" in (out.stdout + out.stderr)



def test_indented_code_block_is_not_a_claim(tmp_path):
    """Round 9 A2: 4 spaces makes it code to the reader, so it is not a claim.

    This gap was known before the parser switch and is closed by the same rule:
    the block is owned by an indented code block, not by the document.
    """
    body = (
        "    <!-- claims:begin\n"
        "    json-eq: linkrot_summary.total_broken == 3\n"
        "    claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 2, out.stdout + out.stderr
    assert "indented code block" in (out.stdout + out.stderr)


def test_tab_indented_marker_is_not_a_claim(tmp_path):
    """Round 9 C2: a tab is four columns, so the marker reads as indented code."""
    body = "\t<!-- claims:begin\n\tjson-eq: linkrot_summary.total_broken == 3\n\tclaims:end -->\n"
    out = _run(body, tmp_path)
    assert out.returncode == 2, out.stdout + out.stderr
    assert "indented code block" in (out.stdout + out.stderr)


def test_tab_indented_quote_inside_a_list_is_still_a_quote(tmp_path):
    """Round 9 T4f: the tab width depends on the container the line sits in.

    After "- a" the content column is 2, so a tab puts "> note" inside the list
    item, where it is a block quote. No absolute-indent regex can see this; the
    parser can. This was the bypass that ended the mask era.
    """
    body = (
        "- a\n"
        "\t> note\n"
        "\t\u200b<!-- claims:begin\n"
        "\tjson-eq: linkrot_summary.total_broken == 3\n"
        "\tclaims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 2, out.stdout + out.stderr
    assert "block quote" in (out.stdout + out.stderr)


def test_marker_nested_in_a_list_item_is_named_as_nested(tmp_path):
    """A claim belongs at the document level; a list item is named when it is not."""
    body = (
        "- a\n"
        "\n"
        "    <!-- claims:begin\n"
        "    json-eq: linkrot_summary.total_broken == 3\n"
        "    claims:end -->\n"
    )
    out = _run(body, tmp_path)
    assert out.returncode == 2, out.stdout + out.stderr
    text = out.stdout + out.stderr
    assert "nested inside" in text or "block quote" in text
