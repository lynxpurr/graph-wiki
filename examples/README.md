# Mini Corpus

This directory contains a self-contained example for the wiki, ontology, and
RAGFlow graph modes:

- `mini-wiki/` has 17 Markdown pages split across concepts, people, and
  frameworks. The pages cover Computer Science fundamentals: algorithms,
  data structures, sorting, recursion, computational complexity, hashing,
  graph theory, and lambda calculus, anchored around Donald Knuth, Alan Turing,
  and Alonzo Church.
- `mini-ontology.jsonl` defines 17 entities and 70 relations that can be
  checked against the wiki links and cross-referenced for consistency scoring.
- `mini-ragflow.jsonl` contains 7 chunks referencing 11 of the 17 ontology
  entities to exercise the appended ragflow_coverage scoring row.

All wiki pages carry explicit `ontology_mappings` frontmatter, so the mapping
pipeline resolves via declared bindings before falling back to title matching.
`algorithms` serves as the central hub with the highest in-degree.

Run the corpus from the repository root:

```bash
.venv/bin/python scripts/graph-test-suite.py \
  --source wiki,ontology \
  --wiki-root examples/mini-wiki \
  --ontology-path examples/mini-ontology.jsonl
```

To exercise all three sources and the appended ragflow_coverage scoring row:

```bash
.venv/bin/python scripts/graph-test-suite.py \
  --source wiki,ontology,ragflow \
  --wiki-root examples/mini-wiki \
  --ontology-path examples/mini-ontology.jsonl \
  --ragflow-path examples/mini-ragflow.jsonl
```

ragflow_coverage reports `11/17 = 6.47/10`. It is omitted when RAGFlow is not one of the
loaded sources.

The exact community labels can vary between Louvain runs, but the suite should
exit successfully with a score above the 60-point (60%) acceptance threshold.
Every run prints this threshold on its last summary line
(`验收阈值: 60 分（60%，评级 B 起点）→ 通过/未通过`); in `--json` mode it is
`summary.acceptance_threshold` / `summary.acceptance_passed`.
The denominators are `120` (wiki only), `130` (wiki+ontology) and `140`
(+ ragflow): `max_score = 10 × scoring rows + 10 bonus ceiling`.
