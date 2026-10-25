#!/usr/bin/env python3
"""Image host abstraction -- only used when references must leave the machine.

The pipeline prefers inline references (multipart / data URLs): nothing is
published, no upload latency. A host is needed only when a provider can consume
nothing but public URLs.

Host kinds (config ``image_host.kind``):

- ``none``  (default) -- publishing disabled; callers must inline or fail loudly
- ``cli``   -- run a command per file; it must print one public URL per line on
               stdout (any image-host script that follows this contract can
               be plugged in, so implementations stay interchangeable)
- ``http``  -- POST ``{"filename": ..., "data_base64": ...}`` and read back
               ``url`` / ``public_url`` from the JSON reply
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import urllib.request
from pathlib import Path


class HostError(RuntimeError):
    pass


def _upload_cli(spec, path):
    command = spec.get("command")
    if not command:
        raise HostError("image_host.kind=cli requires a 'command'")
    timeout = int(spec.get("timeout", 60))
    args = command.split() + [str(path)]
    if spec.get("name_type"):
        args += ["--name-type", str(spec["name_type"])]
    for key in spec.get("extra_args") or []:
        args.append(str(key))
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise HostError(f"upload command failed ({proc.returncode}): {proc.stderr.strip()[:300]}")
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line.startswith(("http://", "https://")):
            return line
    raise HostError(f"upload command printed no URL: {proc.stdout.strip()[:300]}")


def _upload_http(spec, path):
    url = spec.get("url")
    if not url:
        raise HostError("image_host.kind=http requires a 'url'")
    timeout = int(spec.get("timeout", 60))
    raw = Path(path).read_bytes()
    payload = json.dumps({"filename": Path(path).name, "data_base64": base64.b64encode(raw).decode()}).encode()
    headers = {"Content-Type": "application/json"}
    token_env = spec.get("token_env")
    if token_env and os.getenv(token_env):
        headers["Authorization"] = "Bearer " + os.environ[token_env].strip()
    request = urllib.request.Request(url, data=payload, method="POST", headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read())
    data = result.get("data") or result
    return data.get("public_url") or data.get("url")


def upload(spec, path):
    """Publish one local file, returning its public URL."""
    kind = (spec or {}).get("kind", "none")
    if kind == "cli":
        return _upload_cli(spec, path)
    if kind == "http":
        return _upload_http(spec, path)
    raise HostError("image_host.kind is 'none': publishing is disabled -- inline the references instead")


def upload_many(spec, paths, parallel=True):
    """Return (urls, failures). Order is preserved; one failure never aborts the rest."""
    paths = list(paths)
    urls = [None] * len(paths)
    failures = []
    if (spec or {}).get("kind", "none") == "none":
        return urls, [{"path": str(p), "error": "publishing disabled"} for p in paths]

    def one(index, path):
        try:
            urls[index] = upload(spec, path)
        except Exception as error:  # noqa: BLE001 - partial success is the point
            failures.append({"path": str(path), "error": f"{type(error).__name__}: {error}"})

    if parallel and len(paths) > 1:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=int((spec or {}).get("concurrency", 8))) as pool:
            list(pool.map(lambda item: one(*item), list(enumerate(paths))))
    else:
        for item in enumerate(paths):
            one(*item)
    return urls, failures
