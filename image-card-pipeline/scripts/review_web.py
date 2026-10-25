#!/usr/bin/env python3
"""Review + library page -- where the human shows up.

Two entries over one library (left column):
  短效 · 定调   参考区 / 试样区 -- settle what the long line will reuse
  长效 · 复用   出图区 / 素材区 / 真源区 / 废稿区
Each image has three buttons for the human: 要 (keep) / 不要 / 定为真源 ▸.

    python3 scripts/review_web.py --config project.yaml --port 8860

Binds 127.0.0.1 only. Never generates (no spend from this page). Every write
goes through a pipeline command (`keep`, `review`, `pack`, `promote`, ...), so the
page can do nothing the CLI cannot -- except finalising in the library, which is
the human's alone. promote needs an approval line here too.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import mimetypes
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import library
import pipeline as pl

SKILL_ROOT = Path(__file__).resolve().parent.parent
PAGE = SKILL_ROOT / "web" / "review.html"
CONFIG_PATH = "project.yaml"
WRITE_LOCK = threading.Lock()


def cfg():
    return pl.Config(CONFIG_PATH)


def run(func, **fields):
    """Call a pipeline command; return (ok, printed output)."""
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            func(cfg(), argparse.Namespace(**fields))
        return True, buffer.getvalue().strip()
    except SystemExit as stop:
        return False, (buffer.getvalue() + "\n" + str(stop)).strip()
    except Exception as error:  # noqa: BLE001 - report, never crash the page
        return False, (buffer.getvalue() + f"\n{type(error).__name__}: {error}").strip()


def state():
    return pl.project_state(cfg())


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, content_type="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/state":
            return self.send(200, state())
        if url.path == "/file":
            rel = (parse_qs(url.query).get("path") or [""])[0]
            root = cfg().root
            path = (root / rel).resolve()
            if root not in path.parents or not path.is_file():
                return self.send(404, {"error": "not found"})
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if not mime.startswith("image/"):
                return self.send(403, {"error": "images only"})
            return self.send(200, path.read_bytes(), mime)
        return self.send(404, {"error": "not found"})

    def _local_request(self):
        """写接口只认本机页面：Host 必须是 127.0.0.1 / localhost（挡 DNS 重绑定），带了 Origin 也得是它们
        （挡别的网站在浏览器里偷偷 POST 过来）。"""
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if (self.headers.get("Host") or "").lower() not in allowed:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin.lower() in {"http://" + host for host in allowed}

    def do_POST(self):
        if not self._local_request():
            return self.send(403, {"ok": False, "output": "only the local review page may write"})
        # 只收 JSON：表单 / text/plain 是浏览器跨站不用预检就能发的类型
        if (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower() != "application/json":
            return self.send(415, {"ok": False, "output": "Content-Type must be application/json"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send(400, {"ok": False, "output": "bad json"})
        path = urlparse(self.path).path
        try:
            return self.dispatch(path, body)
        except KeyError as missing:
            return self.send(400, {"ok": False, "output": f"missing field: {missing}"})

    def dispatch(self, path, body):
        # 写操作一个一个来：命令的输出靠 redirect_stdout 截（全进程共用），并排跑会串台；
        # 和命令行进程之间靠文件锁（fileio）排队。读（/api/state、看图）不受影响，照样并发。
        # commit: false = 备注写到一半的自动存 / 翻版本途中：只落文件，不存版本；收起备注时再存一版（免得「版本 历史」被刷屏）
        commit = body.get("commit") is not False
        with WRITE_LOCK:
            # 和命令行一样：动手前存一次（收住手改），做完再存一次
            if commit and path != "/api/note":   # 备注前面那几次只落文件的自动存，要算进这一版，不能被当成手改先存走
                pl.save_version(cfg(), "手动改动（审核页操作前自动存）")
            try:
                status, payload = self._dispatch(path, body)
            finally:
                if commit:
                    target = body.get("candidate") or body.get("card") or body.get("id") or body.get("as") or ""
                    pl.save_version(cfg(), f"审核页：{path.rsplit('/', 1)[-1]} {target}".strip())
        return self.send(status, payload)

    def _dispatch(self, path, body):
        if path == "/api/review":
            ok, out = run(pl.cmd_review, card=body["card"], candidate=body["candidate"],
                          verdict=body["verdict"], note=body.get("note", ""), by="human")
            if ok and body["verdict"] == "fail" and body.get("note"):
                run(pl.cmd_feedback, card=body["card"], verdict="revise", reason=f"{body['candidate']}: {body['note']}",
                    root_cause="", by="human (review page)")
        elif path == "/api/promote":
            ok, out = run(pl.cmd_promote, candidate=body["candidate"], as_=body["as"], approved=body.get("approved", ""),
                          face=None, text=body.get("text") or None, role=body.get("role") or None, look=body.get("look") or None,
                          note=body.get("note", ""), force=bool(body.get("force")))
        elif path == "/api/describe":
            ok, out = run(pl.cmd_describe, as_=body["as"], text=body.get("text", ""), approved=body.get("approved", ""))
        elif path == "/api/keep":
            # 「要」= 通过 + 进素材区，一步到位（按钮只留 要 / 不要 / 定为真源）
            ok, out = run(pl.cmd_keep, card=body["card"], candidate=body["candidate"], note=body.get("note", ""), by="human")
        elif path == "/api/pack":
            ok, out = run(pl.cmd_pack, card=body["card"], pack=body.get("pack") or "")
        elif path in ("/api/library/return", "/api/library/discard"):
            ok, out = run(pl.cmd_unregister, card=body["card"], candidate=body["candidate"],
                          discard=path.endswith("discard"), note=body.get("note", ""))
        elif path == "/api/unreview":
            ok, out = run(pl.cmd_unreview, card=body["card"], candidate=body["candidate"])
        elif path == "/api/note":
            ok, out = run(pl.cmd_note, card=body["card"], candidate=body["candidate"], note=body.get("note", ""),
                          no_history=body.get("history") is False)
        elif path == "/api/demote":
            ok, out = run(pl.cmd_demote, as_=body["as"], force=bool(body.get("force")), discard=bool(body.get("discard")))
        elif path == "/api/library/decide":
            try:
                entry = library.decide(cfg().library, body["id"], body["status"], by="human (review page)",
                                       note=body.get("note", ""))
                ok, out = True, f"{entry['id']} -> {entry['status']}"
            except library.LibraryError as error:
                ok, out = False, str(error)
        else:
            return 404, {"ok": False, "output": "unknown endpoint"}
        return (200 if ok else 400), {"ok": ok, "output": out}


def main():
    global CONFIG_PATH
    parser = argparse.ArgumentParser(description="review + library page for image-card-pipeline")
    parser.add_argument("--config", default="project.yaml")
    parser.add_argument("--port", type=int, default=8860)
    args = parser.parse_args()
    CONFIG_PATH = str(Path(args.config).expanduser().resolve())
    if not Path(CONFIG_PATH).is_file():
        raise SystemExit(f"config not found: {CONFIG_PATH}")
    # 多线程：读并发；写在 dispatch 里经 WRITE_LOCK 排队
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:  # 端口被别的项目的审核页占着：别打开别人的页面当成自己的
        raise SystemExit(f"port {args.port} is in use ({e.strerror}); another review page may be running there — "
                         f"pass a free one, e.g. --port {args.port + 10}")
    print(f"review page: http://127.0.0.1:{args.port}/  ({CONFIG_PATH})")
    server.serve_forever()


if __name__ == "__main__":
    main()
