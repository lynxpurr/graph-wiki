"""Focused tests for graph-test-suite metric implementations."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import networkx as nx
from scipy.stats import spearmanr


def _load_graph_test_suite():
    script = Path(__file__).resolve().parents[1] / "scripts" / "graph-test-suite.py"
    spec = importlib.util.spec_from_file_location("graph_test_suite", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_t10_uses_spearman_correlation_with_average_tie_ranks():
    suite = _load_graph_test_suite()
    graph = nx.DiGraph()
    graph.add_nodes_from(["a", "b", "c", "d"])
    graph.add_edges_from([
        ("a", "b"), ("a", "c"), ("b", "d"), ("c", "d"),
    ])
    pagerank = {"a": 0.4, "b": 0.1, "c": 0.1, "d": 0.3}
    nodes = list(graph.nodes())
    expected = spearmanr(
        [pagerank[node] for node in nodes],
        [graph.in_degree(node) for node in nodes],
    )[0]
    suite.G = graph
    suite.pr = pagerank

    score, detail = suite.t10()

    assert score == 5
    assert f"{expected:.2%}" in detail
    assert "Spearman" in detail


def test_t10_handles_undefined_constant_ranks():
    suite = _load_graph_test_suite()
    graph = nx.DiGraph()
    graph.add_nodes_from(["a", "b"])
    suite.G = graph
    suite.pr = {"a": 0.5, "b": 0.5}

    score, detail = suite.t10()

    assert score == 5
    assert "0.00%" in detail


def test_t5_high_reciprocity_keeps_echo_chamber_penalty_and_warning():
    suite = _load_graph_test_suite()
    graph = nx.DiGraph()
    graph.add_edges_from([
        ("a", "b"), ("b", "a"), ("a", "c"),
    ])
    suite.G = graph

    score, detail = suite.t5()

    assert score == 6
    assert "66.7%" in detail
    assert "回音室风险" in detail


def test_t5_reciprocity_between_35_and_50_is_acceptable():
    suite = _load_graph_test_suite()
    graph = nx.DiGraph()
    graph.add_edges_from([
        ("a", "b"), ("b", "a"), ("c", "d"), ("d", "c"),
        ("e", "f"), ("f", "g"), ("g", "h"), ("h", "i"),
        ("i", "j"), ("j", "k"),
    ])
    suite.G = graph

    score, detail = suite.t5()

    assert score == 8
    assert "40.0%" in detail
    assert "回音室风险" not in detail


def test_streams_share_destination_handles_zero_inode_and_same_stream(monkeypatch):
    suite = _load_graph_test_suite()

    class FakeStat:
        def __init__(self, st_dev: int, st_ino: int):
            self.st_dev = st_dev
            self.st_ino = st_ino

    class FakeStream:
        def __init__(self, fileno_val: int):
            self._fileno = fileno_val

        def fileno(self) -> int:
            return self._fileno

    # Identical object
    s1 = FakeStream(1)
    assert suite._streams_share_destination(s1, s1) is True

    # Same fileno
    s2 = FakeStream(1)
    assert suite._streams_share_destination(s1, s2) is True

    orig_fstat = suite.os.fstat
    fake_map = {}

    def mock_fstat(fd):
        if fd in fake_map:
            return fake_map[fd]
        return orig_fstat(fd)

    monkeypatch.setattr(suite.os, "fstat", mock_fstat)

    # Different fileno, both st_ino == 0 (e.g. Windows pipes or devices)
    # Must NOT report as sharing destination!
    fake_map[101] = FakeStat(0, 0)
    fake_map[102] = FakeStat(0, 0)
    assert suite._streams_share_destination(FakeStream(101), FakeStream(102)) is False

    # Different fileno, one st_ino == 0
    fake_map[101] = FakeStat(1, 0)
    fake_map[102] = FakeStat(1, 123)
    assert suite._streams_share_destination(FakeStream(101), FakeStream(102)) is False

    # Different fileno, different non-zero inodes
    fake_map[101] = FakeStat(1, 100)
    fake_map[102] = FakeStat(1, 101)
    assert suite._streams_share_destination(FakeStream(101), FakeStream(102)) is False

    # Different fileno, same dev and same non-zero inode (e.g. 2>&1 on POSIX)
    fake_map[101] = FakeStat(1, 500)
    fake_map[102] = FakeStat(1, 500)
    assert suite._streams_share_destination(FakeStream(101), FakeStream(102)) is True

    # Stream raising OSError or missing fileno
    class BadStream:
        pass

    assert suite._streams_share_destination(BadStream(), BadStream()) is False

