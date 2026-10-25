# card-core copy -- do not edit here: edit card-core/core (next to this skill), then run card-core/sync.py
#!/usr/bin/env python3
"""Asset-library contract (card-core) -- optional; a card pipeline works without a library.
Ecosystem contract v1: image and video pipelines may point at one library; entries are
told apart by ``media`` (image / video) and linked by ``derived_from``.

Config ``library`` (top level, shared by both skills):

    library:
      kind: http            # none (default) | http | local (folder + library.json)
      url: http://127.0.0.1:9000
      root: /path/to/library-project       # file paths are relative to it
      catalog_path: /api/catalog          # GET -> {"token": "..."}
      ingest_path: /api/agent/ingest      # POST
      drop_path: /api/agent/drop          # optional: upload bytes first when the file lives outside root
      token_env: LIB_TOKEN
      token_header: X-Review-Token
      actor: agent:pipeline
      actor_header: X-Actor

The library always receives ``production_approved: false``: only the human
finalises. The pipeline never auto-approves.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .jsonio import locked, write_json

LOCAL_FORMAT = "image-card-pipeline/local-library-v1"   # shared on-disk format, keep the name
LOCAL_STATES = ("draft", "final", "rejected", "pending")
VIDEO_SUFFIXES = {".mp4", ".webm", ".mov"}


class LibraryError(RuntimeError):
    pass


def enabled(spec):
    if not spec:
        return False
    if spec.get("kind") == "local":
        return bool(spec.get("root"))
    return spec.get("kind", "none") == "http" and bool(spec.get("url"))


def _digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# local library: a folder + library.json, no service needed
# --------------------------------------------------------------------------- #

def local_index_path(spec):
    return Path(spec["root"]).expanduser() / "library.json"


def local_load(spec):
    path = local_index_path(spec)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"format": LOCAL_FORMAT, "entries": []}


def _local_save(spec, index):
    write_json(local_index_path(spec), index)


def _free_target(folder, name, digest):
    """Where a new copy goes: the plain name, or ``<sha8>_<name>`` when another file already holds the name."""
    target = folder / name
    if target.exists() and _digest(target) != digest:
        target = folder / f"{digest[:8]}_{name}"
    return target


def _local_ingest(spec, *, title, files, prompt, model, batch, extra):
    # every read-modify-write of library.json happens under the shared lock (the image skill uses the same one)
    with locked(local_index_path(spec)):
        return _local_ingest_locked(spec, title=title, files=files, prompt=prompt, model=model, batch=batch, extra=extra)


def _local_ingest_locked(spec, *, title, files, prompt, model, batch, extra):
    import shutil

    root = Path(spec["root"]).expanduser()
    index = local_load(spec)
    known = {entry["sha256"]: entry for entry in index["entries"]}
    added, existing = [], []
    for item in files:
        source = Path(item)
        source = source if source.is_absolute() else root / source
        digest = _digest(source)
        if digest in known:
            existing.append(known[digest]["id"])
            continue
        folder = root / "草稿区" / (batch or "card-pipeline")
        folder.mkdir(parents=True, exist_ok=True)
        target = _free_target(folder, source.name, digest)
        shutil.copy2(source, target)
        entry = {"id": digest[:8], "title": (title or "generated")[:80], "batch": batch or "card-pipeline",
                 "file": str(target.relative_to(root)), "sha256": digest, "prompt": prompt,
                 "model": model, "status": "draft", "production_approved": False,
                 "media": "video" if source.suffix.lower() in VIDEO_SUFFIXES else "image",
                 "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "decisions": []}
        for key in ("derived_from", "review", "card"):
            if extra and extra.get(key):
                entry[key] = extra[key]
        index["entries"].append(entry)
        known[digest] = entry
        added.append(entry["id"])
    _local_save(spec, index)
    return {"ok": True, "kind": "local", "added": added, "already": existing, "root": str(root)}


def decide(spec, entry_id, status, *, by="human", note=""):
    """Human decision on a local entry. The pipeline itself never calls this."""
    if status not in LOCAL_STATES:
        raise LibraryError(f"status must be one of {', '.join(LOCAL_STATES)}")
    with locked(local_index_path(spec)):
        index = local_load(spec)
        for entry in index["entries"]:
            if entry["id"] == entry_id:
                entry["status"] = status
                entry["production_approved"] = status == "final"
                entry["decisions"].append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "by": by,
                                           "status": status, "note": note})
                _local_save(spec, index)
                return entry
    raise LibraryError(f"no library entry {entry_id}")


def local_remove(spec, entry_id, *, allow_final=False):
    """Take an entry back out of the local library (the "move back" / "discard" half of a move).

    Deletes the library's own copy under the root (unless another entry still points at the same file);
    the pipeline's original file is never touched. A human-finalised entry stays unless ``allow_final``
    (the human took the finalisation back on the review page first).
    """
    root = Path(spec["root"]).expanduser()
    with locked(local_index_path(spec)):
        index = local_load(spec)
        for n, entry in enumerate(index["entries"]):
            if entry["id"] == entry_id:
                if entry.get("status") == "final" and not allow_final:
                    raise LibraryError(f"{entry_id} is finalised by a human; take that back on the review page first")
                copy = root / entry["file"]
                shared = any(other is not entry and other.get("file") == entry["file"] for other in index["entries"])
                if not shared and copy.is_file() and copy.resolve().is_relative_to(root.resolve()):
                    copy.unlink()
                del index["entries"][n]
                _local_save(spec, index)
                return entry
    raise LibraryError(f"no entry {entry_id}")


# --------------------------------------------------------------------------- #
# http library
# --------------------------------------------------------------------------- #

def _headers(spec, content_type="application/json"):
    headers = {"Content-Type": content_type}
    token_header = spec.get("token_header", "X-Review-Token")
    actor = spec.get("actor")
    if actor and not actor.isascii():
        actor = "agent@pipeline"          # HTTP headers are latin-1 only
    if actor:
        headers[spec.get("actor_header", "X-Actor")] = actor
    token_env = spec.get("token_env")
    if token_env and os.getenv(token_env):
        headers[token_header] = os.environ[token_env].strip()
    else:
        token = catalog_token(spec)
        if token:
            headers[token_header] = token
    return headers


def _open(request, timeout):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # local service: never via proxy
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        raise LibraryError(f"HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:300]}") from error


def catalog_token(spec, timeout=20):
    url = str(spec["url"]).rstrip("/") + spec.get("catalog_path", "/api/catalog")
    return _open(urllib.request.Request(url), timeout).get("token")


def _drop(spec, path, timeout):
    """Upload bytes into the library's inbox; returns its project-relative path."""
    url = (str(spec["url"]).rstrip("/") + spec["drop_path"]
           + "?filename=" + urllib.parse.quote(path.name))
    headers = _headers(spec, "application/octet-stream")
    request = urllib.request.Request(url, data=path.read_bytes(), method="POST", headers=headers)
    return _open(request, timeout)


def ingest(spec, *, title, files, prompt="", model=None, operation_id=None, batch=None, extra=None, timeout=60):
    """Register finished media as drafts. ``files`` are absolute paths or paths relative to the library root."""
    if not enabled(spec):
        raise LibraryError("no library configured")
    if spec.get("kind") == "local":
        return _local_ingest(spec, title=title, files=files, prompt=prompt, model=model, batch=batch, extra=extra)
    root = Path(spec["root"]).expanduser() if spec.get("root") else None
    entries = []
    for item in files:
        path = Path(item)
        absolute = path if path.is_absolute() else (root / path if root else path)
        inside = root and str(absolute.resolve()).startswith(str(root.resolve()) + os.sep)
        if inside:
            entries.append({"path": str(absolute.resolve().relative_to(root.resolve())), "sha256": _digest(absolute)})
        elif spec.get("drop_path"):
            reply = _drop(spec, absolute, timeout)
            entries.append({"path": reply["path"], "sha256": reply["sha256"]})
        else:
            raise LibraryError(f"{absolute} is outside the library root and no drop_path is configured")
    body = {
        "operation_id": operation_id or ("op-" + hashlib.sha1(json.dumps(entries).encode()).hexdigest()[:12]),
        "title": (title or "generated")[:80],
        "batch": batch or "card-pipeline",
        "prompt": prompt,
        "requested_model": model,
        "response_model": model,
        "files": entries,
        "production_approved": False,
        "reason": (extra or {}).get("reason") or "pipeline draft registration",
    }
    url = str(spec["url"]).rstrip("/") + spec.get("ingest_path", "/api/agent/ingest")
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=_headers(spec))
    return _open(request, timeout)
