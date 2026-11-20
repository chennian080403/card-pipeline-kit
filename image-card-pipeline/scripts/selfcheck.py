#!/usr/bin/env python3
"""自检：在一个临时项目里把整条流水线跑一遍（只用 mock 供应商，不联网、不花钱）。

    python3 scripts/selfcheck.py [--keep]

每项检查独立执行，一项失败不影响后面的检查。全部结束（--keep 时保留临时目录）后
删除临时目录，并核对 Skill 目录本身没有被改动。有失败时退出码为 1。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
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
PIPELINE = SKILL / "scripts" / "pipeline.py"
REVIEW_WEB = SKILL / "scripts" / "review_web.py"
MYSKILLS = SKILL.parent
CARD = "01-01"
APPROVAL = "自检：两张"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def brief(proc, limit=500):
    """Key output of a finished command, on one line."""
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    text = re.sub(r"\s+", " ", text)
    return text[-limit:] if len(text) > limit else text


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def skill_status():
    """`git -C <Skill 所在目录> status --porcelain -- image-card-pipeline` 的原文（不在 git 里时为空）。"""
    proc = subprocess.run(["git", "-C", str(MYSKILLS), "status", "--porcelain", "--", SKILL.name],
                          capture_output=True, text=True)
    return proc.stdout


class Ctx:
    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.config = self.tmp / "project.yaml"
        self.card_file = None
        self.candidates = []
        self.init_proc = None

    def run(self, *args, timeout=180):
        return subprocess.run(
            [sys.executable, str(PIPELINE), "--config", str(self.config), *args],
            cwd=str(self.tmp), capture_output=True, text=True, timeout=timeout,
        )

    def out(self, proc):
        return (proc.stdout or "") + (proc.stderr or "")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.tmp), *args], capture_output=True, text=True)

    def commit_count(self):
        proc = self.git("rev-list", "--count", "HEAD")
        return int(proc.stdout.strip() or 0)

    def candidates_now(self):
        directory = self.tmp / "output" / "candidates"
        self.candidates = [p.name for p in sorted(directory.glob(f"{CARD}_*")) if p.is_file()]
        return self.candidates

    def load_card(self):
        return yaml.safe_load(self.card_file.read_text(encoding="utf-8")) or {}

    def write_card(self, card):
        self.card_file.write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")


def prepare(tmp):
    """init + 把项目改成 mock / local 素材库。返回 (ctx, init 进程)。"""
    ctx = Ctx(tmp)
    init = ctx.run("init")
    ctx.init_proc = init
    if init.returncode != 0:
        return ctx
    raw = yaml.safe_load(ctx.config.read_text(encoding="utf-8")) or {}
    raw.pop("taxonomy", None)  # The original checks exercise existing projects.
    raw.setdefault("providers", {})["default"] = "mock"
    models = raw.setdefault("models", {})
    models["default"] = "dry"
    models["dry"] = {"provider": "mock"}
    raw["library"] = {"kind": "local", "root": "素材库"}
    ctx.config.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return ctx


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #

def check_01(ctx):
    problems = []
    init = ctx.init_proc
    if init.returncode != 0:
        problems.append(f"init 退出码 {init.returncode}: {brief(init)}")
    if not (ctx.tmp / ".git").is_dir():
        problems.append("项目目录里没有 .git")
    if not (ctx.tmp / ".gitignore").is_file():
        problems.append("项目目录里没有 .gitignore")
    doctor = ctx.run("doctor")
    if doctor.returncode != 0:
        problems.append(f"doctor 退出码 {doctor.returncode}: {brief(doctor)}")
    elif "version : git" not in ctx.out(doctor):
        problems.append("doctor 输出里没有 'version : git': " + brief(doctor, 300))
    return (not problems), "；".join(problems)


def check_02(ctx):
    problems = []
    specs = (
        (CARD, "自检卡", (), "card"),
        ("01-02", "重绘卡", ("--kind", "rerender"), "rerender"),
        ("01-03", "人物卡", ("--kind", "portrait"), "portrait"),
    )
    for card_id, title, extra, expected_kind in specs:
        proc = ctx.run("new-card", card_id, title, *extra)
        if proc.returncode != 0:
            problems.append(f"new-card {card_id} 退出码 {proc.returncode}: {brief(proc)}")
            continue
        found = sorted((ctx.tmp / "cards").glob(f"{card_id}_*.yaml"))
        if not found:
            problems.append(f"new-card {card_id} 没有生成卡片文件")
            continue
        card = yaml.safe_load(found[0].read_text(encoding="utf-8")) or {}
        actual_kind = card.get("kind") or "card"
        if actual_kind != expected_kind:
            problems.append(f"{found[0].name} 的 kind 是 {actual_kind}，期望 {expected_kind}")
        if card_id == CARD:
            ctx.card_file = found[0]
            # 模板只是脚手架：填一句动作，才是一张能出图的卡
            card["action"] = "A lone figure stands on a quiet street at dawn, looking off frame."
            card["characters"] = []
            ctx.write_card(card)

    if ctx.card_file is None:
        problems.append("默认卡不存在，后续检查无法进行")
        return False, "；".join(problems)

    validate = ctx.run("validate", CARD)
    if validate.returncode != 0:
        problems.append(f"validate {CARD} 退出码 {validate.returncode}: {brief(validate)}")
    return (not problems), "；".join(problems)


def check_03(ctx):
    if ctx.card_file is None:
        return False, "默认卡不存在（检查 2 失败）"
    proc = ctx.run("compose", CARD)
    prompt = ctx.tmp / "output" / "prompt" / f"{CARD}.txt"
    if proc.returncode != 0:
        return False, f"compose 退出码 {proc.returncode}: {brief(proc)}"
    if not prompt.is_file():
        return False, f"缺少 output/prompt/{CARD}.txt: {brief(proc, 200)}"
    if prompt.stat().st_size == 0:
        return False, "output/prompt 里的提示词是空文件"
    return True, ""


def check_04(ctx):
    if ctx.card_file is None:
        return False, "默认卡不存在（检查 2 失败）"
    proc = ctx.run("generate", CARD, "--budget", "1")
    if proc.returncode == 0:
        return False, "generate 不带 --approved 却成功了: " + brief(proc)
    if "approval" not in ctx.out(proc).lower():
        return False, f"虽然非 0（{proc.returncode}），但不是付费闸门拒绝: " + brief(proc)
    for fake in ("...", "…", "<who/when>", "ok"):
        faked = ctx.run("generate", CARD, "--budget", "1", "--approved", fake)
        if faked.returncode == 0:
            return False, f"占位符式的批准 {fake!r} 被接受了: " + brief(faked)
    return True, ""


def check_05(ctx):
    if ctx.card_file is None:
        return False, "默认卡不存在（检查 2 失败）"
    proc = ctx.run("generate", CARD, "--budget", "2", "--approved", APPROVAL)
    if proc.returncode != 0:
        return False, f"第一次 generate 退出码 {proc.returncode}: {brief(proc)}"
    names = ctx.candidates_now()
    if len(names) != 2:
        return False, f"第一次 generate 后候选 {len(names)} 张（期望 2）: {brief(proc)}"
    again = ctx.run("generate", CARD, "--budget", "2", "--approved", APPROVAL)
    if again.returncode != 0:
        return False, f"第二次 generate 退出码 {again.returncode}: {brief(again)}"
    if "0 new" not in ctx.out(again):
        return False, "第二次 generate 输出里没有 '0 new': " + brief(again)
    if len(ctx.candidates_now()) != 2:
        return False, f"重跑后候选变成 {len(ctx.candidates)} 张（应保持 2）"
    return True, ""


def check_06(ctx):
    if ctx.card_file is None:
        return False, "默认卡不存在（检查 2 失败）"
    card = ctx.load_card()
    card["action"] = "The figure turns toward the camera and raises one hand in greeting."
    ctx.write_card(card)

    stale = ctx.run("generate", CARD, "--budget", "3", "--approved", "自检：改卡后")
    if stale.returncode == 0:
        return False, "改卡后不 compose 直接 generate 却成功了: " + brief(stale)
    if "stale" not in ctx.out(stale).lower():
        return False, f"generate 非 0（{stale.returncode}）但不是因为提示词过期: " + brief(stale)

    compose = ctx.run("compose", CARD)
    if compose.returncode != 0:
        return False, f"重新 compose 失败（{compose.returncode}）: {brief(compose)}"
    again = ctx.run("generate", CARD, "--budget", "3", "--approved", "自检：改卡后")
    if again.returncode != 0:
        return False, f"compose 后 generate 仍失败（{again.returncode}）: {brief(again)}"
    names = ctx.candidates_now()
    if len(names) != 3:
        return False, f"--budget 3 后候选 {len(names)} 张（期望 3）: {brief(again)}"
    return True, ""


def check_07(ctx):
    if ctx.card_file is None:
        return False, "默认卡不存在（检查 2 失败）"
    proc = ctx.run("sheet", CARD)
    sheets = [p for p in (ctx.tmp / "output" / "sheets").glob("*") if p.is_file()]
    if proc.returncode != 0:
        return False, f"sheet 退出码 {proc.returncode}: {brief(proc)}"
    if not sheets:
        return False, "output/sheets 下没有文件: " + brief(proc, 200)
    return True, ""


def check_08(ctx):
    if ctx.card_file is None:
        return False, "默认卡不存在（检查 2 失败）"
    if not ctx.candidates:
        ctx.candidates_now()
    if not ctx.candidates:
        return False, "没有候选图（前面的 generate 失败）"
    candidate = ctx.candidates[0]
    stem = Path(candidate).stem
    problems = []

    claude = ctx.run("review", CARD, stem, "pass", "--note", "自检通过")
    if claude.returncode != 0:
        problems.append(f"review 默认 --by claude 退出码 {claude.returncode}: {brief(claude)}")

    helper = ctx.run("review", CARD, stem, "pass", "--by", "helper-ai")
    if helper.returncode == 0:
        problems.append("review --by helper-ai 被接受了")
    elif "reviewers" not in ctx.out(helper):
        problems.append(f"--by helper-ai 非 0（{helper.returncode}）但不是规则拒绝: {brief(helper)}")

    human = ctx.run("review", CARD, stem, "pass", "--by", "human", "--note", "人工复核")
    if human.returncode != 0:
        problems.append(f"review --by human 退出码 {human.returncode}: {brief(human)}")

    path = ctx.tmp / "output" / "review" / f"{CARD}.json"
    if not path.is_file():
        problems.append(f"缺少 output/review/{CARD}.json")
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = (data.get("candidates") or {}).get(candidate) or (data.get("candidates") or {}).get(stem) or {}
        if "by" not in entry:
            problems.append("审核记录里没有 by")
    return (not problems), "；".join(problems)


def check_09(ctx):
    if ctx.card_file is None:
        return False, "默认卡不存在（检查 2 失败）"
    if not ctx.candidates:
        ctx.candidates_now()
    if not ctx.candidates:
        return False, "没有候选图（前面的 generate 失败）"
    candidate = ctx.candidates[0]
    stem = Path(candidate).stem
    problems = []

    pick = ctx.run("pick", CARD, stem)
    if pick.returncode != 0:
        problems.append(f"pick 退出码 {pick.returncode}: {brief(pick)}")

    register = ctx.run("register", CARD)
    if register.returncode != 0:
        problems.append(f"register 退出码 {register.returncode}: {brief(register)}")

    library = ctx.tmp / "素材库" / "library.json"
    entries = []
    if not library.is_file():
        problems.append("缺少 素材库/library.json")
    else:
        entries = json.loads(library.read_text(encoding="utf-8")).get("entries") or []
        if not any(str(entry.get("title", "")).split(" ", 1)[0] == CARD for entry in entries):
            problems.append(f"素材库里没有 {CARD} 的条目（{len(entries)} 条）")

    unregister = ctx.run("unregister", CARD, stem)
    if unregister.returncode != 0:
        problems.append(f"unregister 退出码 {unregister.returncode}: {brief(unregister)}")
    if library.is_file():
        left = json.loads(library.read_text(encoding="utf-8")).get("entries") or []
        if left:
            problems.append(f"unregister 后素材库还剩 {len(left)} 条")
    return (not problems), "；".join(problems)


def check_10(ctx):
    if ctx.card_file is None:
        return False, "默认卡不存在（检查 2 失败）"
    before = len(ctx.load_card().get("feedback") or [])
    first = ctx.run("feedback", CARD, "revise", "--reason", "自检：动作偏了")
    second = ctx.run("feedback", CARD, "revise", "--reason", "自检：动作偏了")
    after = len(ctx.load_card().get("feedback") or [])
    problems = []
    if first.returncode != 0:
        problems.append(f"第一次 feedback 退出码 {first.returncode}: {brief(first)}")
    if second.returncode != 0:
        problems.append(f"第二次 feedback 退出码 {second.returncode}: {brief(second)}")
    if after != before + 1:
        problems.append(f"同一条 reason 记两次后 feedback 从 {before} 条变成 {after} 条（期望 +1）")
    return (not problems), "；".join(problems)


def _external_image(ctx, name):
    """在受管目录之外造一张合法 PNG（追加几个字节让内容与候选不同，避开按内容去重）。"""
    if not ctx.candidates:
        ctx.candidates_now()
    source = ctx.tmp / "output" / "candidates" / ctx.candidates[0]
    target = ctx.tmp / "外部素材" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes() + f"# 自检 {name}".encode())
    return target


def _inspiration_snapshot(ctx):
    """(索引字节, inspiration 目录文件列表, 提交数)：用来确认拒绝时零副作用。"""
    path = ctx.tmp / "inspiration" / "index.json"
    index = path.read_bytes() if path.is_file() else b""
    folder = ctx.tmp / "inspiration"
    files = sorted(p.name for p in folder.glob("*") if p.is_file()) if folder.is_dir() else []
    return index, files, ctx.commit_count()


def check_11(ctx):
    if not ctx.candidates:
        ctx.candidates_now()
    if not ctx.candidates:
        return False, "没有候选图（前面的 generate 失败）"
    path = _external_image(ctx, "找来的参考.png")
    added = ctx.run("inspire", "add", str(path), "--pack", "风格/测试组", "--approved", "用户确认收录",
                    "--note", "自检素材", "--tag", "雪")
    if added.returncode != 0:
        return False, f"inspire add 退出码 {added.returncode}: {brief(added)}"
    match = re.search(r"\b([0-9a-f]{8})\b", ctx.out(added))
    if not match:
        return False, "inspire add 输出里没有 8 位 id: " + brief(added)
    ident = match.group(1)
    listing = ctx.run("inspire", "list")
    if listing.returncode != 0:
        return False, f"inspire list 退出码 {listing.returncode}: {brief(listing)}"
    if ident not in ctx.out(listing):
        return False, f"inspire list 里找不到 {ident}: " + brief(listing)
    return True, ""


def check_12(ctx):
    if not ctx.candidates:
        ctx.candidates_now()
    if len(ctx.candidates) < 2:
        return False, "候选不足两张（前面的 generate 失败）"
    first_c, second_c = Path(ctx.candidates[0]).stem, Path(ctx.candidates[1]).stem
    problems = []

    def history():
        data = yaml.safe_load((ctx.tmp / "sources.yaml").read_text(encoding="utf-8")) or {}
        entry = (data.get("characters") or {}).get("甲") or {}
        return entry, list(entry.get("history") or [])

    no_approval = ctx.run("promote", first_c, "--as", "character:甲")
    if no_approval.returncode == 0:
        problems.append("promote 不带 --approved 却成功了")

    one = ctx.run("promote", first_c, "--as", "character:甲", "--approved", "自检：第一张")
    if one.returncode != 0:
        return False, f"第一次 promote 退出码 {one.returncode}: {brief(one)}"
    old_source = history()[0].get("source")

    # 换一张图：旧真源必须进 history
    two = ctx.run("promote", second_c, "--as", "character:甲", "--approved", "自检：换第二张")
    if two.returncode != 0:
        problems.append(f"换图 promote 退出码 {two.returncode}: {brief(two)}")
    entry, hist = history()
    if not hist or hist[-1].get("source") != old_source:
        problems.append(f"换图后 history 里没有旧真源 {old_source}（history={hist}）")
    if "kept in history" not in ctx.out(two):
        problems.append("换图时输出没有提示旧值进了 history")

    # 同一张图再确认一次：不进 history，也不能谎称进了
    again = ctx.run("promote", second_c, "--as", "character:甲", "--approved", "自检：同一张再确认")
    if again.returncode != 0:
        problems.append(f"重复 promote 退出码 {again.returncode}: {brief(again)}")
    _entry, hist_again = history()
    if len(hist_again) != len(hist):
        problems.append(f"同一张图重复 promote 后 history 从 {len(hist)} 条变成 {len(hist_again)} 条")
    if "kept in history" in ctx.out(again):
        problems.append("同一张图重复 promote，输出却说旧值进了 history")
    return (not problems), "；".join(problems)


def check_13(ctx):
    hits = []
    for path in ctx.tmp.rglob("*"):
        if ".git" in path.relative_to(ctx.tmp).parts:
            continue
        if path.is_file() and ".bak" in path.name:
            hits.append(str(path.relative_to(ctx.tmp)))
        elif path.is_dir() and path.name.startswith("bak-"):
            hits.append(str(path.relative_to(ctx.tmp)) + "/")
    if hits:
        return False, "发现备份残留: " + ", ".join(hits[:10])
    return True, ""


def check_14(ctx):
    log = ctx.git("log", "--oneline")
    if log.returncode != 0:
        return False, "git log 失败: " + brief(log)
    lines = [line for line in log.stdout.splitlines() if line.strip()]
    status = ctx.git("status", "--porcelain")
    problems = []
    if len(lines) < 8:
        problems.append(f"提交只有 {len(lines)} 条（期望 ≥8）")
    if not any("自动存：generate" in line for line in lines):
        problems.append("没有「自动存：generate」开头的提交")
    if status.stdout.strip():
        problems.append("工作区不干净: " + re.sub(r"\s+", " ", status.stdout.strip())[:400])
    return (not problems), "；".join(problems)


def check_15(ctx):
    if not ctx.candidates:
        ctx.candidates_now()
    if not ctx.candidates:
        return False, "没有候选图（前面的 generate 失败）"
    candidate = ctx.candidates[1] if len(ctx.candidates) > 1 else ctx.candidates[0]
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, str(REVIEW_WEB), "--config", str(ctx.config), "--port", str(port)],
        cwd=str(ctx.tmp), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    ok, detail = False, ""
    try:
        deadline = time.time() + 20
        up = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
                    if response.status == 200:
                        up = True
                        break
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)
        if not up:
            detail = "审核页没能启动（20 秒内访问不到）"
        else:
            payload = json.dumps({"card": CARD, "candidate": candidate, "verdict": "fail",
                                  "note": "自检：网页否决"}, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(f"http://127.0.0.1:{port}/api/review", data=payload,
                                             method="POST", headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    reply = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                reply = {"ok": False, "body": error.read().decode("utf-8", "replace")}
            if reply.get("ok") is not True:
                detail = "POST /api/review 没有返回 ok:true: " + json.dumps(reply, ensure_ascii=False)[:400]
            else:
                subjects = ctx.git("log", "--format=%s").stdout.splitlines()
                if not any(line.startswith("审核页：review") for line in subjects):
                    detail = "git log 里没有「审核页：review」开头的提交"
                else:
                    ok = True
    except Exception as error:  # noqa: BLE001 - report, keep the run going
        detail = f"{type(error).__name__}: {error}"
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            output, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            output, _ = proc.communicate()
        if not ok and not detail:
            detail = "审核页输出: " + re.sub(r"\s+", " ", (output or "").strip())[-400:]
    return ok, detail


def check_16(ctx):
    before = ctx.commit_count()
    problems = []
    for args in (("inspire", "list"), ("doctor",), ("cards",)):
        proc = ctx.run(*args)
        if proc.returncode != 0:
            problems.append(f"{' '.join(args)} 退出码 {proc.returncode}: {brief(proc)}")
    after = ctx.commit_count()
    if after != before:
        problems.append(f"只读命令产生了新提交（{before} -> {after}）")
    return (not problems), "；".join(problems)


CONCURRENT_MODELS = [f"c{n}" for n in range(1, 8)]


def check_17(ctx):
    """8 个 generate 同时跑（6 个不同模型 + 同卡同模型两份），中途再往卡里写一条反馈。"""
    if ctx.card_file is None:
        return False, "默认卡不存在（检查 2 失败）"
    raw = yaml.safe_load(ctx.config.read_text(encoding="utf-8")) or {}
    for alias in CONCURRENT_MODELS:
        raw["models"][alias] = {"provider": "mock", "delay": 0.6}
    ctx.config.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    ctx.git("commit", "-qam", "自检：并发用的模型")

    def start(*args):
        return subprocess.Popen([sys.executable, str(PIPELINE), "--config", str(ctx.config), *args],
                                cwd=str(ctx.tmp), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    jobs = [start("generate", CARD, "--model", alias, "--budget", "1", "--refresh", "--approved", "自检：并发八路")
            for alias in CONCURRENT_MODELS + ["c7"]]
    time.sleep(0.3)
    feedback = ctx.run("feedback", CARD, "revise", "--reason", "自检：出图途中写的反馈")
    outputs = [job.communicate(timeout=120)[0] for job in jobs]
    problems = []
    for job, output in zip(jobs, outputs):
        if job.returncode != 0:
            tail = re.sub(r"\s+", " ", output)[-300:]
            problems.append(f"有一路 generate 退出码 {job.returncode}: {tail}")
    if feedback.returncode != 0:
        problems.append(f"feedback 退出码 {feedback.returncode}: {brief(feedback)}")
    try:
        manifest = json.loads((ctx.tmp / "output" / "manifest.json").read_text(encoding="utf-8"))
    except ValueError as error:
        return False, f"manifest 写坏了，解析不了: {error}"
    for alias in CONCURRENT_MODELS:
        job = manifest["jobs"].get(f"{CARD}|{alias}|1") or {}
        if job.get("status") != "ok":
            problems.append(f"{CARD}|{alias}|1 状态是 {job.get('status')!r}（期望 ok，记录丢了或被覆盖）")
        if f"{CARD}|{alias}" not in manifest["approvals"]:
            problems.append(f"批准记录 {CARD}|{alias} 丢了")
    same_model = outputs[-2:]
    generated = sum(f"{CARD}|c7|1: ok ->" in out for out in same_model)
    skipped = sum("being generated by another process" in out or "1 already ok" in out for out in same_model)
    if (generated, skipped) != (1, 1):
        problems.append(f"同卡同模型的两个进程应恰好生成一次、跳过一次，实际生成 {generated} 次、跳过 {skipped} 次")
    card = ctx.load_card()
    if not any("出图途中写的反馈" in str(item.get("reason")) for item in card.get("feedback") or []):
        problems.append("出图途中写进卡的反馈被 generate 收尾时盖掉了")
    if card.get("status") != "generated":
        problems.append(f"卡状态是 {card.get('status')!r}（期望 generated）")
    leftovers = [p.name for p in ctx.tmp.rglob(".*.tmp")]
    if leftovers:
        problems.append(f"留下了临时文件: {leftovers[:3]}")
    return (not problems), "；".join(problems)


def start_review_web(ctx):
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, str(REVIEW_WEB), "--config", str(ctx.config), "--port", str(port)],
        cwd=str(ctx.tmp), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.time() + 20
    while time.time() < deadline and proc.poll() is None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
                if response.status == 200:
                    return proc, port
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    return proc, None


def post(port, path, body):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST",
                                     data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return {"ok": False, "body": error.read().decode("utf-8", "replace")}


def check_18(ctx):
    """审核页同时点 6 个「要」，命令行同时给另一张下结论：一条不丢；「要」的图进 pick（视频 image:<卡> 读它）。"""
    import threading

    names = [f"{CARD}_{alias}_1" for alias in CONCURRENT_MODELS]
    if not all(list((ctx.tmp / "output" / "candidates").glob(f"{name}.*")) for name in names):
        return False, "并发出的候选图不全（检查 17 失败）"
    proc, port = start_review_web(ctx)
    problems = []
    try:
        if port is None:
            return False, "审核页没能启动（20 秒内访问不到）"
        replies = {}

        def keep(name):
            replies[name] = post(port, "/api/keep", {"card": CARD, "candidate": name})

        threads = [threading.Thread(target=keep, args=(name,)) for name in names[:6]]
        for thread in threads:
            thread.start()
        cli = ctx.run("review", CARD, names[6], "fail", "--by", "human", "--note", "自检：命令行同时否决")
        for thread in threads:
            thread.join(60)
        for name, reply in replies.items():
            if reply.get("ok") is not True:
                problems.append(f"/api/keep {name} 没成功: {json.dumps(reply, ensure_ascii=False)[:200]}")
        if cli.returncode != 0:
            problems.append(f"命令行 review 退出码 {cli.returncode}: {brief(cli)}")
        review = json.loads((ctx.tmp / "output" / "review" / f"{CARD}.json").read_text(encoding="utf-8"))
        verdicts = {Path(k).stem: v.get("verdict") for k, v in review["candidates"].items()}
        picks = {Path(k).stem for k in review.get("pick") or []}
        for name in names[:6]:
            if verdicts.get(name) != "pass":
                problems.append(f"{name} 的「要」结论丢了（{verdicts.get(name)!r}）")
            if name not in picks:
                problems.append(f"{name} 点了「要」却不在 pick 里，视频 image:<卡> 取不到")
        if verdicts.get(names[6]) != "fail":
            problems.append(f"命令行同时写的结论被盖掉了（{verdicts.get(names[6])!r}）")
        if ctx.load_card().get("status") != "registered":
            problems.append(f"卡状态是 {ctx.load_card().get('status')!r}（期望 registered）")
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return (not problems), "；".join(problems)


KEEP_CARD = "k-01"
STYLE = "自检画风"


def _sources(ctx):
    return yaml.safe_load((ctx.tmp / "sources.yaml").read_text(encoding="utf-8")) or {}


def _keep_card_file(ctx):
    return next((ctx.tmp / "cards").rglob(f"{KEEP_CARD}_*.yaml"))


def _stop(proc):
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def check_19(ctx):
    """审核页「要」= 通过（human）+ 进 pick + 登记草稿；同一张再点不重复登记。"""
    for args in (("new-card", KEEP_CARD, "自检：要"), ("compose", KEEP_CARD),
                 ("generate", KEEP_CARD, "--budget", "1", "--approved", "自检：要的那张")):
        proc = ctx.run(*args)
        if proc.returncode != 0:
            return False, f"{' '.join(args)} 退出码 {proc.returncode}: {brief(proc)}"
        if args[0] == "new-card":
            card_file = _keep_card_file(ctx)
            card = yaml.safe_load(card_file.read_text(encoding="utf-8")) or {}
            card["action"] = "a lone lantern on a snowy road"
            card_file.write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")
    found = sorted((ctx.tmp / "output" / "candidates").glob(f"{KEEP_CARD}_*"))
    if not found:
        return False, f"{KEEP_CARD} 没出候选图"
    name = found[0].name
    proc, port = start_review_web(ctx)
    problems = []
    try:
        if port is None:
            return False, "审核页没能启动（20 秒内访问不到）"
        library_file = ctx.tmp / "素材库" / "library.json"

        def entries():
            return json.loads(library_file.read_text(encoding="utf-8")).get("entries") or [] if library_file.is_file() else []

        def ingest_reply(reply):              # register 打的那段 JSON（added / already 是素材库条目 id）
            text = reply.get("output", "")
            try:
                return json.loads(text[text.index("{"):text.rindex("}") + 1])
            except ValueError:
                return {}

        first = post(port, "/api/keep", {"card": KEEP_CARD, "candidate": name})
        count = len(entries())
        second = post(port, "/api/keep", {"card": KEEP_CARD, "candidate": name})
        for label, reply in (("第一次", first), ("第二次", second)):
            if reply.get("ok") is not True:
                problems.append(f"{label} /api/keep 没成功: {json.dumps(reply, ensure_ascii=False)[:200]}")
        ids = (ingest_reply(first).get("added") or []) + (ingest_reply(first).get("already") or [])
        if len(ids) != 1:
            problems.append(f"第一次「要」应登记 1 条，实际 {ids}")
        if not ingest_reply(second).get("already") or ingest_reply(second).get("added"):
            problems.append("同一张再点「要」，没有返回 already: " + second.get("output", "")[-200:])
        if len(entries()) != count:
            problems.append(f"同一张再点「要」，素材库从 {count} 条变成 {len(entries())} 条")
        entry = next((e for e in entries() if ids and e["id"] == ids[0]), None)
        if not entry or entry.get("status") != "draft" or entry.get("production_approved"):
            problems.append(f"登记的不是草稿: {entry and entry.get('status')!r}")
    finally:
        _stop(proc)
    review = json.loads((ctx.tmp / "output" / "review" / f"{KEEP_CARD}.json").read_text(encoding="utf-8"))
    verdict = review["candidates"].get(name) or {}
    if verdict.get("verdict") != "pass" or verdict.get("by") != "human":
        problems.append(f"结论应是 pass by human，实际 {verdict.get('verdict')!r} by {verdict.get('by')!r}")
    if name not in (review.get("pick") or []):
        problems.append("「要」的图不在 pick 里")
    if yaml.safe_load(_keep_card_file(ctx).read_text(encoding="utf-8")).get("status") != "registered":
        problems.append("卡状态不是 registered")
    return (not problems), "；".join(problems)


def check_20(ctx):
    """promote --as style:X --text --look：真源表写进描述；引用它的卡 compose 后含描述；describe 改描述后 generate 报 stale。"""
    found = sorted((ctx.tmp / "output" / "candidates").glob(f"{KEEP_CARD}_*"))
    if not found:
        return False, "没有候选图（检查 19 失败）"
    text = "misty ink wash, pale paper grain"
    proc = ctx.run("promote", found[0].stem, "--as", f"style:{STYLE}", "--text", text, "--look", "画风",
                   "--approved", "自检：画风就这张")
    if proc.returncode != 0:
        return False, f"promote style 退出码 {proc.returncode}: {brief(proc)}"
    problems = []
    entry = (_sources(ctx).get("styles") or {}).get(STYLE) or {}
    if entry.get("prompt") != text or entry.get("look") != "画风":
        problems.append(f"sources.yaml 里 {STYLE} 的 prompt/look 不对: {entry.get('prompt')!r} / {entry.get('look')!r}")
    card_file = _keep_card_file(ctx)
    card = yaml.safe_load(card_file.read_text(encoding="utf-8")) or {}
    card["styles"] = [STYLE]
    card_file.write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")
    proc = ctx.run("compose", KEEP_CARD)
    if proc.returncode != 0:
        return False, f"引用画风后 compose 退出码 {proc.returncode}: {brief(proc)}"
    if text not in (ctx.tmp / "output" / "prompt" / f"{KEEP_CARD}.txt").read_text(encoding="utf-8"):
        problems.append("compose 出来的提示词里没有画风描述")
    if ctx.run("describe", "--as", f"style:{STYLE}", "--text", "bright flat cel shading").returncode == 0:
        problems.append("describe 不带 --approved 却成功了")
    proc = ctx.run("describe", "--as", f"style:{STYLE}", "--text", "bright flat cel shading", "--approved", "自检：描述改成这样")
    entry = (_sources(ctx).get("styles") or {}).get(STYLE) or {}
    if proc.returncode != 0 or entry.get("prompt") != "bright flat cel shading" or entry.get("history"):
        problems.append(f"describe 改描述不对（退出码 {proc.returncode}，prompt={entry.get('prompt')!r}，"
                        f"history={entry.get('history')!r}）: {brief(proc)}")
    log = [json.loads(x) for x in (ctx.tmp / "output" / "promotions.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    last = log[-1] if log else {}
    if (last.get("action"), last.get("approved"), last.get("before"), last.get("text")) != \
            ("describe", "自检：描述改成这样", text, "bright flat cel shading"):
        problems.append(f"describe 没在 promotions.jsonl 记账（谁批、改前、改后）: {last}")
    proc = ctx.run("generate", KEEP_CARD, "--budget", "2", "--approved", "自检：改了描述再出")
    if proc.returncode == 0 or "stale" not in ctx.out(proc):
        problems.append("改了画风描述，generate 没有报 stale: " + brief(proc, 200))
    proc = ctx.run("compose", KEEP_CARD)    # 收尾：恢复成新描述下的提示词
    if proc.returncode != 0:
        problems.append(f"改描述后重新 compose 退出码 {proc.returncode}: {brief(proc)}")
    return (not problems), "；".join(problems)


def check_21(ctx):
    """pack 命令改归类；packs.categories 删掉「风格」「场景」后，挂在下面的卡和真源照常在 state 里。"""
    problems = []
    proc = ctx.run("pack", KEEP_CARD, "风格/自检/镜1")
    if proc.returncode != 0:
        return False, f"pack 退出码 {proc.returncode}: {brief(proc)}"
    raw = yaml.safe_load(ctx.config.read_text(encoding="utf-8")) or {}
    raw.setdefault("packs", {})["categories"] = ["人物", "道具", "测试"]
    ctx.config.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    ctx.git("commit", "-qam", "自检：分类删掉风格、场景")
    proc = ctx.run("state", "--json")
    if proc.returncode != 0:
        return False, f"state --json 退出码 {proc.returncode}: {brief(proc)}"
    data = json.loads(proc.stdout)
    card = next((c for c in data["cards"] if c["id"] == KEEP_CARD), None)
    if not card:
        problems.append(f"删分类后 state 里没有 {KEEP_CARD}")
    elif card["pack"] != ["风格", "自检", "镜1"]:
        problems.append(f"{KEEP_CARD} 的 pack 是 {card['pack']}（期望 风格/自检/镜1）")
    elif not card["candidates"]:
        problems.append(f"删分类后 {KEEP_CARD} 的候选图不见了")
    style = next((x for x in data["sources"] if x["name"] == STYLE), None)
    if not style or style["pack"][0] != "风格":
        problems.append(f"删分类后画风真源 {STYLE} 不在 state 里或不在「风格」下")
    if "风格" in data["categories"]:
        problems.append("categories 还带着已删的「风格」")
    if not list((ctx.tmp / "cards").rglob(f"{KEEP_CARD}_*.yaml")) or not list((ctx.tmp / "output" / "candidates").glob(f"{KEEP_CARD}_*")):
        problems.append("改 pack 搬动了文件")
    proc = ctx.run("pack", KEEP_CARD)
    if proc.returncode != 0 or "默认规则" not in ctx.out(proc):
        problems.append("pack 给空没回到默认规则: " + brief(proc, 200))
    return (not problems), "；".join(problems)


def check_22(ctx):
    """state --json 和审核页 /api/state 是同一份数据。"""
    proc, port = start_review_web(ctx)
    try:
        if port is None:
            return False, "审核页没能启动（20 秒内访问不到）"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=30) as response:
            page = json.loads(response.read().decode("utf-8"))
    finally:
        _stop(proc)
    cli = ctx.run("state", "--json")
    if cli.returncode != 0:
        return False, f"state --json 退出码 {cli.returncode}: {brief(cli)}"
    data = json.loads(cli.stdout)
    page.pop("today", None)
    data.pop("today", None)
    if data != page:
        diff = [key for key in set(data) | set(page) if data.get(key) != page.get(key)]
        return False, f"state --json 和 /api/state 不一致：{diff}"
    plain = ctx.run("state")
    if plain.returncode != 0 or KEEP_CARD not in plain.stdout:
        return False, "state（不带 --json）没列出卡: " + brief(plain, 200)
    return True, ""


PAGE_ONLY = {"/api/library/decide": "定稿只归人，故意没有命令"}


def check_23(ctx):
    """页面能做的，命令都能做：审核页每个 POST 接口都调一个 pipeline 子命令；页面调的接口后端都有。"""
    sys.path.insert(0, str(SKILL / "scripts"))
    import pipeline

    commands = set()
    for action in pipeline.build_parser()._subparsers._group_actions:
        for sub in action.choices.values():
            func = sub.get_default("func")
            if func:
                commands.add(func.__name__)
    source = REVIEW_WEB.read_text(encoding="utf-8")
    body = source[source.index("def _dispatch("):source.index("def main(")]
    branches = re.split(r"\n\s+(?:el)?if path\b", body)[1:]
    problems, endpoints = [], set()
    for branch in branches:
        head = branch.split("\n", 1)[0]
        names = re.findall(r'"(/api/[^"]+)"', head)
        if not names:
            continue
        endpoints.update(names)
        if all(name in PAGE_ONLY for name in names):
            continue
        called = set(re.findall(r"pl\.(cmd_\w+)", branch))
        if not called:
            problems.append(f"{'/'.join(names)} 没有调任何命令")
        for func in called - commands:
            problems.append(f"{'/'.join(names)} 调的 {func} 不是命令行子命令")
    page = (SKILL / "web" / "review.html").read_text(encoding="utf-8")
    for used in sorted(set(re.findall(r"/api/[a-z/-]+", page)) - endpoints - {"/api/state"}):
        problems.append(f"页面调了 {used}，后端没有这个接口")
    if len(endpoints) < 5:
        problems.append(f"只认出 {len(endpoints)} 个接口，解析 review_web.py 可能失效了")
    return (not problems), "；".join(problems)


def check_24(ctx):
    """sources.kinds 加新种类：promote --as 新种类进 sources.yaml；卡里引用后图进参考、描述进提示词；state 带种类和标签；去掉声明真源不丢。"""
    found = sorted((ctx.tmp / "output" / "candidates").glob(f"{KEEP_CARD}_*"))
    if not found:
        return False, "没有候选图（检查 19 失败）"
    raw = yaml.safe_load(ctx.config.read_text(encoding="utf-8")) or {}
    raw["sources"] = {"kinds": {"scenes": {"label": "场景", "as": "scene"},
                                "lenses": {"label": "镜头语言", "as": "lens", "text": True}}}
    ctx.config.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    ctx.git("commit", "-qam", "自检：加两个真源种类")
    problems = []
    bad = ctx.run("promote", found[0].stem, "--as", "planet:火星", "--approved", "自检：没声明的种类")
    if bad.returncode == 0 or "scene:<name>" not in ctx.out(bad):
        problems.append("没声明的种类 promote 没被拒，或报错没列出可用种类: " + brief(bad, 200))
    proc = ctx.run("promote", found[0].stem, "--as", "scene:城门", "--approved", "自检：场景就这张")
    text = "slow dolly in, shallow depth of field"
    proc2 = ctx.run("promote", found[0].stem, "--as", "lens:推镜", "--text", text, "--approved", "自检：镜头就这样")
    if proc.returncode or proc2.returncode:
        return False, f"新种类 promote 失败: {brief(proc, 150)} / {brief(proc2, 150)}"
    registry = _sources(ctx)
    if not (registry.get("scenes") or {}).get("城门", {}).get("source"):
        problems.append("sources.yaml 里没有 scenes.城门")
    if ((registry.get("lenses") or {}).get("推镜") or {}).get("prompt") != text:
        problems.append("sources.yaml 里 lenses.推镜 没带描述")
    card_file = _keep_card_file(ctx)
    card = yaml.safe_load(card_file.read_text(encoding="utf-8")) or {}
    before = dict(card)
    card.update(scenes=["城门"], lenses=["推镜"])
    card_file.write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")
    proc = ctx.run("compose", KEEP_CARD)
    prompt_file = ctx.tmp / "output" / "prompt" / f"{KEEP_CARD}.txt"
    composed = prompt_file.read_text(encoding="utf-8") if prompt_file.is_file() else ""
    # 场景样张和这张卡别的参考图是同一个文件，按字节去重后只剩一张，所以这里只查描述；场景被引用看下面的 used_by
    if proc.returncode or text not in composed:
        problems.append(f"引用新种类后 compose 没带上描述: {brief(proc, 200)}")
    data = json.loads(ctx.run("state", "--json").stdout or "{}")
    kinds = {k["section"]: k for k in data.get("kinds") or []}
    if kinds.get("scenes", {}).get("label") != "场景" or not kinds.get("lenses", {}).get("text"):
        problems.append(f"state 里的 kinds 不对: {list(kinds)}")
    scene = next((x for x in data.get("sources") or [] if x["section"] == "scenes"), None)
    if not scene or scene["label"] != "场景" or scene["pack"] != ["场景", "城门", "长相"] or KEEP_CARD not in scene["used_by"]:
        problems.append(f"场景真源在 state 里不对: {scene and (scene['label'], scene['pack'], scene['used_by'])}")
    raw.pop("sources", None)
    ctx.config.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    card_file.write_text(yaml.safe_dump(before, allow_unicode=True, sort_keys=False), encoding="utf-8")
    ctx.git("commit", "-qam", "自检：去掉种类声明")
    data = json.loads(ctx.run("state", "--json").stdout or "{}")
    if not any(x["section"] == "scenes" and x["name"] == "城门" for x in data.get("sources") or []):
        problems.append("去掉 sources.kinds 声明后，已登记的场景真源从 state 里丢了")
    proc = ctx.run("compose", KEEP_CARD)    # 收尾：卡恢复原样
    if proc.returncode != 0:
        problems.append(f"收尾 compose 退出码 {proc.returncode}: {brief(proc)}")
    return (not problems), "；".join(problems)


def check_25(ctx):
    """card-core 副本（scripts/core）和旁边的 card-core 源头一致；Skill 单独装、没有源头时跳过。"""
    sync = Path(__file__).resolve().parents[2] / "card-core" / "sync.py"
    if not sync.is_file():
        return True, "跳过：旁边没有 card-core 源头（单独装的 Skill 只带副本）"
    proc = subprocess.run([sys.executable, str(sync), "--check"], capture_output=True, text=True)
    if proc.returncode != 0 or "✓ image-card-pipeline" not in proc.stdout.splitlines():
        return False, "scripts/core 和源头不一致（或生图不在 consumers.json 里）: " + brief(proc, 200)
    return True, ""


def check_26(ctx):
    """卡上 promote 字段：解析 / 校验；审核页定真源的默认值由脚本推（写了照写的，没写按包 / 角色 / styles / 标题）。"""
    sys.path.insert(0, str(SKILL / "scripts"))
    import pipeline as pl
    problems = []
    for value, want in (("character:主角", "look"), ("prop:主角/剑", "look"), ("style:黄昏 @氛围", "mood"),
                        ("style:墨白 @画风", "paint"), ("template:城门回头", "comp")):
        got = pl.parse_promote(value)
        if not got or got["want"] != want:
            problems.append(f"{value} 解析成 {got}")
    for bad in ("style:墨白", "character:x @画风", "foo:x", "character:"):
        try:
            pl.parse_promote(bad)
            problems.append(f"{bad} 没被拒")
        except ValueError:
            pass
    registry = {"styles": {"胶片": {"look": "画风"}, "雪_镜头糊雪": {}}}
    got = pl.promote_defaults({"id": "03-01", "title": "胶片·镜头糊雪", "characters": [{"name": "主角"}],
                               "styles": ["雪_镜头糊雪"]}, registry=registry)
    if (got["paint"], got["mood"]) != ("胶片", "雪_镜头糊雪"):
        problems.append(f"03-01 画风 / 氛围推成 {got['paint']} / {got['mood']}（期望 胶片（标题）/ 雪_镜头糊雪（没标画风的 style 算氛围））")
    cases = [   # (卡, 期望的 want / 长相 / 氛围名)
        ({"id": "02-02", "title": "主角·孤独雪原", "characters": [{"name": "主角"}], "styles": ["雪_孤独雪原"]},
         "look", "character:主角", "雪_孤独雪原"),
        ({"id": "s-真源-水墨", "title": "主角真源·水墨国风", "kind": "sketch", "pack": "人物/主角/画风试样"},
         "look", "character:主角", "水墨国风"),
        ({"id": "s-剑", "title": "主角·剑", "kind": "sketch", "pack": "道具/主角/剑"}, "look", "prop:主角/剑", "剑"),
        ({"id": "s-黄昏", "title": "黄昏夕阳·纯氛围参考", "kind": "sketch", "pack": "风格/黄昏/草图",
          "promote": "style:黄昏夕阳 @氛围"}, "mood", "character:黄昏夕阳", "黄昏夕阳"),
        ({"id": "P-02", "title": "配角·古风剑仙", "characters": [{"name": "配角"}], "outfit": "剑仙",
          "pack": "人物/配角/设定测试"}, "look", "character:配角/剑仙", "古风剑仙"),
    ]
    for card, want, look, style in cases:
        got = pl.promote_defaults(card)
        if (got["want"], f"{got['look_as']}:{got['look_name']}", got["mood"]) != (want, look, style):
            problems.append(f"{card['id']}: {got['want']} / {got['look_as']}:{got['look_name']} / {got['mood']}"
                            f"（期望 {want} / {look} / {style}）")
    proc = ctx.run("sketch", "自检promote", "--idea", "A test.", "--promote", "style:墨白", "--approved", APPROVAL)
    if proc.returncode == 0 or "@画风" not in proc.stdout + proc.stderr:
        problems.append("sketch --promote 写错没被拒: " + brief(proc, 200))
    return (not problems), "；".join(problems)


def check_27(ctx):
    """生态契约：生图只读视频卡，标出哪些素材正被当关键帧（image:<卡> / library:<id> / 按内容认的文件路径）。"""
    sys.path.insert(0, str(SKILL / "scripts"))
    import pipeline as pl
    if not ctx.candidates:
        ctx.candidates_now()
    if len(ctx.candidates) < 2:
        return False, "候选图不到 2 张（前面的 generate 失败）"
    a, b = Path(ctx.candidates[0]).stem, Path(ctx.candidates[1]).stem
    for stem in (a, b):
        keep = ctx.run("keep", CARD, stem)
        if keep.returncode:
            return False, "keep 失败: " + brief(keep)
    entries = {Path(e["file"]).stem: e for e in pl.project_state(pl.Config(ctx.config))["library"]["entries"]}
    if a not in entries or b not in entries:
        return False, f"素材库里没有 {a} / {b}"
    video = ctx.tmp / "视频项目"
    (video / "vcards").mkdir(parents=True, exist_ok=True)
    (video / "project.yaml").write_text(yaml.safe_dump({"video": {"paths": {"cards": "vcards"}}}), encoding="utf-8")
    library_file = ctx.tmp / "素材库" / entries[b]["file"].split("素材库/", 1)[-1]
    card = {"id": "V-01", "title": "自检镜头", "duration": 6, "first_frame": f"image:{CARD}",
            "keyframes": [f"library:{entries[b]['id']}", f"image:{CARD}/{a}"], "last_frame": str(library_file)}
    (video / "vcards" / "V-01_自检.yaml").write_text(yaml.safe_dump(card, allow_unicode=True), encoding="utf-8")
    raw = yaml.safe_load(ctx.config.read_text(encoding="utf-8"))
    raw["link"] = {"video_config": "视频项目/project.yaml"}
    ctx.config.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    problems = []
    try:
        state = pl.project_state(pl.Config(ctx.config))
        got = {Path(e["file"]).stem: [(u["card"], u["slot"], u["at"]) for u in e.get("keyframe_of", [])]
               for e in state["library"]["entries"]}
        first = Path(pl.json.loads((ctx.tmp / "output/review" / f"{CARD}.json").read_text(encoding="utf-8"))["pick"][0]).stem
        want = {a: [("V-01", "middle", 4.0)], b: [("V-01", "middle", 2.0), ("V-01", "last", 6.0)]}
        want.setdefault(first, []).append(("V-01", "first", 0.0))   # image:<卡> = 这张卡 pick 的第一张
        for stem in got:
            if sorted(got[stem]) != sorted(want.get(stem, [])):
                problems.append(f"{stem} 的 keyframe_of 是 {got[stem]}，期望 {want.get(stem, [])}")
    finally:
        raw.pop("link", None)
        ctx.config.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
        for stem in (a, b):
            ctx.run("unregister", CARD, stem)
    if pl.video_keyframe_uses(pl.Config(ctx.config), [{"id": "x", "card": CARD, "candidate": a}]):
        problems.append("没配视频项目时也报了关键帧")
    return (not problems), "；".join(problems)


# --------------------------------------------------------------------------- #
# 付费安全：本机假供应商（不联网、不花钱），按路径给出各种坏回包
# --------------------------------------------------------------------------- #

class FakeProvider:
    """127.0.0.1 上的假上游。base_url 写 http://127.0.0.1:<port>/<模式>/v1，模式决定怎么回；hits 记每种模式被打了几次。"""

    def __init__(self):
        import http.server
        import threading
        fake = self
        self.hits = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code, body, kind="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):          # 结果图的下载地址：一律 404（下载失败）
                self._reply(404, {"error": "gone"})

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                mode = self.path.strip("/").split("/")[0]
                fake.hits[mode] = fake.hits.get(mode, 0) + 1
                port = self.server.server_address[1]
                if mode == "noimage":
                    return self._reply(200, {"data": [{}], "choices": [{"message": {"content": "sorry, no image"}}]})
                if mode == "badurl":
                    return self._reply(200, {"data": [{"url": f"http://127.0.0.1:{port}/gone.png"}]})
                if mode == "notjson":
                    return self._reply(200, b"<html>gateway</html>", "text/html")
                if mode == "e500":
                    return self._reply(500, {"error": "upstream"})
                if mode == "e400":
                    return self._reply(400, {"error": "bad request"})
                if mode == "hang":
                    time.sleep(3)
                    return self._reply(200, {"data": [{}]})
                return self._reply(404, {"error": "unknown mode"})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def base(self, mode):
        return f"http://127.0.0.1:{self.port}/{mode}/v1"

    def close(self):
        self.server.shutdown()


def _set_models(ctx, entries, extra=None):
    raw = yaml.safe_load(ctx.config.read_text(encoding="utf-8")) or {}
    raw.setdefault("providers", {}).setdefault("entries", {}).update(entries.get("providers", {}))
    raw.setdefault("models", {}).update(entries.get("models", {}))
    for key, value in (extra or {}).items():
        if value is None:
            raw.pop(key, None)
        else:
            raw[key] = value
    ctx.config.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    ctx.git("commit", "-qam", "自检：改模型 / 供应商配置")


def _manifest(ctx):
    return json.loads((ctx.tmp / "output" / "manifest.json").read_text(encoding="utf-8"))


def check_28(ctx):
    """付费请求发出后的任何异常都记 unknown、不重发；只有确定没发出去才记 failed；回包原文先落 run 目录；forget 要原因。"""
    if ctx.card_file is None:
        return False, "默认卡不存在（检查 2 失败）"
    fake = FakeProvider()
    key_file = ctx.tmp / ".fake-key"
    key_file.write_text("sk-selfcheck", encoding="utf-8")
    closed = free_port()       # 没人监听的端口：连不上 = 确定没发出去
    cases = {   # 别名: (kind, base_url, 期望状态, 期望假上游被打几次)
        "f-noimg": ("openai_images", fake.base("noimage"), "unknown", 1),
        "f-badurl": ("openai_images", fake.base("badurl"), "unknown", 1),
        "f-notjson": ("openai_images", fake.base("notjson"), "unknown", 1),
        "f-500": ("openai_images", fake.base("e500"), "unknown", 1),
        "f-hang": ("openai_images", fake.base("hang"), "unknown", 1),
        "f-400": ("openai_images", fake.base("e400"), "failed", 1),
        "f-refused": ("openai_images", f"http://127.0.0.1:{closed}/v1", "failed", 0),
        "f-chatnoimg": ("chat_completions", fake.base("noimage"), "unknown", 1),
        "f-chatrefused": ("chat_completions", f"http://127.0.0.1:{closed}/v1", "failed", 0),
    }
    problems = []
    try:
        providers_cfg, models = {}, {}
        for alias, (kind, base, _state, _hits) in cases.items():
            providers_cfg[alias] = {"kind": kind, "base_url": base, "api_key_file": str(key_file), "timeout": 1,
                                    "attempts": 2}
            models[alias] = {"provider": alias, "model": "fake-model"}
        _set_models(ctx, {"providers": providers_cfg, "models": models})
        for alias, (kind, _base, want, hits) in cases.items():
            mode = _base.rsplit("/v1", 1)[0].rsplit("/", 1)[-1]
            before = fake.hits.get(mode, 0)
            proc = ctx.run("generate", CARD, "--model", alias, "--budget", "1", "--refresh", "--approved", "自检：假供应商一张")
            job = _manifest(ctx)["jobs"].get(f"{CARD}|{alias}|1") or {}
            if job.get("status") != want:
                problems.append(f"{alias}: 状态 {job.get('status')!r}（期望 {want}）: {brief(proc, 200)}")
            if kind and mode in fake.hits and fake.hits[mode] - before != hits:
                problems.append(f"{alias}: 假上游被打了 {fake.hits[mode] - before} 次（期望 {hits}，chat 不该重试）")
            run = ctx.tmp / "output" / "runs" / f"{CARD}-{alias}-1"
            if want == "unknown" and alias in ("f-noimg", "f-badurl", "f-chatnoimg") and not (run / "response.json").is_file():
                problems.append(f"{alias}: 拿到了回包却没把原文写进 {run.name}/response.json")
        # unknown 再跑不重发
        before = fake.hits.get("noimage", 0)
        again = ctx.run("generate", CARD, "--model", "f-noimg", "--budget", "1", "--refresh", "--approved", "自检：假供应商再跑")
        if fake.hits.get("noimage", 0) != before or "will not re-send" not in ctx.out(again):
            problems.append("unknown 的序号再跑被重发了（或没提示）: " + brief(again, 200))
        # forget：不写原因被拒；写了原因才清掉，run 目录挪走留底，再跑才重发
        if ctx.run("forget", CARD, "1", "--model", "f-noimg", "--why", " ").returncode == 0:
            problems.append("forget 不写原因也通过了")
        if ctx.run("forget", CARD, "1", "--model", "f-400", "--why", "自检：failed 不需要 forget").returncode == 0:
            problems.append("forget 一个 failed 的序号也通过了（只该清 unknown）")
        done = ctx.run("forget", CARD, "1", "--model", "f-noimg", "--why", "自检：看过 run 目录，没出图")
        manifest = _manifest(ctx)
        if done.returncode != 0 or f"{CARD}|f-noimg|1" in manifest["jobs"] or \
                not any(f.get("why") == "自检：看过 run 目录，没出图" for f in manifest.get("forgotten") or []):
            problems.append("forget 没生效或没留底: " + brief(done, 200))
        if not list((ctx.tmp / "output" / "runs" / "_forgotten").glob(f"{CARD}-f-noimg-1-*")):
            problems.append("forget 后 run 目录没挪进 output/runs/_forgotten/")
        before = fake.hits.get("noimage", 0)
        ctx.run("generate", CARD, "--model", "f-noimg", "--budget", "1", "--refresh", "--approved", "自检：forget 后再跑")
        if fake.hits.get("noimage", 0) != before + 1:
            problems.append("forget 之后再跑没有重发")
        # running 但进程已死：记 unknown，不重发
        _set_models(ctx, {"models": {"f-dead": {"provider": "mock"}}})
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        def orphan(m):
            m["jobs"][f"{CARD}|f-dead|1"] = {"status": "running", "pid": dead.pid, "started": "2000-01-01T00:00:00"}
        path = ctx.tmp / "output" / "manifest.json"
        data = _manifest(ctx)
        orphan(data)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        proc = ctx.run("generate", CARD, "--model", "f-dead", "--budget", "1", "--refresh", "--approved", "自检：死进程留下的")
        job = _manifest(ctx)["jobs"].get(f"{CARD}|f-dead|1") or {}
        if job.get("status") != "unknown" or list((ctx.tmp / "output" / "candidates").glob(f"{CARD}_f-dead_1.*")):
            problems.append(f"running 但进程已死的序号被重发了或没记 unknown（{job.get('status')!r}）: {brief(proc, 200)}")
        ctx.git("add", "-A")
        ctx.git("commit", "-qm", "自检：假供应商跑完")
    finally:
        fake.close()
    return (not problems), "；".join(problems)


def _noise_png(path, side, smooth=False):
    """自检用的大参考图：smooth=True 是渐变（能压很小），否则是噪点（压不下去）。"""
    import os
    import struct
    import zlib
    rows = []
    for y in range(side):
        if smooth:
            rows.append(b"\x00" + b"".join(bytes((x * 255 // side, y * 255 // side, 128)) for x in range(side)))
        else:
            rows.append(b"\x00" + os.urandom(side * 3))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(b"".join(rows), 0)) + chunk(b"IEND", b""))


REF_CARD = "r-01"


def check_29(ctx):
    """参考图发不全就拒跑：超 max_refs、超内联上限又没图床、图床有一张传失败；超体积先自动缩小；图床全成功时按原顺序换成 URL。"""
    refs_dir = ctx.tmp / "自检参考"
    refs_dir.mkdir(exist_ok=True)
    smooth, noise, bad = refs_dir / "smooth.png", refs_dir / "noise.png", refs_dir / "bad.png"
    _noise_png(smooth, 900, smooth=True)
    _noise_png(noise, 300)
    _noise_png(bad, 300)
    card_file = ctx.tmp / "cards" / f"{REF_CARD}_refs.yaml"
    card = {"id": REF_CARD, "title": "参考图自检", "action": "a quiet street", "refs": {"files": [str(smooth), str(noise)]}}
    card_file.write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")
    fake = FakeProvider()
    key_file = ctx.tmp / ".fake-key"
    key_file.write_text("sk-selfcheck", encoding="utf-8")
    host_script = ctx.tmp / "fakehost.py"
    host_script.write_text("import sys, pathlib\np = pathlib.Path(sys.argv[1])\n"
                           "sys.exit('refused') if 'bad' in p.name else print('https://img.example/' + p.name)\n", encoding="utf-8")
    problems = []
    try:
        _set_models(ctx, {"providers": {"f-chat2": {"kind": "chat_completions", "base_url": fake.base("noimage"),
                                                    "api_key_file": str(key_file), "max_refs": 1, "timeout": 1}},
                          "models": {"f-chat2": {"provider": "f-chat2", "model": "fake"},
                                     "m-small": {"provider": "mock", "max_inline_bytes": 600_000},
                                     "m-tiny": {"provider": "mock", "max_inline_bytes": 100}}})
        proc = ctx.run("generate", REF_CARD, "--model", "f-chat2", "--budget", "1", "--approved", "自检：超张数")
        if proc.returncode == 0 or "noise.png" not in ctx.out(proc) or fake.hits.get("noimage"):
            problems.append("超 max_refs 没拒跑、没点名发不出去的图，或请求已经发出: " + brief(proc, 200))
        tiny = ctx.run("generate", REF_CARD, "--model", "m-tiny", "--budget", "1", "--approved", "自检：超体积无图床")
        if tiny.returncode == 0 or "image_host" not in ctx.out(tiny) or (_manifest(ctx)["jobs"].get(f"{REF_CARD}|m-tiny|1")):
            problems.append("超内联上限又没图床没拒跑: " + brief(tiny, 200))
        try:
            import PIL  # noqa: F401
            small = ctx.run("generate", REF_CARD, "--model", "m-small", "--budget", "1", "--approved", "自检：缩小后能发")
            request = ctx.tmp / "output" / "runs" / f"{REF_CARD}-m-small-1" / "request.json"
            sent = json.loads(request.read_text(encoding="utf-8"))["refs"] if request.is_file() else []
            if small.returncode != 0 or len(sent) != 2 or "refs-sent" not in sent[0]:
                problems.append(f"超体积时没先缩小再发（refs={sent}）: {brief(small, 200)}")
        except ImportError:
            pass
        # 图床：有一张传失败 -> 整批拒跑；全成功 -> 按顺序换成 URL
        _set_models(ctx, {}, extra={"image_host": {"kind": "cli", "command": f"{sys.executable} {host_script}"}})
        card["refs"]["files"] = [str(noise), str(bad)]
        card_file.write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")
        partial = ctx.run("generate", REF_CARD, "--model", "m-tiny", "--budget", "1", "--refresh", "--approved", "自检：图床部分失败")
        if partial.returncode == 0 or "bad.png" not in ctx.out(partial) or _manifest(ctx)["jobs"].get(f"{REF_CARD}|m-tiny|1"):
            problems.append("图床有一张传失败却照样出了（或没点名是哪张）: " + brief(partial, 200))
        card["refs"]["files"] = [str(noise), str(smooth)]
        card_file.write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")
        hosted = ctx.run("generate", REF_CARD, "--model", "m-tiny", "--budget", "1", "--refresh", "--approved", "自检：图床全成功")
        request = ctx.tmp / "output" / "runs" / f"{REF_CARD}-m-tiny-1" / "request.json"
        sent = json.loads(request.read_text(encoding="utf-8"))["refs"] if request.is_file() else []
        if hosted.returncode != 0 or [Path(u).name for u in sent] != ["noise.png", "smooth.png"] or not all(u.startswith("https://") for u in sent):
            problems.append(f"图床全成功时没按原顺序换成 URL（{sent}）: {brief(hosted, 200)}")
    finally:
        fake.close()
        _set_models(ctx, {}, extra={"image_host": None})
        ctx.git("add", "-A")
        ctx.git("commit", "-qm", "自检：参考图检查收尾")
    return (not problems), "；".join(problems)


def check_30(ctx):
    """generate 前先过 validate：引用没登记的真源 / 找不到的参考图是 PROBLEM，拒跑；模板里空着的 `- name: ''` 不算。"""
    card_file = ctx.tmp / "cards" / "v-01_validate.yaml"
    card = {"id": "v-01", "title": "缺真源", "action": "a figure waits", "characters": [{"name": "没登记的人"}]}
    card_file.write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")
    problems = []
    validate = ctx.run("validate", "v-01")
    if validate.returncode == 0 or "PROBLEM" not in ctx.out(validate) or "没登记的人" not in ctx.out(validate):
        problems.append("引用没登记的角色，validate 没报 PROBLEM: " + brief(validate, 200))
    gen = ctx.run("generate", "v-01", "--budget", "1", "--approved", "自检：缺真源也想出")
    if gen.returncode == 0 or "PROBLEM" not in ctx.out(gen) or (ctx.tmp / "output" / "runs" / "v-01-dry-1").exists():
        problems.append("有 PROBLEM 的卡 generate 没拒跑: " + brief(gen, 200))
    gen = ctx.run("generate", "v-01", "--budget", "1", "--force", "--approved", "自检：缺真源也想出")
    if gen.returncode == 0:
        problems.append("generate --force 放行了有 PROBLEM 的卡")
    card = {"id": "v-01", "title": "缺文件", "action": "a figure waits", "refs": {"files": ["不存在.png"]}}
    card_file.write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")
    if ctx.run("generate", "v-01", "--budget", "1", "--approved", "自检：缺文件也想出").returncode == 0:
        problems.append("找不到的参考图没拦住 generate")
    card = {"id": "v-01", "title": "模板空人物", "action": "a figure waits", "characters": [{"name": "", "expression": ""}]}
    card_file.write_text(yaml.safe_dump(card, allow_unicode=True, sort_keys=False), encoding="utf-8")
    validate = ctx.run("validate", "v-01")
    if validate.returncode != 0:
        problems.append("模板里空着的人物行被当成缺真源: " + brief(validate, 200))
    card_file.unlink()
    ctx.git("add", "-A")
    ctx.git("commit", "-qm", "自检：validate 检查收尾")
    return (not problems), "；".join(problems)


def check_31(ctx):
    """sketch 只认 id 完全相同的卡：s-前缀 不覆盖 s-前缀_长；load_card 先按 id 精确匹配。"""
    problems = []
    one = ctx.run("sketch", "撞名_长", "--idea", "A long-named sketch.", "--approved", APPROVAL)
    two = ctx.run("sketch", "撞名", "--idea", "A short-named sketch.", "--approved", APPROVAL)
    if one.returncode or two.returncode:
        return False, f"sketch 失败: {brief(one, 150)} / {brief(two, 150)}"
    long_file = ctx.tmp / "cards" / "s-撞名_长_sketch.yaml"
    short_file = ctx.tmp / "cards" / "s-撞名_sketch.yaml"
    long_card = yaml.safe_load(long_file.read_text(encoding="utf-8")) if long_file.is_file() else {}
    if long_card.get("id") != "s-撞名_长" or "long-named" not in str(long_card.get("extra_prompt")):
        problems.append(f"短名 sketch 覆盖了长名那张卡: {long_card.get('id')!r} / {long_card.get('extra_prompt')!r}")
    if not short_file.is_file():
        problems.append("短名 sketch 没建自己的卡")
    show = ctx.run("show", "s-撞名")
    if "short-named" not in show.stdout:
        problems.append("show s-撞名 没拿到它自己那张卡: " + brief(show, 200))
    return (not problems), "；".join(problems)


def check_32(ctx):
    """promote 换图时旧的 face / role 一起进 history、从现条目清掉；demote 换回时原样恢复。"""
    found = sorted((ctx.tmp / "output" / "candidates").glob(f"{CARD}_*"))
    if len(found) < 2:
        return False, "候选图不到 2 张"
    a, b = found[0], found[1]
    problems = []
    one = ctx.run("promote", a.stem, "--as", "character:乙", "--face", str(b), "--role", "front view of 乙",
                  "--approved", "自检：乙第一张带脸")
    two = ctx.run("promote", b.stem, "--as", "character:乙", "--approved", "自检：乙换第二张")
    if one.returncode or two.returncode:
        return False, f"promote 失败: {brief(one, 150)} / {brief(two, 150)}"
    entry = (_sources(ctx).get("characters") or {}).get("乙") or {}
    last = (entry.get("history") or [{}])[-1]
    if entry.get("face") or entry.get("role"):
        problems.append(f"换图后现条目还留着旧图的 face / role: {entry.get('face')!r} / {entry.get('role')!r}")
    if not last.get("face") or last.get("role") != "front view of 乙":
        problems.append(f"旧的 face / role 没进 history: {last}")
    back = ctx.run("demote", "--as", "character:乙")
    entry = (_sources(ctx).get("characters") or {}).get("乙") or {}
    if back.returncode or not entry.get("face") or entry.get("role") != "front view of 乙":
        problems.append(f"demote 后 face / role 没恢复: {entry.get('face')!r} / {entry.get('role')!r}: {brief(back, 150)}")
    ctx.run("demote", "--as", "character:乙", "--force")
    return (not problems), "；".join(problems)


def check_33(ctx):
    """审核页写接口只收本机页面的 JSON（Content-Type / Host / Origin）；promote 的名字不许带 .. 或绝对路径。"""
    import http.client
    problems = []
    found = sorted((ctx.tmp / "output" / "candidates").glob(f"{CARD}_*"))
    for as_ in ("template:../../逃出去", "character:../外面", "prop:/tmp/绝对", "character:a//b"):
        proc = ctx.run("promote", found[0].stem, "--as", as_, "--approved", "自检：坏名字")
        if proc.returncode == 0:
            problems.append(f"promote --as {as_} 被接受了")
    if list(ctx.tmp.parent.glob("逃出去*")) or (ctx.tmp / "外面").exists():
        problems.append("坏名字写到了项目目录外面")
    proc, port = start_review_web(ctx)
    try:
        if port is None:
            return False, "审核页没能启动"
        body = json.dumps({"card": CARD, "candidate": found[0].name, "note": "自检"}).encode()
        for label, headers, want in (("text/plain", {"Content-Type": "text/plain"}, 415),
                                     ("外站 Origin", {"Content-Type": "application/json", "Origin": "http://evil.example"}, 403),
                                     ("外来 Host", {"Content-Type": "application/json", "Host": "evil.example"}, 403),
                                     ("本机", {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"}, 200)):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
            conn.request("POST", "/api/note", body=body, headers=headers)
            status = conn.getresponse().status
            conn.close()
            if status != want:
                problems.append(f"{label} POST 返回 {status}（期望 {want}）")
        reply = post(port, "/api/promote", {"candidate": found[0].name, "as": "template:沉淀/../../逃出去", "approved": "自检：坏名字"})
        if reply.get("ok"):
            problems.append("审核页 promote 带 .. 的模板名被接受了")
    finally:
        _stop(proc)
    return (not problems), "；".join(problems)


def check_34(ctx):
    """项目目录不是 git 仓库根（套在别的仓库里）时不往外层仓库提交；init 给它自己建仓库；.gitignore 挡视频片子。"""
    outer = ctx.tmp.parent / (ctx.tmp.name + "-外层仓库")
    problems = []
    try:
        outer.mkdir()
        run_git = lambda *a: subprocess.run(["git", "-C", str(outer), *a], capture_output=True, text=True)
        run_git("init", "-q")
        (outer / "README").write_text("outer", encoding="utf-8")
        run_git("add", "-A")
        run_git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "outer")
        count = lambda: int(run_git("rev-list", "--count", "HEAD").stdout.strip() or 0)
        nested = outer / "子项目"
        nested.mkdir()
        shutil.copy2(ctx.config, nested / "project.yaml")
        proc = subprocess.run([sys.executable, str(PIPELINE), "--config", str(nested / "project.yaml"), "new-card", "n-01", "套娃"],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            problems.append("套在外层仓库里的项目 new-card 失败: " + brief(proc, 200))
        if count() != 1:
            problems.append(f"项目自己不是仓库根，却往外层仓库提交了（外层提交数 {count()}）")
        fresh = outer / "新项目"
        proc = subprocess.run([sys.executable, str(PIPELINE), "--config", str(fresh / "project.yaml"), "init"],
                              capture_output=True, text=True)
        if not (fresh / ".git").is_dir():
            problems.append("在外层仓库里 init 没给项目自己建仓库: " + brief(proc, 200))
        ignore = (fresh / ".gitignore").read_text(encoding="utf-8") if (fresh / ".gitignore").is_file() else ""
        for pattern in ("*.mp4", "*.mov", "*.webm", "*.part", "output/runs/**/response.json"):
            if pattern not in ignore.splitlines():
                problems.append(f"项目 .gitignore 没有 {pattern}")
        if count() != 1:
            problems.append("init 往外层仓库提交了")
    finally:
        shutil.rmtree(outer, ignore_errors=True)
    return (not problems), "；".join(problems)


def check_35(ctx):
    """pick 保持加入顺序（视频 image:<卡> 取第一张）；重复 pick 不挪位置。"""
    found = sorted((ctx.tmp / "output" / "candidates").glob(f"{CARD}_*"))
    if len(found) < 3:
        return False, "候选图不到 3 张"
    a, b, c = (p.stem for p in found[:3])
    path = ctx.tmp / "output" / "review" / f"{CARD}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["pick"] = []
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    for stem in (c, a, c, b):
        ctx.run("pick", CARD, stem)
    picks = [Path(p).stem for p in json.loads(path.read_text(encoding="utf-8"))["pick"]]
    if picks != [c, a, b]:
        return False, f"pick 顺序是 {picks}（期望加入顺序 {[c, a, b]}）"
    return True, ""


def check_36(ctx):
    """本地素材库：定稿条目 unregister 拒绝（人先撤回定稿）；同名不同内容的副本改名 <sha8>_<名>，移掉一条不误删另一条的文件。"""
    sys.path.insert(0, str(SKILL / "scripts"))
    import library
    problems = []
    root = ctx.tmp / "素材库自检"
    spec = {"kind": "local", "root": str(root)}
    one, two = ctx.tmp / "同名甲" / "x.png", ctx.tmp / "同名乙" / "x.png"
    for path, data in ((one, b"\x89PNG\r\n\x1a\nAAAA"), (two, b"\x89PNG\r\n\x1a\nBBBB")):
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
    first = library.ingest(spec, title="t1", files=[str(one)], batch="b")["added"][0]
    second = library.ingest(spec, title="t2", files=[str(two)], batch="b")["added"][0]
    entries = {e["id"]: e for e in library.local_load(spec)["entries"]}
    if entries[first]["file"] == entries[second]["file"]:
        problems.append("同名不同内容的第二张覆盖了第一张的副本")
    elif (root / entries[first]["file"]).read_bytes() != one.read_bytes():
        problems.append("第一张的副本内容被改了")
    library.local_remove(spec, second)
    if not (root / entries[first]["file"]).is_file():
        problems.append("移掉第二张时把第一张的文件删了")
    library.decide(spec, first, "final")
    try:
        library.local_remove(spec, first)
        problems.append("定稿条目被直接移除了")
    except library.LibraryError:
        pass
    # 命令行 unregister：定稿的拒绝；人撤回定稿后才放行
    found = sorted((ctx.tmp / "output" / "candidates").glob(f"{CARD}_*"))
    stem = found[0].stem
    keep = ctx.run("keep", CARD, stem)
    lib_spec = {"kind": "local", "root": str(ctx.tmp / "素材库")}
    entry = next((e for e in library.local_load(lib_spec)["entries"] if Path(e["file"]).stem == stem), None)
    if keep.returncode or not entry:
        return False, "keep 失败: " + brief(keep, 200)
    library.decide(lib_spec, entry["id"], "final")
    proc = ctx.run("unregister", CARD, stem)
    if proc.returncode == 0 or not any(e["id"] == entry["id"] for e in library.local_load(lib_spec)["entries"]):
        problems.append("定稿的图被 unregister 移走了: " + brief(proc, 200))
    library.decide(lib_spec, entry["id"], "pending")
    proc = ctx.run("unregister", CARD, stem)
    if proc.returncode != 0:
        problems.append("撤回定稿后 unregister 仍失败: " + brief(proc, 200))
    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(one.parent, ignore_errors=True)
    shutil.rmtree(two.parent, ignore_errors=True)
    ctx.git("add", "-A")
    ctx.git("commit", "-qm", "自检：素材库检查收尾")
    return (not problems), "；".join(problems)


def check_37(ctx):
    """卖的骨架不带本机专属：文档 / 代码里没有带日期的个人决定（changelog、契约副本除外），不点名具体的图床 Skill。"""
    problems = []
    skip = {"changelog.md", "ecosystem-contract.md", "selfcheck.py", "roles.md"}
    for path in [*(SKILL / "references").glob("*.md"), SKILL / "SKILL.md", *(SKILL / "scripts").glob("*.py"),
                 *(SKILL / "web").glob("*.html")]:
        if path.name in skip:
            continue
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"20\d\d-\d\d-\d\d", text):
            problems.append(f"{path.name} 带日期 {match.group(0)}")
        if "r2-image-host" in text:
            problems.append(f"{path.name} 点名了具体的图床 Skill")
    return (not problems), "；".join(problems[:6])


def check_38(ctx):
    """参考区准入：缺 --pack / 非法大类（评测/xxx）/ 缺 --approved 都整批拒绝，索引、目录、提交都不动。"""
    problems = []
    ctx.git("add", "-A")
    ctx.git("commit", "-qm", "自检：灵感准入检查前收尾")
    image = _external_image(ctx, "准入.png")
    attempts = (
        ([str(image), "--approved", "用户确认收录"], "pack"),
        ([str(image), "--pack", "评测/对比", "--approved", "用户确认收录"], "白名单"),
        ([str(image), "--pack", "风格/测试组"], "approved"),
    )
    for argv, needle in attempts:
        index, files, commits = _inspiration_snapshot(ctx)
        proc = ctx.run("inspire", "add", *argv)
        if proc.returncode == 0:
            problems.append(f"该拒的 inspire add 却成功了（{argv[-2:]}）: " + brief(proc))
        elif needle not in ctx.out(proc):
            problems.append(f"拒绝原因里没有 {needle!r}（{argv[-2:]}）: " + brief(proc, 300))
        after_index, after_files, after_commits = _inspiration_snapshot(ctx)
        if (after_index, after_files, after_commits) != (index, files, commits):
            problems.append(f"被拒的 inspire add 有副作用（{argv[-2:]}）：索引 / 文件 / 提交变了")
    return (not problems), "；".join(problems[:4])


def check_39(ctx):
    """来源防误入：受管候选目录、评测目录、路径段含 upscale 的图，带 --approved 也拒收且零副作用。"""
    problems = []
    if not ctx.candidates:
        ctx.candidates_now()
    if not ctx.candidates:
        return False, "没有候选图（前面的 generate 失败）"
    ctx.git("add", "-A")
    ctx.git("commit", "-qm", "自检：来源拦截检查前收尾")
    candidate = ctx.tmp / "output" / "candidates" / ctx.candidates[0]
    eval_dir = ctx.tmp / "output" / "评测"
    eval_dir.mkdir(parents=True, exist_ok=True)
    eval_image = eval_dir / "对比图.png"
    eval_image.write_bytes(candidate.read_bytes() + b"# eval")
    upscale_dir = ctx.tmp / "外部素材" / "upscale_4k"
    upscale_dir.mkdir(parents=True, exist_ok=True)
    upscale_image = upscale_dir / "超分对比.png"
    upscale_image.write_bytes(candidate.read_bytes() + b"# upscale")
    for source, needle in ((candidate, "受管目录"), (eval_image, "评测"), (upscale_image, "upscale")):
        index, files, commits = _inspiration_snapshot(ctx)
        proc = ctx.run("inspire", "add", str(source), "--pack", "风格/测试组", "--approved", "用户确认收录")
        if proc.returncode == 0:
            problems.append(f"{source.relative_to(ctx.tmp)} 被收进了参考区: " + brief(proc))
        elif needle not in ctx.out(proc):
            problems.append(f"拒绝原因里没有 {needle!r}（{source.name}）: " + brief(proc, 300))
        after_index, after_files, after_commits = _inspiration_snapshot(ctx)
        if (after_index, after_files, after_commits) != (index, files, commits):
            problems.append(f"被拒的 {source.name} 有副作用：索引 / 文件 / 提交变了")
    shutil.rmtree(eval_dir, ignore_errors=True)
    shutil.rmtree(upscale_dir.parent, ignore_errors=True)
    return (not problems), "；".join(problems[:4])


def check_40(ctx):
    """合法入库：--pack 风格/测试组 + --approved 写进索引；一批里混一个坏文件整批不生效。"""
    problems = []
    image = _external_image(ctx, "收录.png")
    added = ctx.run("inspire", "add", str(image), "--pack", "风格/测试组", "--approved", "用户确认收录")
    if added.returncode != 0:
        return False, f"合法 inspire add 退出码 {added.returncode}: {brief(added)}"
    match = re.search(r"\b([0-9a-f]{8})\b", ctx.out(added))
    index_path = ctx.tmp / "inspiration" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {"items": {}}
    entry = index["items"].get(match.group(1), {}) if match else {}
    if entry.get("pack") != "风格/测试组":
        problems.append(f"入库条目的 pack 是 {entry.get('pack')!r}（期望 '风格/测试组'）")
    if entry.get("approved") != "用户确认收录":
        problems.append(f"入库条目的 approved 是 {entry.get('approved')!r}（期望 '用户确认收录'）")
    good = _external_image(ctx, "批量好图.png")
    bad = ctx.tmp / "外部素材" / "坏文件.txt"
    bad.write_text("not an image", encoding="utf-8")
    before_index, before_files, _commits = _inspiration_snapshot(ctx)
    batch = ctx.run("inspire", "add", str(good), str(bad), "--pack", "风格/测试组", "--approved", "用户确认收录")
    after_index, after_files, _after_commits = _inspiration_snapshot(ctx)
    if batch.returncode == 0:
        problems.append("一批里混了非图片文件却成功了: " + brief(batch))
    if (after_index, after_files) != (before_index, before_files):
        problems.append("整批拒绝失败：索引或 inspiration 目录变了")
    ctx.git("add", "-A")
    ctx.git("commit", "-qm", "自检：灵感入库检查收尾")
    return (not problems), "；".join(problems[:4])


def check_41(ctx):
    """旧索引兼容：没有 pack 的历史条目照旧读取、按 风格/<第一个标签> 分组；state 暴露参考区白名单。"""
    problems = []
    path = ctx.tmp / "inspiration" / "index.json"
    index = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else \
        {"format": "image-card-pipeline/inspiration-v1", "items": {}}
    legacy = "deadbeef"
    index["items"][legacy] = {"file": "deadbeef_旧图.png", "from": "旧目录/雪/旧图.png", "note": "旧条目",
                              "tags": ["雪", "精选"], "at": "2025-01-01T00:00:00"}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    proc = ctx.run("state", "--json")
    if proc.returncode != 0:
        return False, f"state --json 退出码 {proc.returncode}: {brief(proc)}"
    data = json.loads(proc.stdout)
    item = next((row for row in data.get("inspiration") or [] if row.get("id") == legacy), {})
    if item.get("pack") != ["风格", "雪"]:
        problems.append(f"旧条目的 pack 是 {item.get('pack')!r}（期望 ['风格', '雪']）")
    if data.get("inspiration_categories") != ["人物", "道具", "场景", "风格"]:
        problems.append(f"inspiration_categories 是 {data.get('inspiration_categories')!r}")
    if "测试" not in (data.get("categories") or []):
        problems.append(f"其余区的 categories 被改了：{data.get('categories')!r}")
    index["items"].pop(legacy, None)
    path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    ctx.git("add", "-A")
    ctx.git("commit", "-qm", "自检：旧灵感条目兼容检查收尾")
    return (not problems), "；".join(problems[:4])


def check_42(ctx):
    """P1：sketch --no-generate 只建草图卡不生成；import 外部图进试样区/出图区（未审、复制不删、同卡 SHA 去重、失败零副作用）。"""
    problems = []
    ctx.git("add", "-A")
    ctx.git("commit", "-qm", "自检：import 检查前收尾")
    sk = ctx.run("sketch", "外部试样", "--no-generate", "--pack", "风格/外部试样", "--title", "外部试样")
    if sk.returncode != 0:
        return False, f"sketch --no-generate 失败: {brief(sk)}"
    if not (ctx.tmp / "cards" / "s-外部试样_sketch.yaml").is_file():
        problems.append("sketch --no-generate 没建草图卡")
    if list((ctx.tmp / "output" / "candidates").glob("s-外部试样_*")):
        problems.append("sketch --no-generate 竟然生成了候选图")

    img1 = _external_image(ctx, "外部图1.png")
    img2 = _external_image(ctx, "外部图2.png")
    bad = ctx.tmp / "外部素材" / "不是图.txt"
    bad.write_text("not an image", encoding="utf-8")
    before_commits = ctx.commit_count()
    for bad_argv in (
        ("import", "no-such-card", str(img1), "--provider", "ext"),
        ("import", "s-外部试样", str(img1), str(bad), "--provider", "ext"),
    ):
        proc = ctx.run(*bad_argv)
        if proc.returncode == 0:
            problems.append(f"非法 import 却成功了（{bad_argv[:3]}）")
    if ctx.commit_count() != before_commits:
        problems.append("非法 import 触发了额外 git 提交（非零副作用）")

    imp = ctx.run("import", "s-外部试样", str(img1), str(img2), "--provider", "ext-web", "--model", "v2",
                  "--prompt", "snow lantern night")
    if imp.returncode != 0:
        return False, f"合法 import 失败: {brief(imp)}"
    if not img1.is_file() or not img2.is_file():
        problems.append("import 删掉了源文件（应复制不删）")
    # 重复导入相同内容应按 SHA256 去重跳过
    dup = ctx.run("import", "s-外部试样", str(img1), "--provider", "ext-web", "--model", "v2")
    if dup.returncode != 0 or "duplicate skipped" not in ctx.out(dup):
        problems.append("同卡重复 import 没去重跳过: " + brief(dup, 200))

    state = json.loads(ctx.run("state", "--json").stdout)
    card = next((c for c in state["cards"] if c["id"] == "s-外部试样"), {})
    cands = card.get("candidates") or []
    if card.get("kind") != "sketch" or len(cands) != 2:
        problems.append(f"草图卡导入后 kind={card.get('kind')!r} 候选数={len(cands)}（期望 sketch / 2）")
    if any(c.get("verdict") for c in cands):
        problems.append("import 后的候选默认不是未审")
    if any(c.get("origin") != "import" or c.get("model") != "ext-web/v2" for c in cands):
        problems.append(f"import 候选的 origin/model 不对: {[(c.get('origin'), c.get('model')) for c in cands]}")
    if any(c.get("sent_prompt") != "snow lantern night" for c in cands):
        problems.append("import 传入的 --prompt 没体现在 state.sent_prompt 里")

    # 普通卡导入应落在普通卡（出图区）
    imp_reg = ctx.run("import", CARD, str(img1), "--provider", "ext-web")
    if imp_reg.returncode != 0:
        problems.append("普通卡 import 失败: " + brief(imp_reg, 200))
    ctx.git("add", "-A")
    ctx.git("commit", "-qm", "自检：import 检查收尾")
    return (not problems), "；".join(problems[:4])


def check_43(ctx):
    """P1：variant add 挂 2k/4k 到已有候选（存 output/variants/、不动原候选/审核/pick、同 tier 防覆盖与 --replace、list 只读）。"""
    problems = []
    if not ctx.candidates:
        ctx.candidates_now()
    target = ctx.candidates[0]
    ctx.run("review", CARD, target, "pass", "--by", "human", "--note", "保留审核")
    ctx.run("pick", CARD, target)
    review_path = ctx.tmp / "output" / "review" / f"{CARD}.json"
    review_before = review_path.read_text(encoding="utf-8")
    v2k = _external_image(ctx, "超分2k.png")
    v4k = _external_image(ctx, "超分4k.png")
    v2k_alt = _external_image(ctx, "超分2k改版.png")

    add2 = ctx.run("variant", "add", CARD, target, str(v2k), "--tier", "2K", "--provider", "ext-up", "--model", "x2")
    add4 = ctx.run("variant", "add", CARD, target, str(v4k), "--tier", "4k", "--provider", "ext-up", "--model", "x4")
    if add2.returncode != 0 or add4.returncode != 0:
        return False, f"variant add 失败: {brief(add2, 160)} / {brief(add4, 160)}"
    if review_path.read_text(encoding="utf-8") != review_before:
        problems.append("variant add 改动了原候选的 review / pick 记录")
    # 相同内容幂等
    same = ctx.run("variant", "add", CARD, target, str(v2k), "--tier", "2k")
    if same.returncode != 0 or "unchanged" not in ctx.out(same):
        problems.append("同 tier 同内容没幂等跳过: " + brief(same, 160))
    # 不同内容默认拒覆盖且不提交，--replace 才替换
    before_commits = ctx.commit_count()
    deny = ctx.run("variant", "add", CARD, target, str(v2k_alt), "--tier", "2k")
    if deny.returncode == 0 or ctx.commit_count() != before_commits:
        problems.append("同 tier 不同内容未拒覆盖或产生了提交")
    rep = ctx.run("variant", "add", CARD, target, str(v2k_alt), "--tier", "2k", "--replace")
    if rep.returncode != 0 or "replaced" not in ctx.out(rep):
        problems.append("--replace 替换失败: " + brief(rep, 160))
    # variant list 只读不提交
    before_list = ctx.commit_count()
    listed = ctx.run("variant", "list", CARD, target)
    if listed.returncode != 0 or "[2k]" not in ctx.out(listed) or "[4k]" not in ctx.out(listed):
        problems.append("variant list 输出异常: " + brief(listed, 200))
    if ctx.commit_count() != before_list:
        problems.append("只读 variant list 产生了 git 提交")

    state = json.loads(ctx.run("state", "--json").stdout)
    card = next((c for c in state["cards"] if c["id"] == CARD), {})
    cand = next((x for x in card.get("candidates") or [] if x["name"] == target), {})
    tiers = {v["tier"]: v for v in cand.get("variants") or []}
    if set(tiers) != {"2k", "4k"} or not all(v.get("exists") and v.get("file", "").startswith("output/variants/") for v in tiers.values()):
        problems.append(f"state 里的 variants 不完整: {cand.get('variants')!r}")
    ctx.git("add", "-A")
    ctx.git("commit", "-qm", "自检：variant 检查收尾")
    return (not problems), "；".join(problems[:4])


def check_44(ctx):
    """P1：候选查找与审核校验卡归属（跨卡 review/pick/note/register/variant 被拒；_find_candidate 不把 _1 错配成 _10）。"""
    problems = []
    other_id = "02-01"
    ctx.run("new-card", other_id, "归属测试卡")
    cand = ctx.candidates[0]
    v = _external_image(ctx, "归属图.png")
    for label, argv in (
        ("review", ("review", other_id, cand, "pass", "--by", "human")),
        ("pick", ("pick", other_id, cand)),
        ("note", ("note", other_id, cand, "--note", "串卡备注")),
        ("register", ("register", other_id, cand)),
        ("variant", ("variant", "add", other_id, cand, str(v), "--tier", "2k")),
    ):
        proc = ctx.run(*argv)
        if proc.returncode == 0 or "belongs to card" not in ctx.out(proc):
            problems.append(f"跨卡 {label} 没拦截: {brief(proc, 180)}")
    # _find_candidate 精确按文件名/stem：只存在 _10 时查 _1 不该错配到 _10
    sys.path.insert(0, str(SKILL / "scripts"))
    import pipeline as pl
    cfg = pl.Config(str(ctx.config))
    fake10 = ctx.tmp / "output" / "candidates" / f"{other_id}_m_10.png"
    fake10.write_bytes(v.read_bytes())
    try:
        try:
            pl._find_candidate(cfg, f"{other_id}_m_1")
            problems.append("_find_candidate 把 _m_1 错误前缀匹配到了 _m_10.png")
        except SystemExit:
            pass
    finally:
        fake10.unlink(missing_ok=True)
    ctx.git("add", "-A")
    ctx.git("commit", "-qm", "自检：候选归属检查收尾")
    return (not problems), "；".join(problems[:4])


def check_45(ctx):
    """P1：路径段拦截只看相对路径不误伤父目录名含 verify/upscale 的项目；前端草图卡按 kind: sketch 判断。"""
    problems = []
    sys.path.insert(0, str(SKILL / "scripts"))
    import pipeline as pl
    with tempfile.TemporaryDirectory(prefix="icp-verify-upscale-") as weird_root:
        proj = Path(weird_root) / "proj"
        (proj / "外部素材").mkdir(parents=True, exist_ok=True)
        (proj / "外部素材" / "upscale_4k").mkdir(parents=True, exist_ok=True)
        cfg_file = proj / "project.yaml"
        cfg_file.write_text("project:\n  name: test\n  root: .\n", encoding="utf-8")
        good = proj / "外部素材" / "干净参考.png"
        bad = proj / "外部素材" / "upscale_4k" / "超分图.png"
        good.write_bytes(b"\x89PNG\r\n\x1a\n")
        bad.write_bytes(b"\x89PNG\r\n\x1a\n")
        cfg = pl.Config(str(cfg_file))
        if pl.inspiration_source_problem(cfg, good):
            problems.append("父目录名含 verify/upscale 误伤了项目内正常参考图: " + pl.inspiration_source_problem(cfg, good))
        if "upscale" not in pl.inspiration_source_problem(cfg, bad):
            problems.append("项目内 upscale_4k 目录没被拦截")
    html = (SKILL / "web" / "review.html").read_text(encoding="utf-8")
    if 'const homeOf = card => card.startsWith("s-")' in html or 'e.card && e.card.startsWith("s-")' in html:
        problems.append("review.html 仍在直接用 s- 前缀猜草图卡")
    if 'c.kind === "sketch"' not in html or "isSketchCard" not in html:
        problems.append("review.html 没统一走 kind === \"sketch\" 判断草图卡")
    return (not problems), "；".join(problems[:4])


def check_46(ctx):
    root = ctx.tmp / "vocab-project"
    root.mkdir()
    isolated = Ctx(root)
    run = isolated.run

    def ok(*args):
        proc = run(*args)
        assert proc.returncode == 0, brief(proc)
        return proc.stdout

    def write(path, value):
        path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")

    ok("init")
    raw = yaml.safe_load(isolated.config.read_text())
    vocab_file = root / raw["taxonomy"]
    vocab = yaml.safe_load(vocab_file.read_text())
    assert len(vocab["categories"]) == 7
    vocab["categories"]["graphic"]["aliases"].remove("海报")
    vocab["categories"].update(
        product={"label": "商品", "storage": "goods", "behavior": {"reference": True, "source": True},
                 "source_meta": {"section": "products", "as": "product"}},
        poster={"label": "海报", "behavior": {"deliverable": True}})
    vocab["default_reference"] = "product"
    vocab["migrations"]["aliases"]["旧商品"] = "product"
    vocab["migrations"]["merge"]["old-product"] = "旧商品"
    vocab["models"]["image"]["dry"]["aliases"] += ["route-one", "route-two", "b-c", "c"]
    write(vocab_file, vocab)
    raw["rules"].update(autosave=False, require_approval=False)
    raw["library"] = {"kind": "local", "root": "library"}
    raw["models"].update({alias: {"provider": "mock"} for alias in ("route-one", "route-two", "b-c", "c", "flux")})
    raw["catalog"] = {"external_roots": [{"path": "archive", "category": "poster", "depth": 1}]}
    write(isolated.config, raw)
    assert "taxonomy" in ok("doctor")
    raw["sources"] = {"kinds": {"products": {"as": "wrong"}}}
    write(isolated.config, raw)
    conflict = run("doctor")
    assert conflict.returncode and "conflicts with taxonomy" in isolated.out(conflict)
    del raw["sources"]
    write(isolated.config, raw)

    for card in ("a", "a-b"):
        ok("new-card", card, "陶瓷杯：样图 #1")
        path = root / "cards" / (card + ".yaml")
        data = yaml.safe_load(path.read_text())
        assert data["schema_version"] == 2 and data["id"] == card
        data.update(action="A ceramic cup in sunlight.", pack="old-product/杯组", model="dry")
        write(path, data)
        ok("validate", card)
    for bad in ("A", "a_bad", "a bad", "a#bad", "杯" * 41):
        assert run("new-card", bad).returncode, bad
    assert run("sketch", "invalid-pack", "--no-generate", "--pack", "不存在/组").returncode
    ok("sketch", "product", "--no-generate", "--pack", "商品/杯组")
    assert (root / "cards/s-product.yaml").is_file()

    ok("generate", "a", "--model", "b-c")
    ok("generate", "a-b", "--model", "c")
    ok("generate", "a", "--model", "route-one")
    ok("generate", "a", "--model", "flux")
    jobs = json.loads((root / "output/manifest.json").read_text())["jobs"]
    names = {key: Path(job["candidate"]).name for key, job in jobs.items()}
    assert names == {"a|b-c|1": "a__dry__001.png", "a-b|c|1": "a-b__dry__001.png",
                     "a|route-one|1": "a__dry__002.png", "a|flux|1": "a__flux__001.png"}, names
    assert len({job["run_dir"] for job in jobs.values()}) == 4
    assert all((root / "output" / job["run_dir"] / "request.json").is_file() for job in jobs.values())
    assert run("pick", "a", "a-b__dry__001.png").returncode
    ok("register", "a", "a__dry__001", "a__flux__001.png")
    entries = json.loads((root / "library/library.json").read_text())["entries"]
    assert {e["model"] for e in entries} == {"dry", "flux"}, entries
    for entry in entries:
        assert entry["card_id"] == "a" and entry["category_id"] == "product" and entry["job_key"] in jobs
        assert entry["pack_name"] == "杯组" and entry["storage_category"] == "goods"
        assert entry["file"] == "草稿区/goods/杯组/" + entry["candidate_name"]

    external = root / "vocab-import.png"
    external.write_bytes((root / "output/candidates/a__dry__001.png").read_bytes() + b"import fixture")
    for flag in ((), ("--model", "unknown")):
        assert run("import", "a", str(external), "--provider", "external", *flag).returncode
    copied = "a__dry__003_封面.png"
    assert copied in ok("import", "a", str(external), "--provider", "external", "--model", "route-two", "--tag", "封面")
    assert external.is_file()
    assert run("inspire", "add", str(external), "--pack", "海报/样例", "--approved", "自检：用户点名收录").returncode
    ok("inspire", "add", str(external), "--pack", "旧商品/样例", "--approved", "自检：用户点名收录")
    ok("promote", copied, "--as", "product:陶瓷杯", "--approved", "自检：确认这张为真源")
    state = json.loads(ok("state", "--json"))
    assert "商品" in state["inspiration_categories"] and "海报" not in state["inspiration_categories"]
    assert state["taxonomy"]["product"]["behavior"]["source"]
    assert any(s["section"] == "products" and s["pack"][0] == "商品" for s in state["sources"])
    assert state["inspiration"][0]["pack"][0] == "商品"

    legacy = root / "cards/old_card_snapshot.yaml"
    write(legacy, {"id": "old_card", "title": "历史记录", "action": "A ceramic cup.", "pack": "旧分类/组"})
    ok("validate", "old_card")
    ok("generate", "old_card", "--model", "route-one")
    assert (root / "output/candidates/old_card__dry__001.png").is_file()
    assert yaml.safe_load(legacy.read_bytes()).get("schema_version") is None
    write(legacy, {**yaml.safe_load(legacy.read_text()), "pack": "旧商品/组"})
    (root / "cards/historic_name.yaml").write_text("id: historic\npack: 旧分类/组\n", encoding="utf-8")
    report = root / "output/comparison.html"
    report.write_text("<p>comparison</p>")
    pack = root / "archive/sample-pack"
    pack.mkdir(parents=True)
    (pack / "ignored.png").write_bytes(b"test")
    (pack.parent / "same-pack").symlink_to(pack, target_is_directory=True)
    ok("index")
    outputs = [root / "_INDEX.md", root / ".catalog-index.json"]
    baseline = [path.read_bytes() for path in outputs]
    ok("index")
    assert baseline == [path.read_bytes() for path in outputs]
    catalog = json.loads(baseline[1])
    assert catalog["migrations"]["count"] >= 2
    assert any(e["file"] == "output/comparison.html" and e["category_id"] == "eval" for e in catalog["entries"])
    assert [e["file"] for e in catalog["entries"] if e["kind"] == "external"] == ["archive/sample-pack"]
    assert "warning:" in ok("check")
    assert ok("find", copied).splitlines()[0].split("\t")[0].endswith(copied)
    assert copied in ok("find", "thumbnail")
    from core import catalog as catalogmod
    candidates = [entry["key"] for entry in catalog["entries"] if entry["name"] != copied]
    scores = {key: 0.7 for key in candidates[:7]}
    scores[candidates[7]] = 0.55
    keyword = catalogmod.search_catalog(catalog, copied, vocab, lambda query: scores)
    assert keyword[0]["name"] == copied and sum(e["key"] in scores for e in keyword) == 3
    semantic = catalogmod.search_catalog(catalog, "word-without-a-hit", vocab, lambda query: scores)
    assert len(semantic) == 5
    fallback = catalogmod.search_catalog(catalog, "word-without-a-hit", vocab, lambda query: {candidates[7]: 0.55})
    assert len(fallback) == 1
    (root / "output/candidates" / copied).unlink()
    broken = run("check")
    assert broken.returncode == 2 and "error:" in broken.stdout and "broken link" in broken.stdout, brief(broken)
    assert baseline == [path.read_bytes() for path in outputs], "check wrote the indexes"
    return True, ""


def check_47(ctx):
    """P2：inspire remove <完整ID> 可恢复软下架（拒前缀、零副作用、隐藏且禁新试样按编号引用、原文件保留、restore 与重 add 恢复）。"""
    problems = []
    img_file = _external_image(ctx, "可恢复下架参考.png")
    added = ctx.run("inspire", "add", str(img_file), "--pack", "风格/下架测试", "--approved", "用户：收进参考区测下架")
    if added.returncode != 0:
        return False, "inspire add 失败: " + brief(added, 180)
    ident = ctx.out(added).splitlines()[0].split()[0]
    stored = next((ctx.tmp / "inspiration").glob(f"{ident}_*"), None)
    if not stored or not stored.is_file():
        return False, f"找不到落盘的参考图文件: {ident}"

    # 1. 前缀或不存在的 ID 必须拒收且零副作用
    before_commits = ctx.commit_count()
    for bad_arg in (ident[:4], "deadbeef"):
        bad = ctx.run("inspire", "remove", bad_arg)
        if bad.returncode == 0 or ctx.commit_count() != before_commits:
            problems.append(f"非法 inspire remove ({bad_arg}) 未拒收或产生了提交: " + brief(bad, 160))

    # 2. 完整 ID 软下架
    rem = ctx.run("inspire", "remove", ident, "--note", "风格换了")
    if rem.returncode != 0 or "removed" not in ctx.out(rem):
        return False, "inspire remove 失败: " + brief(rem, 180)
    if not stored.is_file():
        problems.append("inspire remove 删除了磁盘原文件（应保留防旧卡断链）")

    # 3. 默认 list 与 state 隐藏；--removed / --all 只读可见
    before_ro = ctx.commit_count()
    list_default = ctx.run("inspire", "list")
    list_removed = ctx.run("inspire", "list", "--removed")
    list_all = ctx.run("inspire", "list", "--all")
    if ctx.commit_count() != before_ro:
        problems.append("只读 inspire list --removed/--all 产生了 git 提交")
    if ident in ctx.out(list_default):
        problems.append("下架后 inspire list 仍列出该编号")
    if ident not in ctx.out(list_removed) or "[removed]" not in ctx.out(list_removed):
        problems.append("inspire list --removed 未列出已下架编号: " + brief(list_removed, 160))
    if ident not in ctx.out(list_all) or "[removed]" not in ctx.out(list_all):
        problems.append("inspire list --all 未标出 [removed]")
    st = json.loads(ctx.run("state", "--json").stdout)
    if any(i.get("id") == ident for i in st.get("inspiration") or []):
        problems.append("下架后 state 仍包含该参考图")

    # 4. 新试样按编号引用（完整 ID / 前缀）均被拒收；显式文件路径仍可用
    for ref_val in (ident, ident[:5]):
        sk = ctx.run("sketch", "p2-ref-blocked", "--no-generate", "--pack", "风格/下架测试", "--ref", ref_val)
        if sk.returncode == 0:
            problems.append(f"已下架参考图编号 ({ref_val}) 仍能被 sketch --ref 引用")
    sk_path = ctx.run("sketch", "p2-ref-path-ok", "--no-generate", "--pack", "风格/下架测试", "--ref", str(stored))
    if sk_path.returncode != 0:
        problems.append("保留的磁盘原文件路径无法被草图引用: " + brief(sk_path, 160))

    # 5. inspire restore 恢复后重新可见且可按编号引用
    res = ctx.run("inspire", "restore", ident)
    if res.returncode != 0 or "restored" not in ctx.out(res):
        problems.append("inspire restore 失败: " + brief(res, 180))
    if ident not in ctx.out(ctx.run("inspire", "list")):
        problems.append("restore 后 inspire list 仍找不到")
    sk_ok = ctx.run("sketch", "p2-ref-restored", "--no-generate", "--pack", "风格/下架测试", "--ref", ident)
    if sk_ok.returncode != 0:
        problems.append("restore 后 sketch --ref 仍不可用: " + brief(sk_ok, 160))

    # 6. 再次下架后，重跑 inspire add 同一文件也能恢复
    ctx.run("inspire", "remove", ident)
    readd = ctx.run("inspire", "add", str(img_file), "--pack", "风格/下架测试", "--approved", "用户：重新启用这张参考")
    if readd.returncode != 0 or "restored" not in ctx.out(readd):
        problems.append("下架后重跑 inspire add 未恢复条目: " + brief(readd, 180))
    st2 = json.loads(ctx.run("state", "--json").stdout)
    if not any(i.get("id") == ident for i in st2.get("inspiration") or []):
        problems.append("re-add 恢复后 state 里未重新出现")

    ctx.git("add", "-A")
    ctx.git("commit", "-qm", "自检：P2 inspire remove/restore 收尾")
    return (not problems), "；".join(problems[:4])


def check_48(ctx):
    """P2：审核页 POST /api/inspire/remove 移出参考区、GET /file 能读 output/variants/、前端含移出按钮与原图/2K/4K 切换。"""
    problems = []
    img_file = _external_image(ctx, "网页移出参考.png")
    added = ctx.run("inspire", "add", str(img_file), "--pack", "风格/网页下架", "--approved", "用户：网页测移出")
    if added.returncode != 0:
        return False, "inspire add 失败: " + brief(added, 180)
    ident = ctx.out(added).splitlines()[0].split()[0]
    stored = next((ctx.tmp / "inspiration").glob(f"{ident}_*"), None)

    proc, port = start_review_web(ctx)
    try:
        if port is None:
            return False, "审核页没能启动"
        # 1. POST /api/inspire/remove 移出参考区
        bad = post(port, "/api/inspire/remove", {"id": ident[:4]})
        if bad.get("ok"):
            problems.append("/api/inspire/remove 接受了短前缀")
        reply = post(port, "/api/inspire/remove", {"id": ident})
        if not reply.get("ok"):
            problems.append("/api/inspire/remove 失败: " + json.dumps(reply, ensure_ascii=False)[:180])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=30) as resp:
            st = json.loads(resp.read().decode("utf-8"))
        if any(i.get("id") == ident for i in st.get("inspiration") or []):
            problems.append("/api/inspire/remove 后 /api/state 仍包含该参考图")
        if not stored or not stored.is_file():
            problems.append("/api/inspire/remove 后原文件不在硬盘上了")

        # 2. 变体文件可通过 /file?path=output/variants/... 正常读取
        card = next((c for c in st.get("cards") or [] if c["id"] == CARD), {})
        vfile = next((v["file"] for x in card.get("candidates") or [] for v in (x.get("variants") or []) if v.get("exists")), "")
        if not vfile:
            problems.append("state 中未找到已存在的 variant 文件（依赖 check_43）")
        else:
            from urllib.parse import quote
            url = f"http://127.0.0.1:{port}/file?path={quote(vfile)}"
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = resp.read()
                if resp.status != 200 or not data.startswith(b"\x89PNG"):
                    problems.append(f"GET {vfile} 未返回合法 PNG (status={resp.status})")
    finally:
        _stop(proc)

    # 3. 前端包含移出参考区按钮与版本切换 UI
    html = (SKILL / "web" / "review.html").read_text(encoding="utf-8")
    for token in ("/api/inspire/remove", "d-insp-remove", "versionsFor", "applyVersion", "d-verbox"):
        if token not in html:
            problems.append(f"review.html 缺少 {token}")

    ctx.git("add", "-A")
    ctx.git("commit", "-qm", "自检：P2 审核页接口与变体读取收尾")
    return (not problems), "；".join(problems[:4])


CHECKS = {
    1: ("init 后 .git / .gitignore 存在，doctor 报 version : git", check_01),
    2: ("new-card 三种卡型都能建，默认卡 validate 返回 0", check_02),
    3: ("compose 默认卡成功，output/prompt/<卡>.txt 存在", check_03),
    4: ("付费闸门：不带批准、或批准是占位符（... / … / <…> / 过短）都被拒", check_04),
    5: ("generate --budget 2 出 2 张，重跑 0 张新图", check_05),
    6: ("改卡后提示词过期被拒，重新 compose 后可生成", check_06),
    7: ("sheet 成功，output/sheets 下有文件", check_07),
    8: ("review 默认 claude 通过、不在名单里的 helper-ai 被拒、human 通过，记录里有 by", check_08),
    9: ("pick + register 进素材库，unregister 移出", check_09),
    10: ("feedback 同一 reason 记两次只多一条", check_10),
    11: ("外部图带 --pack / --approved 能入库，inspire list 能列出", check_11),
    12: ("promote：不带 --approved 被拒，换图进 history，同图重复不进", check_12),
    13: ("项目目录里没有 *.bak* 文件或 bak-* 目录", check_13),
    14: ("提交 ≥8 条且有「自动存：generate」，跑完工作区干净", check_14),
    15: ("审核页 POST /api/review 返回 ok:true 并留下提交", check_15),
    16: ("只读命令（inspire list / doctor / cards）不产生新提交", check_16),
    17: ("并发：8 路 generate 同时跑（含同卡同模型两份）+ 途中写反馈，manifest 完好、一条不丢、不重复出", check_17),
    18: ("并发：审核页同时点 6 个「要」+ 命令行同时下结论，结论一条不丢，「要」的图进 pick", check_18),
    19: ("审核页「要」= 通过（human）+ pick + 登记草稿；重复点不重复登记", check_19),
    20: ("promote style 带描述和画风/氛围；引用它的卡 compose 含描述，describe 改描述（要批准、不动样张、记账）后 generate 报 stale", check_20),
    21: ("pack 命令改归类；packs.categories 删掉分类后，原来的卡和真源都还在 state 里", check_21),
    22: ("state --json 和审核页 /api/state 是同一份数据", check_22),
    23: ("页面能做的命令都能做：每个 POST 接口对应一个子命令（定稿除外），页面调的接口后端都有", check_23),
    24: ("sources.kinds 加新种类（场景 / 带描述的镜头语言）：promote、引用、state 都认；去掉声明真源不丢", check_24),
    25: ("card-core：生图 Skill 里的 scripts/core 和 card-core 源头一致", check_25),
    26: ("卡上 promote：解析 / 校验，sketch 写错被拒；审核页定真源默认值由脚本推", check_26),
    27: ("生图只读视频卡：素材区标出哪些素材正被视频卡当关键帧（image: / library: / 文件路径按内容认）", check_27),
    28: ("付费：请求发出后的异常（没图 / 下载失败 / 非 JSON / 5xx / 超时）记 unknown 不重发，chat 不重试；连不上 / 4xx 记 failed；回包先落 response.json；死进程的 running 记 unknown；forget 要原因", check_28),
    29: ("付费：参考图发不全就拒跑（超 max_refs / 超体积没图床 / 图床部分失败）并点名；超体积先缩小；图床成功按原顺序换 URL", check_29),
    30: ("付费：generate 前先过 validate，引用没登记的真源 / 找不到的参考图拒跑（--force 也不放行）；模板空人物行不算", check_30),
    31: ("sketch / load_card 按 id 精确匹配：短名不覆盖长名的卡", check_31),
    32: ("promote 换图时旧 face / role 进 history 并清掉，demote 恢复", check_32),
    33: ("审核页写接口只收本机 JSON（Content-Type / Host / Origin）；promote 名字不许带 .. / 绝对路径", check_33),
    34: ("项目不是仓库根时不往外层仓库提交；init 自建仓库；.gitignore 挡视频片子和回包原文", check_34),
    35: ("pick 保持加入顺序，重复 pick 不挪位置", check_35),
    36: ("素材库：定稿条目不让 unregister 直接移；同名不同内容不覆盖、共用文件不误删", check_36),
    37: ("卖的骨架：文档 / 代码不带日期化的个人决定、不点名具体图床 Skill", check_37),
    38: ("参考区准入：缺 --pack / 非法大类 / 缺 --approved 都整批拒绝，索引 / 文件 / 提交不变", check_38),
    39: ("来源防误入：候选目录 / 评测目录 / 路径段含 upscale 的图带 --approved 也拒，零副作用", check_39),
    40: ("合法入库：--pack 风格/测试组 + --approved 写进索引；一批里混坏文件整批不生效", check_40),
    41: ("旧索引兼容：无 pack 的历史条目照旧读取与分组；state 暴露 inspiration_categories", check_41),
    42: ("P1 外部导入：sketch --no-generate 只建卡不生成；import 进试样/出图区、未审、复制不删、同卡 SHA 去重、失败零副作用", check_42),
    43: ("P1 超分变体：variant add 挂 2k/4k 到原候选（output/variants/）、不动审核与 pick、防覆盖与 --replace、list 只读", check_43),
    44: ("P1 归属校验：跨卡 review/pick/note/register/variant 被拒；_find_candidate 精确匹配不误撞 _10", check_44),
    45: ("P1 细节修复：路径段拦截不误伤含 verify/upscale 的父目录；review.html 按 kind: sketch 判断草图卡", check_45),
    46: ("词表联动、v2 写入与溯源、确定性台账、体检分级和检索", check_46),
    47: ("P2 参考区下架：inspire remove 需完整 ID、软下架禁编号引用保原文件、restore 与重 add 可恢复", check_47),
    48: ("P2 审核页增强：POST /api/inspire/remove、GET /file 读取 variants、前端移出按钮与原图/2K/4K 切换", check_48),
}

# 14 要放在所有写操作之后；13 也在最后扫一遍更可信。打印仍按编号排序。
ORDER = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 15, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27,
         28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 47, 48, 13, 16, 14, 46]


def main():
    parser = argparse.ArgumentParser(description="image-card-pipeline 自检（只用 mock 供应商）")
    parser.add_argument("--keep", action="store_true", help="保留临时目录，便于排查")
    args = parser.parse_args()

    skill_before = skill_status()
    tmp = tempfile.mkdtemp(prefix="icp-selfcheck-")
    results = {}
    try:
        ctx = prepare(tmp)
        if ctx.init_proc.returncode != 0:
            print("✗ init 失败，后续检查无法进行：" + brief(ctx.init_proc))
            print(f"0 项通过，{len(CHECKS)} 项失败")
            return 1

        for number in ORDER:
            name, func = CHECKS[number]
            try:
                ok, detail = func(ctx)
            except Exception as error:  # noqa: BLE001 - one check must not stop the rest
                ok, detail = False, f"{type(error).__name__}: {error}"
            results[number] = (name, ok, detail)

        for number in sorted(CHECKS):
            name, ok, detail = results[number]
            print(("✓ " if ok else "✗ ") + f"{number}. {name}" + (f"：{detail}" if detail else ""))

        passed = sum(1 for _name, ok, _detail in results.values() if ok)
        failed = len(results) - passed
        skill_after = skill_status()
        intact = skill_before == skill_after
        if intact:
            print("✓ 附加：Skill 目录前后 git status 一致（没有改到 Skill 本身）")
        else:
            print("✗ 附加：Skill 目录被改动\n" + skill_after)
        print(f"{passed} 项通过，{failed} 项失败")
        return 0 if failed == 0 and intact else 1
    finally:
        if args.keep:
            print(f"临时目录已保留：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
