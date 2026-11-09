#!/usr/bin/env python3
"""Verify that machine-readable claims in a PR body actually hold.

A PR that says 'added field X' must carry a claim line for X, and CI checks that
claim against real CLI output from a fixture wiki. This exists because a merged PR
once claimed to expose aggregate fields that never reached any output path — the
claim was written from the function body, not from the artifact.

Claim types (one per line inside the claims block):

    json: <dotted.path>       path must resolve; [] = list element
    json-eq: <path> == <v>    path must resolve AND equal v (JSON literal or string)
    title: <substring>        some diagnosis title must contain it (>= 2 chars)
    absent: <dotted.path>     path must NOT resolve (guards against leftovers)
    no-claims: <reason>       only when no code changed (see --changed-files)

Fail-closed rules (issue #14, #15):

  - zero claims blocks            -> exit 2
  - more than one claims block    -> exit 2  (was: extras silently dropped)
  - empty/short title substring   -> FAIL    (was: '' in t matched everything)
  - no-claims with code changed   -> FAIL    (was: any 8+ char excuse passed)

Exit codes: 0 all claims hold, 1 a claim failed, 2 the block is missing/malformed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DIAGNOSE = REPO_ROOT / "scripts" / "graph-diagnose.py"
DEFAULT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "claims-wiki"
BEGIN = "<!-- claims:begin"
END = "claims:end -->"
CODE_PREFIXES = ("scripts/", "tests/", ".github/", "examples/")
CODE_SUFFIXES = (".py", ".yml", ".yaml", ".toml", ".txt", ".cfg", ".ini", ".sh")


def extract_claims(body: str) -> tuple[list[str], str | None]:
    """Return (claims, error). More than one block is a hard error (issue #14)."""
    blocks = body.count(BEGIN)
    if blocks == 0:
        return [], "no claims block found in PR body"
    if blocks > 1:
        return [], (
            f"{blocks} claims blocks found; exactly one is allowed. "
            "Extra blocks are never silently ignored — that was a bypass (#14)."
        )
    start = body.find(BEGIN)
    nl = body.find("\n", start)
    if nl == -1:
        return [], "claims block is not terminated on a new line"
    end = body.find(END, nl)
    if end == -1:
        return [], "claims block has no 'claims:end -->' terminator"
    claims = [ln.strip() for ln in body[nl:end].splitlines() if ln.strip()]
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


def is_code_path(path: str) -> bool:
    """Anything whose change can alter behaviour, not just .py files.

    Issue found while attacking round 3: a PR touching .github/workflows or a
    dependency file could still claim no-claims and skip every check.
    """
    return path.startswith(CODE_PREFIXES) or path.endswith(CODE_SUFFIXES)


def check_claim(claim: str, data: dict, changed_files, is_template_copy: bool):
    """Return (ok, kind, detail)."""
    if claim.startswith("json-eq:"):
        rest = claim[len("json-eq:"):].strip()
        if "==" not in rest:
            return False, "json-eq", "expected 'path == value'"
        path, raw = rest.split("==", 1)
        path, raw = path.strip(), raw.strip()
        ok, value = resolve(data, path)
        if not ok:
            return False, "json-eq", f"{path} not present in output"
        try:
            expected = json.loads(raw)
        except json.JSONDecodeError:
            expected = raw
        # Strict type comparison: 2 and 2.0 and "2" are different claims. Loose
        # equality let a float assert an int, which is the same class of ambiguity
        # this gate exists to remove.
        if type(value) is not type(expected) or value != expected:
            return False, "json-eq", (
                f"{path} = {value!r} ({type(value).__name__}), ",
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
        code = [f for f in changed_files if is_code_path(f)]
        if code:
            return False, "no-claims", (
                f"code changed ({', '.join(code[:3])}); a skip is not allowed (#15)"
            )
        return True, "no-claims", f"skipped: {reason}"
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
    args = ap.parse_args(argv)

    body = Path(args.body_file).read_text(encoding="utf-8")
    claims, error = extract_claims(body)
    if error:
        print(f"FAIL  {error}", file=sys.stderr)
        print("      a claim that cannot be written is a claim that was not delivered.",
              file=sys.stderr)
        return 2

    changed = read_changed_files(args.changed_files)
    data = run_diagnose(Path(args.wiki_root))
    failed = 0
    for claim in claims:
        ok, kind, detail = check_claim(claim, data, changed, False)
        print(f"{'PASS' if ok else 'FAIL'}  [{kind}] {detail}")
        if not ok:
            failed += 1

    print()
    print(f"{len(claims) - failed}/{len(claims)} claims hold")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())