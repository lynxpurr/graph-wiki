#!/usr/bin/env python3
"""回归基线路径解析 — 唯一真相源（issue #1）。

历史上基线的**写入方**（wiki_analyzer.py `baseline-save`）与**读取方**
（weekly-health-check.py / canary-gate.py）各自实现了一套路径规则，
单源 `wiki` 模式下两者解析出不同的文件，导致周检在「无基线」状态下空转、
输出 `pass: true`（假绿）。三个脚本现在统一走本模块。

路径契约（同 `--source` / `--wiki-root` / `--ontology-path` ⇒ 同一个文件）:

  仅 ontology            dirname(ontology_path)/.graph-baseline.json
  仅 wiki / ragflow      wiki_root/.graph-baseline.json
  多源                   base_dir/.graph-baseline-<slug>.json
                         base_dir = 含 ontology 时为 ontology 目录（ontology
                         是锚点），否则为 wiki_root

向后兼容: 旧版单源基线 `wiki_root/.regression-baseline.json` 在新文件不存在
时仍可读取，并返回明确的迁移提示（不静默忽略）。

纯标准库、无第三方依赖，Python >= 3.10。
"""

from __future__ import annotations

import os
from typing import NamedTuple

VALID_SOURCES = ("wiki", "ontology", "ragflow")
BASELINE_BASENAME = ".graph-baseline.json"
LEGACY_BASELINE_BASENAME = ".regression-baseline.json"
MULTI_BASENAME_TEMPLATE = ".graph-baseline-{slug}.json"

MIGRATION_NOTE_TEMPLATE = (
    "迁移提示: 正在读取旧基线 {legacy}（新路径 {primary} 不存在）。"
    "请运行 `python3 scripts/wiki_analyzer.py baseline-save --source {source}"
    "{root_arg}` 生成新基线，然后删除旧文件 {legacy}。"
)


class BaselineResolution(NamedTuple):
    """解析结果。

    path           实际应读/写的文件（优先新路径，其次旧路径）
    primary_path   新契约路径（写入方总是写这里）
    legacy_path    旧版 .regression-baseline.json 路径（仅单源 wiki/ragflow）
    used_legacy    是否退回到旧文件
    migration_note 迁移提示（None 表示无需提示）
    """

    path: str
    primary_path: str
    legacy_path: str | None
    used_legacy: bool
    migration_note: str | None


def normalize_sources(source: str | list[str] | tuple[str, ...]) -> list[str]:
    """拆分 / 归一化 source 规格，保序去重。

    与 graph_loader.normalize_sources 语义一致，但不引入 networkx 依赖
    （解析一个路径不应该要求图库已安装）。
    """
    raw = source.split(",") if isinstance(source, str) else list(source)
    names: list[str] = []
    for item in raw:
        name = str(item).strip().lower()
        if not name:
            continue
        if name not in VALID_SOURCES:
            raise ValueError(f"Unknown source: {name}")
        if name not in names:
            names.append(name)
    if not names:
        raise ValueError("At least one source is required")
    return names


def _abs_dir(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _base_dirs(ontology_path=None, wiki_root=None, default_dir=None):
    """返回 (ontology_dir, wiki_dir)。缺失时回退到 default_dir（写入方自己的默认目录）。"""
    fallback = _abs_dir(default_dir) if default_dir else os.getcwd()
    ontology_dir = (
        os.path.dirname(_abs_dir(ontology_path)) if ontology_path else fallback
    )
    wiki_dir = _abs_dir(wiki_root) if wiki_root else fallback
    return ontology_dir, wiki_dir


def resolve_primary_path(
    source: str | list[str],
    ontology_path: str | None = None,
    wiki_root: str | None = None,
    default_dir: str | None = None,
) -> str:
    """解析新契约下的基线路径（写入方与读取方共用）。"""
    sources = normalize_sources(source)
    ontology_dir, wiki_dir = _base_dirs(ontology_path, wiki_root, default_dir)

    if len(sources) > 1:
        base_dir = ontology_dir if "ontology" in sources else wiki_dir
        return os.path.join(
            base_dir, MULTI_BASENAME_TEMPLATE.format(slug="-".join(sources)),
        )
    if sources[0] == "ontology":
        return os.path.join(ontology_dir, BASELINE_BASENAME)
    return os.path.join(wiki_dir, BASELINE_BASENAME)


def resolve_legacy_path(
    source: str | list[str],
    wiki_root: str | None = None,
    default_dir: str | None = None,
) -> str | None:
    """旧版单源基线路径（仅 wiki / ragflow 单源曾有过这一路径）。"""
    sources = normalize_sources(source)
    if len(sources) != 1 or sources[0] == "ontology":
        return None
    _, wiki_dir = _base_dirs(None, wiki_root, default_dir)
    return os.path.join(wiki_dir, LEGACY_BASELINE_BASENAME)


def resolve_baseline(
    source: str | list[str],
    ontology_path: str | None = None,
    wiki_root: str | None = None,
    default_dir: str | None = None,
) -> BaselineResolution:
    """解析读取方应使用的基线文件，含旧路径回退与迁移提示。"""
    primary = resolve_primary_path(source, ontology_path, wiki_root, default_dir)
    legacy = resolve_legacy_path(source, wiki_root, default_dir)

    if legacy and not os.path.exists(primary) and os.path.exists(legacy):
        sources = normalize_sources(source)
        root_arg = f" --wiki-root {_abs_dir(wiki_root)}" if wiki_root else ""
        note = MIGRATION_NOTE_TEMPLATE.format(
            legacy=legacy,
            primary=primary,
            source=",".join(sources),
            root_arg=root_arg,
        )
        return BaselineResolution(legacy, primary, legacy, True, note)

    return BaselineResolution(primary, primary, legacy, False, None)
