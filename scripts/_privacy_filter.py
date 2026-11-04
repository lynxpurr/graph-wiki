#!/usr/bin/env python3
"""Three-tier privacy classification for ontology JSONL records."""

from __future__ import annotations

import json
import re
from pathlib import Path


PUBLIC_PATTERNS = [
    r"^[a-z]+-[a-z]+(?:-[a-z]+)*$",
    r"^(?:concepts|frameworks|people)/",
    # The public ontology already contains generic single-token slugs such as
    # ``reciprocity`` and ``batna``.
    r"^[a-z]+$",
]

CONFIDENTIAL_PATTERNS = [
    r"(?:公司|企业|客户|离职|裁员|收入|利润|竞品)",
    r"(?:employee|salary|revenue|competitor|layoff)",
]

_PUBLIC_REGEXES = tuple(re.compile(pattern) for pattern in PUBLIC_PATTERNS)
_CONFIDENTIAL_REGEXES = tuple(
    re.compile(pattern, re.IGNORECASE) for pattern in CONFIDENTIAL_PATTERNS
)


def classify(entity_name: str) -> int:
    """Return the privacy tier for an entity name (1=public, 3=confidential)."""
    if not isinstance(entity_name, str):
        return 2

    name = entity_name.strip()
    if any(pattern.search(name) for pattern in _CONFIDENTIAL_REGEXES):
        return 3
    if any(pattern.search(name) for pattern in _PUBLIC_REGEXES):
        return 1
    return 2


def _record_entity_names(record: dict) -> list[str]:
    """Extract every entity identifier that controls a record's privacy tier."""
    names: list[str] = []

    def add(value: object) -> None:
        if isinstance(value, str) and value.strip() and value not in names:
            names.append(value)

    add(record.get("name"))
    add(record.get("id"))

    entity = record.get("entity")
    if isinstance(entity, dict):
        add(entity.get("name"))
        add(entity.get("id"))

    if record.get("op") == "relate":
        add(record.get("from"))
        add(record.get("to"))
        if isinstance(entity, dict):
            add(entity.get("from"))
            add(entity.get("to"))

    return names


def _record_tier(record: dict) -> int:
    names = _record_entity_names(record)
    if not names:
        return 2
    return max(classify(name) for name in names)


def filter_ontology(jsonl_path: str, tier: int) -> list[dict]:
    """Return records whose privacy tier is at most ``tier``.

    Tier 1 returns public records only, Tier 2 also includes sanitized records,
    and Tier 3 returns all valid object records. Blank or malformed JSONL lines
    are omitted, so corrupt input cannot accidentally pass the public boundary.
    """
    if tier not in (1, 2, 3):
        raise ValueError("tier must be 1, 2, or 3")

    records: list[dict] = []
    path = Path(jsonl_path).expanduser()
    with path.open("r", encoding="utf-8") as ontology_file:
        for line in ontology_file:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if _record_tier(record) <= tier:
                records.append(record)
    return records
