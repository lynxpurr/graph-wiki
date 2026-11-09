#!/usr/bin/env python3
"""Verify that machine-readable claims in a PR body actually hold.

A PR that says 'added field X' must carry a claim line for X, and CI checks that
claim against real CLI output from a fixture wiki. This exists because a merged PR
once claimed to expose aggregate fields that never reached any output path — the
claim was written from the function body, not from the artifact.

Claim types (one per line inside the claims block):

    json: <dotted.path>       path must resolve; [] = list element
    json-eq: <path> == <v>    path must resolve AND equal v, same type (scalar paths)
    title: <substring>        some diagnosis title must contain it (>= 2 chars)
    absent: <dotted.path>     path must NOT resolve (guards against leftovers)
    no-claims: <reason>       only when every changed file is documentation

Fail-closed rules:

  - zero claims blocks                     -> exit 2
  - more than one claims block             -> exit 2
  - empty/short title substring            -> FAIL
  - json-eq on a [] path                   -> FAIL (dual semantics, assert a scalar)
  - json-eq across types (2 vs 2.0, 1 vs true) -> FAIL
  - no-claims with ANY non-document change -> FAIL
  - no-claims with an empty changed list   -> FAIL

Marker discipline: a marker only counts when it occupies its own line. Text that
merely mentions the marker inline is prose, not a block. Zero-width characters are
stripped before counting, so a disguised marker cannot hide a block. A code fence
and a block quote are both quotation containers: a marker inside either reads as
a quotation, not a claim — and when nothing usable remains, the error names the
container instead of just saying the block is missing.

Exit codes: 0 all claims hold, 1 a claim failed, 2 the block is missing/malformed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
import unicodedata

REPO_ROOT = Path(__file__).resolve().parents[1]
DIAGNOSE = REPO_ROOT / "scripts" / "graph-diagnose.py"
DEFAULT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "claims-wiki"

# A marker is defined by what it renders as, not by which characters we happened
# to blacklist. Delete everything that carries no visible meaning — control,
# formatting, separator and combining characters — then compare what is left to
# the one canonical string. Round 5: enumerating Cf missed Cc, Zs, Zl, Zp and Mn,
# and the line anchor accepted only [ \t] — two definitions of "invisible", one
# gap, and both bypasses and false positives lived in that gap.
MARKER_BEGIN_KEY = "<!--claims:begin"
MARKER_END_KEY = "claims:end-->"
INVISIBLE_CATEGORIES = ("C", "Z", "M")

# ---------------------------------------------------------------------------
# Block structure: the reader's own parse is the only authority.
#
# Rounds 5-9 each added a hand-written mask -- fences, block quotes, lazy
# continuations, a rewritten blank-line definition -- and each round the next
# attack lived in the seam between that mask and CommonMark. Round 9 closed
# the argument: a tab-indented '>' inside a list item is a block quote to
# every reader, and no absolute-indent regex can see it, because the width of
# that tab depends on the container the line sits in. Container nesting is not
# recoverable from a line scan.
#
# So structure now comes from a real CommonMark parser, while marker identity
# stays line-based (visible_key). The two rules no longer share a definition,
# which is the gap they disagreed across for four rounds.
# ---------------------------------------------------------------------------

try:
    from markdown_it import MarkdownIt
except ImportError:  # fail closed: never fall back to the masks this replaced
    sys.stderr.write(
        "FAIL  the CommonMark parser (markdown-it-py) is unavailable, so block "
        "structure cannot be checked. Install scripts/requirements.txt. This "
        "gate will not fall back to line masks: a gate that silently checks "
        "less than it claims is worse than no gate.")
    sys.stderr.write(chr(10))
    raise SystemExit(5)

# Where a reader sees a document-level claim. Inside a quote it reads as
# someone else's words; inside a fence or indented code it does not read at all.
# Marker identity stays line-based: invisible padding must never change whether
# a line is a marker. This is the ONLY remaining lens, and it decides identity
# alone -- never structure, which is what the deleted masks used it for.
def visible_key(line: str) -> str:
    """The line as a reader sees it: invisible, spacing, combining chars removed."""
    return "".join(
        ch for ch in line
        if unicodedata.category(ch)[0] not in INVISIBLE_CATEGORIES
    )

TOP_LEVEL_OWNERS = ("html_block", "paragraph")

# Leaf blocks the parser tracks line by line.
LEAF_BLOCKS = ("html_block", "fence", "code_block")

_PARSER = MarkdownIt("commonmark")


def normalize_newlines(body: str) -> str:
    """CommonMark line endings are LF, CRLF and CR -- and only those.

    Marker identity splits the body into lines and the parser numbers the same
    lines, so both must use one definition. str.splitlines() also breaks on
    U+2028, U+0085, form feed and vertical tab, which shifts every index after
    the first such character: a marker then lands on an unrelated line, which
    is a bypass in the permissive direction and a nonsense message in the
    strict one. Normalising first keeps the two views identical by
    construction.
    """
    return body.replace(chr(13) + chr(10), chr(10)).replace(chr(13), chr(10))


def block_structure(body: str) -> list[tuple[str, tuple[str, ...]]]:
    """Per source line: the block that owns it, and the ancestor chain.

    Inner tokens come after outer ones and overwrite them, so the answer is
    always the innermost block -- the one a marker actually sits in.
    """
    body = normalize_newlines(body)
    lines = body.split(chr(10))
    where: list[tuple[str, tuple[str, ...]]] = [("", ())] * len(lines)
    stack: list[str] = []
    for tok in _PARSER.parse(body):
        if tok.nesting == -1:
            if stack:
                stack.pop()
            continue
        if tok.nesting == 1:
            owner = tok.type[:-5] if tok.type.endswith("_open") else tok.type
            ancestors = tuple(stack)
            stack.append(owner)
        elif tok.type in LEAF_BLOCKS:
            owner, ancestors = tok.type, tuple(stack)
        else:
            continue
        if tok.map:
            start, end = tok.map
            for i in range(max(0, start), min(len(lines), end)):
                where[i] = (owner, ancestors)
    return where


def displace_reason(where, line_no: int):
    """Why a marker on this line is not a document-level claim (None if it is)."""
    owner, ancestors = where[line_no]
    if not ancestors and owner in TOP_LEVEL_OWNERS:
        return None
    if not owner:
        return (
            "the claims block is not a document-level block in the parsed "
            "structure, so the reader sees no document-level claim. Move it to "
            "the top level."
        )
    if owner == "fence":
        return (
            "the claims block sits inside a code fence, so it reads as a " +
            "quotation rather than a claim. Move it out of the fence."
        )
    if owner == "code_block":
        return (
            "the claims block sits inside an indented code block (4+ spaces or " +
            "a tab), so it reads as code rather than a claim. Remove the " +
            "indentation."
        )
    if "blockquote" in ancestors:
        return (
            "the claims block sits inside a block quote (its lines carry the " +
            "quote marker), so it reads as a quotation rather than a claim. " +
            "Move it out of the block quote."
        )
    chain = " > ".join(list(ancestors) + [owner or "document"])
    return (
        "the claims block is nested inside " + chain + ", so the reader sees " +
        "no document-level claim. Move it to the top level."
    )

# A marker inside a quote carries an arrow run, so it only matches the canonical
# key once that run is removed. Diagnostic only -- see note in extract_claims.
ARROW_PREFIX_RE = re.compile("^>+")


def strip_arrow_prefix(visible: str) -> str:
    m = ARROW_PREFIX_RE.match(visible)
    return visible[m.end():] if m else visible


def extract_claims(body: str) -> tuple[list[str], str | None]:
    """Return (claims, error).

    A marker is a whole line wherever it sits, but it only counts as a claim
    where the reader puts it at the document level. Both statements come from
    one parse, so there is no second definition left to drift.
    """
    body = normalize_newlines(body)
    lines = body.split(chr(10))
    where = block_structure(body)
    begins = [i for i, ln in enumerate(lines) if visible_key(ln) == MARKER_BEGIN_KEY]
    usable = [i for i in begins if displace_reason(where, i) is None]
    if not usable:
        if begins:
            return [], displace_reason(where, begins[0])
        # Identity is exact-line, so a marker the reader sees inside a quote only
        # matches the canonical key once the arrow run is removed. That lens is
        # diagnostic: the reason still comes from the parse, which knows the line
        # is inside a block quote. It is not a second structural rule.
        quoted = [i for i, ln in enumerate(lines)
                  if strip_arrow_prefix(visible_key(ln)) == MARKER_BEGIN_KEY]
        if quoted and displace_reason(where, quoted[0]):
            return [], displace_reason(where, quoted[0])
        return [], "no claims block found in PR body"
    if len(usable) > 1:
        return [], (
            str(len(usable)) + " claims blocks found; exactly one is allowed. " +
            "Extra blocks are never silently ignored -- that was a bypass (#14)."
        )
    start = usable[0]
    end = next(
        (i for i in range(start + 1, len(lines))
         if visible_key(lines[i]) == MARKER_END_KEY and displace_reason(where, i) is None),
        None,
    )
    if end is None:
        return [], "claims block has no terminator line (claims:end -->)"
    claims = [ln.strip() for ln in lines[start + 1:end] if ln.strip()]
    if not claims:
        return [], "claims block is empty"
    return claims, None


def resolve(obj, path: str):
    """Resolve a dotted path; a [] suffix means every element of a list."""
    segments = path.split(".")
    cur = obj
    for i, seg in enumerate(segments):
        is_list = seg.endswith("[]")
        key = seg[:-2] if is_list else seg
        if not isinstance(cur, dict) or key not in cur:
            return False, None
        cur = cur[key]
        if is_list:
            if not isinstance(cur, list) or not cur:
                return False, None
            rest = segments[i + 1:]
            if not rest:
                return True, cur
            sub = ".".join(rest)
            for item in cur:
                ok, val = resolve(item, sub)
                if ok:
                    return True, val
            return False, None
    return True, cur


def run_diagnose(wiki_root: Path) -> dict:
    out = subprocess.run(
        [sys.executable, str(DIAGNOSE), "--source", "wiki",
         "--wiki-root", str(wiki_root), "--json"],
        capture_output=True, text=True, timeout=300,
    )
    if out.returncode not in (0, 1):
        raise SystemExit(f"diagnose failed rc={out.returncode}\n{out.stderr[-1500:]}")
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"diagnose produced invalid JSON: {exc}")


def normalize_path(raw: str, repo_root: Path) -> str:
    """Strip quotes, ./ prefixes, and make absolute paths repo-relative."""
    p = raw.strip()
    if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'":
        p = p[1:-1]
    if p.startswith("./"):
        p = p[2:]
    if os.path.isabs(p):
        try:
            p = os.path.relpath(p, repo_root)
        except ValueError:
            pass
    return p.replace(os.sep, "/")


# Only pure documentation may skip the gate. Everything else — scripts, tests, CI
# config, dependency pins, Makefiles, extensionless executables — counts as code.
DOC_SUFFIXES = (".md",)
NON_DOC_PREFIXES = ("scripts/", "tests/", ".github/", "examples/")

def is_doc_only(path: str) -> bool:
    """True only for pure documentation. Everything else can change behaviour."""
    return path.endswith(DOC_SUFFIXES) and not path.startswith(NON_DOC_PREFIXES)


def check_claim(claim: str, data: dict, changed_files, repo_root: Path):
    """Return (ok, kind, detail)."""
    if claim.startswith("json-eq:"):
        rest = claim[len("json-eq:"):].strip()
        if "==" not in rest:
            return False, "json-eq", "expected 'path == value'"
        path, raw = rest.split("==", 1)
        path, raw = path.strip(), raw.strip()
        if "[]" in path:
            return False, "json-eq", (
                "json-eq does not accept [] paths: json: takes any element while "
                "json-eq took the first, which read as two different semantics. "
                "Assert a concrete scalar path instead."
            )
        ok, value = resolve(data, path)
        if not ok:
            return False, "json-eq", f"{path} not present in output"
        try:
            expected = json.loads(raw)
        except json.JSONDecodeError:
            expected = raw
        if type(value) is not type(expected) or value != expected:
            return False, "json-eq", (
                f"{path} = {value!r} ({type(value).__name__}), "
                f"expected {expected!r} ({type(expected).__name__})"
            )
        return True, "json-eq", f"{path} == {json.dumps(expected, ensure_ascii=False)}"
    if claim.startswith("json:"):
        path = claim[len("json:"):].strip()
        ok, value = resolve(data, path)
        detail = (
            f"{path} = {json.dumps(value, ensure_ascii=False)[:80]}"
            if ok else f"{path} not present in output"
        )
        return ok, "json", detail
    if claim.startswith("absent:"):
        path = claim[len("absent:"):].strip()
        ok, _ = resolve(data, path)
        if ok:
            return False, "absent", f"{path} exists but claim says it must not"
        return True, "absent", f"{path} correctly absent"
    if claim.startswith("title:"):
        sub = claim[len("title:"):].strip()
        if len(sub) < 2:
            return False, "title", (
                "empty or single-char title substring would match anything (#15)"
            )
        titles = [i.get("title", "") for i in data.get("diagnosis", [])]
        hit = next((t for t in titles if sub in t), None)
        if hit is None:
            return False, "title", f"no diagnosis title contains {sub!r}"
        return True, "title", f"matched {hit[:70]!r}"
    if claim.startswith("no-claims:"):
        reason = claim[len("no-claims:"):].strip()
        if len(reason) < 8:
            return False, "no-claims", "reason too short (>= 8 chars)"
        if changed_files is None:
            return False, "no-claims", (
                "cannot validate a skip without --changed-files; pass it or write real claims"
            )
        if not changed_files:
            return False, "no-claims", (
                "changed-file list is empty; cannot tell whether code changed (fail-closed)"
            )
        blockers = [
            f for f in (normalize_path(x, repo_root) for x in changed_files)
            if not is_doc_only(f)
        ]
        if blockers:
            return False, "no-claims", (
                f"non-document change ({', '.join(blockers[:3])}); a skip is not allowed (#15)"
            )
        return True, "no-claims", f"skipped: {reason} (docs only)"
    return False, "unknown", f"unrecognised claim: {claim!r}"


STATUS_LINE_RE = re.compile(r"^([A-Z])(\d*)\t(.+)$")


def read_changed_files(path) -> list[str] | None:
    """Accept `git diff --name-only` or `--name-status`; a rename yields both sides.

    `--name-only` reports only the new name of a rename, so `evil.py -> notes.md`
    would look like a documentation change. `--name-status` shows the pair.
    """
    if not path:
        return None
    out: list[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        m = STATUS_LINE_RE.match(line)
        if m:
            out.extend(part for part in m.group(3).split("\t") if part)
        else:
            out.append(line)
    return out

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="verify PR claims against CLI output")
    ap.add_argument("--body-file", required=True)
    ap.add_argument("--wiki-root", default=str(DEFAULT_FIXTURE))
    ap.add_argument("--changed-files", default=None,
                    help="file listing changed paths; required for no-claims")
    ap.add_argument("--repo-root", default=str(REPO_ROOT),
                    help="used to make absolute changed paths repo-relative")
    args = ap.parse_args(argv)

    body = Path(args.body_file).read_text(encoding="utf-8")
    claims, error = extract_claims(body)
    if error:
        print(f"FAIL  {error}", file=sys.stderr)
        print("      a claim that cannot be written is a claim that was not delivered.",
              file=sys.stderr)
        return 2

    changed = read_changed_files(args.changed_files)
    repo_root = Path(args.repo_root)
    data = run_diagnose(Path(args.wiki_root))
    failed = 0
    for claim in claims:
        ok, kind, detail = check_claim(claim, data, changed, repo_root)
        print(f"{'PASS' if ok else 'FAIL'}  [{kind}] {detail}")
        if not ok:
            failed += 1

    non_doc = [
        f for f in (normalize_path(x, repo_root) for x in (changed or []))
        if not is_doc_only(f)
    ]
    asserts_value = any(c.startswith(("json-eq:", "absent:")) for c in claims)
    if failed == 0 and non_doc and not asserts_value:
        print(
            "FAIL  [policy] this changes code (" + ", ".join(non_doc[:3]) + ") "
            "but asserts no value: `json:` only proves a path exists, so a hollow "
            "block can still look green. Add at least one json-eq: or absent:.",
            file=sys.stderr,
        )
        failed += 1
    print()
    print(f"{len(claims) - failed}/{len(claims)} claims hold")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())