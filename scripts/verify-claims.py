#!/usr/bin/env python3
"""Verify that machine-readable claims in a PR body actually hold.

A PR that says 'added field X' must carry a claim line for X, and CI checks that
claim against real CLI output from a fixture wiki. This exists because a merged PR
once claimed to expose aggregate fields that never reached any output path — the
claim was written from the function body, not from the artifact.

Claim types (one per line inside the claims block):

    json: <dotted.path>     path must resolve in --json output; [] = list element
    title: <substring>      some diagnosis item title must contain it
    absent: <dotted.path>   path must NOT resolve (guards against leftovers)
    no-claims: <reason>     nothing machine-checkable, reason >= 8 chars

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


def extract_claims(body: str) -> list[str]:
    """Return the stripped claim lines, or [] when the block is absent."""
    start = body.find(BEGIN)
    if start == -1:
        return []
    nl = body.find("\n", start)
    if nl == -1:
        return []
    end = body.find(END, nl)
    if end == -1:
        return []
    return [ln.strip() for ln in body[nl:end].splitlines() if ln.strip()]


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


def check_claim(claim: str, data: dict) -> tuple[bool, str, str]:
    """Return (ok, kind, detail)."""
    if claim.startswith("json:"):
        path = claim[len("json:"):].strip()
        ok, value = resolve(data, path)
        detail = f"{path} = {json.dumps(value, ensure_ascii=False)[:80]}" if ok else f"{path} not present in output"
        return ok, "json", detail
    if claim.startswith("absent:"):
        path = claim[len("absent:"):].strip()
        ok, _ = resolve(data, path)
        if ok:
            return False, "absent", f"{path} exists but claim says it must not"
        return True, "absent", f"{path} correctly absent"
    if claim.startswith("title:"):
        sub = claim[len("title:"):].strip()
        titles = [i.get("title", "") for i in data.get("diagnosis", [])]
        hit = next((t for t in titles if sub in t), None)
        if hit is None:
            return False, "title", f"no diagnosis title contains {sub!r}"
        return True, "title", f"matched {hit[:70]!r}"
    if claim.startswith("no-claims:"):
        reason = claim[len("no-claims:"):].strip()
        if len(reason) < 8:
            return False, "no-claims", "reason too short (>= 8 chars)"
        return True, "no-claims", f"skipped: {reason}"
    return False, "unknown", f"unrecognised claim: {claim!r}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="verify PR claims against CLI output")
    ap.add_argument("--body-file", required=True, help="file holding the PR body")
    ap.add_argument("--wiki-root", default=str(DEFAULT_FIXTURE),
                    help="fixture wiki used for verification")
    args = ap.parse_args(argv)

    body = Path(args.body_file).read_text(encoding="utf-8")
    claims = extract_claims(body)
    if not claims:
        print("FAIL  no claims block found in PR body", file=sys.stderr)
        print("      add a '<!-- claims:begin ... claims:end -->' block;", file=sys.stderr)
        print("      a claim that cannot be written is a claim that was not delivered.", file=sys.stderr)
        return 2

    data = run_diagnose(Path(args.wiki_root))
    failed = 0
    for claim in claims:
        ok, kind, detail = check_claim(claim, data)
        status = "PASS" if ok else "FAIL"
        print(f"{status}  [{kind}] {detail}")
        if not ok:
            failed += 1

    print()
    print(f"{len(claims) - failed}/{len(claims)} claims hold")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())