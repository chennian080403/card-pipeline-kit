#!/usr/bin/env python3
"""Asset-library contract -- optional. A card pipeline works without a library;
this only registers finished images so a human can review and finalise them.

Config ``library``:

    library:
      kind: http            # none (default) | http | local (folder + library.json)
      url: http://127.0.0.1:9000
      catalog_path: /api/catalog          # GET -> {"token": "..."}
      ingest_path: /api/agent/ingest      # POST
      token_env: LIB_TOKEN                # optional bearer-like token in a header
      token_header: X-Review-Token        # header name for the catalog token
      actor: agent:pipeline
      actor_header: X-Actor

The library always receives ``production_approved: false``: only the human
finalises. The pipeline never auto-approves.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import fileio


class LibraryError(RuntimeError):
    pass


def enabled(spec):
    if not spec:
        return False
    if spec.get("kind") == "local":
        return bool(spec.get("root"))
    return spec.get("kind", "none") == "http" and bool(spec.get("url"))


# --------------------------------------------------------------------------- #
# local library: a folder + library.json, no service needed
#
#   library:
#     kind: local
#     root: 素材库            # relative to the project root, or absolute
#
# Drafts are copied into <root>/草稿区/<batch>/; only a human decision
# (decide(), called from the review page) moves an entry to final / rejected.
# --------------------------------------------------------------------------- #

LOCAL_STATES = ("draft", "final", "rejected", "pending")


def local_index_path(spec):
    return Path(spec["root"]).expanduser() / "library.json"


def local_load(spec):
    path = local_index_path(spec)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"format": "image-card-pipeline/local-library-v1", "entries": []}


def _local_save(spec, index):
    fileio.write_json(local_index_path(spec), index)


def _free_target(folder, name, digest):
    """Where a new copy goes: the plain name, or ``<sha8>_<name>`` when another file already holds the name
    (same rule as card-core: a same-named image with other content never overwrites the one already there)."""
    target = folder / name
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        target = folder / f"{digest[:8]}_{name}"
    return target


def _local_ingest(spec, **fields):
    # 审核页和命令行可能同时登记：锁住 library.json 再读改写，免得后写的盖掉先写的
    with fileio.locked(local_index_path(spec)):
        return _local_ingest_locked(spec, **fields)


def _local_ingest_locked(spec, *, title, files, prompt, model, batch):
    import shutil
    import time

    root = Path(spec["root"]).expanduser()
    index = local_load(spec)
    known = {entry["sha256"]: entry for entry in index["entries"]}
    added, existing = [], []
    for item in files:
        source = Path(item)
        source = source if source.is_absolute() else root / source
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
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
                 "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "decisions": []}
        index["entries"].append(entry)
        known[digest] = entry
        added.append(entry["id"])
    _local_save(spec, index)
    return {"ok": True, "kind": "local", "added": added, "already": existing, "root": str(root)}


def local_remove(spec, entry_id, *, allow_final=False):
    """Take an entry back out of the local library (the "move back" / "discard" half of a move).

    Deletes the library's own copy under the root (unless another entry still points at the same file);
    the pipeline's original candidate is never touched. A human-finalised entry stays unless ``allow_final``
    (the human takes the finalisation back on the review page first) -- same rule as card-core.
    """
    root = Path(spec["root"]).expanduser()
    with fileio.locked(local_index_path(spec)):
        return _local_remove_locked(spec, root, entry_id, allow_final)


def _local_remove_locked(spec, root, entry_id, allow_final=False):
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


def decide(spec, entry_id, status, *, by="human", note=""):
    """Human decision on a local entry. The pipeline itself never calls this."""
    import time

    if status not in LOCAL_STATES:
        raise LibraryError(f"status must be one of {', '.join(LOCAL_STATES)}")
    with fileio.locked(local_index_path(spec)):
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


def _headers(spec):
    headers = {"Content-Type": "application/json"}
    token_header = spec.get("token_header", "X-Review-Token")
    actor = spec.get("actor")
    if actor and not actor.isascii():
        actor = "agent@pipeline"          # HTTP headers are latin-1 only
    if actor:
        headers[spec.get("actor_header", "X-Actor")] = actor
    token_env = spec.get("token_env")
    if token_env and os.getenv(token_env):
        headers[token_header] = os.environ[token_env].strip()
    return headers


def catalog_token(spec, timeout=20):
    """Fetch the write token the library hands out from its catalog endpoint."""
    url = str(spec["url"]).rstrip("/") + spec.get("catalog_path", "/api/catalog")
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as response:
        return json.loads(response.read().decode()).get("token")


def ingest(spec, *, title, files, prompt="", model=None, operation_id=None, batch=None, extra=None, timeout=30):
    """Register a finished image. ``files`` is a list of paths relative to the library root.

    Returns the library's JSON reply. Raises LibraryError on any non-2xx.
    """
    if not enabled(spec):
        raise LibraryError("no library configured")
    if spec.get("kind") == "local":
        return _local_ingest(spec, title=title, files=files, prompt=prompt, model=model, batch=batch)
    root = Path(spec["root"]).expanduser() if spec.get("root") else None
    entries = []
    for item in files:
        path = Path(item)
        absolute = path if path.is_absolute() else (root / path if root else path)
        digest = hashlib.sha256(absolute.read_bytes()).hexdigest()
        entries.append({"path": str(item), "sha256": digest})
    body = {
        "operation_id": operation_id or ("op-" + hashlib.sha1(str(files).encode()).hexdigest()[:12]),
        "title": (title or "generated")[:80],
        "batch": batch or "card-pipeline",
        "prompt": prompt,
        "requested_model": model,
        "response_model": model,
        "files": entries,
        "production_approved": False,
        "reason": "pipeline draft registration",
    }
    if extra:
        body.update(extra)
    headers = _headers(spec)
    if spec.get("token_env") is None or not os.getenv(spec["token_env"]):
        token = catalog_token(spec)
        if token:
            headers[spec.get("token_header", "X-Review-Token")] = token
    url = str(spec["url"]).rstrip("/") + spec.get("ingest_path", "/api/agent/ingest")
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        raise LibraryError(f"HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:300]}") from error
