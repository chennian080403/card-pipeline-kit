#!/usr/bin/env python3
"""自检：在临时项目里把视频流水线跑一遍（只用 mock / 本地命令，不联网、不花钱）。

    python3 scripts/selfcheck.py [--keep] [--extra 档案自检.py …]

覆盖：画风真源描述、真源种类可配置（v1.4）、底座（版本、任务状态机、付费闸门、unknown 不重提）、审片协议（token / 署名 / revision /
逐段 / 留痕）、同步与挑选、素材库草稿、后期派生、剪辑单、网页交接、生图联动契约（只读生图的
产出、只调生图的公开命令，不改生图代码）、card-core 副本一致。
--extra 加跑某个接入档案自带的检查（文件里定义 checks(sc)，返回检查函数列表；sc 就是本模块）。
只装了视频、旁边没有生图 Skill 或 card-core 源头时，相关检查记「跳过」，不算失败。

每项独立判定，一项失败不影响后面。结束后删除临时目录（--keep 保留），并核对 Skill 目录没被改动。
有失败时退出码为 1。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

SKILL = Path(__file__).resolve().parent.parent
VPIPELINE = SKILL / "scripts" / "vpipeline.py"
REVIEW_WEB = SKILL / "scripts" / "review_web.py"
MYSKILLS = SKILL.parent
IMAGE_SKILL = MYSKILLS / "image-card-pipeline"
CARD_CORE = MYSKILLS / "card-core"
CARD = "01-01"


class Skip(Exception):
    """A check that does not apply to this install (e.g. the sibling skill is not there)."""


def brief(proc, limit=400):
    text = re.sub(r"\s+", " ", ((proc.stdout or "") + (proc.stderr or "")).strip())
    return text[-limit:]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def wait_http(url, seconds=10):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            http("GET", url)
            return True
        except Exception:   # noqa: BLE001
            time.sleep(0.2)
    return False


def image_skill_status():
    return subprocess.run(["git", "-C", str(MYSKILLS), "status", "--porcelain", "--", IMAGE_SKILL.name],
                          capture_output=True, text=True).stdout


def skill_status():
    return subprocess.run(["git", "-C", str(MYSKILLS), "status", "--porcelain", "--", SKILL.name],
                          capture_output=True, text=True).stdout


class Ctx:
    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.config = self.tmp / "project.yaml"

    def run(self, *args, config=None, timeout=240):
        return subprocess.run([sys.executable, str(VPIPELINE), "--config", str(config or self.config), *args],
                              cwd=str(self.tmp), capture_output=True, text=True, timeout=timeout)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.tmp), *args], capture_output=True, text=True)

    def commits(self):
        out = self.git("rev-list", "--count", "HEAD").stdout.strip()
        return int(out or 0)

    def manifest(self):
        return json.loads((self.tmp / "video-output/manifest.json").read_text(encoding="utf-8"))

    def review(self, card=CARD):
        path = self.tmp / "video-output/review" / f"{card}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def card_path(self, card=CARD):
        return next((self.tmp / "video-cards").glob(f"{card}_*.yaml"))

    def edit_config(self, change):
        data = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        change(data)
        self.config.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def make_card(ctx, card_id, title, **fields):
    """new-card, then set fields (a field set to None is removed)."""
    if not list((ctx.tmp / "video-cards").glob(f"{card_id}_*.yaml")):
        ctx.run("new-card", card_id, title)
    path = ctx.card_path(card_id)
    card = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key, value in fields.items():
        if value is None:
            card.pop(key, None)
        else:
            card[key] = value
    path.write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# checks: each returns a list of problems (empty = pass)
# --------------------------------------------------------------------------- #

def c_init(ctx):
    """init 建项目：.git / .gitignore（片子不进版本），doctor 通过且报 version: git"""
    p = []
    proc = ctx.run("init")
    if proc.returncode:
        return [f"init 退出码 {proc.returncode}: {brief(proc)}"]
    if not (ctx.tmp / ".git").is_dir():
        p.append("没有 .git")
    ignore = (ctx.tmp / ".gitignore").read_text(encoding="utf-8") if (ctx.tmp / ".gitignore").is_file() else ""
    if "*.mp4" not in ignore:
        p.append(".gitignore 没有排除 *.mp4")
    doctor = ctx.run("doctor")
    if doctor.returncode or "version" not in doctor.stdout or "git ·" not in doctor.stdout:
        p.append(f"doctor 没报 version: git（{brief(doctor)}）")
    # paid entry (local command) and an entry that fails "after billing", for the job checks below
    sample = ctx.tmp / "sample.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24",
                    "-t", "1", "-pix_fmt", "yuv420p", str(sample)], check=True)
    copier = f"{sys.executable} -c \"import shutil,sys; shutil.copy(sys.argv[1], sys.argv[2])\" {sample} {{out}}"

    def change(data):
        data["video"]["providers"]["entries"]["paid"] = {"kind": "command", "command": copier}
        data["video"]["providers"]["entries"]["flaky"] = {"kind": "command", "command": "false", "billed_on_failure": True,
                                                         "free": True}
        data["video"]["models"]["paid-cli"] = {"provider": "paid"}
        data["video"]["models"]["flaky-cli"] = {"provider": "flaky"}
        data["video"]["hooks"]["post"] = [{"name": "copy", "command": f"{sys.executable} -c \"import shutil,sys; "
                                           f"shutil.copy(sys.argv[1], sys.argv[2])\" {{in}} {{out}}"}]
        data["library"] = {"kind": "local", "root": "素材库"}
    ctx.edit_config(change)
    ctx.git("add", "-A")
    ctx.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "selfcheck config")
    return p


def c_card(ctx):
    """new-card + 填 motion → validate 通过、状态记 confirmed"""
    proc = ctx.run("new-card", CARD, "自检镜头")
    if proc.returncode:
        return [f"new-card: {brief(proc)}"]
    path = ctx.card_path()
    card = yaml.safe_load(path.read_text(encoding="utf-8"))
    card["motion"] = "Slow push in; wind moves the grass."
    path.write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")
    proc = ctx.run("validate", CARD)
    status = yaml.safe_load(ctx.card_path().read_text(encoding="utf-8")).get("status")
    return [] if proc.returncode == 0 and status == "confirmed" else [f"validate 退出码 {proc.returncode}, status={status}: {brief(proc)}"]


def c_styles(ctx):
    """画风真源描述：卡写 styles → sources.yaml 的 prompt 拼进提示词；没登记报错、没描述只提示；改描述指纹变"""
    sources = ctx.tmp / "sources.yaml"
    backup = sources.read_text(encoding="utf-8") if sources.is_file() else None

    def registry(text):
        data = yaml.safe_load(sources.read_text(encoding="utf-8")) if sources.is_file() else {}
        data = data or {}
        data.setdefault("styles", {}).update({"水墨": {"source": "x.png", "look": "画风", "prompt": text},
                                              "无描述": {"source": "x.png"}})
        sources.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def card(styles):
        path = ctx.card_path("05-01")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data.update(motion="Slow push in.", styles=styles)
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    p = []
    try:
        registry("Ink wash painting, muted palette.")
        ctx.run("new-card", "05-01", "画风描述")
        card(["水墨"])
        prompt = ctx.run("prompt", "05-01").stdout
        if "Ink wash painting, muted palette." not in prompt or prompt.find("Ink wash") > prompt.find("Slow push in."):
            p.append(f"描述没拼进提示词（或不在 motion 之前）: {prompt[:200]}")
        card(["没登记"])
        bad = ctx.run("validate", "05-01")
        if bad.returncode == 0 or "not registered" not in bad.stdout + bad.stderr:
            p.append(f"styles 写了没登记的名字，validate 没报错: {brief(bad)}")
        card(["无描述"])
        ok = ctx.run("validate", "05-01")
        if ok.returncode or "has no prompt" not in ok.stdout:
            p.append(f"登记了但没描述：应通过且提示: {brief(ok)}")
        card(["水墨"])
        ctx.run("generate", "05-01", "--budget", "1")
        registry("Ink wash painting, warm dusk light.")
        ctx.run("generate", "05-01", "--budget", "2")
        metas = [ctx.tmp / f"video-output/takes/05-01_dry_{n}.json" for n in (1, 2)]
        if not all(m.is_file() for m in metas):
            return p + ["dry 出片失败"]
        one, two = (json.loads(m.read_text(encoding="utf-8")) for m in metas)
        if "muted palette" not in one["prompt"] or "warm dusk" not in two["prompt"]:
            p.append("take 记录的提示词没带上当时的描述")
        if one.get("card_fingerprint") == two.get("card_fingerprint"):
            p.append("改了描述，卡片指纹没变")
    finally:
        if backup is None:
            sources.unlink(missing_ok=True)
        else:
            sources.write_text(backup, encoding="utf-8")
    return p


def c_kinds(ctx):
    """真源种类可配置：sources.kinds 加「场景」→ 卡里 scenes 随片送参考图；加带描述的种类 → 描述进提示词；source:scenes/<名> 能解析"""
    sources = ctx.tmp / "sources.yaml"
    backup = sources.read_text(encoding="utf-8") if sources.is_file() else None
    config_backup = ctx.config.read_text(encoding="utf-8")
    frame = ctx.tmp / "城门.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=64x64", "-frames:v", "1", str(frame)],
                   check=True)
    p = []
    try:
        ctx.edit_config(lambda data: data.setdefault("sources", {}).update(
            kinds={"scenes": {"label": "场景", "as": "scene"}, "lenses": {"label": "镜头语言", "as": "lens", "text": True}}))
        data = (yaml.safe_load(sources.read_text(encoding="utf-8")) if sources.is_file() else None) or {}
        data["scenes"] = {"城门": {"source": "城门.png"}}
        data["lenses"] = {"推镜": {"prompt": "Slow dolly in, shallow depth of field."}}
        sources.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        ctx.run("new-card", "06-01", "新种类")
        path = ctx.card_path("06-01")
        card = yaml.safe_load(path.read_text(encoding="utf-8"))
        card.update(motion="Pan left.", scenes=["城门"], lenses=["推镜"], refs={"images": ["source:scenes/城门"]})
        path.write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")
        val = ctx.run("validate", "06-01")
        if val.returncode or "(scene:城门)" not in val.stdout or "城门.png" not in val.stdout:
            p.append(f"scenes: [城门] 没作参考图送出: {brief(val)}")
        prompt = ctx.run("prompt", "06-01").stdout
        if "Slow dolly in" not in prompt:
            p.append(f"带描述的新种类没拼进提示词: {prompt[:160]}")
        card["scenes"] = ["没登记"]
        path.write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")
        bad = ctx.run("validate", "06-01")
        if bad.returncode == 0:
            p.append("scenes 写了没登记的名字，validate 没报错")
    finally:
        ctx.config.write_text(config_backup, encoding="utf-8")
        if backup is None:
            sources.unlink(missing_ok=True)
        else:
            sources.write_text(backup, encoding="utf-8")
    return p


def c_generate(ctx):
    """generate --budget 2 出 2 条并自动送审片台；重跑 0 条新片（ok 幂等）"""
    p = []
    proc = ctx.run("generate", CARD, "--budget", "2")
    takes = sorted((ctx.tmp / "video-output/takes").glob("*.mp4"))
    if proc.returncode or len(takes) != 2:
        return [f"应出 2 条，实际 {len(takes)}: {brief(proc)}"]
    station = json.loads((ctx.tmp / "video-output/station/station.json").read_text(encoding="utf-8"))
    if len(station["takes"]) != 2 or station.get("format") != "video-card-pipeline/station-v2":
        p.append(f"审片台应收到 2 条（station-v2），实际 {len(station['takes'])} / {station.get('format')}")
    again = ctx.run("generate", CARD, "--budget", "2")
    if len(list((ctx.tmp / "video-output/takes").glob("*.mp4"))) != 2 or "ok already" not in again.stdout:
        p.append(f"重跑又出片了: {brief(again)}")
    return p


def c_paid(ctx):
    """付费入口：不带、占位或过短的 --approved 被拒、不提交；写清了才提交并记 approvals"""
    p = []
    refused = ctx.run("generate", CARD, "--model", "paid-cli", "--budget", "1")
    if refused.returncode == 0 or any(k.startswith(f"{CARD}|paid-cli") for k in ctx.manifest()["jobs"]):
        p.append(f"没 --approved 也提交了: {brief(refused)}")
    for bad in ("...", "<谁/何时>", "自检"):
        weak = ctx.run("generate", CARD, "--model", "paid-cli", "--budget", "1", "--approved", bad)
        if weak.returncode == 0 or any(k.startswith(f"{CARD}|paid-cli") for k in ctx.manifest()["jobs"]):
            p.append(f"占位或过短的 --approved \"{bad}\" 也提交了")
    ok = ctx.run("generate", CARD, "--model", "paid-cli", "--budget", "1", "--approved", "自检/今天")
    manifest = ctx.manifest()
    if ok.returncode or manifest["jobs"].get(f"{CARD}|paid-cli|1", {}).get("state") != "ok":
        p.append(f"带 --approved 没出片: {brief(ok)}")
    if not any(a.get("by") == "自检/今天" for a in manifest.get("approvals", [])):
        p.append("approvals 里没记批准人")
    return p


def c_unknown(ctx):
    """状态不明（可能已扣费）不重提；forget 必须写原因；forget 后才会再提交"""
    p = []
    first = ctx.run("generate", CARD, "--model", "flaky-cli", "--budget", "1")
    key = f"{CARD}|flaky-cli|1"
    if ctx.manifest()["jobs"].get(key, {}).get("state") != "unknown":
        return [f"失败后应记 unknown: {brief(first)}"]
    run_dirs = len(list((ctx.tmp / "video-output/runs").iterdir()))
    submitted_at = ctx.manifest()["jobs"][key].get("submitted_at")
    again = ctx.run("generate", CARD, "--model", "flaky-cli", "--budget", "1")
    if ("Not resubmitting" not in again.stdout or ctx.manifest()["jobs"][key].get("submitted_at") != submitted_at
            or len(list((ctx.tmp / "video-output/runs").iterdir())) != run_dirs):
        p.append(f"unknown 被重提了: {brief(again)}")
    nowhy = ctx.run("forget", key, "--why", " ")
    if nowhy.returncode == 0:
        p.append("forget 不写原因也通过了")
    done = ctx.run("forget", key, "--why", "自检：人看过 run 目录，没出片")
    if done.returncode or key in ctx.manifest()["jobs"] or not ctx.manifest().get("forgotten"):
        p.append(f"forget 没生效或没留底: {brief(done)}")
    return p


def c_collect(ctx):
    """--no-wait 只提交；collect 接着轮询收片，不重提"""
    def change(data):
        data["video"]["providers"]["entries"]["mock"]["delay"] = 2
    ctx.edit_config(change)
    ctx.run("new-card", "02-01", "续轮询")
    path = ctx.card_path("02-01")
    card = yaml.safe_load(path.read_text(encoding="utf-8"))
    card["motion"] = "Static shot."
    path.write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")
    proc = ctx.run("generate", "02-01", "--budget", "1", "--no-wait")
    state = ctx.manifest()["jobs"].get("02-01|dry|1", {}).get("state")
    p = [] if state == "submitted" else [f"--no-wait 后应是 submitted，实际 {state}: {brief(proc)}"]
    time.sleep(2.2)
    collect = ctx.run("collect")
    state = ctx.manifest()["jobs"].get("02-01|dry|1", {}).get("state")
    if state != "ok":
        p.append(f"collect 后应是 ok，实际 {state}: {brief(collect)}")
    if len(list((ctx.tmp / "video-output/runs").glob("02-01-dry-1-*"))) != 1:
        p.append("collect 重新提交了任务")
    ctx.edit_config(lambda data: data["video"]["providers"]["entries"]["mock"].pop("delay", None))
    return p


def c_station(ctx, state):
    """审片页：缺 token 403、Agent 署名 403、缺署名 400、过期 revision 409；逐段结论 + 留痕"""
    p = []
    port = free_port()
    server = subprocess.Popen([sys.executable, str(REVIEW_WEB), "--config", str(ctx.config), "--port", str(port)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    base = f"http://127.0.0.1:{port}"
    try:
        if not wait_http(base + "/api/state"):
            return [f"审片页没起来: {server.stderr.read() if server.poll() is not None else ''}"]
        _, data = http("GET", base + "/api/state")
        name = f"{CARD}_dry_1"
        rev, token = data["revision"], data["token"]
        body = {"name": name, "revision": rev, "segment": 0, "verdict": "good", "liked": "推近稳", "disliked": ""}
        cases = [({}, 403, "缺 token"), ({"X-Review-Token": token, "X-Actor": "claude-code:selfcheck"}, 403, "Agent 署名"),
                 ({"X-Review-Token": token}, 400, "缺署名")]
        for headers, code, label in cases:
            got, _ = http("POST", base + "/api/review", body, headers)
            if got != code:
                p.append(f"{label} 应 {code}，实际 {got}")
        human = {"X-Review-Token": token, "X-Actor": "human:selfcheck"}
        got, out = http("POST", base + "/api/review", dict(body, note="整体可以", marks=[{"t": 0.5, "text": "这里好"}]), human)
        if got != 200:
            p.append(f"人的整条结论没存上 {got} {out}")
        got, _ = http("POST", base + "/api/review", dict(body, verdict="bad"), human)   # same revision again
        if got != 409:
            p.append(f"过期 revision 应 409，实际 {got}")
        _, data = http("GET", base + "/api/state")
        got, _ = http("POST", base + "/api/review", {"name": f"{CARD}_dry_2", "revision": data["revision"], "segment": 0,
                                                    "verdict": "bad", "disliked": "人脸糊"}, human)
        if got != 200:
            p.append(f"第二条不满意没存上 {got}")
        log = ctx.tmp / "video-output/station/activity-log.jsonl"
        lines = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
        if len(lines) != 2 or json.loads(lines[0]).get("actor") != "human:selfcheck":
            p.append(f"留痕应 2 行且记署名，实际 {len(lines)} 行")
        state["review_base"] = base
    finally:
        server.terminate()
        server.wait(timeout=5)
    return p


def c_sync(ctx):
    """sync：满意 → pick，不满意 → 不 pick；满意 / 不满意的话带回流水线"""
    p = []
    proc = ctx.run("sync")
    review = ctx.review()
    if review.get("pick") != [f"{CARD}_dry_1"]:
        p.append(f"pick 应只有 {CARD}_dry_1，实际 {review.get('pick')}: {brief(proc)}")
    human = (review.get("takes", {}).get(f"{CARD}_dry_1") or {}).get("human") or {}
    if human.get("verdict") != "pass" or "整条满意：推近稳" not in human.get("note", "") or not human.get("marks"):
        p.append(f"取回的结论不全: {human}")
    bad = (review.get("takes", {}).get(f"{CARD}_dry_2") or {}).get("human") or {}
    if bad.get("verdict") != "fail" or "不满意：人脸糊" not in bad.get("note", ""):
        p.append(f"不满意那条取回不对: {bad}")
    return p


def c_segments(ctx):
    """逐段：剪辑单成片按镜头分段；只审段时 全满意=通过、有不满意无满意=不通过、混合=待定"""
    p = []
    cut = ctx.tmp / "cuts" / "自检.yaml"
    cut.write_text(yaml.safe_dump({"title": "自检成片", "width": 640, "height": 360, "fps": 24, "crossfade": 0.3,
                                   "shots": [{"take": f"{CARD}_dry_1"}, {"take": f"{CARD}_dry_2"}]},
                                  allow_unicode=True), encoding="utf-8")
    first = ctx.run("assemble", "自检")
    second = ctx.run("assemble", "自检")
    finals = sorted(p_.name for p_ in (ctx.tmp / "video-output/final").glob("*.mp4"))
    if finals != ["自检_v01.mp4", "自检_v02.mp4"]:
        p.append(f"成片应 _v01/_v02 递增不覆盖，实际 {finals}: {brief(first)} {brief(second)}")
    sys.path.insert(0, str(SKILL / "scripts"))
    import stations   # noqa: E402
    from config import Config   # noqa: E402
    station = stations.make(Config(ctx.config))
    data = station.load()
    entry = data["takes"].get("自检_v01") or {}
    if len(entry.get("segments") or []) != 2:
        return p + [f"成片应分 2 段，实际 {entry.get('segments')}"]
    token = "t"
    for segment, verdict in ((1, "good"), (2, "bad")):
        station.record("自检_v01", {"revision": station.load()["revision"], "segment": segment, "verdict": verdict,
                                   "liked": "衔接顺" if verdict == "good" else "", "disliked": "跳了" if verdict == "bad" else ""},
                       token=token, expected_token=token, actor="human:selfcheck")
    got = station.fetch({"name": "自检_v01"}, {"key": "自检_v01"})
    if (got or {}).get("verdict") != "borderline" or len(got.get("segments") or []) != 2:
        p.append(f"一段满意一段不满意应为待定: {got}")
    station.record("自检_v01", {"revision": station.load()["revision"], "segment": 2, "verdict": "good"},
                   token=token, expected_token=token, actor="human:selfcheck")
    if station.fetch({"name": "自检_v01"}, {"key": "自检_v01"})["verdict"] != "pass":
        p.append("两段都满意应为通过")
    station.record("自检_v01", {"revision": station.load()["revision"], "segment": 0, "verdict": "discard"},
                   token=token, expected_token=token, actor="human:selfcheck")
    got = station.fetch({"name": "自检_v01"}, {"key": "自检_v01"})
    if got["verdict"] != "fail" or not got["discard"]:
        p.append(f"整条放弃应以整条为准（不通过 + 放弃）: {got}")
    if not data["takes"]["自检_v01"].get("submitted_at"):
        p.append("成片没自动送审片台")
    return p


def c_migration(ctx):
    """审片台旧格式（station-v1）自动迁移：通过 / 不通过 / 待定 / 放弃 都按原意取回，署名和时间不丢"""
    sys.path.insert(0, str(SKILL / "scripts"))
    import stations   # noqa: E402
    from config import Config   # noqa: E402
    old = ctx.tmp / "旧审片台"
    (old / "video-output/station").mkdir(parents=True)
    (old / "project.yaml").write_text("video: {station: {kind: builtin}}\n", encoding="utf-8")
    v1 = {"format": "video-card-pipeline/station-v1", "takes": {
        "a": {"card": "x", "file": "a.mp4", "verdict": "pass", "note": "好", "by": "human@browser", "at": "t1"},
        "b": {"card": "x", "file": "b.mp4", "verdict": "fail", "note": "", "by": "human@browser", "at": "t2"},
        "c": {"card": "x", "file": "c.mp4", "verdict": "borderline", "note": "再看看", "by": "human@browser", "at": "t3"},
        "d": {"card": "x", "file": "d.mp4", "verdict": "borderline", "note": "", "by": "human@browser", "at": "t4"},
        "e": {"card": "x", "file": "e.mp4", "verdict": "fail", "discard": True, "by": "human@browser", "at": "t5"},
        "f": {"card": "x", "file": "f.mp4"}}}
    (old / "video-output/station/station.json").write_text(json.dumps(v1), encoding="utf-8")
    station = stations.make(Config(old / "project.yaml"))
    want = {"a": ("pass", False), "b": ("fail", False), "c": ("borderline", False), "d": ("borderline", False),
            "e": ("fail", True)}
    p = []
    for name, (verdict, discard) in want.items():
        got = station.fetch({"name": name}, {"key": name}) or {}
        if got.get("verdict") != verdict or got.get("discard") != discard or not got.get("by") or not got.get("at"):
            p.append(f"{name}: 应 {verdict}/{'放弃' if discard else '-'}，取回 {got}")
    if "再看看" not in (station.fetch({"name": "c"}, {"key": "c"}) or {}).get("note", ""):
        p.append("旧备注没带回")
    if station.fetch({"name": "f"}, {"key": "f"}) is not None:
        p.append("没审过的条不该有结论")
    return p


def c_post(ctx):
    """后期钩子：出派生条 <条>__<钩子>，原片不动、重跑不重做"""
    p = []
    before = (ctx.tmp / f"video-output/takes/{CARD}_dry_1.mp4").read_bytes()
    proc = ctx.run("post", f"{CARD}_dry_1", "copy")
    derived = ctx.tmp / f"video-output/takes/{CARD}_dry_1__copy.json"
    if proc.returncode or not derived.is_file():
        return [f"没出派生条: {brief(proc)}"]
    if json.loads(derived.read_text(encoding="utf-8")).get("derived_from") != f"{CARD}_dry_1":
        p.append("派生条没记 derived_from")
    again = ctx.run("post", f"{CARD}_dry_1", "copy")
    if "not redone" not in again.stdout:
        p.append(f"派生条被重做: {brief(again)}")
    if (ctx.tmp / f"video-output/takes/{CARD}_dry_1.mp4").read_bytes() != before:
        p.append("原片被改了")
    return p


def c_register(ctx):
    """register：pick 的条进本地素材库，一律草稿（production_approved=false），带 media / 审片结论 / 来源"""
    proc = ctx.run("register", CARD)
    index = ctx.tmp / "素材库" / "library.json"
    if proc.returncode or not index.is_file():
        return [f"register 失败: {brief(proc)}"]
    entries = json.loads(index.read_text(encoding="utf-8"))["entries"]
    p = []
    if len(entries) != 1:
        p.append(f"应登记 1 条，实际 {len(entries)}")
    for e in entries:
        if e.get("status") != "draft" or e.get("production_approved") is not False:
            p.append(f"条目不是草稿: {e.get('status')} {e.get('production_approved')}")
        if e.get("media") != "video" or (e.get("review") or {}).get("verdict") != "pass" or e.get("card") != CARD:
            p.append(f"条目缺 media / review / card: {e}")
    if json.loads(index.read_text(encoding="utf-8")).get("format") != "image-card-pipeline/local-library-v1":
        p.append("素材库格式名变了（契约要求沿用 image-card-pipeline/local-library-v1）")
    return p


def c_handoff(ctx):
    """网页交接：generate 出交接包、记 waiting_import、不重提；import 收回后进审片台；import 默认复制不删源（--move 才删）、
    不给 --model 时只有一个等待任务就对上、多个报错；输出目录里的文件拒收"""
    p = []
    proc = ctx.run("generate", CARD, "--model", "web-manual", "--budget", "1")
    job = ctx.manifest()["jobs"].get(f"{CARD}|web-manual|1", {})
    package = Path(job.get("handle", {}).get("package", "/nonexistent"))
    if job.get("state") != "waiting_import" or not (package / "提示词.txt").is_file():
        return [f"交接包没生成或状态不对 {job.get('state')}: {brief(proc)}"]
    again = ctx.run("generate", CARD, "--model", "web-manual", "--budget", "1")
    if "not resubmitted" not in again.stdout:
        p.append("waiting_import 被重提")
    clip = ctx.tmp / "网页下载.mp4"
    shutil.copy2(ctx.tmp / "sample.mp4", clip)
    imp = ctx.run("import", CARD, str(clip))          # no --model: the card's only waiting job
    if ctx.manifest()["jobs"][f"{CARD}|web-manual|1"].get("state") != "ok":
        p.append(f"不给 --model、只有一个等待任务时 import 没对上它: {brief(imp)}")
    if not clip.exists():
        p.append("import 默认把源文件删了（应复制、--move 才删）")
    inside = ctx.tmp / "video-output/takes" / f"{CARD}_web-manual_1.mp4"
    refused = ctx.run("import", CARD, str(inside), "--model", "web-manual")
    if refused.returncode == 0 or not inside.exists():
        p.append(f"输出目录里的文件也被 import 了: {brief(refused)}")
    ctx.run("generate", CARD, "--model", "web-manual", "--budget", "3")       # #2 and #3 now wait
    several = ctx.run("import", CARD, str(clip))
    jobs_now = ctx.manifest()["jobs"]
    if several.returncode == 0 or jobs_now.get(f"{CARD}|web-manual|2", {}).get("state") != "waiting_import":
        p.append(f"有两个等待任务、不给 --model 时应报错让人选: {brief(several)}")
    moved = ctx.run("import", CARD, str(clip), "--model", "web-manual", "--move")
    jobs_now = ctx.manifest()["jobs"]
    if jobs_now.get(f"{CARD}|web-manual|2", {}).get("state") != "ok" or clip.exists():
        p.append(f"--move 应收回最早的等待任务并删源文件: {brief(moved)}")
    ctx.run("forget", f"{CARD}|web-manual|3", "--why", "自检：不用了")
    return p


def c_versions(ctx):
    """版本：写命令前后自动存；只读命令不产生提交；没有 .bak；跑完工作区干净"""
    p = []
    log = ctx.git("log", "--format=%s").stdout
    for want in ("自动存：generate", "自动存：sync", "自动存：register"):
        if want not in log:
            p.append(f"提交里没有「{want}」")
    card = ctx.card_path()
    original = card.read_text(encoding="utf-8")
    card.write_text(original + "# 自检：手改未存\n", encoding="utf-8")     # a pending hand edit
    before = ctx.commits()
    for args in (("cards",), ("status",), ("show", CARD), ("prompt", CARD), ("doctor",), ("providers",),
                 ("board", "status")):
        ctx.run(*args)
    if ctx.commits() != before:
        p.append(f"只读命令产生了 {ctx.commits() - before} 个提交（把手改存走了）")
    card.write_text(original, encoding="utf-8")
    if list(ctx.tmp.rglob("*.bak*")):
        p.append("项目里出现了 .bak 文件")
    dirty = ctx.git("status", "--porcelain").stdout.strip()
    if dirty:
        p.append(f"工作区不干净: {dirty[:200]}")
    if ctx.git("ls-files", "*.mp4").stdout.strip():
        p.append("片子进了版本库")
    return p


def c_image_contract(ctx):
    """生图联动：首帧写 image:<图卡> 取生图 pick 的那张；首帧问题经生图公开命令回写生图卡"""
    if not (IMAGE_SKILL / "scripts" / "pipeline.py").is_file():
        raise Skip("旁边没有 image-card-pipeline（只装了视频）")
    img = ctx.tmp / "生图项目"
    img.mkdir()

    def image(*args):
        return subprocess.run([sys.executable, str(IMAGE_SKILL / "scripts/pipeline.py"), "--config",
                               str(img / "project.yaml"), *args], cwd=str(img), capture_output=True, text=True, timeout=240)
    image("init")
    image("new-card", "I-01", "首帧图")
    for image_card in (img / "cards").glob("I-01_*.yaml"):     # image validate wants something to draw
        data = yaml.safe_load(image_card.read_text(encoding="utf-8")) or {}
        data["action"] = "A quiet field at dawn."
        image_card.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    image("compose", "I-01", "--force")
    gen = image("generate", "I-01", "--budget", "1", "--approved", "自检批准一张")
    cands = sorted((img / "output/candidates").glob("I-01_*"))
    if not cands:
        return [f"生图 mock 没出图: {brief(gen)}"]
    image("pick", "I-01", cands[0].stem)
    image_before = {str(p.relative_to(img)): p.read_bytes() for p in (img / "output").rglob("*") if p.is_file()}
    image_skill_before = image_skill_status()
    ctx.edit_config(lambda data: data["video"].setdefault("link", {}).update(
        image_config=str(img / "project.yaml"), image_skill=str(IMAGE_SKILL)))
    ctx.run("new-card", "03-01", "接生图", "--first-frame", "image:I-01")
    path = ctx.card_path("03-01")
    card = yaml.safe_load(path.read_text(encoding="utf-8"))
    card["motion"] = "Slow pan."
    path.write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")
    p = []
    val = ctx.run("validate", "03-01")
    if val.returncode or "first_frame" not in val.stdout or "[image]" not in val.stdout:
        p.append(f"image:I-01 没解析成首帧: {brief(val)}")
    gen = ctx.run("generate", "03-01", "--budget", "1")
    meta = ctx.tmp / "video-output/takes/03-01_dry_1.json"
    if not meta.is_file():
        p.append(f"接生图首帧出片失败: {brief(gen)}")
    else:
        origin = (json.loads(meta.read_text(encoding="utf-8"))["refs"]["first_frame"] or {}).get("origin") or {}
        if origin.get("kind") != "image" or origin.get("card") != "I-01":
            p.append(f"take 没记首帧来源: {origin}")
    fb = ctx.run("feedback", "03-01", "reject", "--reason", "首帧手型不对", "--root-cause", "frame", "--take", "03-01_dry_1")
    image_card = next((img / "cards").glob("I-01_*.yaml"), None) if (img / "cards").is_dir() else None
    text = image_card.read_text(encoding="utf-8") if image_card else ""
    if "首帧手型不对" not in text or "视频 03-01" not in text:
        p.append(f"首帧问题没回写到生图卡: {brief(fb)}")
    changed = [n for n, b in image_before.items() if not (img / n).is_file() or (img / n).read_bytes() != b]
    if changed:
        p.append(f"视频改了或删了生图的产出文件: {changed[:3]}")
    if image_skill_status() != image_skill_before:
        p.append("视频这边的操作改到了生图 Skill 目录")
    return p


def c_core(ctx):
    """card-core：视频 Skill 里的 scripts/core 和 card-core 源头一致"""
    sync = CARD_CORE / "sync.py"
    if not sync.is_file():
        raise Skip("旁边没有 card-core 源头（单独装的 Skill 只带副本）")
    proc = subprocess.run([sys.executable, str(sync), "--check"], capture_output=True, text=True)
    if proc.returncode != 0 or f"✓ {SKILL.name}" not in proc.stdout.splitlines():
        return [f"core 副本和源头不一致（或视频不在 consumers 里）: {brief(proc)}"]
    return []


def c_cloud(ctx):
    """云 GPU（假 compshare）：没开机拒出片；up 的 --approved 空/占位/过短都拒；--create 不带 --price-ok 只报价不建机；建机报错但机器已建→记进 session、补兜底关机、醒目提示在计费；
    兜底关机设不上就立刻关机报错；推送 h3_run；出片走远端任务并取回；首帧+中间帧没尾帧退回首帧模式；没 server.command 时健康检查只短探；down 关机"""
    p = []
    box = Path(tempfile.mkdtemp(prefix="vselfcheck-box-"))      # outside the project: the fake box is not project data
    state = box / "state"
    cli = box / "compshare-fake"
    cli.write_text(f"#!/bin/sh\nFAKE_COMPSHARE_STATE={state} exec {sys.executable} "
                   f"{SKILL / 'scripts/remote/fake_compshare.py'} \"$@\"\n")
    cli.chmod(0o755)

    def change(data):
        data["video"]["providers"]["entries"]["cloud"] = {"kind": "compshare"}
        data["video"]["models"]["cloud-h3"] = {"provider": "cloud"}
        data["video"]["cloud"] = {"cli": str(cli), "backend": "mock", "remote_root": str(box / "remote"),
                                  "safety_shutdown_hours": 1,
                                  "create": {"region": "r", "zone": "z", "gpu": "g", "cpu": 1, "memory": "1GiB", "image": "i"}}
    ctx.edit_config(change)
    early = ctx.run("generate", CARD, "--model", "cloud-h3", "--budget", "1")
    if ctx.manifest()["jobs"].get(f"{CARD}|cloud-h3|1", {}).get("state") == "ok":
        p.append(f"没开机也出片了: {brief(early)}")
    def hosts():
        path = state / "state.json"
        return json.loads(path.read_text())["hosts"] if path.is_file() else {}

    unapproved = ctx.run("cloud", "up", "--create", "--price-ok")
    if unapproved.returncode == 0 or hosts():
        p.append(f"没 --approved 也建机了: {brief(unapproved)}")
    for bad in ("...", "<谁/何时>", "ok"):
        weak = ctx.run("cloud", "up", "--create", "--price-ok", "--approved", bad)
        if weak.returncode == 0 or hosts():
            p.append(f"占位或过短的 --approved \"{bad}\" 也建机了: {brief(weak)}")
    plan = ctx.run("cloud", "up", "--create", "--approved", "自检/今天")
    if plan.returncode or hosts() or "price-ok" not in plan.stdout:
        p.append(f"--create 不带 --price-ok 应只报价、退出码 0、不建机: rc={plan.returncode} hosts={hosts()} {brief(plan)}")
    (state / "fail_create").touch()
    lost = ctx.run("cloud", "up", "--create", "--price-ok", "--approved", "自检/今天")
    (state / "fail_create").unlink()
    session_file = ctx.tmp / "video-output/cloud/session.json"
    session = json.loads(session_file.read_text(encoding="utf-8")) if session_file.is_file() else {}
    calls = json.loads(state.joinpath("state.json").read_text())["calls"] if state.joinpath("state.json").is_file() else []
    if (lost.returncode == 0 or "MAY BE BILLING" not in brief(lost, 2000) or session.get("instance") != "uhost-fake"
            or ["instance", "schedule", "set"] not in calls):
        p.append(f"建机报错但机器已建：应记进 session、补兜底关机、提示在计费: rc={lost.returncode} "
                 f"session={session} {brief(lost)}")
    (state / "fail_schedule").touch()
    unguarded = ctx.run("cloud", "up", "--create", "--price-ok", "--approved", "自检/今天")
    (state / "fail_schedule").unlink()
    if unguarded.returncode == 0 or hosts().get("uhost-fake") != "Stopped":
        p.append(f"兜底关机设置失败时应立刻关机并报错: rc={unguarded.returncode} hosts={hosts()} {brief(unguarded)}")
    up = ctx.run("cloud", "up", "--create", "--price-ok", "--approved", "自检/今天")
    calls = json.loads(state.joinpath("state.json").read_text())["calls"] if state.joinpath("state.json").is_file() else []
    if up.returncode or not (box / "remote/h3_run.py").is_file():
        return p + [f"cloud up 失败或没推送 h3_run.py: {brief(up)}"]
    if ["instance", "schedule", "set"] not in calls:
        p.append("cloud up 没设兜底定时关机")
    gen = ctx.run("generate", CARD, "--model", "cloud-h3", "--budget", "1")
    job = ctx.manifest()["jobs"].get(f"{CARD}|cloud-h3|1", {})
    if gen.returncode or job.get("state") != "ok":
        return p + [f"云端出片没成 {job.get('state')}: {job.get('error')} {brief(gen)}"]
    run_dir = Path(job["run_dir"])
    remote_job = json.loads((run_dir / "cloud_job/job.json").read_text(encoding="utf-8"))
    if remote_job.get("mode") != job.get("mode") or not (run_dir / "remote.log").is_file():
        p.append(f"远端 job.json 的 mode 不对或没取回日志: {remote_job.get('mode')} vs {job.get('mode')}")
    # first frame + a middle keyframe, no last frame: runs as first_frame on the box (was KeyError 'last_frame')
    for n, color in ((1, "red"), (2, "blue")):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color={color}:size=64x36", "-frames:v", "1",
                        str(ctx.tmp / f"cf{n}.png")], check=True)
    make_card(ctx, "08-01", "云首中帧", motion="She turns.", duration=4, first_frame="cf1.png", keyframes=[{"ref": "cf2.png", "at": 2}])
    mid = ctx.run("generate", "08-01", "--model", "cloud-h3", "--budget", "1")
    job = ctx.manifest()["jobs"].get("08-01|cloud-h3|1", {})
    remote = Path(job.get("run_dir", "/nonexistent")) / "cloud_job/job.json"
    if job.get("state") != "ok" or json.loads(remote.read_text(encoding="utf-8")).get("mode") != "first_frame":
        p.append(f"首帧+中间帧没尾帧在云 GPU 上应退回首帧模式出片: {job.get('state')} {job.get('error')} {brief(mid)}")
    ctx.edit_config(lambda d: d["video"]["cloud"].update(server={"health": "http://127.0.0.1:9/none", "probe_timeout": 2,
                                                                "poll": 1}))
    started = time.time()
    probe = ctx.run("cloud", "up", "--approved", "自检/今天")
    if probe.returncode == 0 or "server.command" not in brief(probe, 2000) or time.time() - started > 60:
        p.append(f"没 server.command、健康检查不通时应短探后报错提示: rc={probe.returncode} "
                 f"{time.time() - started:.0f}s {brief(probe)}")
    ctx.edit_config(lambda d: d["video"]["cloud"].pop("server", None))
    down = ctx.run("cloud", "down")
    status = ctx.run("cloud", "status")
    if down.returncode or "Stopped" not in status.stdout:
        p.append(f"cloud down 后没关机: {brief(status)}")
    after = ctx.run("generate", CARD, "--model", "cloud-h3", "--budget", "2")
    if ctx.manifest()["jobs"].get(f"{CARD}|cloud-h3|2", {}).get("state") == "ok":
        p.append(f"关机后还出片了: {brief(after)}")
    if list((run_dir / "cloud_job").glob("*.png")) + list((run_dir / "cloud_job").glob("*.jpg")):
        p.append("上传后本地 cloud_job 里还留着图片副本（会进版本）")
    shutil.rmtree(box, ignore_errors=True)
    return p


def c_station_plugin(ctx):
    """审片台插件样板（templates/station_plugin.example.py）：提交进文件夹，人填 review.json 后 sync 取回；Agent 署名被拒"""
    cfg = ctx.tmp / "plugin.yaml"
    data = yaml.safe_load(ctx.config.read_text(encoding="utf-8"))
    data["video"]["paths"]["output"] = "plugin-output"
    data["video"]["station"] = {"kind": "plugin", "module": "templates/station_plugin.example.py", "inbox": "待审片"}
    cfg.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    p = []
    gen = ctx.run("generate", CARD, "--budget", "1", config=cfg)
    sheets = sorted((ctx.tmp / "待审片").rglob("*.review.json"))
    if len(sheets) != 1:
        return [f"文件夹里应有 1 份 review.json，实际 {len(sheets)}: {brief(gen)}"]
    sheet = json.loads(sheets[0].read_text(encoding="utf-8"))
    sheets[0].write_text(json.dumps({**sheet, "verdict": "good", "liked": "节奏好", "by": "agent:x"},
                                    ensure_ascii=False), encoding="utf-8")
    ctx.run("sync", CARD, config=cfg)
    review_path = ctx.tmp / "plugin-output/review" / f"{CARD}.json"
    takes = json.loads(review_path.read_text(encoding="utf-8")).get("takes", {}) if review_path.is_file() else {}
    if ((takes.get(f"{CARD}_dry_1") or {}).get("human") or {}).get("verdict"):
        p.append("Agent 署名的结论被收下了")
    sheets[0].write_text(json.dumps({**sheet, "verdict": "good", "liked": "节奏好", "by": "张三"},
                                    ensure_ascii=False), encoding="utf-8")
    sync = ctx.run("sync", CARD, config=cfg)
    review = json.loads(review_path.read_text(encoding="utf-8"))
    human = (review["takes"].get(f"{CARD}_dry_1") or {}).get("human") or {}
    if human.get("verdict") != "pass" or "满意：节奏好" not in human.get("note", ""):
        p.append(f"文件夹审片的结论没取回: {human} {brief(sync)}")
    return p


def c_keyframes(ctx):
    """关键帧：首帧 + 中间几张 + 尾帧一条时间线；只认定稿时每张都查；只吃首尾的模型如实提示中间几张不送出（不冒充参考图）；
    交接包 / 命令拿到全部且不重复进参考；不从 0 秒开始的关键帧（只有 at:3、只有 at:1/at:4）报 PROBLEM；有首帧+中间帧没尾帧能出片"""
    p = []
    frames = ctx.tmp / "finals"
    frames.mkdir(exist_ok=True)
    for n, color in enumerate(["red", "green", "blue", "yellow"], 1):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color={color}:size=64x36", "-frames:v", "1",
                        str(frames / f"k{n}.png")], check=True)
    outside = ctx.tmp / "k3_draft.png"
    shutil.copy2(frames / "k3.png", outside)
    (frames / "k3.png").unlink()
    ctx.run("new-card", "09-01", "关键帧")
    path = ctx.card_path("09-01")
    card = yaml.safe_load(path.read_text(encoding="utf-8"))
    card.update(motion="She turns, walks, stops.", duration=6, first_frame="finals/k1.png",
                keyframes=["finals/k2.png", {"ref": "k3_draft.png", "at": 4.5}], last_frame="finals/k4.png")
    path.write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")
    val = ctx.run("validate", "09-01")
    if val.returncode or "mode=keyframes" not in val.stdout:
        p.append(f"4 张关键帧没走 keyframes 模式: {brief(val)}")
    shown = [line.split()[1] for line in val.stdout.splitlines() if line.split()[:1] in (["first_frame"], ["keyframe"], ["last_frame"])]
    if shown != ["0s", "2.25s", "4.5s", "6s"]:
        p.append(f"时间线应是 0 / 2.25（没写 at，落在两边中间）/ 4.5 / 6s，实际 {shown}: {brief(val)}")
    if "the 2 in the middle (2.25s, 4.5s) are NOT sent" not in val.stdout or "image " in val.stdout:
        p.append(f"mock 只吃首尾：应如实提示中间两张不送出、也不列成参考图: {brief(val)}")
    # 只认定稿：中间那张不在定稿目录 → 报它，不报别的
    ctx.edit_config(lambda d: d["video"]["rules"].update(keyframes="final", final_dirs=["finals"]))
    val = ctx.run("validate", "09-01")
    if val.returncode == 0 or "keyframes[1] is not a final asset" not in val.stdout or "first_frame is not a final" in val.stdout:
        p.append(f"只认定稿时没卡住中间那张非定稿关键帧: {brief(val)}")
    shutil.copy2(outside, frames / "k3.png")
    val = ctx.run("validate", "09-01")
    if val.returncode:
        p.append(f"同内容的定稿副本放进定稿目录后应通过: {brief(val)}")
    gen = ctx.run("generate", "09-01", "--model", "web-manual", "--budget", "1")
    job = ctx.manifest()["jobs"].get("09-01|web-manual|1", {})
    package = Path(job.get("handle", {}).get("package", "/nonexistent"))
    names = sorted(x.name for x in package.glob("关键帧*")) if package.is_dir() else []
    if len(names) != 4 or not names[0].endswith("_首帧.png") or not names[-1].endswith("_尾帧.png"):
        p.append(f"交接包应有 4 张按时间排号的关键帧: {names} {brief(gen)}")
    if package.is_dir() and list(package.glob("参考*")):
        p.append(f"交接包里中间关键帧又重复进了参考图: {sorted(x.name for x in package.glob('参考*'))}")
    ctx.edit_config(lambda d: d["video"].update(models={**d["video"]["models"], "kf-cli": {"provider": "kfcmd"}},
                                                providers={**d["video"]["providers"], "entries": {
                                                    **d["video"]["providers"]["entries"],
                                                    "kfcmd": {"kind": "command", "free": True, "command":
                                                              f"{sys.executable} -c \"import shutil,sys,json; "
                                                              f"assert len(json.load(open(sys.argv[2])))==4 and sys.argv[3].count(',')==3; "
                                                              f"shutil.copy(sys.argv[1], sys.argv[4])\" {ctx.tmp / 'sample.mp4'} "
                                                              f"{{keyframes_json}} {{keyframes}} {{out}}"}}}))
    gen = ctx.run("generate", "09-01", "--model", "kf-cli", "--budget", "1")
    if ctx.manifest()["jobs"].get("09-01|kf-cli|1", {}).get("state") != "ok":
        p.append(f"命令行供应商没拿到全部 4 张关键帧（{{keyframes}} / {{keyframes_json}}）: {brief(gen)}")
    ctx.edit_config(lambda d: [d["video"]["rules"].pop(k, None) for k in ("keyframes", "final_dirs")])
    # keyframes that miss 0s: never silently text-to-video
    for label, frames_ in (("只有 at:3", [{"ref": "finals/k2.png", "at": 3}]),
                           ("at:1 / at:4", [{"ref": "finals/k2.png", "at": 1}, {"ref": "finals/k3.png", "at": 4}])):
        card.pop("first_frame", None)
        card.pop("last_frame", None)
        card["keyframes"] = frames_
        path.write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")
        val = ctx.run("validate", "09-01")
        if val.returncode == 0 or "not at 0s" not in val.stdout:
            p.append(f"{label}（不落在 0 秒）应报 PROBLEM，不能悄悄变文生视频: {brief(val)}")
    # first + middle, no last: validate says what happens; mock still makes it
    card.update(first_frame="finals/k1.png", keyframes=[{"ref": "finals/k2.png", "at": 2}], duration=4)
    path.write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")
    val = ctx.run("validate", "09-01")
    if val.returncode or "NOT sent" not in val.stdout or "no last_frame" not in val.stdout:
        p.append(f"首帧+中间帧没尾帧：应通过并提示中间帧不送、只用首帧: {brief(val)}")
    prompt = ctx.run("prompt", "09-01")
    if "NOT sent" not in prompt.stdout:
        p.append(f"prompt 没如实提示中间帧不送出: {brief(prompt)}")
    return p


def c_concurrent(ctx):
    """并发写 manifest：多进程同时改各自的 job 不丢；collect 跑着时另一会话 generate 的记录不被旧副本盖掉；
    两个会话同时 generate 同一格只提交一次；片名已存在时新片换名、不覆盖"""
    p = []
    writer = ("import sys; sys.path.insert(0, sys.argv[1]); from config import Config; import store; "
              "cfg = Config(sys.argv[2]); [store.update_manifest(cfg, lambda m, k=k: m['jobs'].__setitem__("
              "f'zz-{sys.argv[3]}-{k}', {'card': 'zz', 'model': 'x', 'n': k, 'state': 'failed'})) for k in range(15)]")
    procs = [subprocess.Popen([sys.executable, "-c", writer, str(SKILL / "scripts"), str(ctx.config), str(w)])
             for w in range(4)]
    for proc in procs:
        proc.wait(timeout=120)
    keys = [k for k in ctx.manifest()["jobs"] if k.startswith("zz-")]
    if len(keys) != 60:
        p.append(f"4 个进程各写 15 条，manifest 里只剩 {len(keys)} 条（互相覆盖）")
    path = ctx.tmp / "video-output/manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["jobs"] = {k: v for k, v in data["jobs"].items() if not k.startswith("zz-")}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    ctx.edit_config(lambda d: d["video"]["providers"]["entries"]["mock"].update(delay=3))
    make_card(ctx, "07-01", "并发", motion="Static.")
    make_card(ctx, "07-02", "并发二", motion="Static.")
    ctx.run("generate", "07-01", "--budget", "1", "--no-wait")
    collector = subprocess.Popen([sys.executable, str(VPIPELINE), "--config", str(ctx.config), "collect"],
                                 cwd=str(ctx.tmp), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(0.5)
    other = ctx.run("generate", "07-02", "--budget", "1")          # writes its own job while collect is polling
    collector.wait(timeout=120)
    jobs_now = ctx.manifest()["jobs"]
    if jobs_now.get("07-01|dry|1", {}).get("state") != "ok" or jobs_now.get("07-02|dry|1", {}).get("state") != "ok":
        p.append(f"collect 和另一会话的 generate 同时跑后有记录丢了: 07-01={jobs_now.get('07-01|dry|1', {}).get('state')} "
                 f"07-02={jobs_now.get('07-02|dry|1', {}).get('state')} {brief(other)}")
    ctx.edit_config(lambda d: d["video"]["providers"]["entries"]["mock"].pop("delay", None))
    slow = (f"{sys.executable} -c \"import shutil,sys,time; time.sleep(1.5); shutil.copy(sys.argv[1], sys.argv[2])\" "
            f"{ctx.tmp / 'sample.mp4'} {{out}}")
    ctx.edit_config(lambda d: (d["video"]["providers"]["entries"].update(slow={"kind": "command", "free": True, "command": slow}),
                               d["video"]["models"].update({"slow-cli": {"provider": "slow"}})))
    twins = [subprocess.Popen([sys.executable, str(VPIPELINE), "--config", str(ctx.config), "generate", "07-01",
                               "--model", "slow-cli", "--budget", "1"], cwd=str(ctx.tmp),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) for _ in range(2)]
    outs = [t.communicate(timeout=120)[0] for t in twins]
    runs = list((ctx.tmp / "video-output/runs").glob("07-01-slow-cli-1-*"))
    takes = list((ctx.tmp / "video-output/takes").glob("07-01_slow-cli_1*.mp4"))
    if len(runs) != 1 or len(takes) != 1:
        p.append(f"两个会话同时 generate 同一格应只提交一次: {len(runs)} 个 run、{len(takes)} 条片 {outs}")
    # a forgotten ok job submitted again must not overwrite the old clip
    old = ctx.tmp / "video-output/takes/07-02_dry_1.mp4"
    before = old.read_bytes() if old.is_file() else b""
    ctx.run("forget", "07-02|dry|1", "--why", "自检：重出一条", "--force")
    again = ctx.run("generate", "07-02", "--budget", "1")
    if not old.is_file() or old.read_bytes() != before or ctx.manifest()["jobs"].get("07-02|dry|1", {}).get("take") != "07-02_dry_1-2":
        p.append(f"片名已存在时应换名（07-02_dry_1-2）、旧片不动: {ctx.manifest()['jobs'].get('07-02|dry|1')} {brief(again)}")
    return p


def c_interrupt(ctx):
    """提交中途出事不重提：命令超时记 unknown；Ctrl-C 打断记 unknown；卡在 submitting（进程被杀）也不重提，forget 后才再提"""
    p = []
    hang = f"{sys.executable} -c \"import time; time.sleep(30)\" {{out}}"
    ctx.edit_config(lambda d: (d["video"]["providers"]["entries"].update(
        hang={"kind": "command", "free": True, "command": hang, "timeout": 1}),
        d["video"]["models"].update({"hang-cli": {"provider": "hang"}})))
    make_card(ctx, "06-11", "中断", motion="Static.")
    first = ctx.run("generate", "06-11", "--model", "hang-cli", "--budget", "1")
    if ctx.manifest()["jobs"].get("06-11|hang-cli|1", {}).get("state") != "unknown":
        p.append(f"命令超时应记 unknown: {ctx.manifest()['jobs'].get('06-11|hang-cli|1')} {brief(first)}")
    again = ctx.run("generate", "06-11", "--model", "hang-cli", "--budget", "1")
    if "Not resubmitting" not in again.stdout or len(list((ctx.tmp / "video-output/runs").glob("06-11-hang-cli-1-*"))) != 1:
        p.append(f"超时（unknown）后被重提了: {brief(again)}")
    ctx.edit_config(lambda d: d["video"]["providers"]["entries"]["hang"].update(timeout=60))
    proc = subprocess.Popen([sys.executable, str(VPIPELINE), "--config", str(ctx.config), "generate", "06-11",
                             "--model", "hang-cli", "--budget", "2"], cwd=str(ctx.tmp),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 30
    while time.time() < deadline and (ctx.tmp / "video-output/manifest.json").is_file() \
            and ctx.manifest()["jobs"].get("06-11|hang-cli|2", {}).get("state") != "submitting":
        time.sleep(0.2)
    time.sleep(0.5)
    proc.send_signal(signal.SIGINT)
    proc.communicate(timeout=60)
    state = ctx.manifest()["jobs"].get("06-11|hang-cli|2", {}).get("state")
    if state != "unknown":
        p.append(f"Ctrl-C 打断提交后应记 unknown，实际 {state}")
    # a run killed hard leaves `submitting`: not resubmitted, forget works
    def stuck(data):
        data["jobs"]["06-11|dry|1"] = {"card": "06-11", "model": "dry", "n": 1, "state": "submitting",
                                       "submitted_at": "2026-01-01T00:00:00", "run_dir": "x"}
    path = ctx.tmp / "video-output/manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    stuck(data)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    held = ctx.run("generate", "06-11", "--budget", "1")
    if "SUBMITTING" not in held.stdout or ctx.manifest()["jobs"]["06-11|dry|1"].get("state") != "submitting":
        p.append(f"submitting 状态被重提了: {brief(held)}")
    gone = ctx.run("forget", "06-11|dry|1", "--why", "自检：确认那个进程已经没了")
    redo = ctx.run("generate", "06-11", "--budget", "1")
    if gone.returncode or ctx.manifest()["jobs"].get("06-11|dry|1", {}).get("state") != "ok":
        p.append(f"forget submitting 后应能再出片: {brief(gone)} {brief(redo)}")
    return p


def c_validate_model(ctx):
    """validate：generate --model X 按 X 的时长上下限校验；拼错字段给 note；非法 mode 报 PROBLEM"""
    p = []
    ctx.edit_config(lambda d: d["video"]["models"].update({"short": {"provider": "mock", "max_seconds": 3}}))
    make_card(ctx, "06-02", "校验", motion="Static.", duration=5, motoin="typo", mode="auto")
    val = ctx.run("validate", "06-02")
    if val.returncode or "unknown field 'motoin'" not in val.stdout or "did you mean 'motion'" not in val.stdout:
        p.append(f"拼错的字段应给 note（并猜原名）: {brief(val)}")
    gen = ctx.run("generate", "06-02", "--model", "short", "--budget", "1")
    if gen.returncode == 0 or "06-02|short|1" in ctx.manifest()["jobs"] or "maximum" not in brief(gen):
        p.append(f"--model short（最长 3 秒）应按它的上限拒掉 5 秒的卡: {brief(gen)}")
    make_card(ctx, "06-02", "校验", mode="keyframe", motoin=None)
    val = ctx.run("validate", "06-02")
    if val.returncode == 0 or "mode 'keyframe' is not one of" not in val.stdout:
        p.append(f"非法 mode 应报 PROBLEM: {brief(val)}")
    make_card(ctx, "06-02", "校验", mode=None)
    return p


def c_withdraw(ctx):
    """撤回结论（契约 §9 空=撤回）后 sync：清掉 human、移出 sync 带进的 pick，人手动 pick 的保留；pick 保持加入顺序"""
    p = []
    hand = f"{CARD}_paid-cli_1"
    ctx.run("pick", CARD, hand)
    before = ctx.review().get("pick") or []
    if before[-1:] != [hand] or f"{CARD}_dry_1" not in before:
        p.append(f"手动 pick 应排在已有的后面（保持加入顺序）: {before}")
    path = ctx.tmp / "video-output/station/station.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    entry = data["takes"][f"{CARD}_dry_1"]
    entry["reviews"] = {"0": {"verdict": "", "liked": "", "disliked": "", "by": "human:selfcheck", "at": "x"}}
    entry.pop("note", None)
    entry.pop("marks", None)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    sync = ctx.run("sync", CARD)
    review = ctx.review()
    if (review["takes"].get(f"{CARD}_dry_1") or {}).get("human"):
        p.append(f"撤回后 human 结论没清掉: {review['takes'].get(f'{CARD}_dry_1')}")
    if f"{CARD}_dry_1" in review.get("pick", []) or hand not in review.get("pick", []):
        p.append(f"撤回后应移出 sync 带进的那条、保留手动 pick 的: {review.get('pick')} {brief(sync)}")
    return p


def c_assemble_bounds(ctx):
    """剪辑单：out 超过片长 → 截到实测时长并告警；镜头不长于转场 → 报错；卡有多条 pick 时提示用的是哪条"""
    p = []
    take = f"{CARD}_dry_2"
    real = json.loads((ctx.tmp / "video-output/takes" / f"{take}.json").read_text(encoding="utf-8"))["probe"]["duration"]
    cut = ctx.tmp / "cuts" / "边界.yaml"
    cut.write_text(yaml.safe_dump({"width": 320, "height": 180, "fps": 24, "crossfade": 0.3,
                                   "shots": [{"take": take, "out": 99}, {"take": take}]}, allow_unicode=True),
                   encoding="utf-8")
    long = ctx.run("assemble", "边界")
    final = ctx.tmp / "video-output/final/边界_v01.mp4"
    if long.returncode or "past the end" not in long.stdout or not final.is_file():
        p.append(f"out 超片长应截到实测时长并告警: {brief(long)}")
    else:
        meta = json.loads((ctx.tmp / "video-output/takes/边界_v01.json").read_text(encoding="utf-8"))
        if meta["probe"]["duration"] > 2 * real:
            p.append(f"成片 {meta['probe']['duration']}s 比两段实测 {real}s 加起来还长（out 没截）")
    cut.write_text(yaml.safe_dump({"width": 320, "height": 180, "fps": 24, "crossfade": 1.0,
                                   "shots": [{"take": take, "out": 0.5}, {"take": take}]}, allow_unicode=True),
                   encoding="utf-8")
    short = ctx.run("assemble", "边界")
    if short.returncode == 0 or "crossfade" not in brief(short):
        p.append(f"镜头不长于转场应报错: {brief(short)}")
    ctx.run("pick", "02-01", "02-01_dry_1")
    picks = [f"{CARD}_dry_2"]
    ctx.run("pick", CARD, *picks)
    cut.write_text(yaml.safe_dump({"width": 320, "height": 180, "fps": 24, "crossfade": 0.3,
                                   "shots": [{"card": CARD}, {"card": "02-01"}]}, allow_unicode=True), encoding="utf-8")
    several = ctx.run("assemble", "边界", "--dry-run")
    if "picked takes" not in several.stdout or "take:" not in several.stdout:
        p.append(f"卡有多条 pick 时应提示用的哪条、可写 take: 指定: {brief(several)}")
    return p


def c_init_image(ctx):
    """视频 init 往生图项目追加 video 段时，给它的 .gitignore 补 *.mp4 *.mov *.webm *.part（只补缺的行）"""
    proj = ctx.tmp / "生图项目-追加"
    proj.mkdir()
    (proj / "project.yaml").write_text("project: {name: 追加}\npaths: {cards: cards}\n", encoding="utf-8")
    (proj / ".gitignore").write_text("output/\n*.mp4\n", encoding="utf-8")
    proc = ctx.run("init", config=proj / "project.yaml")
    lines = (proj / ".gitignore").read_text(encoding="utf-8").splitlines()
    p = []
    if proc.returncode or "video:" not in (proj / "project.yaml").read_text(encoding="utf-8"):
        p.append(f"init 没追加 video 段: {brief(proc)}")
    if lines.count("*.mp4") != 1 or not all(x in lines for x in ("*.mov", "*.webm", "*.part")):
        p.append(f".gitignore 应补齐缺的行、不重复: {lines}")
    shutil.rmtree(proj, ignore_errors=True)
    return p


CHECKS = [c_init, c_card, c_generate, c_paid, c_unknown, c_collect, c_station, c_sync, c_segments, c_migration, c_post,
          c_register, c_handoff, c_cloud, c_image_contract, c_versions, c_core, c_styles, c_kinds,
          c_station_plugin, c_keyframes, c_validate_model, c_interrupt, c_concurrent, c_withdraw, c_assemble_bounds,
          c_init_image]


def extra_checks(path):
    """Checks shipped by a profile: the file defines checks(sc) and gets this module as sc."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"extra_{Path(path).stem}", Path(path).expanduser().resolve())
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.checks(sys.modules[__name__])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--extra", action="append", default=[], metavar="FILE",
                        help="also run the checks a profile ships (a .py defining checks(sc))")
    args = parser.parse_args()
    status_before = skill_status()
    tmp = Path(tempfile.mkdtemp(prefix="vselfcheck-"))
    ctx = Ctx(tmp)
    state = {}
    checks = CHECKS + [check for path in args.extra for check in extra_checks(path)]
    failed = skipped = 0
    for n, check in enumerate(checks, 1):
        try:
            problems = check(ctx, state) if check is c_station else check(ctx)
        except Skip as why:
            skipped += 1
            print(f"· {n}. {check.__doc__.strip()}（跳过：{why}）")
            continue
        except Exception as error:   # noqa: BLE001
            problems = [f"{type(error).__name__}: {error}"]
        failed += bool(problems)
        print(f"{'✓' if not problems else '✗'} {n}. {check.__doc__.strip()}")
        for problem in problems:
            print(f"     - {problem}")
    if skill_status() != status_before:
        failed += 1
        print("✗ 附加：Skill 目录在自检前后 git status 不一致（自检改到了 Skill 本身）")
    else:
        print("✓ 附加：Skill 目录前后 git status 一致（没有改到 Skill 本身）")
    passed = max(len(checks) - failed - skipped, 0)
    print(f"{passed} 项通过，{failed} 项失败" + (f"，{skipped} 项跳过" if skipped else ""))
    if args.keep:
        print(f"临时目录保留在 {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
