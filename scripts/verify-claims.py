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
stripped before counting, so a disguised marker cannot hide a block.

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

INVISIBLE_CHARS = set("\u200b\u200c\u200d\u200e\u200f\u2060\u00ad\u180e\u3164\ufeff")

# ``` and ~~~ fences hold quotations, not claims. A PR that shows the template
# inside a fence is being helpful, not smuggling a second block.
FENCE_RE = re.compile(
    r"^[ \t]*```.*?^[ \t]*```[ \t]*$|^[ \t]*~~~.*?^[ \t]*~~~[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
BEGIN_RE = re.compile(r"^[ \t]*<!-- claims:begin[ \t]*$", re.MULTILINE)
END_RE = re.compile(r"^[ \t]*claims:end -->[ \t]*$", re.MULTILINE)

# Only pure documentation may skip the gate. Everything else — scripts, tests, CI
# config, dependency pins, Makefiles, extensionless executables — counts as code.
DOC_SUFFIXES = (".md",)
NON_DOC_PREFIXES = ("scripts/", "tests/", ".github/", "examples/")


def normalize_body(body: str) -> str:
    """Erase what cannot be seen: invisible characters and fenced quotations."""
    kept = []
    for ch in body:
        if ch in INVISIBLE_CHARS:
            continue
        if unicodedata.category(ch) == "Cf":
            continue
        if 0xE0000 <= ord(ch) <= 0xE007F:
            continue
        kept.append(ch)
    body = "".join(kept)
    return FENCE_RE.sub(lambda m: chr(10) * m.group(0).count(chr(10)), body)

def extract_claims(body: str) -> tuple[list[str], str | None]:
    """Return (claims, error). Markers must occupy their own line."""
    body = normalize_body(body)
    begins = BEGIN_RE.findall(body)
    if not begins:
        return [], "no claims block found in PR body"
    if len(begins) > 1:
        return [], (
            f"{len(begins)} claims blocks found; exactly one is allowed. "
            "Extra blocks are never silently ignored — that was a bypass (#14)."
        )
    if not END_RE.search(body):
        return [], "claims block has no 'claims:end -->' terminator line"
    start = BEGIN_RE.search(body).end()
    end = END_RE.search(body, start)
    if end is None:
        return [], "terminator appears before the block content"
    claims = [ln.strip() for ln in body[start:end.start()].splitlines() if ln.strip()]
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


def read_changed_files(path) -> list[str] | None:
    if not path:
        return None
    text = Path(path).read_text(encoding="utf-8")
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


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

    print()
    print(f"{len(claims) - failed}/{len(claims)} claims hold")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())