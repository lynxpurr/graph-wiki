#!/usr/bin/env python3
"""Unified graph builder for wiki-graph — shared module
Supports wiki Markdown, ontology JSONL, and offline ragflow dumps.

v3.1.1: Centralized wikilink parser (iter_wikilinks), root-relative node IDs,
Obsidian alias support, removed os.chdir side-effect.
"""
from __future__ import annotations

import os, re, json, sys, argparse, io
from datetime import datetime
from typing import Tuple, Iterator, Dict, List
import networkx as nx

from markdown_it import MarkdownIt

from _privacy_filter import filter_ontology


DEFAULT_RAGFLOW_API_BASE = "http://localhost:9380"


def get_ontology_path() -> str:
    """Resolve the ontology path selected by ``ONTOLOGY_OUTPUT``."""
    output_mode = os.environ.get('ONTOLOGY_OUTPUT', 'public').strip().lower()
    if output_mode == 'private':
        private_path = os.environ.get('ONTOLOGY_PRIVATE_PATH')
        if not private_path:
            raise ValueError(
                "ONTOLOGY_OUTPUT=private requires ONTOLOGY_PRIVATE_PATH to be set explicitly"
            )
        return os.path.expanduser(private_path)
    if output_mode not in ('public', 'private', 'sanitized', 'confidential'):
        raise ValueError(
            "ONTOLOGY_OUTPUT must be 'public', 'sanitized', 'private', or 'confidential' "
            f"(got {output_mode!r})"
        )
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(project_root, 'examples', 'mini-ontology.jsonl')


def default_wiki_root() -> str:
    """Return the bundled mini wiki, or the current directory if absent."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    examples_dir = os.path.join(project_root, 'examples')
    if os.path.isdir(examples_dir):
        return os.path.join(examples_dir, 'mini-wiki')
    return os.getcwd()

# ── Wikilink parsing (shared public API) ──────────────────

_WIKILINK_RE = re.compile(r'\[\[([^\[\]]+?)\]\]')

def _line_offsets(content: str) -> List[int]:
    """Char offset of every line start; index == 0-based line number."""
    offsets = [0]
    for i, ch in enumerate(content):
        if ch == '\n':
            offsets.append(i + 1)
    return offsets


_MD_PARSER = MarkdownIt('commonmark')


def _block_code_spans(content: str) -> List[Tuple[int, int]]:
    """Char ranges covered by CommonMark fenced or indented code blocks."""
    offsets = _line_offsets(content)

    def char_at(lineno: int) -> int:
        if lineno >= len(offsets):
            return len(content)
        return offsets[lineno]

    spans: List[Tuple[int, int]] = []
    for tok in _MD_PARSER.parse(content):
        if tok.type not in ('fence', 'code_block'):
            continue
        if not tok.map:
            continue
        spans.append((char_at(tok.map[0]), char_at(tok.map[1])))
    return spans


def _inline_code_spans(content: str,
                       block_spans: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Char ranges covered by inline code spans (matched backtick runs).

    Scanned only outside block code. A backtick run that opens inside a fence
    must not be allowed to pair up with one out in prose, which the whole-file
    scan this replaced could do.
    """
    spans: List[Tuple[int, int]] = []
    cursor = 0
    i = 0
    n = len(content)
    while i < n:
        while cursor < len(block_spans) and block_spans[cursor][1] <= i:
            cursor += 1
        if (cursor < len(block_spans)
                and block_spans[cursor][0] <= i < block_spans[cursor][1]):
            i = block_spans[cursor][1]
            continue
        if content[i] != '`':
            i += 1
            continue
        run_end = i
        while run_end < n and content[run_end] == '`':
            run_end += 1
        run = run_end - i
        # CommonMark: a code span closes on a run of exactly equal length.
        k = run_end
        close = -1
        while k < n:
            if content[k] != '`':
                k += 1
                continue
            run_close_end = k
            while run_close_end < n and content[run_close_end] == '`':
                run_close_end += 1
            if run_close_end - k == run:
                close = k
                break
            k = run_close_end
        if close == -1:
            i = run_end  # an unmatched run is literal text
            continue
        spans.append((i, close + run))
        i = close + run
    return spans


def _code_spans(content: str) -> List[Tuple[int, int]]:
    """Char ranges that are code, and therefore not references.

    Block structure comes from a CommonMark parser, not from a hand-written
    line scan. The scan this replaced keyed on backticks alone, so `~~~` fences
    and 4-space indented blocks were read as prose — real references inside them
    were reported as link-rot — and a 4-backtick line closed a 3-backtick fence
    early. Every hand-written mask has its own seam, and which lines are code is
    not recoverable from a line scan, so it comes from the parser.
    """
    block = _block_code_spans(content)
    spans = block + _inline_code_spans(content, block)
    spans.sort()
    return spans


def _span_covers(spans: List[Tuple[int, int]], start: int, end: int) -> bool:
    for span_start, span_end in spans:
        if span_start <= start and end <= span_end:
            return True
    return False


def _is_inside_code(content: str, start: int, end: int) -> bool:
    """True when [start, end) sits inside fenced, indented, or inline code."""
    return _span_covers(_code_spans(content), start, end)


def iter_wikilinks(content: str) -> Iterator[Tuple[str, str, bool]]:
    """Yield (target, label, is_inside_code) for every [[wikilink]] in content.

    Parses: [[target]], [[target|label]], [[target#anchor]], [[target#anchor|label]]
    Returns the resolved target (anchor stripped), the display label (or None),
    and whether the link was inside a fenced/indented/inline code block.
    """
    spans = _code_spans(content)
    for m in _WIKILINK_RE.finditer(content):
        body = m.group(1).strip()
        inside_code = _span_covers(spans, m.start(), m.end())
        # Split alias: [[target|label]] or [[target#anchor|label]]
        if '|' in body:
            body, label = body.split('|', 1)
            label = label.strip()
        else:
            label = None
        # Strip anchor: [[target#section]]
        if '#' in body:
            body = body.split('#', 1)[0].strip()
        yield (body.strip(), label, inside_code)

def _all_md_files(wiki_root):
    md_files = []
    for root, dirs, files in os.walk(wiki_root):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules',)]
        for f in files:
            if f.endswith('.md'): md_files.append(os.path.join(root, f))
    return md_files

def _canonical_id(filepath: str, wiki_root: str) -> str:
    """Root-relative canonical node ID (no .md, no leading ./).

    a/page.md → 'a/page', b/page.md → 'b/page', dir/index.md → 'dir'.
    """
    rel = os.path.relpath(filepath, wiki_root)
    rel = rel.replace('\\', '/')
    slug = rel.replace('.md', '')
    if slug.endswith('/index'):
        slug = slug[:-len('/index')]
    return slug

def _alias_index(md_files, wiki_root) -> Dict[str, List[str]]:
    """Build basename → [canonical_id, ...] alias map."""
    aliases: Dict[str, List[str]] = {}
    for path in md_files:
        cid = _canonical_id(path, wiki_root)
        basename = os.path.basename(cid)
        aliases.setdefault(basename, []).append(cid)
        aliases.setdefault(cid, []).append(cid)  # exact self-match
    return aliases


def _frontmatter_tags(content: str) -> list[str]:
    match = re.search(r'^tags:\s*(.*)$', content, re.MULTILINE)
    if not match:
        return []
    inline = match.group(1).strip()
    if inline:
        return _unique(_as_list(inline))
    tags = []
    for line in content[match.end():].splitlines():
        item = re.match(r'^\s+-\s*(.+?)\s*$', line)
        if item:
            tags.append(item.group(1).strip('"\''))
            continue
        if line.strip():
            break
    return _unique(tags)


def _frontmatter_ontology_mappings(content: str) -> list[str]:
    match = re.search(r'^ontology_mappings:[ \t]*(.*)$', content, re.MULTILINE)
    if not match:
        return []
    inline = match.group(1).strip()
    if inline:
        return _unique(_as_list(inline))
    mappings = []
    remaining = content[match.end():]
    if remaining.startswith('\r\n'):
        remaining = remaining[2:]
    elif remaining.startswith('\n'):
        remaining = remaining[1:]
    for line in remaining.splitlines():
        if not line.strip():
            break
        item = re.match(r'^\s+-\s*(.+?)\s*$', line)
        if not item:
            break
        mappings.append(item.group(1).strip('"\''))
    return _unique(mappings)


def _extract_title(content: str, default=None):
    """Extract a quoted or unquoted title from Markdown frontmatter."""
    match = re.search(r'^title:\s*(.+)\s*$', content, re.MULTILINE)
    if not match:
        return default
    return match.group(1).strip().strip('"\'')


def _build_wiki(wiki_root):
    G = nx.DiGraph(); meta = {}
    G.graph['source'] = 'wiki'
    wiki_root = os.path.expanduser(wiki_root)
    md_files = _all_md_files(wiki_root)
    alias_index = _alias_index(md_files, wiki_root)
    # Populate nodes
    for path in md_files:
        cid = _canonical_id(path, wiki_root)
        content = open(path, encoding='utf-8').read()
        st = re.search(r'^source_theorist:\s*(.+)', content, re.MULTILINE)
        theorist = st.group(1).strip() if st else 'unknown'
        title = _extract_title(content, default=cid)
        dc = re.search(r'^date_created:\s*(\d{4}-\d{2}-\d{2})', content, re.MULTILINE)
        date_created = dc.group(1) if dc else None
        dm = re.search(r'^date_modified:\s*(\d{4}-\d{2}-\d{2})', content, re.MULTILINE)
        date_modified = dm.group(1) if dm else None
        desc = re.search(r'^description:\s*["\']?(.+?)["\']?\s*$', content, re.MULTILINE)
        description = desc.group(1).strip() if desc else None
        tags = _frontmatter_tags(content)
        ontology_mappings = _frontmatter_ontology_mappings(content)
        def_m = re.search(r'^## .+?\n+(.+?)(?=\n## |\n---|\Z)', content, re.DOTALL | re.MULTILINE)
        definition_text = def_m.group(1).strip() if def_m else ''
        if not G.has_node(cid):
            G.add_node(cid, theorist=theorist, title=title)
            meta[cid] = {'canonical_id': cid, 'theorist': theorist, 'title': title,
                        'tags': tags, 'ontology_mappings': ontology_mappings,
                        'description': description,
                        'date_created': date_created,
                        'date_modified': date_modified, 'definition_text': definition_text,
                        'file_mtime': datetime.fromtimestamp(os.path.getmtime(path)),
                        'path': os.path.relpath(path, wiki_root)}
    # Populate edges
    for path in md_files:
        source_cid = _canonical_id(path, wiki_root)
        content = open(path, encoding='utf-8').read()
        for target_raw, _label, inside_code in iter_wikilinks(content):
            if inside_code:
                continue
            if target_raw.startswith('skill:') or target_raw.startswith('skills:'):
                continue
            target = target_raw
            if target.startswith('../'):
                rel = os.path.relpath(path, wiki_root)
                target = os.path.normpath(os.path.join(os.path.dirname(rel), target))
            # Resolve through alias index
            matches = alias_index.get(target, [])
            if len(matches) == 0:
                pass  # broken link — registered as missing edge later
            elif len(matches) == 1:
                G.add_edge(source_cid, matches[0])
            else:
                # Ambiguous basename — pick exact match if available, else warn
                if target in matches:
                    G.add_edge(source_cid, target)
                else:
                    print(f"WARN: ambiguous link [[{target}]] in {source_cid} → candidates {matches}",
                          file=sys.stderr)
                    G.add_edge(source_cid, matches[0])  # first-wins
    return G, meta

ONTOLOGY_RELATION_TYPES = {
    '引用': 'references',
    '实例化': 'instantiates',
    '反驳': 'refutes',
    '先决条件': 'prerequisite_of',
    '属于': 'belongs_to',
    '发生于': 'occurred_in',
    '对称': 'mutual_reinforcement',
}


def _build_ontology(ontology_path):
    G = nx.DiGraph(); meta = {}
    G.graph['source'] = 'ontology'
    ontology_path = os.path.expanduser(ontology_path)
    if not os.path.exists(ontology_path):
        print(f"WARN: ontology not found: {ontology_path}", file=sys.stderr)
        return G, meta
    get_ontology_path()  # Validate ONTOLOGY_OUTPUT even when a path was supplied.
    output_mode = os.environ.get('ONTOLOGY_OUTPUT', 'public').strip().lower()
    try:
        if output_mode == 'public':
            records = filter_ontology(ontology_path, tier=1)
            # P0-1 fix: never drop records silently — surface the drop count so
            # users notice when their ontology entities are being filtered.
            raw_count = 0
            try:
                with open(ontology_path, 'r', encoding='utf-8') as _raw:
                    for _line in _raw:
                        if _line.strip():
                            raw_count += 1
            except OSError:
                raw_count = 0
            kept = len(records)
            if raw_count > kept:
                print(
                    f"WARN: privacy filter dropped {raw_count - kept} of {raw_count} "
                    f"ontology records (ONTOLOGY_OUTPUT=public). "
                    f"Set ONTOLOGY_OUTPUT=sanitized/private to load them.",
                    file=sys.stderr,
                )
            fh = io.StringIO(''.join(
                json.dumps(record, ensure_ascii=False) + '\n'
                for record in records
            ))
        else:
            fh = open(ontology_path, 'r', encoding='utf-8')
    except (PermissionError, UnicodeDecodeError, FileNotFoundError) as e:
        print(f"ERROR reading ontology {ontology_path}: {e}", file=sys.stderr)
        return G, meta
    with fh:
        for line_number, line in enumerate(fh, start=1):
            line = line.strip()
            if not line: continue
            try: rec = json.loads(line)
            except json.JSONDecodeError:
                print(
                    f"WARN: skipping malformed line {line_number} in {ontology_path}",
                    file=sys.stderr,
                )
                continue
            op = rec.get('op', '')
            if op == 'create':
                entity = rec.get('entity', {}); node_id = entity.get('id', '')
                if not node_id: continue
                entity_type = entity.get('type', 'Unknown'); props = entity.get('properties', {})
                title = props.get('title', props.get('name', node_id))
                _safe = {k:v for k,v in props.items() if k not in ("title","type")}; G.add_node(node_id, type=entity_type, title=title, **_safe)
                meta[node_id] = {'type': entity_type, 'title': title, 'properties': props}
            elif op == 'update':
                entity = rec.get('entity', {})
                node_id = entity.get('id', '')
                if not node_id or node_id not in G:
                    if node_id:
                        print(
                            f"[WARN] ontology update references unknown entity: {node_id}",
                            file=sys.stderr,
                        )
                    continue
                props = entity.get('properties', {})
                if not isinstance(props, dict):
                    continue
                current_meta = meta.setdefault(node_id, {})
                current_props = dict(current_meta.get('properties', {}))
                current_props.update(props)
                current_meta['properties'] = current_props
                entity_type = entity.get('type')
                if entity_type:
                    G.nodes[node_id]['type'] = entity_type
                    current_meta['type'] = entity_type
                if 'title' in props or 'name' in props:
                    title = props.get('title', props.get('name', node_id))
                    G.nodes[node_id]['title'] = title
                    current_meta['title'] = title
                G.nodes[node_id].update({
                    key: value for key, value in props.items()
                    if key not in ('title', 'type')
                })
            elif op == 'relate':
                entity = rec.get('entity', {})
                src = entity.get('from', rec.get('from', ''))
                dst = entity.get('to', rec.get('to', ''))
                if not (src and dst): continue
                missing = []
                if src not in G: missing.append(src)
                if dst not in G: missing.append(dst)
                if missing:
                    print(f"WARN: relate references unknown node(s) {missing}: {src} -> {dst}", file=sys.stderr)
                    continue
                relation = (
                    entity.get('relation') or entity.get('rel')
                    or rec.get('relation') or rec.get('rel') or 'unspecified'
                )
                relation_type = ONTOLOGY_RELATION_TYPES.get(relation, relation)
                edge_meta = {
                    'relation': relation,
                    'relation_type': relation_type,
                }
                G.add_edge(src, dst, **edge_meta)
                if relation == '对称':
                    G.add_edge(dst, src, **edge_meta)
    return G, meta


def _iter_ragflow_records(ragflow_path: str) -> Iterator[dict]:
    """Yield chunk records from a JSONL file or a directory of JSON/JSONL files."""
    path = os.path.expanduser(ragflow_path)
    files = []
    if os.path.isdir(path):
        files = [
            os.path.join(path, name)
            for name in sorted(os.listdir(path))
            if name.lower().endswith(('.json', '.jsonl'))
        ]
    elif os.path.isfile(path):
        files = [path]

    for filename in files:
        try:
            with open(filename, 'r', encoding='utf-8') as fh:
                if filename.lower().endswith('.jsonl'):
                    records = []
                    for line_number, line in enumerate(fh, start=1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            print(
                                f"WARN: skipping malformed line {line_number} "
                                f"in {filename}",
                                file=sys.stderr,
                            )
                            continue
                else:
                    try:
                        payload = json.load(fh)
                    except json.JSONDecodeError:
                        continue
                    records = payload if isinstance(payload, list) else [payload]
        except (OSError, PermissionError, UnicodeDecodeError) as exc:
            print(f"WARN: could not read ragflow dump {filename}: {exc}", file=sys.stderr)
            continue
        for record in records:
            if isinstance(record, dict):
                yield record


def _build_ragflow(ragflow_path=None):
    """Build a chunk graph from an offline ragflow JSONL/JSON dump."""
    G = nx.DiGraph()
    G.graph['source'] = 'ragflow'
    path = os.path.expanduser(ragflow_path) if ragflow_path else None
    if not path or not os.path.exists(path):
        if path:
            print(f"WARN: ragflow dump not found: {path}; skipping", file=sys.stderr)
        return G, {'ragflow_status': 'skipped', 'ragflow_path': path}

    meta = {'ragflow_status': 'loaded', 'ragflow_path': path}
    previous_by_document = {}
    for position, record in enumerate(_iter_ragflow_records(path)):
        chunk_id = str(record.get('chunk_id', '')).strip()
        if not chunk_id:
            continue
        kb_name = str(record.get('kb_name', '') or 'unknown')
        document_id = str(
            record.get('document_id') or record.get('doc_id')
            or record.get('document') or record.get('source') or kb_name
        )
        entities = record.get('entities', [])
        if isinstance(entities, str):
            entities = [entities]
        entities = [str(entity).strip() for entity in entities if str(entity).strip()]
        attrs = {
            'title': record.get('title') or f"Chunk {chunk_id}",
            'type': 'ragflow_chunk',
            'chunk_id': chunk_id,
            'kb_name': kb_name,
            'document_id': document_id,
            'content': record.get('content', ''),
            'entities': entities,
            'position': record.get('position', position),
        }
        G.add_node(chunk_id, **attrs)
        meta[chunk_id] = dict(attrs)
        previous = previous_by_document.get(document_id)
        if previous is not None:
            G.add_edge(previous, chunk_id, relation='next')
        previous_by_document[document_id] = chunk_id
    meta['ragflow_chunks'] = G.number_of_nodes()
    return G, meta


# ── RAGFlow Real-Time API ──────────────────────────────

_ALLOWED_URL_SCHEMES = ("http", "https")


def _reject_unsafe_scheme(url: str):
    """Return an error payload unless *url* uses http or https.

    The API base URL comes from user input or configuration, so an exotic
    scheme (file://, ftp:// ...) must never reach urlopen: file:// in
    particular would allow reading arbitrary local files.
    """
    from urllib.parse import urlparse

    try:
        scheme = urlparse(url).scheme.lower()
    except ValueError:
        return {"_error": "invalid URL: %r" % (url,)}
    if scheme not in _ALLOWED_URL_SCHEMES:
        return {
            "_error": "unsupported URL scheme %r; only http/https are allowed" % (scheme,)
        }
    return None


def _http_get(url: str, api_key: str, timeout: int = 30) -> dict:
    """HTTP GET with Bearer auth. Returns parsed JSON or {'_error': msg}."""
    import urllib.request
    import urllib.error
    scheme_error = _reject_unsafe_scheme(url)
    if scheme_error:
        return scheme_error
    req = urllib.request.Request(url)
    req.add_header('Authorization', f'Bearer {api_key}')
    try:
        # Scheme allow-listed by _reject_unsafe_scheme above.
        with urllib.request.urlopen(  # nosec B310
                req, timeout=timeout
            ) as resp:
            body = resp.read().decode('utf-8')
            data = json.loads(body) if body else {}
            return data if isinstance(data, dict) else {'_raw': data}
    except urllib.error.HTTPError as e:
        return {'_error': f'HTTP {e.code}: {e.reason}', '_status': e.code}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {'_error': str(e)}


def _http_post(url: str, api_key: str, payload: dict, timeout: int = 30) -> dict:
    """HTTP POST with Bearer auth + JSON body."""
    import urllib.request
    import urllib.error
    scheme_error = _reject_unsafe_scheme(url)
    if scheme_error:
        return scheme_error
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Authorization', f'Bearer {api_key}')
    req.add_header('Content-Type', 'application/json')
    try:
        # Scheme allow-listed by _reject_unsafe_scheme above.
        with urllib.request.urlopen(  # nosec B310
                req, timeout=timeout
            ) as resp:
            body = resp.read().decode('utf-8')
            result = json.loads(body) if body else {}
            return result if isinstance(result, dict) else {'_raw': result}
    except urllib.error.HTTPError as e:
        return {'_error': f'HTTP {e.code}: {e.reason}', '_status': e.code}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {'_error': str(e)}


def _list_ragflow_datasets(api_key: str,
                           api_base: str = DEFAULT_RAGFLOW_API_BASE) \
        -> list[dict]:
    """List all RAGFlow datasets (KB list API on port 9380)."""
    url = f"{api_base.rstrip('/')}/api/v1/datasets?page=1&page_size=200"
    result = _http_get(url, api_key)
    if '_error' in result:
        print(f"WARN: RAGFlow dataset list failed: {result['_error']}", file=sys.stderr)
        return []
    return result.get('data', []) if isinstance(result.get('data'), list) else []


def _fetch_ragflow_chunks(dataset_id: str, doc_id: str, kb_name: str,
                           api_key: str, api_base: str) -> list[dict]:
    """Paginate all chunks for a document. Returns list of chunk dicts."""
    chunks = []
    page = 1
    page_size = 128
    while True:
        url = (
            f"{api_base.rstrip('/')}/api/v1/datasets/{dataset_id}"
            f"/documents/{doc_id}/chunks?page={page}&page_size={page_size}"
        )
        result = _http_get(url, api_key, timeout=30)
        if '_error' in result:
            print(f"WARN: chunk fetch failed KB={kb_name} doc={doc_id}: {result['_error']}",
                  file=sys.stderr)
            break
        data = result.get('data', {})
        batch = data.get('chunks', []) if isinstance(data, dict) else []
        if not batch:
            break
        for ch in batch:
            chunks.append({
                'chunk_id': str(ch.get('id', '')),
                'document_id': doc_id,
                'kb_name': kb_name,
                'content': ch.get('content', ''),
                'title': ch.get('document_name', f'Chunk {ch.get("id", "?")}')[:120],
                'position': ch.get('position', len(chunks)),
            })
        if len(batch) < page_size:
            break
        page += 1
    return chunks


def _build_ragflow_realtime(api_key: str = None, api_base: str = None,
                             kb_filter: str = None,
                             max_kbs: int = 20, max_chunks_per_kb: int = 2000):
    """Build a chunk graph from the live RAGFlow API.

    Args:
        api_key: RAGFlow bearer token (reads RAGFLOW_API_KEY if None)
        api_base: RAGFlow server base URL (default http://localhost:9380)
        kb_filter: Substring to filter KB names (e.g. "kb:research" or "kb:product")
        max_kbs: Max number of KBs to load
        max_chunks_per_kb: Per-KB chunk limit

    Returns (nx.DiGraph, meta).
    """
    G = nx.DiGraph()
    G.graph['source'] = 'ragflow'

    if api_key is None:
        api_key = os.environ.get('RAGFLOW_API_KEY', '')
    if not api_key:
        print("WARN: no RAGFlow API key; skipping real-time mode", file=sys.stderr)
        return G, {'ragflow_status': 'skipped', 'ragflow_mode': 'realtime'}

    if not api_base:
        api_base = DEFAULT_RAGFLOW_API_BASE

    meta = {'ragflow_status': 'realtime', 'ragflow_mode': 'realtime',
            'api_base': api_base, 'kb_filter': kb_filter}

    # List datasets
    datasets = _list_ragflow_datasets(api_key, api_base)
    datasets.sort(key=lambda d: d.get('document_count', 0) or 0, reverse=True)

    # Filter by name
    if kb_filter:
        datasets = [d for d in datasets if kb_filter.lower() in d.get('name', '').lower()]
    datasets = datasets[:max_kbs]

    kb_seen = 0
    total_chunks = 0
    previous_by_document: dict = {}
    kb_meta_list: list[dict] = []

    for ds in datasets:
        kb_name = ds.get('name', '')
        ds_id = ds.get('id', '')
        doc_count = ds.get('document_count', 0) or 0

        # List documents for this KB
        url = (
            f"{api_base.rstrip('/')}/api/v1/datasets/{ds_id}"
            "/documents?page=1&page_size=100"
        )
        result = _http_get(url, api_key, timeout=15)
        if '_error' in result:
            print(f"WARN: skip KB {kb_name}: {result['_error']}", file=sys.stderr)
            continue

        docs = result.get('data', {}).get('docs', []) if isinstance(result.get('data'), dict) else []
        kb_chunks = 0
        kb_nodes = 0

        for doc in docs:
            if kb_chunks >= max_chunks_per_kb:
                break
            doc_id = doc.get('id', '')
            chunks = _fetch_ragflow_chunks(ds_id, doc_id, kb_name, api_key, api_base)
            for i, ch in enumerate(chunks):
                node_id = f"ragflow:{kb_name}:{ch['chunk_id']}"
                attrs = {
                    'title': ch['title'],
                    'type': 'ragflow_chunk',
                    'chunk_id': ch['chunk_id'],
                    'kb_name': kb_name,
                    'document_id': doc_id,
                    'content': ch['content'][:500],
                    'position': total_chunks + i,
                    'entities': [],
                }
                G.add_node(node_id, **attrs)
                meta[node_id] = dict(attrs)
                doc_key = f"{kb_name}:{doc_id}"
                prev = previous_by_document.get(doc_key)
                if prev is not None:
                    G.add_edge(prev, node_id, relation='next')
                previous_by_document[doc_key] = node_id
                kb_nodes += 1
            kb_chunks += len(chunks)
            total_chunks += len(chunks)

        kb_seen += 1
        kb_meta_list.append({
            'name': kb_name, 'id': ds_id,
            'docs_loaded': min(len(docs), 100),
            'chunks_loaded': kb_chunks,
            'nodes': kb_nodes,
        })
        print(f"  RAGFlow: {kb_name} → {kb_nodes} nodes / {kb_chunks} chunks", file=sys.stderr)

    meta['ragflow_chunks'] = G.number_of_nodes()
    meta['ragflow_kbs'] = kb_meta_list
    meta['ragflow_total_chunks'] = total_chunks
    print(f"RAGFlow real-time: {kb_seen} KBs, {total_chunks} chunks, "
          f"{G.number_of_nodes()} nodes, {G.number_of_edges()} edges", file=sys.stderr)
    return G, meta


def normalize_sources(source: str | list[str]) -> list[str]:
    """Normalize comma-separated or list input while preserving source order."""
    raw = source.split(',') if isinstance(source, str) else source
    if not isinstance(raw, (list, tuple)):
        raise TypeError("source must be a string or list of strings")
    sources = []
    for item in raw:
        if not isinstance(item, str):
            raise TypeError("source entries must be strings")
        name = item.strip().lower()
        if not name:
            continue
        if name not in {'wiki', 'ontology', 'ragflow'}:
            raise ValueError(f"Unknown source: {name}")
        if name not in sources:
            sources.append(name)
    if not sources:
        raise ValueError("At least one source is required")
    return sources


def _source_for_graph(graph: nx.DiGraph, meta: dict) -> str:
    source = graph.graph.get('source')
    if source:
        return source
    node_attrs = [attrs for _, attrs in graph.nodes(data=True)]
    if any(attrs.get('chunk_id') for attrs in node_attrs):
        return 'ragflow'
    if any(attrs.get('type') for attrs in node_attrs):
        return 'ontology'
    if any(attrs.get('theorist') for attrs in node_attrs):
        return 'wiki'
    return str(meta.get('_source', 'unknown'))


def _node_data(graph: nx.DiGraph, meta: dict, node) -> dict:
    data = dict(meta.get(node, {})) if isinstance(meta.get(node), dict) else {}
    data.update(graph.nodes[node])
    return data


def _title_tokens(value) -> set[str]:
    return set(re.findall(r'\w+', str(value or '').strip().lower(), flags=re.UNICODE))


def _title_overlap(title_a, title_b) -> float:
    left, right = _title_tokens(title_a), _title_tokens(title_b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, str):
        stripped = value.strip().strip('[]')
        if not stripped:
            return []
        return [part.strip().strip('"\'') for part in stripped.split(',') if part.strip()]
    return [value]


def _unique(values) -> list:
    result = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _entity_ref(source: str, node, meta: dict) -> str:
    if source == 'wiki':
        data = meta.get(node, {}) if isinstance(meta.get(node), dict) else {}
        return f"wiki:{data.get('path', node)}"
    return f"{source}:{node}"


def _resolve_wiki_ontology(wiki_graph, wiki_meta, ontology_graph, ontology_meta):
    """Return wiki-to-ontology mappings plus unresolved ambiguity conflicts."""
    mapping = {}
    conflicts = []
    exact_wiki = set()

    for wiki_id in wiki_graph.nodes():
        explicit_mappings = (wiki_meta.get(wiki_id, {}) or {}).get(
            'ontology_mappings', [],
        )
        for ontology_id in explicit_mappings:
            if ontology_id in ontology_graph:
                mapping[wiki_id] = ontology_id
                exact_wiki.add(wiki_id)
                break

    wiki_by_basename = {}
    for wiki_id in wiki_graph.nodes():
        wiki_by_basename.setdefault(os.path.basename(wiki_id), []).append(wiki_id)
    for wiki_id in wiki_graph.nodes():
        if wiki_id in exact_wiki:
            continue
        ontology_id = wiki_id if wiki_id in ontology_graph \
            else os.path.basename(wiki_id)
        if ontology_id not in ontology_graph:
            continue
        if ontology_id != wiki_id \
                and len(wiki_by_basename.get(ontology_id, [])) != 1:
            continue
        mapping[wiki_id] = ontology_id
        exact_wiki.add(wiki_id)

    exact_title_by_wiki = {}
    fuzzy_by_wiki = {}
    for wiki_id in wiki_graph.nodes():
        if wiki_id in exact_wiki:
            continue
        wiki_title = _node_data(wiki_graph, wiki_meta, wiki_id).get('title', wiki_id)
        exact_title_candidates = []
        fuzzy_candidates = []
        for ontology_id in ontology_graph.nodes():
            ontology_title = _node_data(
                ontology_graph, ontology_meta, ontology_id,
            ).get('title', ontology_id)
            if str(wiki_title).strip().casefold() == \
                    str(ontology_title).strip().casefold():
                exact_title_candidates.append(ontology_id)
            elif _title_overlap(wiki_title, ontology_title) > 0.7:
                fuzzy_candidates.append(ontology_id)
        if exact_title_candidates:
            exact_title_by_wiki[wiki_id] = exact_title_candidates
        if fuzzy_candidates:
            fuzzy_by_wiki[wiki_id] = fuzzy_candidates

    exact_title_claims = {}
    for wiki_id, candidates in exact_title_by_wiki.items():
        for ontology_id in candidates:
            exact_title_claims.setdefault(ontology_id, []).append(wiki_id)
    for wiki_id, candidates in exact_title_by_wiki.items():
        if len(candidates) != 1:
            continue
        ontology_id = candidates[0]
        if len(exact_title_claims[ontology_id]) == 1 \
                and ontology_id not in mapping.values():
            mapping[wiki_id] = ontology_id
            exact_wiki.add(wiki_id)

    reserved_ontology = set(mapping.values())
    candidates_by_wiki = {}
    for wiki_id in wiki_graph.nodes():
        if wiki_id in exact_wiki:
            continue
        candidates = exact_title_by_wiki.get(wiki_id) or fuzzy_by_wiki.get(wiki_id, [])
        candidates = [item for item in candidates if item not in reserved_ontology]
        if candidates:
            candidates_by_wiki[wiki_id] = candidates

    wiki_by_ontology = {}
    for wiki_id, candidates in candidates_by_wiki.items():
        for ontology_id in candidates:
            wiki_by_ontology.setdefault(ontology_id, []).append(wiki_id)

    ambiguous_wiki = {
        wiki_id for wiki_id, candidates in candidates_by_wiki.items()
        if len(candidates) != 1
    }
    ambiguous_ontology = {
        ontology_id for ontology_id, wiki_ids in wiki_by_ontology.items()
        if len(wiki_ids) != 1
    }
    reported = set()
    for wiki_id, candidates in candidates_by_wiki.items():
        blocked = wiki_id in ambiguous_wiki or any(
            ontology_id in ambiguous_ontology for ontology_id in candidates
        )
        if not blocked:
            mapping[wiki_id] = candidates[0]
            continue
        key = (wiki_id, tuple(sorted(candidates)))
        if key in reported:
            continue
        reported.add(key)
        related_wiki = _unique([
            related
            for ontology_id in candidates
            for related in wiki_by_ontology.get(ontology_id, [])
        ])
        refs = [_entity_ref('wiki', item, wiki_meta) for item in related_wiki]
        refs.extend(_entity_ref('ontology', item, ontology_meta) for item in candidates)
        conflicts.append({
            'entity': str(wiki_id),
            'sources': _unique(refs),
            'reason': 'ambiguous_title',
            'resolution': 'unmerged',
        })

    if ontology_graph and wiki_graph:
        mapped_ontology_ids = set(mapping.values())
        unmapped_ontology_ids = set(ontology_graph.nodes()) - mapped_ontology_ids
        if unmapped_ontology_ids:
            print(
                f"[WARN] {len(unmapped_ontology_ids)} ontology entities not mapped to any wiki node:",
                file=sys.stderr,
            )
            for ontology_id in sorted(unmapped_ontology_ids)[:10]:
                print(f"  - {ontology_id}", file=sys.stderr)
            if len(unmapped_ontology_ids) > 10:
                print(
                    f"  ... and {len(unmapped_ontology_ids) - 10} more",
                    file=sys.stderr,
                )
    return mapping, conflicts


def _fuse_node_attributes(wiki_data: dict | None, ontology_data: dict | None,
                          other_data: dict | None, sources: list[str]) -> dict:
    wiki_data = wiki_data or {}
    ontology_data = ontology_data or {}
    attrs = {}
    attrs.update(wiki_data)
    attrs.update(other_data or {})
    attrs.update(ontology_data)

    attrs['title'] = ontology_data.get('title') or wiki_data.get('title') \
        or attrs.get('title')
    if ontology_data.get('type') is not None:
        attrs['type'] = ontology_data['type']
    theorist = wiki_data.get('theorist') or wiki_data.get('source_theorist')
    if theorist is not None:
        attrs['theorist'] = theorist
    tags = _unique(_as_list(wiki_data.get('tags')) + _as_list(ontology_data.get('tags')))
    if tags:
        attrs['tags'] = tags

    wiki_description = wiki_data.get('description') or wiki_data.get('definition_text')
    ontology_description = ontology_data.get('description')
    if ontology_description:
        attrs['description'] = ontology_description
        if wiki_description:
            attrs['wiki_description'] = wiki_description
    elif wiki_description:
        attrs['description'] = wiki_description
    attrs['sources'] = list(sources)
    return {key: value for key, value in attrs.items() if value is not None}


def merge_graphs(graphs: list[Tuple[nx.DiGraph, dict]]) \
        -> Tuple[nx.DiGraph, dict, list[dict]]:
    """Merge source graphs using ontology-authoritative entity resolution."""
    merged = nx.DiGraph()
    merged_meta = {}
    entries = [(_source_for_graph(graph, meta), graph, meta) for graph, meta in graphs]
    source_names = _unique(source for source, _, _ in entries)
    by_source = {source: (graph, meta) for source, graph, meta in entries}

    wiki_graph, wiki_meta = by_source.get('wiki', (nx.DiGraph(), {}))
    ontology_graph, ontology_meta = by_source.get('ontology', (nx.DiGraph(), {}))
    if wiki_graph and ontology_graph:
        wiki_mapping, conflicts = _resolve_wiki_ontology(
            wiki_graph, wiki_meta, ontology_graph, ontology_meta,
        )
    else:
        wiki_mapping, conflicts = {}, []

    node_mapping = {}
    contributions = {}
    for source, graph, meta in entries:
        for node in graph.nodes():
            target = wiki_mapping.get(node, node) if source == 'wiki' else node
            if source == 'ragflow' and target in contributions:
                target = f"ragflow:{node}"
            node_mapping[(source, node)] = target
            contribution = contributions.setdefault(target, {})
            contribution[source] = _node_data(graph, meta, node)

    for node, contribution in contributions.items():
        present_sources = [source for source in source_names if source in contribution]
        attrs = _fuse_node_attributes(
            contribution.get('wiki'), contribution.get('ontology'),
            contribution.get('ragflow'), present_sources,
        )
        merged.add_node(node, **attrs)
        merged_meta[node] = dict(attrs)

    for source, graph, _meta in entries:
        for from_node, to_node, edge_data in graph.edges(data=True):
            source_id = node_mapping[(source, from_node)]
            target_id = node_mapping[(source, to_node)]
            relation = edge_data.get('relation') or (
                'references' if source == 'wiki' else 'unspecified'
            )
            if merged.has_edge(source_id, target_id):
                current = merged[source_id][target_id]
                current['edge_sources'] = _unique(
                    _as_list(current.get('edge_sources')) + [source]
                )
                if source == 'ontology' and relation:
                    current['relation'] = relation
                for key, value in edge_data.items():
                    current.setdefault(key, value)
            else:
                attrs = dict(edge_data)
                attrs['relation'] = relation
                attrs['edge_sources'] = [source]
                merged.add_edge(source_id, target_id, **attrs)

    title_index = {}
    for node, attrs in merged.nodes(data=True):
        if 'ontology' not in attrs.get('sources', []):
            continue
        title_index.setdefault(' '.join(sorted(_title_tokens(attrs.get('title')))), []).append(node)
    for source, graph, _meta in entries:
        if source != 'ragflow':
            continue
        for chunk, attrs in graph.nodes(data=True):
            chunk_target = node_mapping[(source, chunk)]
            for entity in _as_list(attrs.get('entities')):
                target = entity if (
                    entity in merged
                    and ('ontology' not in source_names
                         or 'ontology' in merged.nodes[entity].get('sources', []))
                ) else None
                if target is None:
                    matches = title_index.get(' '.join(sorted(_title_tokens(entity))), [])
                    if len(matches) == 1:
                        target = matches[0]
                if target is not None and target != chunk_target:
                    if merged.has_edge(chunk_target, target):
                        edge = merged[chunk_target][target]
                        edge['edge_sources'] = _unique(
                            _as_list(edge.get('edge_sources')) + ['ragflow']
                        )
                    else:
                        merged.add_edge(
                            chunk_target, target, relation='mentions',
                            edge_sources=['ragflow'],
                        )

    self_loops = list(nx.selfloop_edges(merged))
    if self_loops:
        print(
            f"[WARN] fused graph contains {len(self_loops)} self-loop(s) "
            "after entity resolution:",
            file=sys.stderr,
        )
        for node, _ in self_loops[:10]:
            print(f"  - {node}", file=sys.stderr)
        if len(self_loops) > 10:
            print(f"  ... and {len(self_loops) - 10} more", file=sys.stderr)

    merged.graph.update({
        'source': ','.join(source_names),
        'sources': source_names,
        'source_nodes': {
            source: list(graph.nodes()) for source, graph, _ in entries
        },
        'entity_mappings': {'wiki': wiki_mapping},
        'merge_conflicts': conflicts,
        '_source_graphs': {source: graph for source, graph, _ in entries},
        '_source_meta': {source: meta for source, _, meta in entries},
    })
    merged_meta['_merge'] = {
        'sources': source_names,
        'entity_mappings': {'wiki': wiki_mapping},
        'conflicts': conflicts,
    }
    return merged, merged_meta, conflicts


def _write_merge_conflicts(path: str, conflicts: list[dict]) -> str:
    path = os.path.abspath(os.path.expanduser(path))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temporary = f"{path}.tmp.{os.getpid()}"
    with open(temporary, 'w', encoding='utf-8') as fh:
        json.dump(conflicts, fh, ensure_ascii=False, indent=2)
        fh.write('\n')
    os.replace(temporary, path)
    return path


def build_graph(
    source: str | list[str] = "wiki", read_only: bool = False, **kwargs,
) -> Tuple[nx.DiGraph, dict]:
    """Build one source or merge several independently built source graphs.

    ``source`` accepts ``"wiki"``, ``"ontology"``, ``"ragflow"``, a
    comma-separated string, or a list of source names.

    RAGFlow kwargs (real-time API mode):
      - ragflow_api_key: Bearer token (reads RAGFLOW_API_KEY if omitted)
      - ragflow_api_base: RAGFlow server URL (default http://localhost:9380)
      - ragflow_kb_filter: Substring to filter KB names (e.g. "kb:" for production KBs)
      - ragflow_max_kbs: Max KBs to load (default 20)
      - ragflow_max_chunks: Per-KB chunk cap (default 2000)

    Offline mode (ragflow_path or ragflow_dump_path): reads a JSONL dump file.
    When both a dump path and an API key are available, the offline dump
    takes precedence.

    Set ``read_only`` to suppress optional file output, including the merged
    graph conflict report.
    """
    sources = normalize_sources(source)
    built = []
    for source_name in sources:
        if source_name == 'wiki':
            wiki_root = kwargs.get('wiki_root')
            if not wiki_root:
                raise ValueError("source='wiki' requires wiki_root")
            built.append(_build_wiki(wiki_root))
        elif source_name == 'ontology':
            ontology_path = kwargs.get('ontology_path') or get_ontology_path()
            ontology_path = os.path.expanduser(ontology_path)
            if not os.path.exists(ontology_path):
                print(f"ERROR: {ontology_path} not found", file=sys.stderr)
                raise SystemExit(1)
            built.append(_build_ontology(ontology_path))
        else:
            # RAGFlow: an explicitly provided offline dump always wins; the
            # real-time API is used only when no dump path is given but a key
            # is available. Keeps test/CI runs hermetic even when the host
            # happens to run a live RAGFlow server with a key in the env.
            ragflow_path = kwargs.get('ragflow_path') or kwargs.get('ragflow_dump_path')
            api_key = kwargs.get('ragflow_api_key')
            if api_key is None:
                api_key = os.environ.get('RAGFLOW_API_KEY', '')
            if ragflow_path:
                ragflow_path = os.path.expanduser(ragflow_path)
                if not os.path.exists(ragflow_path):
                    print(f"ERROR: {ragflow_path} not found", file=sys.stderr)
                    raise SystemExit(1)
                built.append(_build_ragflow(ragflow_path))
            elif api_key:
                built.append(_build_ragflow_realtime(
                    api_key=api_key,
                    api_base=kwargs.get('ragflow_api_base'),
                    kb_filter=kwargs.get('ragflow_kb_filter'),
                    max_kbs=kwargs.get('ragflow_max_kbs', 20),
                    max_chunks_per_kb=kwargs.get('ragflow_max_chunks', 2000),
                ))
            else:
                built.append(_build_ragflow(ragflow_path))

    if len(built) == 1:
        graph, meta = built[0]
    else:
        graph, meta, conflicts = merge_graphs(built)
        default_dir = kwargs.get('wiki_root') or os.getcwd()
        conflict_path = kwargs.get('conflict_path') or kwargs.get('merge_conflicts_path') \
            or os.path.join(os.path.expanduser(default_dir), '.merge-conflicts.json')
        if read_only:
            written_path = None
        else:
            try:
                written_path = _write_merge_conflicts(conflict_path, conflicts)
            except OSError as exc:
                print(f"WARN: could not write merge conflicts {conflict_path}: {exc}", file=sys.stderr)
                written_path = None
        graph.graph['merge_conflicts_path'] = written_path
        meta['_merge']['conflict_path'] = written_path

    if graph.number_of_nodes() == 0 and 'wiki' in sources:
        print("ERROR: No wiki pages found", file=sys.stderr)
        raise SystemExit(1)
    return graph, meta

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Unified graph builder')
    parser.add_argument('--source', default='wiki',
                        help='wiki, ontology, ragflow, or a comma-separated list')
    parser.add_argument('--ontology-path', default=None)
    parser.add_argument(
        '--wiki-root',
        default=os.environ.get('WIKI_ROOT', default_wiki_root()),
    )
    parser.add_argument('--ragflow-path', default=None,
                        help='Offline ragflow JSONL dump path')
    parser.add_argument('--ragflow-kb-filter', default=None,
                        help='Substring filter for KB names (e.g. kb:) — real-time API')
    parser.add_argument('--ragflow-max-kbs', type=int, default=20,
                        help='Max KBs to load from API (default 20)')
    parser.add_argument('--ragflow-max-chunks', type=int, default=2000,
                        help='Per-KB chunk cap (default 2000)')
    parser.add_argument('--ragflow-api-base', default=DEFAULT_RAGFLOW_API_BASE,
                        help='RAGFlow server base URL (default http://localhost:9380)')
    args = parser.parse_args()
    G, meta = build_graph(source=args.source, ontology_path=args.ontology_path,
                          wiki_root=args.wiki_root, ragflow_path=args.ragflow_path,
                          ragflow_kb_filter=args.ragflow_kb_filter,
                          ragflow_max_kbs=args.ragflow_max_kbs,
                          ragflow_max_chunks=args.ragflow_max_chunks,
                          ragflow_api_base=args.ragflow_api_base)
    print(f"Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
    if 'ragflow' in (args.source or ''):
        kbs = meta.get('ragflow_kbs', [])
        if kbs:
            print(f"RAGFlow KBs loaded: {len(kbs)}")
            for kb in kbs[:5]:
                print(f"  {kb['name']}: {kb['nodes']} nodes / {kb['chunks_loaded']} chunks")
    if args.source == 'ontology' or 'ontology' in (args.source or ''):
        types = {}
        for n in G.nodes():
            t = G.nodes[n].get('type', 'Unknown'); types[t] = types.get(t, 0) + 1
        print("Entity types:", types)
