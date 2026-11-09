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
    assert result["total_broken"] == 1, result
    assert result["unique_broken"] == 1
    assert result["duplicated_entries"] == 2
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
    p2 = [i for i in items if i["severity"] == "P2" and "代码块内链接" in i["title"]]
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
