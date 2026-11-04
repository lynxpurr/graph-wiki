#!/usr/bin/env python3
"""Wiki 知识图谱计算引擎

构建有向图（支持单源或逗号分隔多源），计算：
- PageRank 中心性
- Louvain 社区检测
- 孤立概念（零入度）
- 桥接节点（连接不同社区）
- 最短路（任意两概念间）

用法:
  python3 graph-compute.py              # 全量计算，输出摘要
  python3 graph-compute.py --full        # 输出完整排名
  python3 graph-compute.py --path A B    # A→B 最短路径
  python3 graph-compute.py --community N # 导出 Louvain 社区
  python3 graph-compute.py --json        # 输出 JSON 到 stdout
"""

import os, re, json, sys, argparse
from collections import defaultdict

import networkx as nx
from community import community_louvain  # python-louvain
from graph_loader import build_graph, normalize_sources
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

WIKI_ROOT = os.environ.get('WIKI_ROOT', os.getcwd())
ONTOLOGY_PATH = os.environ.get('ONTOLOGY_PATH', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', 'memory', 'ontology', 'graph.jsonl'))

def compute_pagerank(G):
    """PageRank — 枢纽概念排名"""
    pr = nx.pagerank(G, alpha=0.85)
    return sorted(pr.items(), key=lambda x: -x[1])


def compute_communities(G):
    """Louvain 社区检测（使用无向版本）"""
    UG = G.to_undirected()
    # v3.9.0: fixed seed for reproducibility
    partition = community_louvain.best_partition(UG, random_state=42)
    communities = defaultdict(list)
    for node, comm_id in partition.items():
        communities[comm_id].append(node)
    return partition, communities


def find_isolates(G):
    """零入度概念 — 有反链的才不算孤立"""
    isolates = []
    for node in G.nodes():
        in_deg = G.in_degree(node)
        out_deg = G.out_degree(node)
        if in_deg == 0:
            isolates.append((node, out_deg, G.nodes[node].get('theorist', '?')))
    return sorted(isolates, key=lambda x: -x[1])


def find_bridges(G, partition):
    """桥接节点 — 出边跨越 ≥2 个不同社区的概念"""
    bridges = []
    for node in G.nodes():
        my_comm = partition.get(node)
        neighbor_comms = set()
        for _, target in G.out_edges(node):
            if target in partition and partition[target] != my_comm:
                neighbor_comms.add(partition[target])
        if len(neighbor_comms) >= 2:
            bridges.append((node, len(neighbor_comms), G.out_degree(node)))
    return sorted(bridges, key=lambda x: -x[1])


def shortest_path(G, source, target):
    """两概念间最短路径"""
    try:
        path = nx.shortest_path(G, source=source, target=target)
        return path
    except nx.NetworkXNoPath:
        return None


def node_source(G, node):
    """返回节点的主来源，与多源图的 ontology 优先规则一致。"""
    attrs = G.nodes[node]
    value = attrs.get('sources') or attrs.get('source') \
        or G.graph.get('sources') or G.graph.get('source')
    if isinstance(value, str):
        sources = [item.strip() for item in value.split(',') if item.strip()]
    elif isinstance(value, (list, tuple, set)):
        sources = [str(item) for item in value if str(item)]
    else:
        sources = []
    if 'ontology' in sources:
        return 'ontology'
    return sources[0] if sources else 'unknown'


def community_node(G, node, community_id, pagerank):
    """构建带稳定查询字段的社区节点记录。"""
    attrs = dict(G.nodes[node])
    return {
        **attrs,
        'id': node,
        'title': attrs.get('title') or str(node),
        'type': attrs.get('type') or 'concept',
        'source': node_source(G, node),
        'community': community_id,
        'pagerank': pagerank.get(node, 0.0),
    }


def community_edge(source, target, data):
    """构建与现有 subgraph JSON 一致的边记录。"""
    return {'from': source, 'to': target, **dict(data)}


def community_selector(value):
    """Parse a community ID or the special selector ``all``."""
    if value == 'all':
        return value
    try:
        return int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "community must be an integer or 'all'"
        ) from exc


def main():
    parser = argparse.ArgumentParser(description='Knowledge Graph 知识图谱计算')
    parser.add_argument('--full', action='store_true', help='输出完整排名')
    parser.add_argument('--json', action='store_true', help='JSON 格式输出')
    parser.add_argument('--path', nargs=2, metavar=('A', 'B'), help='最短路径 A→B')
    parser.add_argument('--source', default='wiki',
                        help='wiki, ontology, ragflow, or comma-separated sources')
    parser.add_argument('--ontology-path', default=ONTOLOGY_PATH,
                        help='Ontology JSONL 路径')
    parser.add_argument('--wiki-root', default=WIKI_ROOT,
                        help='Wiki 根目录')
    parser.add_argument('--ragflow-path', default=None,
                        help='Offline ragflow JSONL file or chunk JSON directory')
    parser.add_argument('--pagerank', action='store_true',
                        help='Compute PageRank (included in default output)')
    parser.add_argument('--communities', action='store_true',
                        help='Compute communities (included in default output)')
    parser.add_argument('--bridges', action='store_true',
                        help='Compute bridge nodes (included in default output)')
    parser.add_argument('--path-from', dest='path_from',
                        help='Shortest-path source (use with --path-to)')
    parser.add_argument('--path-to', dest='path_to',
                        help='Shortest-path target (use with --path-from)')
    parser.add_argument('--subgraph', metavar='NODE',
                        help='Extract a NODE-centered neighborhood')
    parser.add_argument('--radius', type=int, default=2,
                        help='Subgraph radius (default: 2)')
    parser.add_argument('--community', type=community_selector, metavar='N|all',
                        help='Export Louvain community N or the full partition')
    args = parser.parse_args()

    sources = normalize_sources(args.source)
    graph_source = sources if len(sources) > 1 else sources[0]
    G, _meta = build_graph(
        source=graph_source, ontology_path=args.ontology_path,
        wiki_root=args.wiki_root, ragflow_path=args.ragflow_path,
    )

    if bool(args.path_from) != bool(args.path_to):
        parser.error('--path-from and --path-to must be provided together')
    if args.path_from and args.path_to:
        args.path = [args.path_from, args.path_to]
    
    # 最短路径模式
    if args.path:
        path = shortest_path(G, args.path[0], args.path[1])
        if args.json:
            print(json.dumps({
                'path': path or [],
                'length': len(path) - 1 if path else None,
                'source': args.path[0],
                'target': args.path[1],
            }, ensure_ascii=False, indent=2, default=str))
        elif path:
            print(f"{' → '.join(path)}")
        else:
            print(f"无路径: {args.path[0]} → {args.path[1]}")
        return

    if args.subgraph:
        if args.subgraph not in G:
            parser.error(f"subgraph node not found: {args.subgraph}")
        neighborhood = nx.ego_graph(
            G.to_undirected(), args.subgraph, radius=max(0, args.radius),
        )
        subgraph = G.subgraph(neighborhood.nodes()).copy()
        if args.json:
            print(json.dumps({
                'center': args.subgraph,
                'radius': max(0, args.radius),
                'nodes': [
                    {'id': node, **dict(subgraph.nodes[node])}
                    for node in subgraph.nodes()
                ],
                'edges': [
                    {'from': source, 'to': target, **dict(data)}
                    for source, target, data in subgraph.edges(data=True)
                ],
            }, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"Subgraph [{args.subgraph}] radius={max(0, args.radius)}")
            print(f"Nodes: {', '.join(str(node) for node in subgraph.nodes())}")
            for source, target, data in subgraph.edges(data=True):
                print(f"  {source} → {target} ({data.get('relation', 'references')})")
        return

    if args.community is not None:
        partition, communities = compute_communities(G)
        if args.community == 'all':
            if args.json:
                print(json.dumps(
                    partition, ensure_ascii=False, indent=2, default=str,
                ))
            else:
                for node in sorted(partition, key=str):
                    print(f"{node}\t{partition[node]}")
            return

        members = communities.get(args.community)
        if not members:
            available = ', '.join(str(cid) for cid in sorted(communities))
            suffix = f'; available communities: {available}' if available else ''
            parser.error(f'community {args.community} not found{suffix}')

        subgraph = G.subgraph(members).copy()
        pagerank = dict(compute_pagerank(G))
        ranked = sorted(
            members, key=lambda node: (-pagerank.get(node, 0.0), str(node)),
        )
        nodes = [
            community_node(G, node, args.community, pagerank)
            for node in members
        ]
        edges = [
            community_edge(source, target, data)
            for source, target, data in subgraph.edges(data=True)
        ]
        top_nodes = [
            community_node(G, node, args.community, pagerank)
            for node in ranked[:5]
        ]
        stats = {
            'nodes': subgraph.number_of_nodes(),
            'edges': subgraph.number_of_edges(),
            'node_count': subgraph.number_of_nodes(),
            'edge_count': subgraph.number_of_edges(),
        }

        if args.json:
            print(json.dumps({
                'community_id': args.community,
                'nodes': nodes,
                'edges': edges,
                'top_nodes': top_nodes,
                'stats': stats,
            }, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"Community {args.community}")
            print(f"Nodes: {stats['node_count']}  Edges: {stats['edge_count']}")
            print("\nMembers")
            for node in nodes:
                print(
                    f"  [{node['id']}] title={node['title']}  "
                    f"type={node['type']}  source={node['source']}"
                )
            print("\nEdges")
            for edge in edges:
                print(
                    f"  {edge['from']} → {edge['to']} "
                    f"({edge.get('relation', 'references')})"
                )
            print("\nPageRank Top 5")
            for index, node in enumerate(top_nodes, 1):
                print(
                    f"  {index:2d}. [{node['id']}]  "
                    f"PR={node['pagerank']:.5f}  {node['title']}"
                )
        return
    
    # 计算
    pr = compute_pagerank(G)
    partition, communities = compute_communities(G)
    isolates = find_isolates(G)
    bridges = find_bridges(G, partition)
    
    if args.json:
        result = {
            'nodes': len(G.nodes()),
            'edges': len(G.edges()),
            'communities': len(communities),
            'isolates': len(isolates),
            'bridges': len(bridges),
            'top_pagerank': [(slug, round(score, 5), G.nodes[slug].get('title','')) 
                            for slug, score in pr[:20]],
            'communities': {str(k): v for k, v in communities.items()},
            'isolate_list': [(s, d, t) for s, d, t in isolates],
            'bridge_list': [(s, c, d) for s, c, d in bridges[:20]],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    
    # 文本输出
    print(f"╔══════════════════════════════════════╗")
    print(f"║   Knowledge Graph 知识图谱计算           ║")
    print(f"╠══════════════════════════════════════╣")
    print(f"║ 节点: {len(G.nodes()):>4d}  边: {len(G.edges()):>4d}  社区: {len(communities):>2d}  ║")
    print(f"║ 孤立: {len(isolates):>4d}  桥接: {len(bridges):>4d}              ║")
    print(f"╚══════════════════════════════════════╝")
    
    # PageRank Top 15
    print(f"\n{'='*60}")
    print(f"PageRank Top 15 — 枢纽概念")
    print(f"{'='*60}")
    for i, (slug, score) in enumerate(pr[:15], 1):
        title = G.nodes[slug].get('title', slug)
        in_deg = G.in_degree(slug)
        print(f"  {i:2d}. [{slug}]  PR={score:.4f}  in={in_deg}  {title[:50]}")
    
    # 社区
    print(f"\n{'='*60}")
    print(f"社区检测 (Louvain) — {len(communities)} 个社区")
    print(f"{'='*60}")
    for cid in sorted(communities.keys()):
        members = communities[cid]
        # Find top member by PR
        top = max(members, key=lambda n: dict(pr).get(n, 0))
        print(f"\n  社区 {cid} ({len(members)} 个概念) — 核心: {top}")
        # Sample members
        sample = sorted(members, key=lambda n: -G.in_degree(n))[:5]
        for m in sample:
            print(f"    [{m}] in={G.in_degree(m)}")
    
    # 桥接 Top 10
    print(f"\n{'='*60}")
    print(f"桥接节点 Top 10 — 跨社区缝合点")
    print(f"{'='*60}")
    for i, (slug, comms, out_deg) in enumerate(bridges[:10], 1):
        print(f"  {i:2d}. [{slug}] 跨{comms}个社区  out={out_deg}")
    
    # 孤立 Top 10
    if isolates:
        print(f"\n{'='*60}")
        print(f"孤立概念 Top 10 — 零反链（可能已死）")
        print(f"{'='*60}")
        for i, (slug, out_deg, theorist) in enumerate(isolates[:10], 1):
            print(f"  {i:2d}. [{slug}] ({theorist}) out={out_deg}")
    
    # 全量排名模式
    if args.full:
        print(f"\n{'='*60}")
        print(f"完整 PageRank 排名")
        print(f"{'='*60}")
        for i, (slug, score) in enumerate(pr, 1):
            print(f"  {i:3d}. [{slug}]  {score:.4f}")


if __name__ == '__main__':
    main()
