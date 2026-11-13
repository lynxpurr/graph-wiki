"""Tests for scripts/utils.py durable file operations across platforms."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import pytest

from scripts import utils


def test_atomic_write_creates_and_replaces_file(tmp_path: Path):
    target = tmp_path / "sub" / "test.txt"
    written_path = utils.atomic_write(target, "hello world")

    assert Path(written_path) == target
    assert target.read_text(encoding="utf-8") == "hello world"

    # Overwrite existing file
    utils.atomic_write(target, b"binary content")
    assert target.read_bytes() == b"binary content"


def test_atomic_write_preserves_file_mode(tmp_path: Path):
    target = tmp_path / "script.sh"
    target.write_text("echo hi\n", encoding="utf-8")
    initial_mode = stat.S_IMODE(os.stat(target).st_mode)

    utils.atomic_write(target, "echo updated\n")
    assert target.read_text(encoding="utf-8") == "echo updated\n"
    assert stat.S_IMODE(os.stat(target).st_mode) == initial_mode


def test_atomic_append_jsonl_validates_type(tmp_path: Path):
    target = tmp_path / "test.jsonl"
    with pytest.raises(TypeError, match="line_dict must be a dict"):
        utils.atomic_append_jsonl(target, ["not", "a", "dict"])  # type: ignore


def test_atomic_append_jsonl_appends_records(tmp_path: Path):
    target = tmp_path / "records.jsonl"
    utils.atomic_append_jsonl(target, {"a": 1, "msg": "first"})
    utils.atomic_append_jsonl(target, {"b": 2, "msg": "second"})

    lines = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert lines == [
        {"a": 1, "msg": "first"},
        {"b": 2, "msg": "second"},
    ]


def test_fsync_directory_returns_safely_on_windows(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(os, "name", "nt")
    # Must not raise PermissionError or try to os.open directory
    utils._fsync_directory(str(tmp_path))


def test_fsync_directory_calls_os_fsync_on_posix(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(os, "name", "posix")
    opened_fds = []
    fsynced_fds = []
    closed_fds = []

    def mock_open(path, flags):
        opened_fds.append((path, flags))
        return 999

    def mock_fsync(fd):
        fsynced_fds.append(fd)

    def mock_close(fd):
        closed_fds.append(fd)

    monkeypatch.setattr(os, "open", mock_open)
    monkeypatch.setattr(os, "fsync", mock_fsync)
    monkeypatch.setattr(os, "close", mock_close)

    utils._fsync_directory(str(tmp_path))

    assert len(opened_fds) == 1
    assert opened_fds[0][0] == str(tmp_path)
    assert fsynced_fds == [999]
    assert closed_fds == [999]


def test_lock_and_unlock_file_with_mock_msvcrt(monkeypatch):
    class FakeMsvcrt:
        def __init__(self):
            self.LK_LOCK = 1
            self.LK_UNLCK = 2
            self.calls = []

        def locking(self, fd, mode, nbytes):
            self.calls.append((fd, mode, nbytes))

    fake = FakeMsvcrt()
    monkeypatch.setattr(utils, "fcntl", None)
    monkeypatch.setattr(utils, "msvcrt", fake)
    monkeypatch.setattr(os, "lseek", lambda fd, pos, how: 0)

    utils._lock_file(10, 1)
    utils._unlock_file(10, 1)

    assert fake.calls == [(10, fake.LK_LOCK, 1), (10, fake.LK_UNLCK, 1)]


def test_lock_file_warns_on_locking_oserror(monkeypatch):
    class FailingMsvcrt:
        def __init__(self):
            self.LK_LOCK = 1
            self.LK_UNLCK = 2

        def locking(self, fd, mode, nbytes):
            raise OSError("lock failed")

    monkeypatch.setattr(utils, "fcntl", None)
    monkeypatch.setattr(utils, "msvcrt", FailingMsvcrt())
    monkeypatch.setattr(os, "lseek", lambda fd, pos, how: 0)

    with pytest.warns(RuntimeWarning, match="file locking unavailable on descriptor 10"):
        utils._lock_file(10, 1)
    utils._unlock_file(10, 1)


def test_lock_file_warns_when_no_locking_backend(monkeypatch):
    monkeypatch.setattr(utils, "fcntl", None)
    monkeypatch.setattr(utils, "msvcrt", None)

    with pytest.warns(RuntimeWarning, match="no platform file locking mechanism available"):
        utils._lock_file(10, 1)
