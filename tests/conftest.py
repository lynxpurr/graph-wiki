from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# Hermetic tests: a developer machine may legitimately run a live RAGFlow
# server with RAGFLOW_API_KEY exported. Tests must never touch it — force
# the offline path so results only depend on the repo's fixture files.
for _var in ("RAGFLOW_API_KEY", "RAGFLOW_API_BASE"):
    os.environ.pop(_var, None)


@pytest.fixture
def load_script_module():
    """Load a hyphenated script as an importable module."""
    loaded = []

    def load(filename: str):
        module_name = "wiki_graph_test_" + filename.replace("-", "_").replace(".", "_")
        spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / filename)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        loaded.append(module_name)
        return module

    yield load
    for module_name in loaded:
        sys.modules.pop(module_name, None)


@pytest.fixture
def tmp_wiki(tmp_path: Path) -> Path:
    wiki = tmp_path / "wiki"
    (wiki / "concepts").mkdir(parents=True)
    (wiki / "archive").mkdir()

    (wiki / "home.md").write_text(
        """---
title: "Home"
source_theorist: systems
date_created: 2026-07-01
date_modified: 2026-07-14
---
# Home

See [[concepts/alpha]], [[beta|Beta label]], and [[archive]].

```markdown
[[ignored-code-link]]
```
""",
        encoding="utf-8",
    )
    (wiki / "concepts" / "alpha.md").write_text(
        """---
title: "Alpha"
source_theorist: systems
date_created: 2026-07-02
date_modified: 2026-07-14
---
# Alpha

Alpha points to [[beta#details]].
""",
        encoding="utf-8",
    )
    (wiki / "concepts" / "beta.md").write_text(
        """---
title: "Beta"
source_theorist: networks
date_created: 2026-07-03
date_modified: 2026-07-14
---
# Beta

Beta points to [[home]].
""",
        encoding="utf-8",
    )
    (wiki / "archive" / "index.md").write_text(
        """---
title: "Archive"
source_theorist: history
date_created: 2026-07-04
date_modified: 2026-07-14
---
# Archive

See [[alpha]].
""",
        encoding="utf-8",
    )
    return wiki


@pytest.fixture
def tmp_ontology_graph(tmp_path: Path) -> Path:
    graph_path = tmp_path / "graph.jsonl"
    records = [
        {"op": "create", "entity": {"id": "a", "type": "Concept", "properties": {"title": "A"}}},
        {"op": "create", "entity": {"id": "b", "type": "Person", "properties": {"title": "B"}}},
        {"op": "create", "entity": {"id": "c", "type": "Concept", "properties": {"title": "C"}}},
        {"op": "relate", "from": "a", "to": "b", "rel": "references"},
        {"op": "relate", "from": "b", "to": "c", "rel": "references"},
    ]
    graph_path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return graph_path
