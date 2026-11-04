#!/usr/bin/env python3
"""Wiki 图谱综合测试套件
11+ 项测试，每项 0-10 分。多源模式追加 D11/D12 评分。
"""

import contextlib
import os, re, time, json, argparse, sys
from collections import defaultdict

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import networkx as nx
from community import community_louvain
from scipy.stats import spearmanr
from _graph_utils import load_graph, normalize_sources
from scorecard import build_scorecard
# 验收阈值：100 分制下的 60 分（即 60%，评级 B 起点）
ACCEPTANCE_THRESHOLD = 60

WIKI = os.environ.get('WIKI_ROOT', os.getcwd())
SOURCE = 'wiki'
ONTOLOGY_PATH = os.environ.get(
    'ONTOLOGY_PATH',
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'data', 'ontology.jsonl',
    ),
)

parser = argparse.ArgumentParser(description='Knowledge Graph 图谱综合测试套件')
parser.add_argument('--json', action='store_true', help='输出 JSON 格式结果')
parser.add_argument('--scorecard', action='store_true',
                    help='输出 B3 结构化质量记分卡 JSON')
parser.add_argument('--quiet', action='store_true',
                    help='CI-friendly: 仅输出 scorecard JSON 到 stdout，无表格/标题')
parser.add_argument('--diagnosis-json',
                    help="已有 graph-diagnose JSON 文件；'-' 表示从 stdin 读取")
parser.add_argument('--source', default='wiki',
                    help='wiki, ontology, ragflow, or comma-separated sources')
parser.add_argument('--ontology-path', default=ONTOLOGY_PATH,
                    help='Ontology JSONL 路径')
parser.add_argument('--wiki-root', default=WIKI,
                    help='Wiki 根目录')
parser.add_argument('--ragflow-path', default=None,
                    help='Offline ragflow JSONL file or chunk JSON directory')
parser.add_argument('--no-write', '--read-only', dest='read_only',
                    action='store_true',
                    help='Suppress optional analysis file output')
parser.add_argument('--export-canary-data', metavar='FILE',
                    help='Write computed canary metrics to FILE as JSON')
parser.add_argument(
    '--debug', choices=['D8', 'D9', 'D10', 'D11', 'fusion', 'mappings'],
    help='输出单个融合维度或映射的详细调试信息',
)
args = None
SOURCE_NAMES = []
SCORECARD_MERGED_OUTPUT = False
diagnosis_data = None
G = None
_meta = None
UG = None
partition = None
pr = None
FUSION_VALUES = None
ONTOLOGY_ALIGNMENT = None
RAGFLOW_COVERAGE = None


def _streams_share_destination(first, second):
    """Return whether two file objects point at the same pipe or file."""
    try:
        first_stat = os.fstat(first.fileno())
        second_stat = os.fstat(second.fileno())
    except (AttributeError, OSError):
        return False
    return (
        first_stat.st_dev == second_stat.st_dev
        and first_stat.st_ino == second_stat.st_ino
    )


def _load_diagnosis(path):
    """Load an existing diagnosis without invoking graph-diagnose again."""
    if not path:
        return None
    try:
        if path == '-':
            return json.load(sys.stdin)
        with open(path, 'r', encoding='utf-8') as diagnosis_file:
            return json.load(diagnosis_file)
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(f"invalid --diagnosis-json: {exc}")


def _configure_runtime(argv=None):
    """Parse CLI arguments without doing any graph work at import time."""
    global args, SOURCE_NAMES, SOURCE
    global SCORECARD_MERGED_OUTPUT, diagnosis_data
    global G, _meta, UG, partition, pr
    global FUSION_VALUES, ONTOLOGY_ALIGNMENT, RAGFLOW_COVERAGE

    args = parser.parse_args(argv)
    if args.quiet:
        args.scorecard = True
    SOURCE_NAMES = normalize_sources(args.source)
    SOURCE = ','.join(SOURCE_NAMES)
    SCORECARD_MERGED_OUTPUT = (
        args.scorecard and _streams_share_destination(sys.stdout, sys.stderr)
    )
    diagnosis_data = (
        _load_diagnosis(args.diagnosis_json) if args.scorecard else None
    )
    G = _meta = UG = partition = pr = None
    FUSION_VALUES = ONTOLOGY_ALIGNMENT = RAGFLOW_COVERAGE = None


def _best_partition(graph):
    """Preserve the suite's python-louvain behavior for non-trivial graphs."""
    nodes = list(graph.nodes())
    if not nodes:
        return {}
    if graph.number_of_edges() == 0:
        return {node: index for index, node in enumerate(nodes)}
    # v3.9.0: fixed seed for reproducibility
    return community_louvain.best_partition(graph, random_state=42)


def _initialize_graph():
    """Build graph-derived state on first runtime use, never on import."""
    global G, _meta, UG, partition, pr
    global FUSION_VALUES, ONTOLOGY_ALIGNMENT, RAGFLOW_COVERAGE

    if G is not None:
        return

    def build():
        return load_graph(
            SOURCE_NAMES, args.wiki_root, args.ontology_path,
            args.ragflow_path, read_only=args.read_only,
        )

    if SCORECARD_MERGED_OUTPUT:
        with open(os.devnull, 'w', encoding='utf-8') as discarded_stderr:
            with contextlib.redirect_stderr(discarded_stderr):
                G, _meta = build()
    else:
        G, _meta = build()
    UG = G.to_undirected()
    partition = _best_partition(UG)
    pr = nx.pagerank(G, alpha=0.85) if G.number_of_nodes() else {}
    FUSION_VALUES = (
        _fusion_score_values() if len(SOURCE_NAMES) > 1 else None
    )
    ONTOLOGY_ALIGNMENT = test_ontology_alignment(G, _meta, pr)
    RAGFLOW_COVERAGE = test_ragflow_coverage(G)

# ============================================================
# Test framework
# ============================================================
results = []

def test(name, description, manual_cost, fn):
    """manual_cost: estimated seconds for manual equivalent"""
    start = time.time()
    try:
        score, detail = fn()
        elapsed = time.time() - start
        graph_time = f"{elapsed:.2f}s"
        speedup = f"{manual_cost/elapsed:.0f}x" if elapsed > 0 else "∞"
    except Exception as e:
        score, detail = 0, f"ERROR: {e}"
        graph_time = "FAIL"
        speedup = "N/A"
    results.append({
        'name': name, 'description': description, 'score': score,
        'detail': detail, 'graph_time': graph_time,
        'manual_cost': f"{manual_cost}s", 'speedup': speedup
    })

# ============================================================
# TEST 1: 多跳最短路径 — 两个无直连的概念
# ============================================================
def t1():
    # Find two concepts with no direct edge, at least 3 hops apart
    candidates = []
    for a in G.nodes():
        for b in G.nodes():
            if a >= b: continue
            if G.has_edge(a, b) or G.has_edge(b, a): continue
            try:
                path = nx.shortest_path(G, a, b)
                if len(path) >= 4:  # at least 3 hops
                    candidates.append((len(path), a, b, path))
            except nx.NetworkXNoPath:
                pass
    
    if not candidates:
        return (0, "No 3+ hop pairs found")
    
    candidates.sort(key=lambda x: -x[0])
    hops, a, b, path = candidates[0]
    
    # Manual: would need to explore branching factor at each hop
    avg_degree = sum(G.out_degree(n) for n in G.nodes()) / len(G.nodes())
    manual_branches = avg_degree ** (hops-1)
    
    score = min(10, 3 + hops)  # base 3 + 1 per hop, max 10
    detail = (f"{a} → {b}: {hops-1}跳 ({' → '.join(path)})\n"
              f"  平均出度{avg_degree:.1f}，手动需探索 ~{manual_branches:.0f} 条路径")
    return (score, detail)

# ============================================================
# TEST 2: 中心性三角对比 — PR vs Betweenness vs Closeness
# ============================================================
def t2():
    bc = nx.betweenness_centrality(G)
    cc = nx.closeness_centrality(G)
    
    # Top 5 by each metric
    pr_top = set(sorted(pr, key=pr.get, reverse=True)[:5])
    bc_top = set(sorted(bc, key=bc.get, reverse=True)[:5])
    cc_top = set(sorted(cc, key=cc.get, reverse=True)[:5])
    
    overlap = len(pr_top & bc_top & cc_top)
    union = pr_top | bc_top | cc_top
    
    detail = (f"PR Top5: {', '.join(sorted(pr_top, key=pr.get, reverse=True)[:3])}...\n"
              f"BC Top5: {', '.join(sorted(bc_top, key=bc.get, reverse=True)[:3])}...\n"
              f"CC Top5: {', '.join(sorted(cc_top, key=cc.get, reverse=True)[:3])}...\n"
              f"三榜交集: {overlap}/5, 总不同概念: {len(union)}/15")
    
    # overlap = number of concepts in all 3 lists. Ideal: 1-2 (some overlap, not identical)
    # 0 overlap = different dimensions (ok), 5 overlap = identical (bad)
    if overlap == 0:
        score = 8  # different centrality measures capture different dimensions — healthy
        detail += "\n  → 三种中心性测量不同维度——图谱有真正的结构深度"
    elif overlap >= 4:
        score = 3  # too much overlap — graph is too flat
        detail += "\n  → 过度重叠——图谱可能过于扁平"
    else:
        score = 10  # golden middle
        detail += "\n  → 适度重叠——结构既有深度又有共识"

    return (score, detail)

# ============================================================
# TEST 3: 社区-理论家对齐率
# ============================================================
def t3():
    # For each community, find dominant theorist
    comm_theorists = defaultdict(lambda: defaultdict(int))
    for node in G.nodes():
        cid = partition[node]
        t = G.nodes[node].get('theorist', 'unknown')
        comm_theorists[cid][t] += 1
    
    # Dominance = max_theorist_count / total_community_size
    dominance_scores = []
    for cid, theorists in comm_theorists.items():
        total = sum(theorists.values())
        top_count = max(theorists.values())
        dominance_scores.append(top_count / total)
    
    # v3.9.0: weighted average by community size (single-node communities no longer distort)
    community_sizes = [sum(theorists.values()) for theorists in comm_theorists.values()]
    total_nodes = sum(community_sizes)
    avg_purity = sum(p * s for p, s in zip(dominance_scores, community_sizes)) / total_nodes if total_nodes else 0
    
    detail = (f"{len(comm_theorists)} 个社区，加权平均理论家纯度: {avg_purity:.1%} (weighted by community size)\n"
              f"  纯度分布: " + ", ".join(f"C{cid}={d:.0%}" for cid, d in 
              sorted(zip(comm_theorists.keys(), dominance_scores), key=lambda x: -x[1])[:5]))
    
    # Post-bridging: LOWER purity = MORE cross-theorist integration = BETTER
    # <40% = excellent integration, 40-60% = good, >80% = isolated silos
    if avg_purity < 0.40:
        score = 10
        detail += "\n  → 优秀的跨理论家整合——社区不是理论家回音室"
    elif avg_purity < 0.60:
        score = 8
        detail += "\n  → 良好的跨理论家混合"
    elif avg_purity < 0.80:
        score = 6
    else:
        score = 4
    return (score, detail)

# ============================================================
# TEST 4: 结构洞 — 删除后网络碎裂最严重的节点
# ============================================================
def t4():
    # For fully connected graph: find articulation points (nodes whose removal disconnects the graph)
    # If no articulation points: measure component size reduction
    before = nx.number_weakly_connected_components(G)
    if before > 1:
        # Not fully connected — use old fragmentation logic
        top_nodes = sorted(G.degree(), key=lambda x: -x[1])[:20]
        max_frag = 0
        worst_node = None
        for node, _ in top_nodes:
            H = G.copy()
            H.remove_node(node)
            after = nx.number_weakly_connected_components(H)
            frag = after - before
            if frag > max_frag:
                max_frag = frag
                worst_node = node
    else:
        # Fully connected — find nodes whose removal creates most new components
        top_nodes = sorted(G.degree(), key=lambda x: -x[1])[:30]
        max_frag = 0
        worst_node = None
        for node, _ in top_nodes:
            H = G.copy()
            H.remove_node(node)
            after = nx.number_weakly_connected_components(H)
            frag = after - before
            if frag > max_frag:
                max_frag = frag
                worst_node = node
    
    if worst_node is None:
        return (5, "网络过于强健——删除任何节点都无法产生新分量")
    
    in_d = G.in_degree(worst_node)
    out_d = G.out_degree(worst_node)
    
    detail = (f"删除 [{worst_node}] → 网络从 {before} → {before + max_frag} 个连通分量 (+{max_frag})\\n"
              f"  入度={in_d}, 出度={out_d}, PR={pr.get(worst_node,0):.4f}")
    
    # Scoring: any fragmentation = 8+, significant = 10
    if max_frag >= 3:
        score = 10
    elif max_frag >= 1:
        score = 8
    else:
        score = 6  # fully connected, no single point of failure — resilient but less informative
        detail += "\\n  → 网络无单点故障——强健但结构洞检测不适用"
    
    return (score, detail)

# ============================================================
# TEST 5: 互惠链接 — 双向引用的概念对
# ============================================================
def t5():
    reciprocal = []
    for a, b in G.edges():
        if G.has_edge(b, a) and a < b:  # a<b to avoid duplicates
            reciprocal.append((a, b))
    
    total_edges = G.number_of_edges()
    reciprocal_ratio = len(reciprocal) * 2 / total_edges if total_edges else 0
    
    detail = (f"{len(reciprocal)} 对互惠链接 / {total_edges} 条边 = {reciprocal_ratio:.1%}\n"
              f"  示例: " + ", ".join(f"{a}↔{b}" for a,b in reciprocal[:5]))
    
    # Reciprocal links indicate strong conceptual coupling.
    reciprocal_pct = reciprocal_ratio * 100
    # v3.9.0: raised upper bound from 25% → 35% (knowledge graphs have natural high reciprocity)
    if 10 <= reciprocal_pct <= 35:
        score = 10
    elif (5 <= reciprocal_pct < 10) or (35 < reciprocal_pct <= 50):
        score = 8
    elif reciprocal_pct > 50:
        score = 6
        detail += "\n  [WARN] 互惠比 >50%，可能存在回音室风险"
    else:
        score = 6
    return (score, detail)

# ============================================================
# TEST 6: 死胡同 — 出度=0 的概念（只收不发）
# ============================================================
def t6():
    dead_ends = [(n, G.in_degree(n)) for n in G.nodes() if G.out_degree(n) == 0 and G.in_degree(n) > 0 and not n.startswith('audit-logs/')]
    
    detail = (f"{len(dead_ends)} 个死胡同（出度=0，链接到别人但自己不链出）\n")
    if dead_ends:
        dead_ends.sort(key=lambda x: -x[1])
        detail += f"  最高入度死胡同: [{' ,'.join(f'{n}(in={d})' for n,d in dead_ends[:5])}]"
    
    # Ideal: < 5% dead ends. 222 nodes → < 11
    score = 10 if len(dead_ends) <= 11 else max(1, 10 - len(dead_ends)//3)
    return (score, detail)

# ============================================================
# TEST 7: 图直径 — 最长最短路径
# ============================================================
def t7():
    try:
        diameter = nx.diameter(UG)
    except nx.NetworkXError:
        # Graph not strongly connected — find diameter of largest component
        largest_cc = max(nx.connected_components(UG), key=len)
        subg = UG.subgraph(largest_cc)
        diameter = nx.diameter(subg)
    
    detail = (f"图直径: {diameter} 跳\n"
              f"  → 任意两个连通概念间最多 {diameter} 步可达")
    
    # Small diameter in a dense knowledge graph is good (3-8 ideal)
    score = 10 if 4 <= diameter <= 8 else (8 if diameter <= 3 else max(3, 10 - diameter))
    return (score, detail)

# ============================================================
# TEST 8: 种子扩散 — 从一个概念可触达的范围
# ============================================================
def t8():
    # Pick a seed with moderate out-degree but high PR
    seeds = sorted(pr, key=pr.get, reverse=True)[:10]
    best_reach = 0
    best_seed = None
    
    for seed in seeds:
        reachable = set()
        stack = [seed]
        while stack:
            n = stack.pop()
            if n in reachable: continue
            reachable.add(n)
            stack.extend(list(G.successors(n)))
        if len(reachable) > best_reach:
            best_reach = len(reachable)
            best_seed = seed
    
    coverage = best_reach / len(G.nodes())
    
    detail = (f"从 [{best_seed}] 出发可触达 {best_reach}/{len(G.nodes())} 个概念 ({coverage:.0%})\n"
              f"  PR={pr.get(best_seed,0):.4f}, 出度={G.out_degree(best_seed)}")
    
    score = min(10, int(coverage * 15))  # 66% = 10 points
    return (score, detail)

# ============================================================
# TEST 9: 自引用检测 — 概念是否引用了自己？
# ============================================================
def t9():
    self_refs = [n for n in G.nodes() if G.has_edge(n, n)]
    
    detail = (f"自引用概念: {len(self_refs)} 个\n")
    if self_refs:
        detail += f"  示例: {self_refs[:5]}"
    
    score = 10 if len(self_refs) == 0 else max(1, 10 - len(self_refs))
    return (score, detail)

# ============================================================
# TEST 10: 加权入度 vs PageRank 相关性
# ============================================================
def t10():
    """Measure true Spearman rank correlation between PageRank and in-degree.

    ``scipy.stats.spearmanr`` assigns average ranks to tied values.
    """
    in_deg = {n: G.in_degree(n) for n in G.nodes()}

    nodes = list(G.nodes())
    pr_values = [pr[node] for node in nodes]
    in_degree_values = [in_deg[node] for node in nodes]
    if len(nodes) < 2 or len(set(pr_values)) < 2 \
            or len(set(in_degree_values)) < 2:
        correlation = 0.0
    else:
        correlation, _pvalue = spearmanr(pr_values, in_degree_values)
        correlation = float(correlation)

    detail = (f"PR-入度 Spearman 排序相关性: {correlation:.2%}\n"
              f"  → {'PR 和入度高度一致（图谱结构扁平）' if correlation > 0.7 else 'PR 与入度显著不同（图谱有深层结构）' if correlation < 0.5 else 'PR 与入度中度相关'}")
    
    # Neither extreme is bad — moderate correlation shows structure exists
    score = 10 if 0.3 < correlation < 0.7 else (8 if correlation > 0 else 5)
    return (score, detail)


# ============================================================
# TEST 11: 跨理论家链接密度
# ============================================================

# ============================================================
# TEST 12: 实体类型多样性 (Ontology only)
# ============================================================
def t12():
    import math
    types = {}
    for n in G.nodes():
        t = G.nodes[n].get('type', 'Unknown')
        types[t] = types.get(t, 0) + 1
    total = sum(types.values())
    # Shannon entropy normalized to 0-10
    entropy = 0.0
    for count in types.values():
        if count > 0:
            p = count / total
            entropy -= p * math.log(p)
    max_entropy = math.log(len(types)) if len(types) > 1 else 1.0
    norm_entropy = entropy / max_entropy if max_entropy > 0 else 0
    score = int(norm_entropy * 10)
    detail = (f"Entity types: {types}\n"
              f"  Shannon entropy: {entropy:.3f} (norm={norm_entropy:.2f}) → score={score}/10\n"
              f"  High entropy = diverse entity types (healthy)")
    if norm_entropy > 0.7: detail += "\n  → Rich entity type diversity"
    elif norm_entropy > 0.4: detail += "\n  → Moderate type diversity"
    else: detail += "\n  → Limited type diversity — consider adding more entity types"
    return (score, detail)

def t11():
    # Count edges where source_theorist ≠ target_theorist
    cross_edges = 0
    same_edges = 0
    for src, dst in G.edges():
        st = G.nodes[src].get('theorist', '?')
        dt = G.nodes[dst].get('theorist', '?')
        if st != dt and st != '?' and dt != '?':
            cross_edges += 1
        else:
            same_edges += 1
    
    total = cross_edges + same_edges
    cross_ratio = cross_edges / total if total else 0
    
    # Also: unique theorist pairs connected
    theorist_pairs = set()
    all_theorists = set(G.nodes[n].get('theorist', '?') for n in G.nodes()) - {'?'}
    for src, dst in G.edges():
        st = G.nodes[src].get('theorist', '?')
        dt = G.nodes[dst].get('theorist', '?')
        if st != dt and st != '?' and dt != '?':
            theorist_pairs.add(tuple(sorted([st, dt])))
    
    max_pairs = len(all_theorists) * (len(all_theorists) - 1) // 2
    pair_coverage = len(theorist_pairs) / max_pairs if max_pairs else 0
    
    detail = (f"跨理论家边: {cross_edges}/{total} ({cross_ratio:.0%})\n"
              f"  理论家对: {len(theorist_pairs)}/{max_pairs} ({pair_coverage:.0%}) 已连接\n"
              f"  理论家: {sorted(all_theorists)}")
    
    # >30% cross edges is excellent integration
    if cross_ratio > 0.30:
        score = 10
        detail += "\n  → 优秀的跨理论家整合"
    elif cross_ratio > 0.20:
        score = 8
    elif cross_ratio > 0.10:
        score = 6
    else:
        score = 4
    
    return (score, detail)


# ============================================================
# D8-D10: Multi-source fusion scores
# ============================================================
def _ontology_theorist_map():
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


def _d9_values(node, attrs, declared_theorists):
    ontology_type = str(attrs.get('type', '')).strip().casefold()
    wiki_theorist = str(attrs.get('theorist', '')).strip().casefold()
    ontology_id = attrs.get('ontology_id', node)
    declared_theorist = declared_theorists.get(ontology_id, '')
    if not declared_theorist:
        # No declared theorist → skip; ontology type vs wiki theorist
        # are different semantic spaces (classification ≠ attribution)
        matched = None
    else:
        # Substring match: e.g. "gruenfeld" ⊂ "gruenfeld-deborah"
        matched = (declared_theorist in wiki_theorist
                   or wiki_theorist in declared_theorist)
    return ontology_type, wiki_theorist, declared_theorist, matched


def _fusion_score_values():
    source_nodes = G.graph.get('source_nodes', {})
    wiki_nodes = source_nodes.get('wiki', [])
    mappings = G.graph.get('entity_mappings', {}).get('wiki', {})

    N_TOP = int(os.environ.get('D8_TOP_N', '100'))

    source_graphs = G.graph.get('_source_graphs', {})
    wiki_graph = source_graphs.get('wiki')
    top_nodes = list(wiki_nodes)
    mapped_in_top = sum(1 for node in top_nodes if node in mappings)

    if wiki_graph is None or len(wiki_nodes) < 2:
        coverage = mapped_in_top / len(wiki_nodes) \
            if wiki_nodes else 0.0
        _coverage_detail = f"D8 (full): {int(coverage * len(wiki_nodes))}/{len(wiki_nodes)} = {coverage:.1%}" if wiki_nodes else "D8: no wiki nodes"
    else:
        pr = nx.pagerank(wiki_graph)
        top_n = sorted(pr.items(), key=lambda x: x[1], reverse=True)[:N_TOP]
        top_nodes = [node for node, _score in top_n]
        denom = min(N_TOP, len(top_nodes))
        mapped_in_top = sum(1 for n in top_nodes if n in mappings)
        coverage = mapped_in_top / denom if denom else 0.0
        _coverage_detail = f"D8 (weighted): {mapped_in_top}/{denom} = {coverage:.1%} (Top-{N_TOP} PR, {len(wiki_nodes)} total wiki)"
        # --json: 进度行完全静默，保证 stdout 及合并后的输出流都是纯 JSON
        if not SCORECARD_MERGED_OUTPUT and not getattr(args, 'json', False):
            print(_coverage_detail, file=sys.stderr)

    unmapped_top = [node for node in top_nodes if node not in mappings]
    if (unmapped_top and not SCORECARD_MERGED_OUTPUT
            and not getattr(args, 'json', False)):
        print(
            f"[D8] {len(unmapped_top)} Top-{N_TOP} wiki nodes without ontology mapping:",
            file=sys.stderr,
        )
        for wiki_id in unmapped_top[:8]:
            print(f"  - {wiki_id}", file=sys.stderr)

    compared = 0
    consistent = 0
    declared_theorists = _ontology_theorist_map()
    for node, attrs in G.nodes(data=True):
        if not {'wiki', 'ontology'}.issubset(set(attrs.get('sources', []))):
            continue
        ontology_type, wiki_theorist, _declared, matched = _d9_values(
            node, attrs, declared_theorists,
        )
        if not ontology_type or not wiki_theorist:
            continue
        if matched is None:
            continue  # no declared theorist — skip, not a mismatch
        compared += 1
        if matched:
            consistent += 1
    consistency = consistent / compared if compared else 0.0

    gain = 0.0
    source_graphs = G.graph.get('_source_graphs', {})
    primary_source = os.environ.get('D10_PRIMARY', 'ontology')
    if primary_source not in source_graphs:
        primary_source = SOURCE_NAMES[0]
    primary_graph = source_graphs.get(primary_source)
    if primary_graph is not None and primary_graph.number_of_nodes() and G.number_of_nodes():
        # v3.9.0: fixed seed for reproducibility
        single_partition = community_louvain.best_partition(
            primary_graph.to_undirected(), random_state=42
        )
        single_groups = defaultdict(set)
        for node, community_id in single_partition.items():
            single_groups[community_id].add(node)
        merged_groups = defaultdict(set)
        for node, community_id in partition.items():
            merged_groups[community_id].add(node)
        primary_mapping = G.graph.get('entity_mappings', {}).get(primary_source, {})
        similarities = []
        for group in single_groups.values():
            transformed = {primary_mapping.get(node, node) for node in group}
            best = 0.0
            for merged_group in merged_groups.values():
                union = transformed | merged_group
                if union:
                    best = max(best, len(transformed & merged_group) / len(union))
            similarities.append(best)
        average_jaccard = sum(similarities) / len(similarities) if similarities else 0.0
        gain = 1.0 - average_jaccard

    return {
        'coverage': coverage,
        'coverage_score': round(coverage * 10),
        'consistency': consistency,
        'consistency_score': round(consistency * 10),
        'gain': gain,
        'gain_score': round(gain * 10),
        'mapped_in_top': mapped_in_top,
    }


def test_ontology_alignment(G, meta, pr):
    """Measure ontology relations independently corroborated by wiki edges."""
    del meta, pr  # Reserved for future weighted alignment variants.
    sources = set(G.graph.get('sources', []))
    if not {'wiki', 'ontology'}.issubset(sources):
        return None

    ontology_graph = G.graph.get('_source_graphs', {}).get('ontology')
    if ontology_graph is None or ontology_graph.number_of_nodes() == 0:
        return None

    total_relations = 0
    verified_relations = 0
    for source, target in ontology_graph.edges():
        if source not in G or target not in G:
            continue
        total_relations += 1
        if not G.has_edge(source, target):
            continue
        edge_sources = G[source][target].get('edge_sources', [])
        if isinstance(edge_sources, str):
            edge_sources = [edge_sources]
        if 'wiki' in edge_sources:
            verified_relations += 1

    alignment_ratio = (
        verified_relations / total_relations if total_relations else 0.0
    )
    return {
        'ontology_alignment': {
            'total_relations': total_relations,
            'verified_relations': verified_relations,
            'alignment_ratio': round(alignment_ratio, 4),
            'score': round(min(10.0, alignment_ratio * 10), 2),
        },
    }


def test_ragflow_coverage(G):
    """追加评分 ragflow_coverage: RAGFlow 实体覆盖融合后的 wiki+ontology 实体。"""
    sources = set(G.graph.get('sources', []))
    ragflow_meta = G.graph.get('_source_meta', {}).get('ragflow', {})
    if ('ragflow' not in sources
            or ragflow_meta.get('ragflow_status') == 'skipped'):
        return None

    fused_entities = set()
    mappings = G.graph.get('entity_mappings', {}).get('wiki', {})
    fused_entities.update(
        str(ontology_id).strip()
        for ontology_id in mappings.values()
        if str(ontology_id).strip()
    )
    fused_entities.update(
        str(ontology_id).strip()
        for ontology_id in G.graph.get('source_nodes', {}).get('ontology', [])
        if str(ontology_id).strip()
    )

    ragflow_chunks = G.graph.get('source_nodes', {}).get('ragflow', [])
    ragflow_entities = set()
    for chunk_id in ragflow_chunks:
        attrs = G.nodes.get(chunk_id, {})
        entities = attrs.get('entities', [])
        if isinstance(entities, str):
            entities = [entities]
        ragflow_entities.update(
            str(entity).strip()
            for entity in entities
            if str(entity).strip()
        )

    if not fused_entities:
        return {
            'ragflow_coverage': {
                'fused_entities': 0,
                'ragflow_chunks': len(ragflow_chunks),
                'ragflow_entities': len(ragflow_entities),
                'matched_entities': 0,
                'coverage_ratio': 0,
                'score': 0,
            },
        }

    matched_entities = ragflow_entities & fused_entities
    coverage_ratio = len(matched_entities) / len(fused_entities)
    return {
        'ragflow_coverage': {
            'fused_entities': len(fused_entities),
            'ragflow_chunks': len(ragflow_chunks),
            'ragflow_entities': len(ragflow_entities),
            'matched_entities': len(matched_entities),
            'coverage_ratio': round(coverage_ratio, 4),
            'score': round(min(10.0, coverage_ratio * 10), 2),
        },
    }


def _export_canary_data(path):
    """Write the computed multi-source metrics consumed by canary-gate."""
    fusion_values = {
        key: FUSION_VALUES.get(key) if FUSION_VALUES is not None else None
        for key in ('coverage_score', 'consistency_score', 'gain_score')
    }
    canary_data = {
        'fusion_values': fusion_values,
        'ontology_alignment': (
            ONTOLOGY_ALIGNMENT['ontology_alignment']
            if ONTOLOGY_ALIGNMENT is not None else None
        ),
    }
    if RAGFLOW_COVERAGE is not None:
        canary_data['ragflow_coverage'] = (
            RAGFLOW_COVERAGE['ragflow_coverage']
        )

    with open(path, 'w', encoding='utf-8') as canary_file:
        json.dump(canary_data, canary_file, ensure_ascii=False, indent=2)
        canary_file.write('\n')


def _debug_d8():
    wiki_graph = G.graph.get('_source_graphs', {}).get('wiki')
    mappings = G.graph.get('entity_mappings', {}).get('wiki', {})
    top_count = int(os.environ.get('D8_TOP_N', '100'))
    if wiki_graph is None:
        print("D8 Debug: wiki source not loaded")
        return
    ranked = sorted(
        nx.pagerank(wiki_graph).items(), key=lambda item: item[1], reverse=True,
    )[:top_count]
    print(f"D8 Debug: Top-{top_count} wiki nodes by PageRank")
    print(f"{'Rank':<6} {'Node':<45} {'PR':<10} {'Mapped':<8} {'Ontology ID':<30}")
    for rank, (node, score) in enumerate(ranked, 1):
        mapped_id = mappings.get(node, '-')
        mapped = 'YES' if mapped_id != '-' else 'NO'
        print(f"{rank:<6} {node:<45} {score:.6f}  {mapped:<8} {mapped_id:<30}")


def _debug_d9():
    declared_theorists = _ontology_theorist_map()
    rows = []
    for node, attrs in G.nodes(data=True):
        if not {'wiki', 'ontology'}.issubset(set(attrs.get('sources', []))):
            continue
        ontology_type, wiki_theorist, declared, matched = _d9_values(
            node, attrs, declared_theorists,
        )
        rows.append((node, ontology_type or '?', declared or '-',
                     wiki_theorist or '?', 'YES' if matched else 'NO'))
    print("D9 Debug: ontology declaration vs wiki theorist")
    print(
        f"{'Node':<40} {'Ontology Type':<16} {'Declared Theorist':<22} "
        f"{'Wiki Theorist':<22} {'Match':<6}"
    )
    for node, ontology_type, declared, wiki_theorist, matched in rows:
        print(
            f"{node:<40} {ontology_type:<16} {declared:<22} "
            f"{wiki_theorist:<22} {matched:<6}"
        )
    matches = sum(1 for row in rows if row[-1] == 'YES')
    print(f"Matched: {matches}/{len(rows)}")


def _debug_d10():
    source_graphs = G.graph.get('_source_graphs', {})
    primary_source = os.environ.get('D10_PRIMARY', 'ontology')
    if primary_source not in source_graphs and SOURCE_NAMES:
        primary_source = SOURCE_NAMES[0]
    primary_graph = source_graphs.get(primary_source)
    if primary_graph is None:
        print("D10 Debug: primary source not loaded")
        return
    # v3.9.0: fixed seed for reproducibility
    primary_partition = community_louvain.best_partition(
        primary_graph.to_undirected(), random_state=42
    ) if primary_graph.number_of_nodes() else {}
    print(f"D10 Debug: primary source = {primary_source}")
    print(
        f"Primary graph: {primary_graph.number_of_nodes()} nodes, "
        f"{primary_graph.number_of_edges()} edges, "
        f"{len(set(primary_partition.values()))} communities"
    )
    print(
        f"Merged graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges, "
        f"{len(set(partition.values()))} communities"
    )
    if FUSION_VALUES is not None:
        print(
            f"Information gain: {FUSION_VALUES['gain']:.1%} "
            f"({FUSION_VALUES['gain_score']}/10)"
        )


def _debug_d11():
    ontology_graph = G.graph.get('_source_graphs', {}).get('ontology')
    if ontology_graph is None or ONTOLOGY_ALIGNMENT is None:
        print("D11 Debug: N/A (wiki + ontology sources required)")
        return
    print("D11 Debug: ontology relations verified by wiki edges")
    print(f"{'Source':<32} {'Target':<32} {'Relation':<18} {'Verified':<8}")
    for source, target, attrs in ontology_graph.edges(data=True):
        merged_edge = G.get_edge_data(source, target, {})
        edge_sources = merged_edge.get('edge_sources', [])
        if isinstance(edge_sources, str):
            edge_sources = [edge_sources]
        verified = 'YES' if 'wiki' in edge_sources else 'NO'
        print(
            f"{source:<32} {target:<32} "
            f"{str(attrs.get('relation', '?')):<18} {verified:<8}"
        )
    alignment = ONTOLOGY_ALIGNMENT['ontology_alignment']
    print(
        f"Verified: {alignment['verified_relations']}/"
        f"{alignment['total_relations']} "
        f"({alignment['alignment_ratio']:.1%}, {alignment['score']}/10)"
    )


def _debug_mappings():
    mappings = G.graph.get('entity_mappings', {}).get('wiki', {})
    print(f"Total mapped: {len(mappings)}")
    print(f"{'Wiki Node':<45} {'Ontology ID':<30}")
    for wiki_id, ontology_id in sorted(mappings.items()):
        print(f"{wiki_id:<45} {ontology_id:<30}")


def _run_debug():
    handlers = {
        'D8': _debug_d8,
        'D9': _debug_d9,
        'D10': _debug_d10,
        'D11': _debug_d11,
        'mappings': _debug_mappings,
    }
    if args.debug == 'fusion':
        print(json.dumps(FUSION_VALUES, ensure_ascii=False, indent=2))
    else:
        handlers[args.debug]()


def t13_coverage():
    return (
        FUSION_VALUES['coverage_score'],
        f"wiki cross-source coverage: {FUSION_VALUES['coverage']:.1%}",
    )


def t14_consistency():
    return (
        FUSION_VALUES['consistency_score'],
        f"ontology type vs wiki theorist consistency: {FUSION_VALUES['consistency']:.1%}",
    )


def t15_gain():
    return (
        FUSION_VALUES['gain_score'],
        f"merged vs single-source community information gain: {FUSION_VALUES['gain']:.1%}",
    )


def t16_ontology_alignment():
    alignment = ONTOLOGY_ALIGNMENT['ontology_alignment']
    return (
        alignment['score'],
        f"ontology relations verified by wiki edges: "
        f"{alignment['verified_relations']}/{alignment['total_relations']} "
        f"({alignment['alignment_ratio']:.1%})",
    )


def t17_ragflow_coverage():
    coverage = RAGFLOW_COVERAGE['ragflow_coverage']
    return (
        coverage['score'],
        f"RAGFlow entities matched to fused entities: "
        f"{coverage['matched_entities']}/{coverage['fused_entities']} "
        f"({coverage['coverage_ratio']:.1%}) across "
        f"{coverage['ragflow_chunks']} chunks",
    )


# ============================================================
# Run all tests
# ============================================================
def main(argv=None):
    _configure_runtime(argv)
    _initialize_graph()
    results.clear()

    if args.export_canary_data:
        try:
            _export_canary_data(args.export_canary_data)
        except OSError as exc:
            parser.error(f"could not export canary data: {exc}")

    if args.debug:
        _run_debug()
        return 0

    if not args.json and not args.scorecard:
        dimension_count = 11 + (1 if SOURCE_NAMES == ['ontology'] else 0) \
            + (1 if ONTOLOGY_ALIGNMENT is not None else 0) \
            + (1 if RAGFLOW_COVERAGE is not None else 0)
        banner_max = dimension_count * 10 + 10  # +10 bonus 上限
        print("╔══════════════════════════════════════════════════════════╗")
        print("║     Knowledge Graph 图谱综合测试套件                         ║")
        print(f"║     {dimension_count:>2d} 项 × 10 分 + bonus 上限 10 = 满分 {banner_max:<3d}             ║")
        print("╚══════════════════════════════════════════════════════════╝")
        print(f"满分公式: max_score = {dimension_count} 项 × 10 + 10 bonus 上限 = {banner_max}")
        print()

    test("多跳最短路径", "3+跳概念对的最短路径发现", 180, t1)
    test("中心性三角", "PageRank vs Betweenness vs Closeness 对比", 300, t2)
    test("社区-理论家对齐", "Louvain社区 vs 理论家来源一致性", 600, t3)
    test("结构洞检测", "删除后网络碎裂最严重的枢纽节点", 120, t4)
    test("互惠链接", "双向引用概念对检测", 180, t5)
    test("死胡同检测", "出度=0的概念（接收链接但不发出）", 120, t6)
    test("图直径", "最长最短路径 — 网络紧凑度", 60, t7)
    test("种子扩散", "从枢纽概念可触达的概念覆盖率", 240, t8)
    test("自引用检测", "概念是否意外引用了自己", 60, t9)
    test("PR-入度相关性", "PageRank与简单入度的排序相关性", 120, t10)
    test("跨理论家密度", "不同理论家概念间的链接覆盖率", 300, t11)
    if SOURCE_NAMES == ['ontology']:
        test("实体类型多样性", "Ontology实体类型的Shannon熵", 60, t12)
    if ONTOLOGY_ALIGNMENT is not None:
        test("ontology_alignment", "追加评分·ontology_alignment（本体关系对齐）", 180,
             t16_ontology_alignment)
    if RAGFLOW_COVERAGE is not None:
        test("ragflow_coverage", "追加评分·ragflow_coverage（RAGFlow 实体覆盖）", 120,
             t17_ragflow_coverage)

    multi_source_scores = {
        'status': 'computed' if FUSION_VALUES is not None else 'N/A',
        'coverage_score': FUSION_VALUES['coverage_score'] if FUSION_VALUES else None,
        'consistency_score': FUSION_VALUES['consistency_score'] if FUSION_VALUES else None,
        'gain_score': FUSION_VALUES['gain_score'] if FUSION_VALUES else None,
    }
    if ONTOLOGY_ALIGNMENT is not None:
        multi_source_scores['D11'] = ONTOLOGY_ALIGNMENT
    if RAGFLOW_COVERAGE is not None:
        multi_source_scores['D12'] = RAGFLOW_COVERAGE
    if FUSION_VALUES is not None:
        multi_source_scores['d8_detail'] = {
            'top_n': int(os.environ.get('D8_TOP_N', '100')),
            'mapped_in_top': FUSION_VALUES['mapped_in_top'],
            'total_wiki_nodes': len(
                G.graph.get('source_nodes', {}).get('wiki', []),
            ),
        }
    if ONTOLOGY_ALIGNMENT is not None:
        multi_source_scores['d11_detail'] = (
            ONTOLOGY_ALIGNMENT['ontology_alignment']
        )
    if RAGFLOW_COVERAGE is not None:
        multi_source_scores['d12_detail'] = (
            RAGFLOW_COVERAGE['ragflow_coverage']
        )

    # ============================================================
    # Score summary
    # ============================================================
    total = sum(r['score'] for r in results)
    if FUSION_VALUES is not None:
        multi_source_bonus = round(
            (FUSION_VALUES['coverage_score'] + FUSION_VALUES['consistency_score'] + FUSION_VALUES['gain_score']) / 3
        )
        total_with_bonus = total + multi_source_bonus
    else:
        multi_source_bonus = 0
        total_with_bonus = total
    max_score = len(results) * 10 + 10  # +10 bonus 上限: (覆盖率+一致性+信息增益)/3

    # Grade
    pct = total_with_bonus / max_score * 100
    if pct >= 90:   grade = "A+ 图谱计算表现卓越"
    elif pct >= 75: grade = "A  图谱计算表现优秀"
    elif pct >= 60: grade = "B  图谱计算表现良好"
    elif pct >= 40: grade = "C  图谱计算有提升空间"
    else:           grade = "D  需要检查图谱构建逻辑"

    if args.scorecard:
        legacy_scores = {
            'total': total_with_bonus,
            'max': max_score,
            'raw_total': total,
            'multi_source_bonus': multi_source_bonus,
            'percentage': round(total_with_bonus / max_score * 100) if max_score else 0,
            'grade': grade,
            'dimensions': {
                r['name']: r['score'] for r in results
            },
            'multi_source': multi_source_scores,
        }
        scorecard = build_scorecard(
            graph=G,
            diagnosis_data=diagnosis_data,
            source=SOURCE,
            legacy_scores=legacy_scores,
            multi_source_scores=multi_source_scores,
        )
        scorecard['weights'] = {
            'main_dimensions': {result['name']: 1.0 for result in results},
            'multi_source_bonus': {
                'D8_coverage': f'bonus @ 1/3× (coverage={FUSION_VALUES["coverage_score"]}/10)' if FUSION_VALUES else 'N/A',
                'D9_consistency': f'bonus @ 1/3× (consistency={FUSION_VALUES["consistency_score"]}/10)' if FUSION_VALUES else 'N/A',
                'D10_gain': f'bonus @ 1/3× (gain={FUSION_VALUES["gain_score"]}/10)' if FUSION_VALUES else 'N/A',
            },
            'formula': 'total = sum(D1..D12) + round((D8 + D9 + D10) / 3)',
        }
        print(json.dumps(scorecard, ensure_ascii=False, indent=2))
        failing = [result for result in results if result['score'] < 4]
        if failing:
            if not SCORECARD_MERGED_OUTPUT:
                print(
                    f"Failing dimensions: {[result['name'] for result in failing]}",
                    file=sys.stderr,
                )
            return 1
        return 0

    acceptance_passed = pct >= ACCEPTANCE_THRESHOLD

    if args.json:
        summary = {
            'score': total_with_bonus,
            'max_score': max_score,
            'acceptance_threshold': ACCEPTANCE_THRESHOLD,
            'acceptance_passed': acceptance_passed,
            'raw_score': total,
            'multi_source_bonus': multi_source_bonus,
            'percentage': round(total_with_bonus / max_score * 100),
            'grade': grade,
            'nodes': G.number_of_nodes(),
            'edges': G.number_of_edges(),
            'dead_ends': len([n for n in G.nodes() if G.out_degree(n) == 0 and G.in_degree(n) > 0]),
            'isolated': len([n for n in G.nodes() if G.in_degree(n) == 0]),
            'sponges': len([n for n in G.nodes() if G.out_degree(n) > 0 and G.in_degree(n) / (G.in_degree(n) + G.out_degree(n)) >= 0.75]),
            'reciprocal_ratio': round(len([(a,b) for a,b in G.edges() if G.has_edge(b,a) and a < b]) * 2 / G.number_of_edges() * 100) / 100 if G.number_of_edges() else 0,
            'diameter': nx.diameter(UG) if nx.is_connected(UG) else nx.diameter(UG.subgraph(max(nx.connected_components(UG), key=len))),
        }
        output = {
            'summary': summary,
            'results': results,
            'multi_source_scores': multi_source_scores,
            'total': total_with_bonus,
            'max_score': max_score,
            'multi_source_bonus': multi_source_bonus,
            'raw_total': total,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    print()
    print("═" * 70)
    print(f"{'测试':<22} {'得分':<6} {'图计算':<8} {'手动估算':<10} {'加速比':<8}")
    print("═" * 70)
    for r in results:
        flag = "✓" if r['score'] >= 7 else ("△" if r['score'] >= 4 else "✗")
        print(f"{flag} {r['name']:<20} {r['score']:>4g}/10  {r['graph_time']:<8} {r['manual_cost']:<10} {r['speedup']:<8}")
    print("═" * 70)
    print(f"{'总计':<22} {total_with_bonus:>4g}/{max_score}  ({total_with_bonus/max_score:.0%})")
    if FUSION_VALUES and multi_source_bonus:
        print(f"{'  (含多源融合 bonus)':<22} raw={total:.1f} + bonus={multi_source_bonus}")
    print("═" * 70)
    if FUSION_VALUES is None:
        print("D8-D10 multi-source scores: N/A (single-source mode)")
    else:
        print(
            "D8-D10 multi-source: "
            f"coverage={FUSION_VALUES['coverage_score']}/10, "
            f"consistency={FUSION_VALUES['consistency_score']}/10, "
            f"gain={FUSION_VALUES['gain_score']}/10 "
            f"(bonus → +{multi_source_bonus}: "
            f"D8={FUSION_VALUES['coverage_score']} + "
            f"D9={FUSION_VALUES['consistency_score']} + "
            f"D10={FUSION_VALUES['gain_score']} → /3)"
        )

    print(f"\n综合评级: {grade}")
    print(
        f"验收阈值: {ACCEPTANCE_THRESHOLD} 分（{ACCEPTANCE_THRESHOLD}%，评级 B 起点）"
        f"→ {'通过' if acceptance_passed else '未通过'}"
    )
    print()

    # Detailed output for each test
    print("=" * 70)
    print("详细结果")
    print("=" * 70)
    for i, r in enumerate(results, 1):
        print(f"\n── 测试{i}: {r['name']} ({r['score']}/10) {r['description']} ──")
        print(r['detail'])

    return 0


if __name__ == '__main__':
    sys.exit(main())
