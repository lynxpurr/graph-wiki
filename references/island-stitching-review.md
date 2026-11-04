# Island Stitching Architecture Review

Five-dimension methodology for reviewing proposed stitching plans when isolated concept nodes need to be linked into the mainland graph.

## When to Use

- User proposes a stitching plan for isolated concepts (single-node components)
- Wiki has >1 weak connected component
- New concepts were batch-created but never cross-linked

## The Five Dimensions

### 1. Anchor Choice Reasonableness

For each proposed anchor, evaluate semantic fit:

| Fit Level | Criteria | Action |
|-----------|----------|--------|
| Strong | Same mechanism, different abstraction level; or complementary perspectives on same phenomenon | Keep as primary anchor |
| Medium | Overlapping vocabulary but different mechanisms; or tangential connection | Keep as secondary anchor only |
| Weak | Shared keyword only; no mechanism overlap | Replace with better anchor |

**Key question**: If someone reads the anchor concept, will navigating to the island concept feel like a natural continuation? If not, the link is noise.

**Common failure mode**: Anchoring on shared vocabulary ("cost" in both names) when the mechanisms are unrelated.

### 2. Island-to-Island Linking Potential

Before stitching islands individually to the mainland, check if islands can form a sub-network. Signals:

- Same case study / event referenced in both concepts
- One concept's mechanism explicitly names the other
- Complementary perspectives (attacker vs defender, cause vs effect)
- Concepts already list each other in `related_concepts` frontmatter

**Highest-value island pairs**: When stitching them together first creates a micro-subnet, then stitching the pair to the mainland activates both simultaneously.

### 3. Backlink Placement: In-Degree vs Semantics

**Rule**: Semantic tightness > structural centrality for backlinks.

| Strategy | When to use | Risk |
|----------|-------------|------|
| Semantic anchor as primary backlink | Always | None — meaningful navigation |
| High-in-degree anchor as secondary backlink | When the connection is at least medium-fit | Noise if fit is weak — dilutes hub quality |
| High-in-degree with weak semantics | Never | Creates false navigation paths; degrades both nodes |

**Dual backlink strategy** (optimal):
- Primary backlink from the semantically tightest anchor → deep, meaningful path
- Secondary backlink from a high-in-degree anchor → discovery path (only if semantic fit is medium+)

### 4. Stitching Priority Ordering

Rank by marginal impact on overall wiki structure:

| Factor | Weight | Rationale |
|--------|--------|-----------|
| Concept redundancy (merge candidate) | Highest | Eliminating a split concept reduces structural debt immediately |
| Island has rich internal links to mainland concepts | High | Stitching activates N pre-existing conceptual connections |
| Island pairs with another island | High | Stitching a pair creates a sub-net with multiplicative value |
| Concept is a specialization of a mainland hub | Medium | Enriches an existing cluster |
| Concept is generic / weakly power-specific | Low | Marginal structural gain |

### 5. Merge vs Stitch Decision

**Merge when**:
- Same definition with different wording
- Same canonical example/case study
- Same mechanism (same steps / iron laws / phases)
- Content overlap >70%

**Stitch when**:
- Different mechanism but complementary perspective
- Different abstraction level (theory vs tactic)
- Different domain (code vs narrative vs personnel)
- Content overlap <30%

**Merge protocol**: Absorb unique content (tests, flowcharts, edge cases) from the absorbed concept into the surviving concept. Update aliases. Global search-and-replace all wikilink references. Update index counts.

## Execution Order

1. First: identify merge candidates and merge them. Merged concepts are no longer islands.
2. Second: identify island pairs and cross-link them.
3. Third: stitch remaining islands (or island pairs) to mainland via primary + secondary anchors.
4. Fourth: add backlinks from anchor nodes to islands.
5. Fifth: run graph verification (`graph-compute.py --json`) to confirm 1 connected component.

## Output Format

Present findings as:

```
MERGE: X → Y (reason)
PAIR: A ↔ B (reason)
STITCH: C → anchor1 (primary) + anchor2 (secondary)
STITCH: D → anchor3 (primary)
PRIORITY: P0 > P1 > P2 > P3 > P4
```
