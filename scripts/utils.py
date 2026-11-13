#!/usr/bin/env python3
"""Durable file-write helpers shared by wiki-graph scripts."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from typing import Any
import warnings

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore

try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore


def _fsync_directory(directory: str) -> None:
    if os.name == "nt":
        return
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _lock_file(fd: int, nbytes: int = 1) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX)
    elif msvcrt is not None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, nbytes)
        except OSError as exc:
            warnings.warn(
                f"atomic_append_jsonl: file locking unavailable on descriptor {fd} ({exc}); proceeding unlocked",
                RuntimeWarning,
                stacklevel=2,
            )
    else:
        warnings.warn(
            "atomic_append_jsonl: no platform file locking mechanism available; proceeding unlocked",
            RuntimeWarning,
            stacklevel=2,
        )


def _unlock_file(fd: int, nbytes: int = 1) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    elif msvcrt is not None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, nbytes)
        except OSError:
            pass


def atomic_write(path: str | os.PathLike[str], content: str | bytes) -> str:
    """Atomically replace a file with complete content and fsync the result."""
    target = os.path.abspath(os.fspath(path))
    directory = os.path.dirname(target) or os.curdir
    os.makedirs(directory, exist_ok=True)

    existing_mode = None
    try:
        existing_mode = stat.S_IMODE(os.stat(target, follow_symlinks=False).st_mode)
    except FileNotFoundError:
        pass

    binary = isinstance(content, bytes)
    fd, tmp_path = tempfile.mkstemp(prefix=".wiki-graph-", dir=directory)
    try:
        mode = "wb" if binary else "w"
        kwargs = {} if binary else {"encoding": "utf-8"}
        with os.fdopen(fd, mode, **kwargs) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if existing_mode is not None:
            os.chmod(tmp_path, existing_mode)
        os.replace(tmp_path, target)
        _fsync_directory(directory)
        return target
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def atomic_append_jsonl(path: str | os.PathLike[str], line_dict: dict[str, Any]) -> str:
    """Append one JSON object under an exclusive process lock and fsync it."""
    if not isinstance(line_dict, dict):
        raise TypeError("line_dict must be a dict")

    target = os.path.abspath(os.fspath(path))
    directory = os.path.dirname(target) or os.curdir
    os.makedirs(directory, exist_ok=True)
    encoded = (json.dumps(line_dict, ensure_ascii=False) + "\n").encode("utf-8")

    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        _lock_file(fd)
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        try:
            _unlock_file(fd)
        finally:
            os.close(fd)
    _fsync_directory(directory)
    return target

