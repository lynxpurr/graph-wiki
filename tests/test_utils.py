"""Tests for scripts/utils.py durable file operations across platforms."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
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


def test_lock_file_propagates_locking_oserror(monkeypatch):
    class FailingMsvcrt:
        def __init__(self):
            self.LK_LOCK = 1
            self.LK_UNLCK = 2

        def locking(self, fd, mode, nbytes):
            raise OSError("lock failed")

    monkeypatch.setattr(utils, "fcntl", None)
    monkeypatch.setattr(utils, "msvcrt", FailingMsvcrt())
    monkeypatch.setattr(os, "lseek", lambda fd, pos, how: 0)

    with pytest.raises(OSError, match="lock failed"):
        utils._lock_file(10, 1)


def test_lock_file_fails_when_no_locking_backend(monkeypatch):
    monkeypatch.setattr(utils, "fcntl", None)
    monkeypatch.setattr(utils, "msvcrt", None)

    with pytest.raises(OSError, match="no platform file locking mechanism available") as caught:
        utils._lock_file(10, 1)
    assert caught.value.errno == errno.ENOSYS


@pytest.mark.parametrize('backend', ['windows', 'posix', 'none', 'seek'])
def test_failed_lock_never_appends_or_unlocks_and_closes_fd(monkeypatch, tmp_path, backend):
    target = tmp_path / 'records.jsonl'
    original = b'{"existing": true}\n'
    target.write_bytes(original)
    lock_error = OSError(errno.EACCES, 'lock denied')
    calls = []
    closed_fds = []
    real_close = os.close

    class FailingWindows:
        LK_LOCK = 1

        @staticmethod
        def locking(fd, mode, nbytes):
            calls.append('lock')
            raise lock_error

    class FailingPosix:
        LOCK_EX = 2

        @staticmethod
        def flock(fd, mode):
            calls.append('lock')
            raise lock_error

    def failed_seek(fd, offset, whence):
        raise lock_error

    def unexpected_call(*args):
        pytest.fail('Failed acquisition must not write, fsync, or unlock')

    def close(fd):
        closed_fds.append(fd)
        real_close(fd)

    monkeypatch.setattr(utils, 'fcntl', FailingPosix() if backend == 'posix' else None)
    monkeypatch.setattr(utils, 'msvcrt', FailingWindows() if backend in ('windows', 'seek') else None)
    if backend == 'seek':
        monkeypatch.setattr(os, 'lseek', failed_seek)
    monkeypatch.setattr(utils, '_unlock_file', unexpected_call)
    monkeypatch.setattr(os, 'write', unexpected_call)
    monkeypatch.setattr(os, 'fsync', unexpected_call)
    monkeypatch.setattr(os, 'close', close)

    with pytest.raises(OSError) as caught:
        utils.atomic_append_jsonl(target, {'new': True})
    if backend != 'none':
        assert caught.value is lock_error
    else:
        assert caught.value.errno == errno.ENOSYS
    assert calls == (['lock'] if backend in ('windows', 'posix') else [])
    assert target.read_bytes() == original
    assert len(closed_fds) == 1
    with pytest.raises(OSError):
        os.fstat(closed_fds[0])


@pytest.mark.parametrize('failure_point', ['write', 'fsync', 'unlock'])
def test_append_releases_lock_and_closes_fd_after_io_failure(monkeypatch, tmp_path, failure_point):
    target = tmp_path / 'records.jsonl'
    events = []
    closed_fds = []
    real_close = os.close
    io_error = OSError(errno.EIO, 'injected failure')

    def lock(fd):
        events.append('lock')

    def write(fd, data):
        events.append('write')
        if failure_point == 'write':
            raise io_error
        return len(data)

    def fsync(fd):
        events.append('fsync')
        if failure_point == 'fsync':
            raise io_error

    def unlock(fd):
        events.append('unlock')
        if failure_point == 'unlock':
            raise io_error

    def close(fd):
        events.append('close')
        closed_fds.append(fd)
        real_close(fd)

    monkeypatch.setattr(utils, '_lock_file', lock)
    monkeypatch.setattr(utils, '_unlock_file', unlock)
    monkeypatch.setattr(os, 'write', write)
    monkeypatch.setattr(os, 'fsync', fsync)
    monkeypatch.setattr(os, 'close', close)
    with pytest.raises(OSError) as caught:
        utils.atomic_append_jsonl(target, {'new': True})
    assert caught.value is io_error
    expected = ['lock', 'write']
    if failure_point != 'write':
        expected.append('fsync')
    assert events == expected + ['unlock', 'close']
    with pytest.raises(OSError):
        os.fstat(closed_fds[0])


def test_concurrent_append_serializes_partial_writes(tmp_path):
    target = tmp_path / 'concurrent.jsonl'
    # Force each JSON record to require several writes. Without mutual exclusion,
    # separate processes can interleave bytes even when the fd uses O_APPEND.
    worker = '''
import sys, time
from scripts import utils
real_write = utils.os.write
def partial_write(fd, data):
    written = real_write(fd, data[:64])
    time.sleep(0.001)
    return written
utils.os.write = partial_write
for index in range(8):
    utils.atomic_append_jsonl(sys.argv[1], {
        'worker': int(sys.argv[2]), 'index': index, 'payload': 'x' * 1000,
    })
'''
    processes = [
        subprocess.Popen(
            [sys.executable, '-c', worker, str(target), str(worker_id)],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for worker_id in range(3)
    ]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, (stdout, stderr)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate()
    records = [json.loads(line) for line in target.read_text(encoding='utf-8').splitlines()]
    assert len(records) == 24
    assert {(row['worker'], row['index']) for row in records} == {
        (worker_id, index) for worker_id in range(3) for index in range(8)
    }
    assert all(row['payload'] == 'x' * 1000 for row in records)
