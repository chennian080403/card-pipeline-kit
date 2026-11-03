#!/usr/bin/env python3
"""Built-in review station page: watch takes, give verdicts, write notes, mark moments.

    python3 review_web.py --config project.yaml [--port 8861]

Only binds 127.0.0.1. Writes only the station files (<output>/station/station.json and the
append-only activity-log.jsonl); `vpipeline.py sync` pulls the verdicts into the pipeline.
Never generates, never spends.

Writes follow card-core's review protocol (the same gate any station plugin uses):
X-Review-Token from /api/state (403), X-Actor user@browser | human:<name> (400; agents 403),
the revision the page was showing (409 when stale).
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stations  # noqa: E402
from config import SKILL_ROOT, Config  # noqa: E402
from core import station as protocol  # noqa: E402

PAGE = SKILL_ROOT / "web" / "review.html"


def media_url(path):
    return "/media?path=" + quote(str(path)) if path else None


def state(cfg, station):
    data = station.load()
    takes, allowed = [], set()
    for path in sorted(cfg.takes_dir.glob("*.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        entry = data["takes"].get(meta["name"])
        if entry is None:
            continue
        first = (meta.get("refs") or {}).get("first_frame") or {}
        files = [meta.get("file"), meta.get("sheet"), first.get("path")]
        allowed.update(str(Path(f).resolve()) for f in files if f)
        takes.append({
            "name": meta["name"], "card": meta.get("card"), "title": meta.get("title", ""), "group": meta.get("group") or "",
            "model": meta.get("model"), "mode": meta.get("mode"), "prompt": meta.get("prompt", ""),
            "probe": meta.get("probe") or {}, "derived_from": meta.get("derived_from"), "kind": meta.get("kind", "take"),
            "video": media_url(meta.get("file")), "sheet": media_url(meta.get("sheet")),
            "first_frame": media_url(first.get("path")), "at": meta.get("at"),
            "segments": entry.get("segments") or [], "reviews": entry.get("reviews") or {},
            "note": entry.get("note", ""), "marks": entry.get("marks") or [], "history": entry.get("history") or [],
            "verdict": (station.fetch(meta, {"key": meta["name"]}) or {}).get("verdict"),
        })
    return {"project": cfg.name, "takes": takes, "revision": data["revision"], "token": Handler.token}, allowed


class Handler(BaseHTTPRequestHandler):
    cfg = None
    station = None
    token = protocol.new_token()
    allowed = set()

    def log_message(self, fmt, *args):
        pass

    def reply(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if url.path == "/api/state":
            data, Handler.allowed = state(self.cfg, self.station)
            return self.reply(data)
        if url.path == "/media":
            return self.media((parse_qs(url.query).get("path") or [""])[0])
        self.reply({"error": "not found"}, 404)

    def media(self, raw):
        path = Path(raw).resolve()
        if str(path) not in Handler.allowed:
            _, Handler.allowed = state(self.cfg, self.station)
        if str(path) not in Handler.allowed or not path.is_file():
            return self.reply({"error": "not allowed"}, 403)
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end = 0, size - 1
        match = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if match:
            if match[1]:
                start = int(match[1])
                end = int(match[2]) if match[2] else size - 1
            elif match[2]:
                start = max(size - int(match[2]), 0)
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            try:
                while remaining > 0:
                    chunk = handle.read(min(1 << 16, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_POST(self):
        if urlparse(self.path).path != "/api/review":
            return self.reply({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length < 200_000:
                raise protocol.StationError("bad request size")
            body = json.loads(self.rfile.read(length))
            entry, revision = self.station.record(str(body["name"]), body, token=self.headers.get("X-Review-Token"),
                                                  expected_token=Handler.token, actor=self.headers.get("X-Actor"))
            self.reply({"ok": True, "revision": revision, "reviews": entry.get("reviews")})
        except protocol.StationError as error:
            self.reply({"error": str(error)}, error.code)
        except (ValueError, KeyError, TypeError) as error:
            self.reply({"error": f"bad request: {error}"}, 400)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=os.getenv("VIDEO_CARD_CONFIG", "project.yaml"))
    parser.add_argument("--port", type=int, default=8861)
    args = parser.parse_args()
    cfg = Config(args.config)
    station = stations.make(cfg)
    if not isinstance(station, stations.BuiltinStation):
        raise SystemExit("this project uses a plugin station -- review there; this page serves station.kind: builtin")
    Handler.cfg, Handler.station = cfg, station
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:  # 端口被别的项目的审核页占着：别打开别人的页面当成自己的
        raise SystemExit(f"port {args.port} is in use ({e.strerror}); another review page may be running there — "
                         f"pass a free one, e.g. --port {args.port + 10}")
    print(f"review station: http://127.0.0.1:{args.port}/  ({cfg.name})")
    server.serve_forever()


if __name__ == "__main__":
    main()
