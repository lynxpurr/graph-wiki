# Security Policy

## Data Privacy

graph-wiki analyzes your knowledge base **entirely on your machine**:

- All analysis (graph building, scoring, diagnostics) runs locally.
- The tool only reads paths you explicitly provide via `--wiki-root`, `--ontology-path`, or `--ragflow-path`.
- No telemetry, no analytics, no network calls in the default code path.

The only optional network feature is the RAGFlow real-time API mode, which is
activated solely when `RAGFLOW_API_KEY` is set **and** no offline dump path is
given. An explicitly provided offline dump always takes precedence.

## Reporting a Vulnerability

Please open a private security advisory on GitHub
(Repository → Security → Advisories) or contact the maintainer directly.
Do not open public issues for security vulnerabilities.
