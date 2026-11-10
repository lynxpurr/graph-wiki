#!/usr/bin/env python3
"""Wiki 知识图谱诊断引擎 v3.2.0 — provenance-aware

7 项诊断：
  1. 死胡同 (Dead End) — 出度=0，被链但不链出；Jaccard 匹配高PR节点 top3 候选
  2. 孤立 (Isolated) — 入度=0，链出但不被链
  3. 海绵 (Sponge) — 高入度/低出度比，吸走链接不回馈
  4. 种子扩散 (Seed Diffusion) — 从各高PR种子出发的图覆盖率
  5. 互惠 (Reciprocity) — 双向引用对 + 互惠比例
  6. 链接腐烂 (Link Rot) — wikilink 指向不存在的页面
  7. 概念陈旧 (Staleness) — 长期未修改/未被引用的概念

输出：纯文本摘要（默认）或 JSON（--json）。
死胡同匹配：文件名 + 定义段 → Jaccard 字符级 bigram → 高PR节点 top3。
不自动修复。

v2 预留：回音簇、密度失衡

用法:
  python3 graph-diagnose.py              # 纯文本摘要
  python3 graph-diagnose.py --json        # JSON 输出
  python3 graph-diagnose.py --json --full # JSON + 全量列表
"""

import os, re, sys, json, argparse, math
from collections import defaultdict, Counter
from datetime import datetime

import networkx as nx
from graph_loader import (build_graph, iter_wikilinks, _canonical_id,
                          normalize_sources, _WIKILINK_RE)
from utils import atomic_append_jsonl, atomic_write
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

__version__ = "3.2.0"


def _sha256_file(path: str) -> str:
    import hashlib
    try:
        with open(path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    except (OSError, PermissionError):
        return "(unavailable)"


def _compute_analyzer_manifest() -> tuple[str, dict]:
    """Bind provenance to the analyzer, graph parser/loader, and scorer."""
    import hashlib
    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    graph_loader_hash = _sha256_file(os.path.join(scripts_dir, "graph_loader.py"))
    manifest = {
        "analyzer": _sha256_file(__file__),
        "graph_loader": graph_loader_hash,
        "wikilink_parser": graph_loader_hash,
        "scoring": _sha256_file(os.path.join(scripts_dir, "graph-test-suite.py")),
    }
    if any(value == "(unavailable)" for value in manifest.values()):
        return "(unavailable)", manifest
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest(), manifest

def _file_or_directory_hash(path: str) -> str:
    import hashlib
    expanded = os.path.abspath(os.path.expanduser(path))
    if os.path.isfile(expanded):
        with open(expanded, 'rb') as source_file:
            return hashlib.sha256(source_file.read()).hexdigest()
    if os.path.isdir(expanded):
        digest = hashlib.sha256()
        found = False
        for dirpath, dirnames, filenames in os.walk(expanded):
            dirnames[:] = sorted(name for name in dirnames if not name.startswith('.'))
            for filename in sorted(filenames):
                if not filename.lower().endswith(('.json', '.jsonl')):
                    continue
                found = True
                full_path = os.path.join(dirpath, filename)
                relative = os.path.relpath(full_path, expanded).replace(os.sep, '/')
                digest.update(relative.encode('utf-8'))
                digest.update(b'\0')
                with open(full_path, 'rb') as source_file:
                    digest.update(source_file.read())
                digest.update(b'\0')
        return digest.hexdigest() if found else "(empty)"
    return "(unavailable)"


def compute_source_snapshot(source: str, wiki_root: str = None,
                            ontology_path: str = None,
                            ragflow_path: str = None) -> str:
    import hashlib
    try:
        sources = normalize_sources(source)
        if len(sources) > 1:
            digest = hashlib.sha256()
            for name in sources:
                part = compute_source_snapshot(
                    name, wiki_root=wiki_root, ontology_path=ontology_path,
                    ragflow_path=ragflow_path,
                )
                if part == "(unavailable)":
                    return part
                digest.update(name.encode('utf-8'))
                digest.update(b'\0')
                digest.update(part.encode('ascii'))
                digest.update(b'\0')
            return digest.hexdigest()
        if sources[0] == 'ontology' and ontology_path and os.path.exists(ontology_path):
            return _file_or_directory_hash(ontology_path)
        elif sources[0] == 'ragflow' and ragflow_path:
            return _file_or_directory_hash(ragflow_path)
        elif sources[0] == 'wiki' and wiki_root and os.path.isdir(wiki_root):
            h = hashlib.sha256()
            wiki_root = os.path.abspath(os.path.expanduser(wiki_root))
            markdown_files = []
            for dirpath, dirnames, filenames in os.walk(wiki_root):
                dirnames[:] = sorted(
                    d for d in dirnames
                    if not d.startswith('.') and d not in ('node_modules',)
                )
                for fn in filenames:
                    if fn.endswith('.md'):
                        fpath = os.path.join(dirpath, fn)
                        relpath = os.path.relpath(fpath, wiki_root).replace(os.sep, '/')
                        markdown_files.append((relpath, fpath))
            for relpath, fpath in sorted(markdown_files):
                h.update(relpath.encode('utf-8'))
                h.update(b'\0')
                with open(fpath, 'rb') as f:
                    h.update(f.read())
                h.update(b'\0')
            digest = h.hexdigest()
            return digest if digest != hashlib.sha256(b'').hexdigest() else "(empty)"
        return "(unavailable)"
    except Exception:
        return "(unavailable)"

def _make_provenance(source: str, wiki_root: str = None,
                     ontology_path: str = None,
                     ragflow_path: str = None) -> dict:
    analyzer_sha256, analyzer_manifest = _compute_analyzer_manifest()
    return {
        "analyzer": "graph-diagnose.py",
        "analyzer_version": __version__,
        "analyzer_sha256": analyzer_sha256,
        "analyzer_manifest": analyzer_manifest,
        "source": source,
        "source_snapshot_hash": compute_source_snapshot(
            source, wiki_root, ontology_path, ragflow_path,
        ),
        "threshold_version": __version__,
        "run_at": datetime.now().isoformat(),
    }

SCRIPT_SHA256 = "(linked)"

WIKI_ROOT = os.environ.get('WIKI_ROOT', os.getcwd())
ONTOLOGY_PATH = os.environ.get(
    'ONTOLOGY_PATH',
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'ontology.jsonl',
    ),
)


def _validated_writeback_path(path: str) -> str:
    """Resolve a non-symlink writeback path within its real parent directory."""
    absolute = os.path.abspath(os.path.expanduser(path))
    parent = os.path.realpath(os.path.dirname(absolute))
    resolved = os.path.realpath(absolute)
    try:
        inside_parent = os.path.commonpath([parent, resolved]) == parent
    except ValueError:
        inside_parent = False
    if not inside_parent:
        raise ValueError(f"writeback path escapes parent directory: {path}")
    if os.path.islink(absolute):
        raise ValueError(f"writeback symlink target rejected: {path}")
    return resolved


# ══════════════════════════════════════════════════
# 可配置阈值
# ══════════════════════════════════════════════════
THRESHOLDS = {
    # 海绵：入度比例 ≥ 此值 且 总度数 ≥ min
    "sponge_in_ratio": 0.75,
    "sponge_min_total_degree": 5,

    # 种子扩散：覆盖率低于此值标记警告
    "seed_coverage_warn": 0.30,
    "seed_count": 10,

    # 互惠
    "reciprocity_low": 0.05,
    "reciprocity_high": 0.25,

    # 概念陈旧
    "staleness_unmodified_days": 90,
    "staleness_unlinked_days": 60,
    "staleness_min_age_days": 30,

    # 死胡同 Jaccard
    "dead_end_candidate_count": 3,
    "dead_end_min_pr_percentile": 70,
}

# ══════════════════════════════════════════════════
# Jaccard
# ══════════════════════════════════════════════════

def jaccard_similarity(text_a, text_b):
    """字符级 bigram Jaccard——快速、语言无关。"""
    def bigrams(s):
        return set(s[i:i+2] for i in range(len(s)-1))
    a, b = bigrams(text_a.lower()), bigrams(text_b.lower())
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ══════════════════════════════════════════════════
# D1: 死胡同 (Dead End) — 出度=0
# ══════════════════════════════════════════════════

def diagnose_deadends(G, meta, pr):
    dead_ends = []
    pr_sorted = sorted(pr.items(), key=lambda x: -x[1])
    cutoff = max(int(len(pr_sorted) * THRESHOLDS["dead_end_min_pr_percentile"] / 100), 1)
    high_pr_nodes = [slug for slug, _ in pr_sorted[:cutoff]]

    for node in G.nodes():
        if G.out_degree(node) != 0:
            continue
        in_deg = G.in_degree(node)
        # True dead ends: in>0 (receives links) and not an audit/log page
        if in_deg == 0 or node.startswith('audit-logs/'):
            continue
        candidates = []
        m = meta[node]
        query = f"{m.get('title', node)} {m.get('definition_text', '')[:300]}"
        scored = []
        for target in high_pr_nodes:
            if target == node:
                continue
            if G.has_edge(node, target):
                continue
            tm = meta[target]
            tq = f"{tm.get('title', target)} {tm.get('definition_text', '')[:300]}"
            sim = jaccard_similarity(query, tq)
            scored.append((target, sim))
        scored.sort(key=lambda x: -x[1])
        candidates = [
            {'slug': s, 'similarity': round(sim, 4), 'pr': round(pr.get(s, 0), 5)}
            for s, sim in scored[:THRESHOLDS["dead_end_candidate_count"]]
        ]

        dead_ends.append({
            'slug': node,
            'title': meta[node].get('title', node),
            'in_degree': in_deg,
            'pr': round(pr.get(node, 0), 5),
            'candidates': candidates,
            'top_candidates': [c['slug'] for c in candidates],
        })

    dead_ends.sort(key=lambda x: (-x['in_degree'], -x['pr']))
    return dead_ends


# ══════════════════════════════════════════════════
# D2: 孤立 (Isolated) — 入度=0
# ══════════════════════════════════════════════════

def diagnose_isolated(G, meta, pr):
    isolated = []
    for node in G.nodes():
        if G.in_degree(node) != 0:
            continue
        isolated.append({
            'slug': node,
            'title': meta[node].get('title', node),
            'theorist': meta[node].get('theorist', 'unknown'),
            'out_degree': G.out_degree(node),
            'pr': round(pr.get(node, 0), 5),
            'date_created': meta[node].get('date_created', 'unknown'),
        })
    isolated.sort(key=lambda x: (-x['out_degree'], -x['pr']))
    return isolated


# ══════════════════════════════════════════════════
# D3: 海绵 (Sponge) — 高入度比
# ══════════════════════════════════════════════════

def diagnose_sponges(G, meta, pr):
    sponges = []
    for node in G.nodes():
        in_d = G.in_degree(node)
        out_d = G.out_degree(node)
        total = in_d + out_d
        if total < THRESHOLDS["sponge_min_total_degree"]:
            continue
        ratio = in_d / total
        if ratio >= THRESHOLDS["sponge_in_ratio"]:
            sponges.append({
                'slug': node,
                'title': meta[node].get('title', node),
                'in_degree': in_d,
                'out_degree': out_d,
                'total_degree': total,
                'in_ratio': round(ratio, 3),
                'pr': round(pr.get(node, 0), 5),
            })
    sponges.sort(key=lambda x: (-x['in_ratio'], -x['total_degree']))
    return sponges


# ══════════════════════════════════════════════════
# D4: 种子扩散 (Seed Diffusion)
# ══════════════════════════════════════════════════

def diagnose_seed_diffusion(G, meta, pr):
    total = len(G.nodes())
    seeds = sorted(pr, key=pr.get, reverse=True)[:THRESHOLDS["seed_count"]]
    results = []
    for seed in seeds:
        reachable = set()
        stack = [seed]
        while stack:
            n = stack.pop()
            if n in reachable:
                continue
            reachable.add(n)
            for succ in G.successors(n):
                if succ not in reachable:
                    stack.append(succ)
        cov = len(reachable) / total if total else 0
        results.append({
            'slug': seed,
            'title': meta[seed].get('title', seed),
            'pr': round(pr.get(seed, 0), 5),
            'reachable': len(reachable),
            'total': total,
            'coverage': round(cov, 3),
            'warning': cov < THRESHOLDS["seed_coverage_warn"],
        })
    return results


# ══════════════════════════════════════════════════
# D5: 互惠 (Reciprocity)
# ══════════════════════════════════════════════════

def diagnose_reciprocity(G, meta, pr):
    reciprocal_pairs = []
    for a, b in G.edges():
        if G.has_edge(b, a) and a < b:
            reciprocal_pairs.append((a, b))

    total_edges = G.number_of_edges()
    reciprocal_edges = len(reciprocal_pairs) * 2
    ratio = reciprocal_edges / total_edges if total_edges else 0

    if ratio < THRESHOLDS["reciprocity_low"]:
        status, status_text = "low", "交叉引用不足——概念间对话太少"
    elif ratio > THRESHOLDS["reciprocity_high"]:
        status, status_text = "high", "回音室风险——双向引用比例过高"
    else:
        status, status_text = "healthy", "健康——交叉引用适度"

    return {
        'reciprocal_pairs': len(reciprocal_pairs),
        'reciprocal_edges': reciprocal_edges,
        'total_edges': total_edges,
        'ratio': round(ratio, 4),
        'status': status,
        'status_text': status_text,
        'pairs': [{'a': a, 'b': b} for a, b in reciprocal_pairs[:30]],
    }


# ══════════════════════════════════════════════════
# D6: 链接腐烂 (Link Rot)
# ══════════════════════════════════════════════════

def diagnose_linkrot(G, meta, wiki_root=None):
    broken = defaultdict(list)
    broken_seen = {}        # (source_cid, target) -> entry, dedup per issue #4
    code_block_exempt = 0   # wikilinks skipped inside code fences, issue #6
    code_block_samples = []
    skill_scheme_exempt = 0  # skill:/skills: prefixed wikilinks, issue #10
    skill_scheme_samples = []
    empty_target_skipped = 0  # malformed [[   ]] targets, not missing pages
    empty_target_samples = []
    source_paths = {}       # canonical id -> unfolded relative path, issue #9

    # Discover all markdown files and build canonical ID set
    wiki_root = os.path.expanduser(wiki_root or '.')
    md_files = []
    for root, dirs, files in os.walk(wiki_root):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules',)]
        for f in files:
            if f.endswith('.md'):
                md_files.append(os.path.join(root, f))

    canonical_targets = {_canonical_id(p, wiki_root) for p in md_files}
    # Basename aliases: wikilinks may use the bare page name (e.g. [[algorithms]]
    # for concepts/algorithms.md). Resolve both forms so subdirectory pages are
    # not falsely reported as broken links (P0-2 fix).
    basename_targets = {os.path.basename(p)[:-3] for p in md_files if p.endswith('.md')}
    # Raw relative paths (uncollapsed): _canonical_id collapses dir/index.md -> dir,
    # so a link written as [[dir/index]] matched no canonical id and was falsely
    # reported as broken (issue #3). Accept both forms.
    raw_targets = set()
    for _p in md_files:
        _rel = os.path.relpath(_p, wiki_root).replace('\\', '/')
        if not _rel.endswith('.md'):
            continue
        raw_targets.add(_rel[:-3])
        # A directory landing page written as README.md is addressed by the bare
        # directory name in this wiki's convention ([[dir]] for dir/README.md),
        # mirroring the dir/index.md collapse in _canonical_id (issue #3).
        if _rel.endswith('/README.md'):
            raw_targets.add(_rel[:-len('/README.md')])
    valid_targets = set(canonical_targets) | basename_targets | raw_targets

    # Index link patterns — auto-generated directory/index pages whose
    # outbound wikilinks are informational, not content-level references.
    def _is_index_slug(slug: str) -> bool:
        parts = slug.lower().split('/')
        last = parts[-1] if parts else slug.lower()
        return (
            last.startswith('index')
            or last.startswith('log')
            or last == 'readme'
            or any(p.startswith('index') for p in parts)
        )

    def _is_skill_scheme(target):
        """skill:/skills: prefixed wikilinks are scheme references, not pages."""
        return target.startswith('skill:') or target.startswith('skills:')

    for path in md_files:
        source_cid = _canonical_id(path, wiki_root)
        content = open(path, encoding='utf-8').read()
        # Raw inner text of every wikilink in this file, in parse order. iter_wikilinks()
        # strips the body and drops |label / #anchor before yielding, so all three
        # malformed empty-target shapes arrive as "". A sample of target="" then cannot
        # tell [[   |label]] from [[   #section]] from a full-width space. Re-scanning with
        # the same regex keeps counts untouched and only adds fidelity to the payload.
        raw_bodies = [m.group(1) for m in _WIKILINK_RE.finditer(content)]
        # Issue #9: index-ness must be decided from the UNFOLDED path. _canonical_id()
        # collapses dir/index.md to dir, so feeding it back to _is_index_slug() tested
        # 'incidents' (starts with 'incid', not 'index') and silently failed, making
        # the exemption surface inconsistent between a root index.md and dir/index.md.
        source_rel = os.path.relpath(path, wiki_root).replace(os.sep, '/')
        source_paths[source_cid] = source_rel
        source_is_index = _is_index_slug(source_rel)
        for link_idx, (target_raw, _label, inside_code) in enumerate(iter_wikilinks(content)):
            if inside_code:
                # Code-fenced links stay exempt from link-rot (a code sample is not
                # a reference), but the exemption must be observable — dropping them
                # silently hid real references (issue #6).
                if _is_skill_scheme(target_raw):
                    # issue #10: scheme-prefixed links are exempt everywhere, but the
                    # exemption still has to be observable, exactly like the fence one.
                    skill_scheme_exempt += 1
                    if len(skill_scheme_samples) < 20:
                        skill_scheme_samples.append({'source': source_cid, 'target': target_raw})
                else:
                    code_block_exempt += 1
                    if len(code_block_samples) < 20:
                        code_block_samples.append({'source': source_cid, 'target': target_raw})
                continue
            if _is_skill_scheme(target_raw):
                skill_scheme_exempt += 1
                if len(skill_scheme_samples) < 20:
                    skill_scheme_samples.append({'source': source_cid, 'target': target_raw})
                continue
            if not target_raw.strip():
                # An empty or whitespace-only target (`[[   ]]`) is a malformed
                # link, not a reference to a page that does not exist. Counting it
                # as link-rot inflated the headline number with something no author
                # can act on as a "missing page". It stays exempt, but observably so
                # — same rule as the fence (#6) and scheme (#10) exemptions.
                empty_target_skipped += 1
                if len(empty_target_samples) < 20:
                    as_written = (raw_bodies[link_idx]
                                  if link_idx < len(raw_bodies) else target_raw)
                    empty_target_samples.append({'source': source_cid,
                                                 'target': target_raw,
                                                 'as_written': as_written})
                continue
            target = target_raw
            if target.startswith('../'):
                rel = os.path.relpath(path, wiki_root)
                target = os.path.normpath(os.path.join(os.path.dirname(rel), target))
            if target not in valid_targets:
                # Find context snippet
                idx = content.find(f'[[{target_raw}')
                if idx == -1:
                    idx = content.find(f'[[{target_raw}')
                start = max(0, idx - 30) if idx >= 0 else 0
                end = min(len(content), (idx if idx >= 0 else 0) + len(target_raw) + 34)
                ctx = content[start:end].replace('\n', ' ').strip()
                seen_key = (source_cid, target)
                existing = broken_seen.get(seen_key)
                if existing is not None:
                    existing['occurrences'] += 1
                else:
                    entry = {
                        'target': target, 'context': ctx,
                        'is_index_link': source_is_index,
                        'occurrences': 1,
                    }
                    broken_seen[seen_key] = entry
                    broken[source_cid].append(entry)

    details = []
    for source in sorted(broken, key=lambda s: -len(broken[s])):
        details.append({
            'source': source,
            # issue #9: keep the unfolded location so a folded slug stays traceable.
            'source_path': source_paths.get(source, source),
            'title': meta[source].get('title', source) if source in meta else source,
            'broken_count': len(broken[source]),
            'broken_targets': broken[source],
        })

    return {
        # issue #16: total_broken is the RAW reference count so the three fields
        # form a checkable identity: total == unique + duplicated. It previously
        # summed the same deduped set as unique_broken, which made the two fields
        # always equal and one of them meaningless.
        'total_broken': sum(e['occurrences'] for e in broken_seen.values()),
        'affected_sources': len(details),
        'details': details,
        # issue #8: these aggregates were computed but never reached any CLI exit.
        # Semantics, locked by test_dedupe_aggregates_*:
        #   dedupe key              = (source, target), so the same missing page
        #                             linked from two sources stays two work items
        #   unique_broken           = deduped work items
        #   duplicated_entries      = references merged away = sum(occurrences - 1)
        #   entries_with_duplicates = deduped items that had occurrences > 1
        'unique_broken': len(broken_seen),
        'duplicated_entries': sum(e['occurrences'] - 1 for e in broken_seen.values()),
        'entries_with_duplicates': sum(1 for e in broken_seen.values() if e['occurrences'] > 1),
        'code_block_exempt': code_block_exempt,
        'code_block_samples': code_block_samples,
        'skill_scheme_exempt': skill_scheme_exempt,
        'skill_scheme_samples': skill_scheme_samples,
        'empty_target_skipped': empty_target_skipped,
        'empty_target_samples': empty_target_samples,
    }


# ══════════════════════════════════════════════════
# D7: 概念陈旧 (Staleness)
# ══════════════════════════════════════════════════

def diagnose_staleness(G, meta, pr):
    now = datetime.now()
    stale_list = []

    for node in G.nodes():
        m = meta[node]
        dc = m.get('date_created')
        # Skip if no date metadata (ontology mode may not have dates)
        if not dc:
            continue
        try:
            created = datetime.strptime(dc, '%Y-%m-%d')
        except ValueError:
            continue

        age_days = (now - created).days
        if age_days < THRESHOLDS["staleness_min_age_days"]:
            continue

        flags = []
        score = 0

        dm = m.get('date_modified')
        days_since_mod = age_days
        if dm:
            try:
                days_since_mod = (now - datetime.strptime(dm, '%Y-%m-%d')).days
            except ValueError:
                pass

        if days_since_mod >= THRESHOLDS["staleness_unmodified_days"]:
            flags.append(f"未修改{days_since_mod}天")
            score += 2

        in_deg = G.in_degree(node)
        if in_deg == 0 and age_days >= THRESHOLDS["staleness_unlinked_days"]:
            flags.append(f"零入度{age_days}天")
            score += 1

        file_mtime = m.get('file_mtime')
        if file_mtime:
            file_age = (now - file_mtime).days
            if file_age >= THRESHOLDS["staleness_unmodified_days"]:
                flags.append(f"文件未触达{file_age}天")
                score += 1

        if score >= 2:
            stale_list.append({
                'slug': node,
                'title': m['title'],
                'age_days': age_days,
                'days_since_mod': days_since_mod,
                'in_degree': in_deg,
                'out_degree': G.out_degree(node),
                'pr': round(pr.get(node, 0), 5),
                'flags': flags,
                'staleness_score': score,
            })

    stale_list.sort(key=lambda x: -x['staleness_score'])
    return stale_list


# ══════════════════════════════════════════════════
# D8-D10: Multi-source fusion diagnostics
# ══════════════════════════════════════════════════

def diagnose_cross_source_coverage(G):
    """Measure the share of wiki nodes resolved to ontology entities."""
    source_nodes = G.graph.get('source_nodes', {})
    wiki_nodes = list(source_nodes.get('wiki', []))
    mappings = G.graph.get('entity_mappings', {}).get('wiki', {})
    mapped = [node for node in wiki_nodes if node in mappings]
    unmapped = [node for node in wiki_nodes if node not in mappings]
    source_graphs = G.graph.get('_source_graphs', {})
    wiki_graph = source_graphs.get('wiki')
    if wiki_graph is not None:
        unmapped.sort(key=lambda node: -wiki_graph.degree(node))
    coverage = len(mapped) / len(wiki_nodes) if wiki_nodes else 0.0
    return {
        'wiki_nodes': len(wiki_nodes),
        'mapped_wiki_nodes': len(mapped),
        'wiki_coverage': round(coverage, 4),
        'unmapped_wiki_nodes': unmapped[:20],
        'threshold': 0.15,
        'pass': coverage > 0.15,
        'conflicts': len(G.graph.get('merge_conflicts', [])),
    }


def _ontology_theorist_map(G):
    """Build dict: ontology_id → declared theorist (from ontology properties.theorist)."""
    ontology_meta = G.graph.get('_source_meta', {}).get('ontology', {})
    declared = {}
    for node_id, data in ontology_meta.items():
        if not isinstance(data, dict):
            continue
        properties = data.get('properties', {})
        if not isinstance(properties, dict):
            continue
        theorist = properties.get('theorist', '')
        if theorist:
            declared[node_id] = str(theorist).strip().casefold()
    return declared


def diagnose_type_consistency(G):
    """Compare ontology declared theorists with wiki theorist fields on resolved entities.

    Only compares nodes where ontology declares a theorist (via properties.theorist).
    Nodes without declared theorists are skipped (not counted as mismatches).
    """
    compared = 0
    mismatches = []
    type_values = []
    declared_theorists = _ontology_theorist_map(G)
    for node, attrs in G.nodes(data=True):
        sources = attrs.get('sources', [])
        if not {'wiki', 'ontology'}.issubset(set(sources)):
            continue
        ontology_id = attrs.get('ontology_id', node)
        declared_theorist = declared_theorists.get(ontology_id, '')
        if not declared_theorist:
            continue  # no declared theorist → skip (not a mismatch)
        ontology_type = str(attrs.get('type', '')).strip().casefold()
        wiki_theorist = str(attrs.get('theorist', '')).strip().casefold()
        if not ontology_type or not wiki_theorist:
            continue
        type_values.append(ontology_type)
        compared += 1
        # Substring match to handle short-name aliases
        matched = (declared_theorist in wiki_theorist
                   or wiki_theorist in declared_theorist)
        if not matched:
            mismatches.append({
                'node': node,
                'ontology_type': attrs.get('type'),
                'wiki_theorist': attrs.get('theorist'),
                'declared_theorist': attrs.get('theorist'),
            })

    distribution = Counter(type_values)
    total = sum(distribution.values())
    entropy = 0.0
    for count in distribution.values():
        probability = count / total if total else 0
        if probability:
            entropy -= probability * math.log2(probability)
    max_entropy = math.log2(len(distribution)) if len(distribution) > 1 else 0.0
    normalized_entropy = entropy / max_entropy if max_entropy else 0.0
    consistency = (compared - len(mismatches)) / compared if compared else 0.0
    return {
        'compared_nodes': compared,
        'consistent_nodes': compared - len(mismatches),
        'consistency_ratio': round(consistency, 4),
        'type_distribution': dict(sorted(distribution.items())),
        'entropy': round(entropy, 4),
        'normalized_entropy': round(normalized_entropy, 4),
        'mismatches': mismatches,
        'mismatch_count': len(mismatches),
        'pass': not mismatches and bool(compared),
    }


def _louvain_communities(graph):
    if graph.number_of_nodes() == 0:
        return []
    try:
        louvain = nx.algorithms.community.louvain_communities
    except AttributeError:
        try:
            from community import community_louvain
        except ImportError:
            return None
        # v3.9.0: fixed seed for reproducibility
        partition = community_louvain.best_partition(
            graph.to_undirected(), random_state=42
        )
        communities = defaultdict(set)
        for node, community_id in partition.items():
            communities[community_id].add(node)
        return list(communities.values())
    return [set(group) for group in louvain(graph.to_undirected(), seed=42)]


def diagnose_information_gain(G):
    """Compare merged communities with the primary single-source graph.

    Primary source is controlled by D10_PRIMARY env var (default: 'ontology').
    """
    source_graphs = G.graph.get('_source_graphs', {})
    source_names = G.graph.get('sources', [])
    primary_source = os.environ.get('D10_PRIMARY', 'ontology')
    if primary_source not in source_graphs:
        primary_source = source_names[0] if source_names else None
    primary_graph = source_graphs.get(primary_source) if primary_source else None
    if primary_graph is None:
        return {
            'method': 'unavailable', 'primary_source': primary_source,
            'average_jaccard': None, 'information_gain': 0.0,
            'note': 'single-source graph unavailable',
        }

    single_communities = _louvain_communities(primary_graph)
    merged_communities = _louvain_communities(G)
    if single_communities is not None and merged_communities is not None:
        mapping = G.graph.get('entity_mappings', {}).get(primary_source, {})
        transformed = [
            {mapping.get(node, node) for node in community}
            for community in single_communities
        ]
        similarities = []
        for community in transformed:
            best = 0.0
            for merged_community in merged_communities:
                union = community | merged_community
                score = len(community & merged_community) / len(union) if union else 0.0
                best = max(best, score)
            similarities.append(best)
        average_jaccard = sum(similarities) / len(similarities) if similarities else 0.0
        return {
            'method': 'louvain_jaccard',
            'primary_source': primary_source,
            'single_communities': len(single_communities),
            'merged_communities': len(merged_communities),
            'average_jaccard': round(average_jaccard, 4),
            'information_gain': round(1.0 - average_jaccard, 4),
        }

    primary_edges = primary_graph.number_of_edges()
    new_edges = max(0, G.number_of_edges() - primary_edges)
    ratio = new_edges / primary_edges if primary_edges else float(bool(new_edges))
    return {
        'method': 'new_edges_ratio',
        'primary_source': primary_source,
        'single_edges': primary_edges,
        'merged_edges': G.number_of_edges(),
        'new_edges': new_edges,
        'new_edges_ratio': round(ratio, 4),
        'average_jaccard': None,
        'information_gain': round(min(1.0, ratio), 4),
    }


def diagnose_multi_source(G, sources):
    if len(sources) < 2:
        return {
            'status': 'skipped',
            'note': 'D8-D10 require multiple sources',
        }
    # Check if ontology graph is effectively empty (no nodes from ontology source)
    source_graphs = G.graph.get('_source_graphs', {})
    onto_graph = source_graphs.get('ontology')
    if onto_graph is None or onto_graph.number_of_nodes() == 0:
        return {
            'status': 'skipped',
            'note': 'ontology graph is empty — D8-D10 not applicable',
        }
    return {
        'status': 'computed',
        'sources': list(sources),
        'D8': diagnose_cross_source_coverage(G),
        'D9': diagnose_type_consistency(G),
        'D10': diagnose_information_gain(G),
    }


# ══════════════════════════════════════════════════
# 输出
# ══════════════════════════════════════════════════

def text_report(G, de, iso, spo, sd, rec, lr, st, multi_source=None):
    lines = []
    lines.append("╔══════════════════════════════════════════════════════════╗")
    lines.append("║     Knowledge Graph 知识图谱诊断引擎 v1                     ║")
    lines.append("╠══════════════════════════════════════════════════════════╣")
    lines.append(f"║  节点:{len(G.nodes()):>4d}  边:{G.number_of_edges():>4d}  阈值见 --json            ║")
    lines.append("╚══════════════════════════════════════════════════════════╝")
    lines.append("")

    # 1. 死胡同
    lines.append("━" * 60)
    lines.append(f"1. 死胡同 (Dead End) — 出度=0，被链但不链出: {len(de)} 个")
    lines.append("━" * 60)
    for d in de[:10]:
        lines.append(f"  [{d['slug']}] in={d['in_degree']}  PR={d['pr']:.4f}")
        lines.append(f"    「{d['title']}」")
        if d['candidates']:
            cs = ", ".join(f"{c['slug']}(sim={c['similarity']:.2f})" for c in d['candidates'])
            lines.append(f"    → 建议链接: {cs}")
    if len(de) > 10:
        lines.append(f"  ... 还有 {len(de)-10} 个 (--json 查看全量)")
    lines.append("")

    # 2. 孤立
    lines.append("━" * 60)
    lines.append(f"2. 孤立 (Isolated) — 入度=0，链出但不被链: {len(iso)} 个")
    lines.append("━" * 60)
    for x in iso[:10]:
        lines.append(f"  [{x['slug']}] out={x['out_degree']}  ({x['theorist']})  created={x['date_created']}")
    if len(iso) > 10:
        lines.append(f"  ... 还有 {len(iso)-10} 个")
    lines.append("")

    # 3. 海绵
    lines.append("━" * 60)
    t = THRESHOLDS
    lines.append(f"3. 海绵 (Sponge) — 入度比≥{t['sponge_in_ratio']}, 总度≥{t['sponge_min_total_degree']}: {len(spo)} 个")
    lines.append("━" * 60)
    for s in spo[:10]:
        lines.append(f"  [{s['slug']}] in={s['in_degree']}/{s['total_degree']}  ratio={s['in_ratio']:.2f}  PR={s['pr']:.4f}")
        lines.append(f"    「{s['title']}」")
    if len(spo) > 10:
        lines.append(f"  ... 还有 {len(spo)-10} 个")
    lines.append("")

    # 4. 种子扩散
    lines.append("━" * 60)
    lines.append("4. 种子扩散 (Seed Diffusion) — 高PR节点触达范围")
    lines.append("━" * 60)
    for sd_ in sd:
        flag = " ⚠ 扩散不足" if sd_['warning'] else ""
        lines.append(f"  [{sd_['slug']}] 覆盖率={sd_['coverage']:.0%} ({sd_['reachable']}/{sd_['total']})  PR={sd_['pr']:.4f}{flag}")
    lines.append("")

    # 5. 互惠
    lines.append("━" * 60)
    lines.append("5. 互惠 (Reciprocity)")
    lines.append("━" * 60)
    lines.append(f"  互惠边: {rec['reciprocal_edges']}/{rec['total_edges']} = {rec['ratio']:.1%}")
    lines.append(f"  状态: {rec['status_text']}")
    if rec['pairs']:
        lines.append(f"  示例互惠对 ({min(5, len(rec['pairs']))}):")
        for p in rec['pairs'][:5]:
            lines.append(f"    {p['a']} ↔ {p['b']}")
    lines.append("")

    # 6. 链接腐烂
    lines.append("━" * 60)
    # Issue #16 follow-up: total_broken is raw references, while the per-source
    # detail lines count deduped targets. Printing only the raw number next to a
    # deduped breakdown read as two different accounts of one thing.
    _lr_unique = lr.get('unique_broken', lr['total_broken'])
    lines.append(
        f"6. 链接腐烂 (Link Rot) — 断链引用:{lr['total_broken']}处"
        f" / {_lr_unique}个目标, 涉及{lr['affected_sources']}个源"
    )
    lines.append("━" * 60)
    for d in lr['details'][:10]:
        title = d.get('title', d['source'])
        lines.append(f"  [{d['source']}] {d['broken_count']}个目标:")
        for bt in d['broken_targets'][:3]:
            lines.append(f"    → [[{bt['target']}]]  ...{bt['context'][:60]}...")
    if len(lr['details']) > 10:
        lines.append(f"  ... 还有 {len(lr['details'])-10} 个")
    lines.append("")

    # 7. 概念陈旧
    lines.append("━" * 60)
    lines.append(f"7. 概念陈旧 (Staleness) — 陈旧度≥2: {len(st)} 个")
    lines.append("━" * 60)
    for s in st[:10]:
        lines.append(f"  [{s['slug']}] score={s['staleness_score']}  age={s['age_days']}d  in={s['in_degree']}")
        lines.append(f"    {', '.join(s['flags'])}")
    if len(st) > 10:
        lines.append(f"  ... 还有 {len(st)-10} 个")
    lines.append("")

    multi_source = multi_source or {
        'status': 'skipped', 'note': 'D8-D10 require multiple sources',
    }
    if multi_source.get('status') == 'computed':
        d8, d9, d10 = (
            multi_source['D8'], multi_source['D9'], multi_source['D10'],
        )
        lines.append("━" * 60)
        lines.append("8. 跨源覆盖度 (Cross-Source Coverage)")
        lines.append("━" * 60)
        lines.append(
            f"  wiki coverage: {d8['wiki_coverage']:.1%} "
            f"({d8['mapped_wiki_nodes']}/{d8['wiki_nodes']}) — "
            f"{'PASS' if d8['pass'] else 'FAIL'}"
        )
        if d8['unmapped_wiki_nodes']:
            lines.append(f"  未映射 Top 20: {', '.join(d8['unmapped_wiki_nodes'])}")
        lines.append("")
        lines.append("━" * 60)
        lines.append("9. 类型一致性 (Type Consistency)")
        lines.append("━" * 60)
        lines.append(
            f"  consistency: {d9['consistency_ratio']:.1%}; "
            f"entropy={d9['entropy']:.4f}; mismatches={d9['mismatch_count']}"
        )
        lines.append("")
        lines.append("━" * 60)
        lines.append("10. 信息增益 (Information Gain)")
        lines.append("━" * 60)
        lines.append(
            f"  method={d10['method']}; gain={d10['information_gain']:.1%}; "
            f"average_jaccard={d10.get('average_jaccard')}"
        )
        lines.append("")
    else:
        lines.append(f"D8-D10: skipped — {multi_source.get('note', 'multiple sources required')}")
        lines.append("")

    # 汇总
    lines.append("═" * 60)
    lines.append("诊断汇总")
    lines.append("═" * 60)
    lines.append(f"  死胡同:      {len(de):>4d}")
    lines.append(f"  孤立:        {len(iso):>4d}")
    lines.append(f"  海绵:        {len(spo):>4d}")
    lines.append(f"  扩散不足:    {sum(1 for x in sd if x['warning']):>4d}/{len(sd)}")
    lines.append(f"  互惠:        {rec['status']:>6s} ({rec['ratio']:.1%})")
    lines.append(
        f"  链接腐烂:    {lr['total_broken']:>4d}处引用"
        f" / {lr.get('unique_broken', lr['total_broken'])}个目标"
    )
    lines.append(f"  概念陈旧:    {len(st):>4d}")
    if multi_source.get('status') == 'computed':
        lines.append(f"  跨源覆盖:    {multi_source['D8']['wiki_coverage']:>6.1%}")
        lines.append(f"  类型一致:    {multi_source['D9']['consistency_ratio']:>6.1%}")
        lines.append(f"  信息增益:    {multi_source['D10']['information_gain']:>6.1%}")
    lines.append("")

    return "\n".join(lines)


SCHEMA_VERSION = "3.0.0"
SCHEMA_V3 = {
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "graph-diagnose-output-v3",
  "description": "WIP: legacy draft; not yet aligned with the normalized JSON response",
  "x-status": "WIP",
  "type": "object",
  "required": ["version", "meta", "summary", "dead_ends", "isolated", "sponges"],
  "properties": {
    "version": {"type": "string"},
    "meta": {
      "type": "object",
      "required": ["analyzer_version", "generated_at", "thresholds_version"],
      "properties": {
        "analyzer_version": {"type": "string"},
        "generated_at": {"type": "string", "format": "date-time"},
        "thresholds_version": {"type": "string"},
        "source": {"type": "string"},
        "duration_ms": {"type": "number"},
        "exit_status": {"type": "string", "enum": ["ok", "partial", "error"]}
      }
    },
    "summary": {
      "type": "object",
      "required": ["nodes", "edges", "score"],
      "properties": {
        "nodes": {"type": "integer"},
        "edges": {"type": "integer"},
        "score": {"type": "integer", "minimum": 0, "maximum": 110},
        "dead_ends_count": {"type": "integer"},
        "isolated_count": {"type": "integer"},
        "sponges_count": {"type": "integer"},
        "rot_count": {"type": "integer"},
        "diagnostic_version": {"type": "string"}
      }
    }
  }
}

def json_output(G, de, iso, spo, sd, rec, lr, st, full=False, source="wiki",
                provenance=None, multi_source=None):
    """Schema v1.0 — 标准化 JSON 输出（供下游工具消费）。"""
    isolated_nodes = []
    try:
        from graph_loader import build_graph as _bg_ref
    except ImportError:
        pass
    for n in G.nodes():
        if G.degree(n) == 0:
            isolated_nodes.append(n)
    components = 0
    try:
        if G.number_of_nodes() > 0:
            from networkx import number_weakly_connected_components
            components = number_weakly_connected_components(G)
    except Exception:
        pass

    result = {
        'schema_version': SCHEMA_VERSION,
        'source': source,
        'timestamp': datetime.now().isoformat(),
        'provenance': provenance or {}, 
        'graph_stats': {
            'nodes': G.number_of_nodes(),
            'edges': G.number_of_edges(),
            'isolated': len(isolated_nodes),
            'density': round(nx.density(G), 6) if G.number_of_nodes() > 1 else 0,
            'components': components,
        },
        'scores': {
            # graph-diagnose does NOT compute a score. The authoritative score is
            # produced by graph-test-suite (summary.score), and its maximum is
            # active_dimensions * 10 + 10 (bonus cap) — 120 for an 11-dimension
            # single-source wiki, not 100. A hardcoded 100 here produced a false
            # "score exceeds max" reading (102 vs 100) — issue #5.
            'total': None,
            'max': None,
            'dimensions': {},
            'source': 'graph-test-suite',
            'max_formula': 'active_dimensions * 10 + 10',
            'note': 'score/max are not produced by graph-diagnose; see graph-test-suite',
        },
        'diagnosis': _diagnosis_to_array(
            de, iso, spo, sd, rec, lr, st, full, multi_source,
        ),
        # issue #8: link-rot aggregates were computed by diagnose_linkrot() and then
        # discarded — no CLI exit ever carried them. Delivered here, with the dedupe
        # semantics spelled out so the numbers cannot be read two ways.
        'linkrot_summary': {
            'total_broken': lr.get('total_broken', 0),
            'unique_broken': lr.get('unique_broken', 0),
            'duplicated_entries': lr.get('duplicated_entries', 0),
            'entries_with_duplicates': lr.get('entries_with_duplicates', 0),
            'affected_sources': lr.get('affected_sources', 0),
            'code_block_exempt': lr.get('code_block_exempt', 0),
            'skill_scheme_exempt': lr.get('skill_scheme_exempt', 0),
            'empty_target_skipped': lr.get('empty_target_skipped', 0),
            'dedupe_key': '(source, target)',
            'duplicated_entries_meaning': 'references merged away = sum(occurrences - 1)',
            'total_broken_meaning': 'raw wikilink references, not deduped work items',
            'unique_broken_meaning': 'distinct (source, target) pairs, NOT distinct target pages — the same missing page linked from two sources counts twice',
            'empty_target_skipped_meaning': 'whitespace-only [[   ]] targets, exempt: a malformed link is not a missing page',
            'identity': 'total_broken == unique_broken + duplicated_entries',
        },
        'multi_source': multi_source or {
            'status': 'skipped', 'note': 'D8-D10 require multiple sources',
        },
        'meta': {
            'thresholds': THRESHOLDS,
        },
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


def _diagnosis_to_array(de, iso, spo, sd, rec, lr, st, full,
                        multi_source=None):
    """Convert raw diagnosis results to a flat, machine-parseable array."""
    items = []
    # D1: Dead Ends
    seen = 0
    for d in de:
        if not full and seen >= 50:
            break
        seen += 1
        items.append({
            'dimension': 'D1',
            'title': f"死胡同: {d.get('slug', '?')}",
            'severity': 'P1',
            'task_type': 'bugfix',
            'auto_queue': True,
            'recommendation': f"in_degree={d.get('in_degree',0)} pr={d.get('pr',0):.5f} — "
                             f"添加出站链接，候选: {d.get('top_candidates', [])}",
            'evidence': d,
        })
    # D2: Isolated
    seen = 0
    for x in iso:
        if not full and seen >= 50:
            break
        seen += 1
        items.append({
            'dimension': 'D2',
            'title': f"孤立节点: {x.get('slug', '?')}",
            'severity': 'P2',
            'task_type': 'bugfix',
            'auto_queue': True,
            'recommendation': f"out_degree={x.get('out_degree',0)} — 添加入站引用",
            'evidence': x,
        })
    # D3: Sponges
    seen = 0
    for s in spo:
        if not full and seen >= 50:
            break
        seen += 1
        items.append({
            'dimension': 'D3',
            'title': f"海绵节点: {s.get('slug', '?')}",
            'severity': 'P1',
            'task_type': 'refactor',
            'auto_queue': False,
            'recommendation': f"in_ratio={s.get('in_ratio',0)} total_degree={s.get('total_degree',0)} — 检查是否过度吸收链接",
            'evidence': s,
        })
    # D4: Link Rot — flattened per-link
    # Index links (auto-generated directory pages) → P2 informational
    for l in lr.get('details', []):
        source_slug = l.get('source', '?')
        for bt in l.get('broken_targets', [])[:50 if not full else None]:
            is_index = bt.get('is_index_link', False)
            items.append({
                'dimension': 'D4',
                'title': f"链接腐烂: {source_slug} → {bt.get('target', '?')}",
                'severity': 'P2' if is_index else 'P0',
                'task_type': 'bugfix',
                'auto_queue': True,
                'recommendation': (
                    f"[index link] 目录页面自动链接，可忽略"
                    if is_index
                    else f"创建目标页面或移除死链接"
                ),
                'evidence': {
                    'source': source_slug, 'target': bt.get('target', ''),
                    'context': bt.get('context', ''),
                    'is_index_link': is_index,
                    # issue #8: occurrences was computed then dropped here, so a target
                    # referenced N times looked identical to one referenced once.
                    'occurrences': bt.get('occurrences', 1),
                    # issue #9: pre-fold location, so a folded slug stays traceable.
                    'source_path': l.get('source_path') or source_slug,
                },
            })

    # D4b: wikilinks exempted inside code fences — informational disclosure only.
    # The exemption itself is intentional (a code sample is not a reference); what
    # was wrong was dropping them silently with no way to see how many (issue #6).
    if lr.get('code_block_exempt'):
        items.append({
            'dimension': 'D4',
            'title': f"代码围栏/行内代码内链接未检查: {lr.get('code_block_exempt')} 处",
            'severity': 'P2',
            'task_type': 'docs',
            'auto_queue': False,
            'recommendation': (
                '代码围栏内的 [[wikilink]] 按设计豁免（代码示例不等于引用）；'
                '此处仅披露豁免量，如确属真实引用请移出代码块'
            ),
            'evidence': {
                'count': lr.get('code_block_exempt'),
                'samples': lr.get('code_block_samples', []),
            },
        })

    # D4c: skill:/skills: prefixed wikilinks — exempt from link-rot (a scheme reference
    # is not a page), but the exemption must be observable, same standard as D4b
    # (issue #10: these were dropped with no counting at all).
    if lr.get('skill_scheme_exempt'):
        items.append({
            'dimension': 'D4',
            'title': f"skill:/skills: 前缀链接未检查: {lr.get('skill_scheme_exempt')} 处",
            'severity': 'P2',
            'task_type': 'docs',
            'auto_queue': False,
            'recommendation': (
                'skill:/skills: 前缀的 [[wikilink]] 按设计不是页面链接，已豁免；'
                '此处仅披露豁免量，如确属页面引用请改写为页面名'
            ),
            'evidence': {
                'count': lr.get('skill_scheme_exempt'),
                'samples': lr.get('skill_scheme_samples', []),
            },
        })
    # D4d: whitespace-only targets (`[[   ]]`) — a malformed link, not a missing
    # page, so it is exempt from link-rot. Same disclosure standard as D4b/D4c:
    # the exemption must be visible, never a silent drop.
    if lr.get('empty_target_skipped'):
        items.append({
            'dimension': 'D4',
            'title': f"空目标链接未计入死链: {lr.get('empty_target_skipped')} 处",
            'severity': 'P2',
            'task_type': 'docs',
            'auto_queue': False,
            'recommendation': (
                '空或全空白的 [[ ]] 目标是写残的链接，不是指向缺失页面，已按豁免处理；'
                '此处仅披露豁免量，请补全目标或删除该链接'
            ),
            'evidence': {
                'count': lr.get('empty_target_skipped'),
                'samples': lr.get('empty_target_samples', []),
            },
        })
    # D5: Seed Diffusion
    for s_item in sd:
        if s_item.get('warning'):
            items.append({
                'dimension': 'D5',
                'title': f"扩散不足: {s_item.get('slug', '?')}",
                'severity': 'P2',
                'task_type': 'docs',
                'auto_queue': False,
                'recommendation': f"覆盖率={s_item.get('coverage',0):.1%} — 增加下游引用",
                'evidence': s_item,
            })
    # D6: Reciprocity
    items.append({
        'dimension': 'D6',
        'title': f"互惠评估",
        'severity': 'P2',
        'task_type': 'docs',
        'auto_queue': False,
        'recommendation': f"ratio={rec.get('ratio',0):.1%} status={rec.get('status','?')} — "
                         f"互惠对={rec.get('reciprocal_pairs',0)}",
        'evidence': rec,
    })
    # D7: Staleness
    seen = 0
    for s_item in st:
        if not full and seen >= 50:
            break
        seen += 1
        items.append({
            'dimension': 'D7',
            'title': f"概念陈旧: {s_item.get('slug', '?')}",
            'severity': 'P2',
            'task_type': 'docs',
            'auto_queue': False,
            'recommendation': f"age={s_item.get('age_days',0)}d in_degree={s_item.get('in_degree',0)} — 更新或归档",
            'evidence': s_item,
        })
    if multi_source and multi_source.get('status') == 'computed':
        d8 = multi_source['D8']
        if not d8.get('pass'):
            items.append({
                'dimension': 'D8',
                'title': '跨源覆盖度不足',
                'severity': 'P1',
                'task_type': 'enhancement',
                'auto_queue': True,
                'recommendation': (
                    f"wiki coverage={d8.get('wiki_coverage', 0):.1%} — "
                    "为未映射 wiki 节点补充 ontology 实体或稳定 ID"
                ),
                'evidence': d8,
            })
        d9 = multi_source['D9']
        for mismatch in d9.get('mismatches', [])[:None if full else 20]:
            items.append({
                'dimension': 'D9',
                'title': f"类型不一致: {mismatch.get('node', '?')}",
                'severity': 'P1',
                'task_type': 'refactor',
                'auto_queue': True,
                'recommendation': (
                    f"ontology type={mismatch.get('ontology_type')} vs "
                    f"wiki theorist={mismatch.get('wiki_theorist')} — 对齐类型元数据"
                ),
                'evidence': mismatch,
            })
        d10 = multi_source['D10']
        if d10.get('information_gain', 0) < 0.1:
            items.append({
                'dimension': 'D10',
                'title': '多源信息增益不足',
                'severity': 'P2',
                'task_type': 'enhancement',
                'auto_queue': False,
                'recommendation': (
                    f"information gain={d10.get('information_gain', 0):.1%} — "
                    "检查跨源实体映射和新增关系"
                ),
                'evidence': d10,
            })
    return items


# ══════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='Knowledge Graph 知识图谱诊断引擎 v1')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--full', action='store_true', help='全量列表（配合 --json）')
    parser.add_argument('--source', default='wiki',
                        help='wiki, ontology, ragflow, or comma-separated sources')
    parser.add_argument('--ontology-path', default=ONTOLOGY_PATH,
                        help='Ontology JSONL 路径')
    parser.add_argument('--wiki-root', default=WIKI_ROOT,
                        help='Wiki 根目录')
    parser.add_argument('--ragflow-path', default=None,
                        help='Offline ragflow JSONL file or chunk JSON directory')
    parser.add_argument('--writeback', action='store_true',
                        help='诊断结果回写 ontology (仅 source=ontology)')
    parser.add_argument('--writeback-dryrun', action='store_true',
                        help='--writeback 预览模式，不实际写入')
    parser.add_argument('--writeback-clean', action='store_true',
                        help='清理 30 天前的陈旧 Action 实体（在 append 前执行）')
    parser.add_argument('--version', action='store_true', help='输出版本号')
    parser.add_argument('--schema', action='store_true',
                        help='WIP: 输出尚未与实际响应对齐的 JSON Schema 草案')
    args = parser.parse_args()

    # --version: output version and exit
    if args.version:
        print(f"graph-diagnose v{__version__}")
        return

    # --schema: output JSON Schema
    if args.schema:
        print("WARNING: --schema is WIP and does not yet validate normalized output",
              file=sys.stderr)
        print(json.dumps(SCHEMA_V3, indent=2, ensure_ascii=False))
        return

    sources = normalize_sources(args.source)
    normalized_source = ','.join(sources)
    graph_source = sources if len(sources) > 1 else sources[0]
    G, meta = build_graph(
        source=graph_source, ontology_path=args.ontology_path,
        wiki_root=args.wiki_root, ragflow_path=args.ragflow_path,
    )
    pr = nx.pagerank(G, alpha=0.85) if G.number_of_nodes() else {}

    de = diagnose_deadends(G, meta, pr)
    iso = diagnose_isolated(G, meta, pr)
    spo = diagnose_sponges(G, meta, pr)
    sd = diagnose_seed_diffusion(G, meta, pr)
    rec = diagnose_reciprocity(G, meta, pr)
    # H3: skip Markdown link-rot for ontology source (different data model)
    if 'wiki' in sources:
        lr = diagnose_linkrot(G, meta, wiki_root=args.wiki_root)
    else:
        lr = {'total_broken': 0, 'affected_sources': 0, 'details': []}
    st = diagnose_staleness(G, meta, pr)
    multi_source = diagnose_multi_source(G, sources)


    # Writeback: append diagnostic Action entities to ontology JSONL
    if args.writeback and sources == ['ontology'] and args.ontology_path:
        from datetime import datetime, timezone, timedelta
        ontology_write_path = _validated_writeback_path(args.ontology_path)
        ts = datetime.now(timezone.utc).isoformat()
        ts_short = datetime.now(timezone.utc).strftime('%Y%m%d%H%M')

        # Clean stale Action entities (30-day window)
        if args.writeback_clean and os.path.exists(ontology_write_path):
            cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            with open(ontology_write_path, 'r', encoding='utf-8') as source:
                lines = source.read().splitlines()
            kept, cleaned = [], 0
            for line in lines:
                try:
                    rec = json.loads(line)
                    if rec.get('entity', {}).get('type') == 'Action':
                        props = rec.get('entity', {}).get('properties', {})
                        if props.get('type') == 'graph_repair':
                            try:
                                at = datetime.fromisoformat(props.get('timestamp', ''))
                                if at < cutoff:
                                    cleaned += 1
                                    continue
                            except (ValueError, TypeError):
                                pass  # no timestamp → keep
                except json.JSONDecodeError:
                    pass
                kept.append(line)
            if cleaned:
                clean_content = ''.join(f"{line}\n" for line in kept)
                atomic_write(ontology_write_path, clean_content)
                print(f"[writeback-clean] removed {cleaned} stale Action entities (>30d), kept {len(kept)}")

        actions = []
        # D1: Dead ends → Action
        for d in de[:50]:
            actions.append(json.dumps({"op":"create","entity":{
                "id":f"act_diag_de_{d['slug']}_{ts_short}","type":"Action",
                "properties":{"type":"graph_repair","target":d['slug'],
                    "outcome":f"dead_end: in_degree={d['in_degree']} pr={d['pr']:.5f}",
                    "timestamp":ts}}}))
        # D2: Isolated → Action
        for x in iso[:50]:
            actions.append(json.dumps({"op":"create","entity":{
                "id":f"act_diag_iso_{x['slug']}_{ts_short}","type":"Action",
                "properties":{"type":"graph_repair","target":x['slug'],
                    "outcome":f"isolated: out_degree={x['out_degree']}",
                    "timestamp":ts}}}))
        # D3: Sponges → Action
        for s in spo[:50]:
            actions.append(json.dumps({"op":"create","entity":{
                "id":f"act_diag_spg_{s['slug']}_{ts_short}","type":"Action",
                "properties":{"type":"graph_repair","target":s['slug'],
                    "outcome":f"sponge: in_ratio={s['in_ratio']} total={s['total_degree']}",
                    "timestamp":ts}}}))
        # D4: Link Rot → Action (v2.8.0 新增)
        for l in lr.get('details', [])[:50]:
            src = l.get('source', '?'); tgt = l.get('target', '?')
            actions.append(json.dumps({"op":"create","entity":{
                "id":f"act_diag_lr_{src}_{tgt}_{ts_short}".replace('/','_').replace('\\','_'),
                "type":"Action",
                "properties":{"type":"graph_repair","target":src,
                    "outcome":f"link_rot: → {tgt} not found (file={l.get('source_file','?')})",
                    "timestamp":ts}}}))
        # D5: Seed Diffusion (覆盖率 < 30%) → Action
        for s_item in sd:
            if s_item.get('warning') and s_item.get('coverage', 1.0) < 0.3:
                actions.append(json.dumps({"op":"create","entity":{
                    "id":f"act_diag_sd_{s_item['seed']}_{ts_short}","type":"Action",
                    "properties":{"type":"graph_repair","target":s_item['seed'],
                        "outcome":f"seed_diffusion: coverage={s_item['coverage']:.1%} from {s_item['seed']}",
                        "timestamp":ts}}}))

        if args.writeback_dryrun:
            print(f"\n[writeback dryrun] would append {len(actions)} Action entities, sample:")
            for a in actions[:3]:
                print(f"  {a[:120]}...")
        else:
            for action in actions:
                atomic_append_jsonl(ontology_write_path, json.loads(action))
            print(f"\n[writeback] appended {len(actions)} diagnostic Action entities (D1+D2+D3+D4+D5) to {ontology_write_path}")

    if args.json:
        provenance = _make_provenance(
            normalized_source, args.wiki_root, args.ontology_path,
            args.ragflow_path,
        ) if args.json else None
        print(json_output(
            G, de, iso, spo, sd, rec, lr, st, full=args.full,
            source=normalized_source, provenance=provenance,
            multi_source=multi_source,
        ))
    else:
        print(text_report(G, de, iso, spo, sd, rec, lr, st, multi_source))


if __name__ == '__main__':
    try:
        main()
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
