#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

@dataclass(frozen=True)
class DimensionChange:
    name: str
    before: float
    after: float
    @property
    def delta(self) -> float:
        return self.after - self.before
    @property
    def direction(self) -> str:
        if self.delta > 0: return "↑"
        if self.delta < 0: return "↓"
        return "→"
    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "before": self.before, "after": self.after, "delta": self.delta, "direction": self.direction}

@dataclass(frozen=True)
class ReportDiff:
    changes: list[DimensionChange]
    @property
    def total_before(self) -> float:
        return sum(c.before for c in self.changes)
    @property
    def total_after(self) -> float:
        return sum(c.after for c in self.changes)
    @property
    def total_delta(self) -> float:
        return self.total_after - self.total_before
    def to_dict(self) -> dict[str, Any]:
        return {"dimensions": [c.to_dict() for c in self.changes], "summary": {"total_before": self.total_before, "total_after": self.total_after, "total_delta": self.total_delta}}

def load_dimensions(path: Path) -> dict[str, float]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or "dimensions" not in data:
        raise ValueError(f"Invalid format in {path}")
    dims = data["dimensions"]
    if not isinstance(dims, dict):
        raise ValueError(f"Invalid format in {path}")
    return {k: float(v) for k, v in dims.items()}

def compute_diff(before: dict[str, float], after: dict[str, float]) -> ReportDiff:
    all_keys = sorted(set(before) | set(after))
    changes: list[DimensionChange] = []
    for key in all_keys:
        changes.append(DimensionChange(name=key, before=before.get(key, 0.0), after=after.get(key, 0.0)))
    return ReportDiff(changes=changes)

def format_table(diff: ReportDiff) -> str:
    headers = ["Dimension", "Before", "After", "Delta", "Dir"]
    rows: list[list[str]] = []
    for c in diff.changes:
        rows.append([c.name, f"{c.before:.2f}", f"{c.after:.2f}", f"{c.delta:+.2f}", c.direction])
    rows.append(["TOTAL", f"{diff.total_before:.2f}", f"{diff.total_after:.2f}", f"{diff.total_delta:+.2f}", "↑" if diff.total_delta > 0 else "↓" if diff.total_delta < 0 else "→"])
    col_widths = [max(len(row[i]) for row in ([headers] + rows)) for i in range(len(headers))]
    def fmt_row(row: list[str]) -> str:
        return " | ".join(cell.ljust(col_widths[i]) for i, cell in enumerate(row))
    lines = [fmt_row(headers), "-" * (sum(col_widths) + 3 * (len(headers) - 1))]
    lines.extend(fmt_row(row) for row in rows)
    return "\n".join(lines)

def format_json(diff: ReportDiff) -> str:
    return json.dumps(diff.to_dict(), indent=2, ensure_ascii=False)

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare two JSON dimension reports and show changes.")
    parser.add_argument("--before", type=Path, required=True, help="Path to the before JSON file.")
    parser.add_argument("--after", type=Path, required=True, help="Path to the after JSON file.")
    parser.add_argument("--format", choices=["table", "json"], default="table", help="Output format (default: table).")
    return parser.parse_args(argv)

def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    before_dims = load_dimensions(args.before)
    after_dims = load_dimensions(args.after)
    diff = compute_diff(before_dims, after_dims)
    if args.format == "table":
        print(format_table(diff))
    else:
        print(format_json(diff))
    return 0

if __name__ == "__main__":
    sys.exit(main())
