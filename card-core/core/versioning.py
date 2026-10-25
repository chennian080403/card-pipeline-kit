"""Project versions (card-core): the project folder is a git repository, no .bak files.

A write command saves once before it runs (catching hand edits by a human or an AI)
and once after. Nothing happens when the folder is not its own repository, or when
the project turns it off (``autosave: false``). Restoring is plain git (or the `版本` tool).

Only the project folder's own repository counts: a project that merely sits inside a
bigger repository is left alone, so its files never land in someone else's history.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

_LOCK = threading.Lock()


def git(root, *argv):
    return subprocess.run(["git", "-C", str(root), *argv], capture_output=True, text=True)


def tracked(root):
    """True when ``root`` is the top of its own git repository."""
    root = Path(root)
    if not root.is_dir():
        return False
    top = git(root, "rev-parse", "--show-toplevel")
    return top.returncode == 0 and Path(top.stdout.strip()).resolve() == root.resolve()


def save(root, message, enabled=True):
    """Commit everything under ``root`` when something changed. Returns whether a version was saved."""
    if not enabled or not tracked(root):
        return False
    with _LOCK:
        if git(root, "add", "-A", "--", ".").returncode != 0:
            return False
        if git(root, "diff", "--cached", "--quiet", "--", ".").returncode == 0:
            return False
        done = git(root, "-c", "user.name=AI", "-c", "user.email=ai@local", "commit", "-q",
                   "-m", message[:200], "--", ".")
        return done.returncode == 0


def init(root, gitignore_text):
    """Make ``root`` a repository (with a .gitignore) unless it already is one. Returns lines describing what it did."""
    root = Path(root)
    wrote = []
    ignore = root / ".gitignore"
    if not ignore.exists():
        ignore.write_text(gitignore_text, encoding="utf-8")
        wrote.append(str(ignore))
    if not tracked(root):
        if git(root, "rev-parse", "--is-inside-work-tree").returncode == 0:
            wrote.append(f"(inside another git repository: versions are not saved here)")
        elif git(root, "init", "-q").returncode == 0:
            wrote.append(f"{root}/.git  (version history: git log / 版本 历史)")
    return wrote


def describe(root):
    """One line for doctor."""
    if not tracked(root):
        return False, "not a git repository (versions are not saved; `init` sets it up)"
    count = git(root, "rev-list", "--count", "HEAD")
    dirty = git(root, "status", "--porcelain", "--", ".").stdout.strip()
    n = count.stdout.strip() if count.returncode == 0 else "0"
    return True, f"git · {n} version(s)" + (" · uncommitted changes" if dirty else "")
