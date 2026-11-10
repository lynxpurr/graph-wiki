"""Regression tests for the D4 link-rot fixes (issues #3, #4, #5, #6).

Each test pins one behaviour that broke when the tool was run against a real
570-page wiki by an independent tester:

- #3 `dir/index.md` collapses to `dir`, so a link written as `[[dir/index]]`
     matched no canonical id and was reported as a false P0 dead link.
- #4 the same (source, target) pair was appended once per occurrence, so one
     missing page linked 4 times produced 4 queue items.
- #5 `graph-diagnose` printed a hardcoded `max: 100` while the real maximum is
     `active_dimensions * 10 + 10` (120 for an 11-dimension single-source wiki),
     which made a valid score of 102 look like it exceeded the cap.
- #6 wikilinks inside code fences were dropped with `continue` and left no
     trace, so real references could vanish silently.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
DIAGNOSE = SCRIPTS / "graph-diagnose.py"


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.fixture()
def wiki(tmp_path):
    """Wiki with a directory landing page referenced in both link forms."""
    root = tmp_path / "wiki"
    _write(root, "concepts/seed.md", "# seed\n\n[[incidents/index]] [[incidents]]\n")
    _write(
        root,
        "incidents/index.md",
        "---\ntitle: 事故索引\ntype: directory-index\n---\n\n# 事故索引\n",
    )
    return root


def test_dir_index_link_resolves_both_forms(load_script_module, wiki):
    """Issue #3: both [[dir]] and [[dir/index]] must resolve, neither is broken."""
    diag = load_script_module("graph-diagnose.py")
    result = diag.diagnose_linkrot(None, {}, wiki_root=str(wiki))
    assert result["total_broken"] == 0, result


def test_dir_index_without_landing_page_still_broken(load_script_module, tmp_path):
    """Issue #3 must not turn genuine dead links into false passes."""
    root = tmp_path / "wiki"
    _write(root, "a.md", "# a\n\n[[nowhere/index]]\n")
    diag = load_script_module("graph-diagnose.py")
    result = diag.diagnose_linkrot(None, {}, wiki_root=str(root))
    assert result["total_broken"] == 1, result


def test_readme_landing_page_resolves_by_directory_name(load_script_module, tmp_path):
    """Issue #3, second form: dir/README.md is addressed as [[dir]] in this wiki."""
    root = tmp_path / "wiki"
    _write(root, "concepts/a.md", "# a\n\n[[abaqus-linux-install]]\n")
    _write(root, "abaqus-linux-install/README.md", "# readme\n")
    _write(root, "abaqus-linux-install/deep-dive.md", "# deep\n")
    diag = load_script_module("graph-diagnose.py")
    result = diag.diagnose_linkrot(None, {}, wiki_root=str(root))
    assert result["total_broken"] == 0, result


def test_duplicate_links_counted_not_duplicated(load_script_module, tmp_path):
    """Issue #4: 3 references to one missing target = 1 entry + occurrences=3."""
    root = tmp_path / "wiki"
    _write(root, "a.md", "[[missing-page]]\n[[missing-page]]\n[[missing-page]]\n")
    diag = load_script_module("graph-diagnose.py")
    result = diag.diagnose_linkrot(None, {}, wiki_root=str(root))
    # issue #16: total_broken is the raw reference count; the three fields must
    # satisfy total == unique + duplicated.
    assert result["total_broken"] == 3, result
    assert result["unique_broken"] == 1
    assert result["duplicated_entries"] == 2
    assert result["total_broken"] == result["unique_broken"] + result["duplicated_entries"]
    entry = result["details"][0]["broken_targets"][0]
    assert entry["target"] == "missing-page"
    assert entry["occurrences"] == 3


def test_code_block_links_exempt_but_visible(load_script_module, tmp_path):
    """Issue #6: exempt from link-rot, but the exemption must be countable."""
    root = tmp_path / "wiki"
    _write(root, "a.md", "# a\n\n```bash\n# 参考 [[kb-migration-playbook]] skill SOP\n```\n")
    diag = load_script_module("graph-diagnose.py")
    result = diag.diagnose_linkrot(None, {}, wiki_root=str(root))
    assert result["total_broken"] == 0, result
    assert result["code_block_exempt"] == 1, result
    assert result["code_block_samples"][0]["target"] == "kb-migration-playbook"


def test_code_block_exemption_surfaces_as_p2(load_script_module):
    """Issue #6: exemption reaches the diagnosis array as P2, never P0."""
    diag = load_script_module("graph-diagnose.py")
    lr = {
        "total_broken": 0,
        "affected_sources": 0,
        "details": [],
        "unique_broken": 0,
        "duplicated_entries": 0,
        "code_block_exempt": 2,
        "code_block_samples": [{"source": "a", "target": "kb-migration-playbook"}],
    }
    items = diag._diagnosis_to_array({}, {}, {}, {}, {}, lr, {}, False)
    p2 = [i for i in items if i["severity"] == "P2" and "代码围栏/行内代码内链接未检查" in i["title"]]
    assert len(p2) == 1, items
    assert p2[0]["evidence"]["count"] == 2
    assert not [i for i in items if i["severity"] == "P0"]


def test_diagnose_does_not_fabricate_max_score(tmp_path):
    """Issue #5: max must not be a hardcoded 100 that the real 120 contradicts."""
    root = tmp_path / "wiki"
    _write(root, "a.md", "# a\n\n[[b]]\n")
    _write(root, "b.md", "# b\n\n[[a]]\n")
    out = subprocess.run(
        [sys.executable, str(DIAGNOSE), "--source", "wiki",
         "--wiki-root", str(root), "--json"],
        capture_output=True, text=True, timeout=180,
    )
    assert out.returncode in (0, 1), out.stderr
    data = json.loads(out.stdout)
    scores = data["scores"]
    assert scores["max"] is None, scores
    assert scores["total"] is None, scores
    assert scores["source"] == "graph-test-suite", scores

def test_linkrot_summary_reaches_cli_output(load_script_module, tmp_path):
    """Issue #8: the aggregates must exist in --json, not just in the return value."""
    root = tmp_path / "wiki"
    _write(root, "a.md", "# a\n\n[[gone]]\n[[gone]]\n[[gone]]\n")
    out = subprocess.run(
        [sys.executable, str(DIAGNOSE), "--source", "wiki",
         "--wiki-root", str(root), "--json"],
        capture_output=True, text=True, timeout=180,
    )
    assert out.returncode in (0, 1), out.stderr
    data = json.loads(out.stdout)
    summary = data.get("linkrot_summary")
    assert summary is not None, sorted(data)
    assert summary["unique_broken"] == 1, summary
    assert summary["duplicated_entries"] == 2, summary
    assert summary["entries_with_duplicates"] == 1, summary
    assert summary["dedupe_key"] == "(source, target)", summary
    assert "sum(occurrences - 1)" in summary["duplicated_entries_meaning"], summary


def test_occurrences_reach_evidence(load_script_module):
    """Issue #8: occurrences was computed then stripped when building evidence."""
    diag = load_script_module("graph-diagnose.py")
    lr = {
        "total_broken": 1,
        "affected_sources": 1,
        "details": [{
            "source": "a",
            "source_path": "a.md",
            "title": "a",
            "broken_count": 1,
            "broken_targets": [{
                "target": "gone", "context": "ctx",
                "is_index_link": False, "occurrences": 4,
            }],
        }],
        "unique_broken": 1,
        "duplicated_entries": 3,
        "entries_with_duplicates": 1,
        "code_block_exempt": 0,
        "code_block_samples": [],
        "skill_scheme_exempt": 0,
        "skill_scheme_samples": [],
    }
    items = diag._diagnosis_to_array({}, {}, {}, {}, {}, lr, {}, False)
    p0 = [i for i in items if i["severity"] == "P0"]
    assert len(p0) == 1, items
    assert p0[0]["evidence"]["occurrences"] == 4, p0[0]["evidence"]
    assert p0[0]["evidence"]["source_path"] == "a.md", p0[0]["evidence"]


def test_same_target_across_sources_stays_two_items(load_script_module, tmp_path):
    """Issue #8 semantics: dedupe key is (source, target), so cross-source stays 2."""
    root = tmp_path / "wiki"
    _write(root, "a.md", "[[gone]]\n[[gone]]\n")
    _write(root, "b.md", "[[gone]]\n")
    diag = load_script_module("graph-diagnose.py")
    result = diag.diagnose_linkrot(None, {}, wiki_root=str(root))
    assert result["unique_broken"] == 2, result
    assert result["total_broken"] == 3, result   # 2 refs in a.md + 1 in b.md
    # one repeat inside a.md, none inside b.md
    assert result["duplicated_entries"] == 1, result
    assert result["entries_with_duplicates"] == 1, result
    assert result["total_broken"] == result["unique_broken"] + result["duplicated_entries"]


def test_index_source_exemption_is_consistent(load_script_module, tmp_path):
    """Issue #9: dir/index.md must be exempt exactly like root index.md."""
    root = tmp_path / "wiki"
    _write(root, "index.md", "[[missing-a]]\n")
    _write(root, "incidents/index.md", "[[missing-b]]\n")
    diag = load_script_module("graph-diagnose.py")
    result = diag.diagnose_linkrot(None, {}, wiki_root=str(root))
    flags = {e["target"]: e["is_index_link"]
             for d in result["details"] for e in d["broken_targets"]}
    assert flags["missing-a"] is True, flags
    assert flags["missing-b"] is True, flags
    paths = {e["target"]: d["source_path"]
             for d in result["details"] for e in d["broken_targets"]}
    assert paths["missing-a"] == "index.md", paths
    assert paths["missing-b"] == "incidents/index.md", paths


def test_skill_scheme_exemption_is_visible(load_script_module, tmp_path):
    """Issue #10: skill: links were dropped with no counting at all."""
    root = tmp_path / "wiki"
    _write(root, "a.md", "# a\n\n[[skill:ragflux-connector-pipeline]]\n")
    diag = load_script_module("graph-diagnose.py")
    result = diag.diagnose_linkrot(None, {}, wiki_root=str(root))
    assert result["total_broken"] == 0, result
    assert result["skill_scheme_exempt"] == 1, result
    assert result["skill_scheme_samples"][0]["target"].startswith("skill:")
    items = diag._diagnosis_to_array({}, {}, {}, {}, {}, result, {}, False)
    p2 = [i for i in items if "skill:/skills: 前缀链接未检查" in i["title"]]
    assert len(p2) == 1, items
    assert p2[0]["severity"] == "P2", p2
    assert not [i for i in items if i["severity"] == "P0"], items


def test_skill_scheme_inside_code_block_counted_once(load_script_module, tmp_path):
    """Issue #10: a skill: link inside a fence is a scheme exemption, not a fence one."""
    root = tmp_path / "wiki"
    _write(root, "a.md", "# a\n\n```md\n[[skill:foo]]\n[[plain-gone]]\n```\n")
    diag = load_script_module("graph-diagnose.py")
    result = diag.diagnose_linkrot(None, {}, wiki_root=str(root))
    assert result["skill_scheme_exempt"] == 1, result
    assert result["code_block_exempt"] == 1, result
    assert result["total_broken"] == 0, result


def test_whitespace_only_target_is_exempt_not_linkrot(load_script_module, tmp_path):
    """A whitespace-only target is a malformed link, not a missing page.

    Found by an independent tester: injecting "[[" + spaces + "]]" into a page
    raised total_broken from 3 to 4, reporting "README -> ''" as link-rot.
    Nothing an author can act on as a page that does not exist. Exempt, but
    counted, same standard as the fence (#6) and scheme (#10) exemptions.
    """
    root = tmp_path / "wiki"
    _write(root, "a.md", "# a\n\n[[   ]]\n")
    diag = load_script_module("graph-diagnose.py")
    result = diag.diagnose_linkrot(None, {}, wiki_root=str(root))
    assert result["total_broken"] == 0, result
    assert result["unique_broken"] == 0, result
    assert result["empty_target_skipped"] == 1, result
    assert result["empty_target_samples"][0]["source"] == "a"


def test_empty_brackets_are_not_wikilinks_at_all(load_script_module, tmp_path):
    """A bare "[[]]" never reaches the parser: the wikilink regex needs 1+ chars.

    Pinned because the two forms look alike in a diff but behave differently:
    "[[" + spaces + "]]" is parsed and then exempted (1 skip), "[[]]" is not a
    wikilink at all (0). Reading only the counts, this looked like "two empty
    targets merged into one" when in fact only one was ever a link.
    """
    root = tmp_path / "wiki"
    _write(root, "a.md", "# a\n\n[[]]\n")
    diag = load_script_module("graph-diagnose.py")
    result = diag.diagnose_linkrot(None, {}, wiki_root=str(root))
    assert result["total_broken"] == 0, result
    assert result["empty_target_skipped"] == 0, result


def test_empty_target_exemption_does_not_swallow_real_dead_links(load_script_module, tmp_path):
    """Exempting malformed links must not hide a genuine missing page beside them."""
    root = tmp_path / "wiki"
    _write(root, "a.md", "# a\n\n[[   ]]\n\n[[gone-page]]\n")
    diag = load_script_module("graph-diagnose.py")
    result = diag.diagnose_linkrot(None, {}, wiki_root=str(root))
    assert result["empty_target_skipped"] == 1, result
    assert result["total_broken"] == 1, result
    assert result["unique_broken"] == 1, result
    assert result["details"][0]["broken_targets"][0]["target"] == "gone-page"


def test_empty_target_exemption_surfaces_as_p2(load_script_module, tmp_path):
    """Disclosure path: an exempted malformed link still shows up in diagnosis."""
    root = tmp_path / "wiki"
    _write(root, "a.md", "# a\n\n[[   ]]\n")
    diag = load_script_module("graph-diagnose.py")
    result = diag.diagnose_linkrot(None, {}, wiki_root=str(root))
    items = diag._diagnosis_to_array({}, {}, {}, {}, {}, result, {}, False)
    found = [i for i in items if "空目标链接未计入死链" in i["title"]]
    assert len(found) == 1, items
    assert found[0]["severity"] == "P2", found
    assert found[0]["evidence"]["count"] == 1
    assert not [i for i in items if i["severity"] == "P0"], items


def test_linkrot_summary_documents_unique_broken_semantics(load_script_module):
    """unique_broken counts (source, target) pairs, not distinct target pages.

    Observed on a real wiki: one missing page linked from two sources yields
    unique_broken=2. Callers read "unique" as "how many bad pages", so the JSON
    has to state which one it is instead of leaving it to the dedupe_key hint.
    """
    diag = load_script_module("graph-diagnose.py")
    src = Path(diag.__file__).read_text(encoding="utf-8")
    assert "unique_broken_meaning" in src
    assert "(source, target) pairs" in src
