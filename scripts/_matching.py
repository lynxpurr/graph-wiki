"""Wiki-graph mapping suggestion engine.

Three-layer matching pipeline: string similarity -> community affinity ->
combined scoring.
"""

from __future__ import annotations


def normalize_slug(s: str) -> str:
    """Normalize a slug/name: lowercase, replace separators, and strip."""
    return s.lower().replace("-", " ").replace("_", " ").strip()


def string_similarity(a: str, b: str) -> float:
    """Return bidirectional-substring/Levenshtein similarity in [0, 1]."""
    left, right = normalize_slug(a), normalize_slug(b)
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0

    substring_score = 0.75 if left in right or right in left else 0.0

    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            current.append(min(
                current[-1] + 1,
                previous[right_index] + 1,
                previous[right_index - 1] + (left_char != right_char),
            ))
        previous = current

    distance = previous[-1]
    levenshtein_ratio = 1.0 - distance / max(len(left), len(right))
    return max(substring_score, levenshtein_ratio)


def community_affinity(
    wiki_node: str,
    ontology_entity: str,
    partition: dict,
) -> float:
    """Return 1.0 when both nodes belong to the same community, else 0.0."""
    if wiki_node not in partition or ontology_entity not in partition:
        return 0.0
    return float(partition[wiki_node] == partition[ontology_entity])


def _names(identifier: str, metadata: object) -> list[str]:
    """Collect stable ID and optional title/name strings for comparison."""
    names = [identifier]
    if not isinstance(metadata, dict):
        return names
    properties = metadata.get("properties", {})
    for value in (
        metadata.get("title"), metadata.get("name"),
        properties.get("title") if isinstance(properties, dict) else None,
        properties.get("name") if isinstance(properties, dict) else None,
    ):
        if value is not None and str(value) not in names:
            names.append(str(value))
    return names


def suggest_mappings(
    unmapped: list[str],
    ontology_entities: dict[str, dict],
    graph,
    top_k: int = 10,
    threshold: float = 0.3,
) -> list[dict]:
    """Score and return the top ontology candidates for each wiki node."""
    if not unmapped or top_k <= 0:
        return []

    partition = (
        graph.graph.get("partition")
        or graph.graph.get("community_partition")
        or {}
    )
    wiki_meta = graph.graph.get("_source_meta", {}).get("wiki", {})
    suggestions = []

    for wiki_node in unmapped:
        node_metadata = wiki_meta.get(wiki_node, {}) if isinstance(wiki_meta, dict) else {}
        if wiki_node in graph:
            node_metadata = {**node_metadata, **dict(graph.nodes[wiki_node])}
        wiki_names = _names(wiki_node, node_metadata)
        candidates = []

        for entity_id, entity_metadata in ontology_entities.items():
            entity_names = _names(entity_id, entity_metadata)
            string_score = max(
                string_similarity(wiki_name, entity_name)
                for wiki_name in wiki_names
                for entity_name in entity_names
            )
            community_score = community_affinity(wiki_node, entity_id, partition)
            score = 0.7 * string_score + 0.3 * community_score
            if score < threshold:
                continue
            layers = []
            if string_score > 0:
                layers.append("str")
            if community_score > 0:
                layers.append("comm")
            candidates.append({
                "wiki_node": wiki_node,
                "ontology_entity": entity_id,
                "score": score,
                "layers": layers,
            })

        candidates.sort(key=lambda item: (-item["score"], item["ontology_entity"]))
        suggestions.extend(candidates[:top_k])

    return suggestions
