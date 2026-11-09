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

FENCE_OPEN_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")

# A block-quote prefix on the visible form is a leading run of '>' characters:
# visible_key already strips every spacing character, so '> ', '>> ' and '> > '
# all reduce to this one shape (X4 — same one-canonical-string principle as the
# marker keys below).
QUOTE_PREFIX_RE = re.compile(r"^>+")


def visible_key(line: str) -> str:
    """The line as a reader sees it: invisible, spacing, combining chars removed."""
    return "".join(
        ch for ch in line
        if unicodedata.category(ch)[0] not in INVISIBLE_CATEGORIES
    )


def fence_mask(lines: list[str]) -> list[bool]:
    """Flag the lines a Markdown reader would treat as fenced code (CommonMark)."""
    mask = [False] * len(lines)
    i = 0
    while i < len(lines):
        m = FENCE_OPEN_RE.match(lines[i])
        if not m:
            i += 1
            continue
        run, info = m.group(1), m.group(2)
        if run[0] == "`" and "`" in info:
            i += 1  # a backtick fence may not carry backticks in its info string
            continue
        marker = run[0]
        # Closing fence: same character, at least as long, and nothing else on it.
        close_re = re.compile(
            r"^[ \t]*" + re.escape(marker) + "{" + str(len(run)) + r",}[ \t]*$"
        )
        mask[i] = True
        j = i + 1
        while j < len(lines):
            mask[j] = True
            if close_re.match(lines[j]):
                break
            j += 1
        i = j + 1
    return mask


def strip_quote_prefix(line: str) -> str:
    """The line as it reads once a leading block-quote prefix is removed.

    Runs on the visible form (same lens that defines a marker), so a quoted
    marker is recognised however it was padded. Lines that do not start with
    '>' come back unchanged and can never match a marker key this way.
    """
    visible = visible_key(line)
    m = QUOTE_PREFIX_RE.match(visible)
    return visible[m.end():] if m else visible


# CommonMark lets a block quote continue on lines that carry no '>': an open
# paragraph keeps absorbing them (a "lazy continuation") until a blank line or
# a block that can interrupt a paragraph. An HTML block such as '<!--' can
# interrupt, which is exactly why a bare marker escapes the quote while the
# same marker padded with an invisible character does not (round 6 M1).
QUOTE_OPEN_RE = re.compile(r"^ {0,3}>")   # raw form: block structure allows <=3 spaces
HTML_BLOCK_START_RE = re.compile(r"^ {0,3}<!--")


def quote_mask(lines: list[str], fmask: list[bool]) -> list[bool]:
    """Flag the lines a reader sees inside an open block quote.

    Explicit '>' lines open a quote. Following non-blank lines keep it open as
    lazy continuations unless they open an HTML block, which can interrupt a
    paragraph and therefore lands outside the quote.
    """
    mask = [False] * len(lines)
    open_quote = False
    in_paragraph = False
    for i, line in enumerate(lines):
        if fmask[i]:
            continue  # fence content is opaque: it neither opens nor closes a quote
        if not line.strip(" \t"):  # CommonMark blank line: spaces and tabs only
            open_quote = False
            in_paragraph = False
            continue
        if QUOTE_OPEN_RE.match(line):
            open_quote = True
            in_paragraph = True
            mask[i] = True
            continue
        if open_quote and in_paragraph and not HTML_BLOCK_START_RE.match(line):
            mask[i] = True  # lazy continuation: still inside the quote
            continue
        open_quote = False
        in_paragraph = False
    return mask


def extract_claims(body: str) -> tuple[list[str], str | None]:
    """Return (claims, error). A marker is a whole line, outside any code fence
    or block quote — including one the line only joins as a lazy continuation."""
    lines = body.splitlines()
    fmask = fence_mask(lines)
    qmask = quote_mask(lines, fmask)
    begins = [i for i, ln in enumerate(lines) if visible_key(ln) == MARKER_BEGIN_KEY]
    usable = [i for i in begins if not fmask[i] and not qmask[i]]
    if not usable:
        if any(fmask[i] for i in begins):
            return [], (
                "the claims block sits inside a code fence, so it reads as a "
                "quotation rather than a claim. Move it out of the fence."
            )
        # No line matches the begin key exactly here, so anything matching after
        # the '>' strip was quoted outright; anything else that matched exactly
        # was swallowed by an open quote as a lazy continuation.
        quoted = [i for i, ln in enumerate(lines)
                  if not fmask[i] and strip_quote_prefix(ln) == MARKER_BEGIN_KEY]
        if any(qmask[i] for i in quoted):
            return [], (
                "the claims block sits inside a block quote (lines start with "
                "'>'), so it reads as a quotation rather than a claim. Move it "
                "out of the block quote: remove the leading '>' characters."
            )
        return [], "no claims block found in PR body"
    if len(usable) > 1:
        return [], (
            f"{len(usable)} claims blocks found; exactly one is allowed. "
            "Extra blocks are never silently ignored — that was a bypass (#14)."
        )
    start = usable[0]
    end = next(
        (i for i in range(start + 1, len(lines))
         if not fmask[i] and not qmask[i] and visible_key(lines[i]) == MARKER_END_KEY),
        None,
    )
    if end is None:
        return [], "claims block has no 'claims:end -->' terminator line"
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