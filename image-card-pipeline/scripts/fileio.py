#!/usr/bin/env python3
"""并发安全的文件读写：几个 generate 同时跑、审核页和命令行同时点，都不能写坏或丢记录。

- ``atomic_write``：先写同目录临时文件（名字带进程号和线程号），再 ``os.replace``。读的一方要么看到旧版、要么看到新版，看不到写了一半的。
- ``locked``：``fcntl.flock`` 锁住目标旁边的 ``.<文件名>.lock``，跨进程、跨线程都排队。同一线程重复进同一把锁不会自己卡死。
- ``update_json``：锁住 → 重新读 → 改 → 原子写回。任何「读出来改一条再写回」都走它，避免后写的覆盖先写的。

锁文件、临时文件都以点开头（``.*.lock`` / ``.*.tmp``），项目 .gitignore 里忽略它们。
写法与 card-core 的 jsonio 同源；以后生图接入 card-core 时换成那边的。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import threading
from pathlib import Path

_held = threading.local()        # 本线程已经拿着的锁（按锁文件路径），让嵌套不自锁
_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


def _lock_path(path):
    path = Path(path)
    return path.parent / f".{path.name}.lock"


@contextlib.contextmanager
def locked(path):
    """独占锁住 ``path`` 这份文件（锁的是旁边的锁文件，不碰文件本身）。"""
    lock = _lock_path(path).resolve()
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
    """整份替换：str 按 UTF-8 写，bytes 原样写。"""
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


def read_json(path, default):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def write_json(path, data):
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


def update_json(path, default, change):
    """锁住 → 重新读（没有就用 ``default()``）→ ``change(data)`` 原地改 → 原子写回。返回 (data, change 的返回值)。"""
    with locked(path):
        data = read_json(path, None)
        if data is None:
            data = default()
        result = change(data)
        write_json(path, data)
        return data, result
