"""JSON files on disk (card-core): read with a default, write atomically, read-modify-write under a lock.

- ``atomic_write``: write a temp file next to the target (name carries pid + thread id), then ``os.replace``.
  Readers see the old or the new file, never half of one; two writers never share a temp file.
- ``locked``: ``fcntl.flock`` on ``.<name>.lock`` next to the target -- across processes and threads,
  re-entrant within one thread. The image skill's ``fileio`` uses the same lock file names, so both
  skills queue on one shared library.json.
- ``update_json``: lock -> re-read -> change -> atomic write. Any "read, change one thing, write back" goes through it.

Lock and temp files start with a dot (``.*.lock`` / ``.*.tmp``); project .gitignore files ignore them.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import threading
import time
from pathlib import Path

_held = threading.local()
_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def read_json(path, default):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def lock_path(path):
    path = Path(path)
    return path.parent / f".{path.name}.lock"


@contextlib.contextmanager
def locked(path):
    """Exclusive lock on ``path`` (via the lock file next to it; the file itself is not touched)."""
    lock = lock_path(path).resolve()
    key = str(lock)
    held = getattr(_held, "keys", None)
    if held is None:
        held = _held.keys = set()
    if key in held:
        yield
        return
    with _thread_locks_guard:
        thread_lock = _thread_locks.setdefault(key, threading.Lock())
    lock.parent.mkdir(parents=True, exist_ok=True)
    with thread_lock:
        with open(lock, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            held.add(key)
            try:
                yield
            finally:
                held.discard(key)
                fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_write(path, data):
    """Replace the whole file: str as UTF-8, bytes as is."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        if isinstance(data, bytes):
            tmp.write_bytes(data)
        else:
            tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_json(path, data):
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


def update_json(path, default, change):
    """Lock -> re-read (``default()`` when missing) -> ``change(data)`` in place -> atomic write. Returns (data, result)."""
    with locked(path):
        data = read_json(path, None)
        if data is None:
            data = default()
        result = change(data)
        write_json(path, data)
        return data, result


def append_jsonl(path, record):
    """Append-only log: one JSON object per line, never rewritten."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
