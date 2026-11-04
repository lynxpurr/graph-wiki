# B3 Quality Scorecard Schema

The B3 scorecard is the stable quality-baseline contract produced by
`graph-test-suite.py --scorecard` and saved by `wiki_analyzer.py baseline-save`.

## JSON shape

```json
{
  "schema_version": "b3-scorecard-v1",
  "baseline_id": "baseline_2026-07-14",
  "timestamp": "2026-07-14T09:00:00+00:00",
  "source": "wiki",
  "metrics": {
    "structure": {
      "dead_end_ratio": 0.05,
      "isolated_ratio": 0.12,
      "sponge_ratio": 0.03,
      "link_rot_ratio": 0.01,
      "density": 0.08
    },
    "quality": {
      "outlink_coverage": 0.85,
      "reciprocity": 0.15,
      "type_diversity": 0.6
    },
    "composite": {
      "health_score": 99,
      "grade": "A",
      "penalties": {
        "dead_end": 0,
        "link_rot": 0,
        "isolated": 1.2,
        "reciprocity": 0,
        "density": 0
      }
    }
  },
  "thresholds": {
    "dead_end_warn": 0.1,
    "isolated_warn": 0.2,
    "link_rot_crit": 0.05,
    "health_score_crit": 50,
    "reciprocity_warn": 0.05,
    "density_low_warn": 0.01,
    "density_high_warn": 0.25
  },
  "threshold_evaluation": {
    "status": "green",
    "critical": [],
    "warnings": []
  },
  "canary_eligibility": "low_risk",
  "legacy_scores": {
    "total": 88,
    "max": 110,
    "percentage": 80,
    "grade": "A  Graph performance is healthy",
    "dimensions": {}
  },
  "metadata": {
    "nodes": 100,
    "edges": 800,
    "dead_ends": 5,
    "isolated": 12,
    "sponges": 3,
    "broken_links": 8,
    "diagnosis_available": true
  }
}
```

`legacy_scores` is included so v3.0-v3.1 trend consumers can continue reading
the 11-dimension score. A saved `.graph-baseline.json` also mirrors this object
as the top-level `scores` field.

## Metric definitions

| Metric | Definition |
|---|---|
| `dead_end_ratio` | D1 findings / nodes |
| `isolated_ratio` | D2 findings / nodes |
| `sponge_ratio` | D3 findings / nodes |
| `link_rot_ratio` | D4 broken links / (resolved graph edges + broken links) |
| `density` | Directed graph density, `edges / (nodes * (nodes - 1))` |
| `outlink_coverage` | Nodes with at least one outbound edge / nodes |
| `reciprocity` | Directed edges whose reverse edge exists / edges |
| `type_diversity` | Normalised Shannon entropy of ontology types; wiki mode falls back to `theorist` |

Pass the full `graph-diagnose.py --json --full` result through
`--diagnosis-json FILE` or `--diagnosis-json -`. If it is omitted, graph-derived
metrics remain available, link rot defaults to zero, and
`diagnosis_unavailable` makes canary eligibility at least `moderate_risk`.

## Composite score

Start at 100 and subtract these penalties:

- Dead ends: 20 points for each `dead_end_warn` multiple above the threshold,
  capped at 40.
- Link rot: 15 points for each `link_rot_crit` multiple above the threshold,
  capped at 30.
- Isolated nodes: `isolated_ratio * 10`, capped at 10.
- Reciprocity: 5 points when below `reciprocity_warn`.
- Density: 5 points when outside the configured low/high band.

Clamp to 0-100 and round to the nearest integer. Grades are A (90-100),
B (75-89), C (60-74), D (40-59), and F (0-39).

## Canary eligibility

- `low_risk`: all threshold checks are green.
- `moderate_risk`: no critical breach and at least one warning.
- `high_risk`: any critical breach.

For release gating, `dead_end_ratio > dead_end_warn`,
`link_rot_ratio > link_rot_crit`, or `health_score < health_score_crit` is a
critical breach. This makes `dead_end_ratio > 0.10` explicitly ineligible for a
low-risk canary, as required by Wave 5B.
