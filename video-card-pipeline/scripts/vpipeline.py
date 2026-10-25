#!/usr/bin/env python3
"""video-card-pipeline -- one card = one shot.

card -> refs (first frame / last frame / characters / reference media)
     -> prompt (motion + rules + tail) -> provider (async submit / poll)
     -> take (+ frame sheet, + optional post hooks)
     -> review station (human verdicts, notes, time marks) -> sync -> pick
     -> library (drafts; the human finalises) -> board (canvas of approved takes)
     -> cut list -> assembled film

Works alone (first frames as file paths); linked to image-card-pipeline it reads
`image:<card>` picks and the shared sources.yaml, and sends first-frame problems
back to the image card. Credentials only come from environment variables.

    python3 vpipeline.py --config project.yaml <command> -h
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import admin  # noqa: E402
import cloud  # noqa: E402
import delivery  # noqa: E402
from core import library  # noqa: E402
import media  # noqa: E402
import produce  # noqa: E402
import providers  # noqa: E402
import refs as refmod  # noqa: E402
import reviewing  # noqa: E402
import stations  # noqa: E402
from config import Config, ConfigError  # noqa: E402
from core import jobs, versioning  # noqa: E402

# Commands that change the project. Each one saves a version before it runs (catching hand
# edits) and after (its own result) -- only when the project folder is its own git repository
# and video.rules.autosave is not false. Read-only commands never commit.
WRITE_COMMANDS = {"new-card", "validate", "generate", "collect", "import", "forget", "review", "submit",
                  "sync", "pick", "feedback", "post", "register", "board", "assemble"}


def die(message):
    raise SystemExit(f"✗ {message}")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=os.getenv("VIDEO_CARD_CONFIG", "project.yaml"),
                        help="project config (default: $VIDEO_CARD_CONFIG or ./project.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help_text):
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=func)
        return p

    add("init", admin.cmd_init, "scaffold a project, or add the video section to an image project")
    p = add("new-card", admin.cmd_new_card, "new video card from the template")
    p.add_argument("card_id"); p.add_argument("title", nargs="?")
    p.add_argument("--first-frame"); p.add_argument("--group")
    add("cards", admin.cmd_cards, "list cards")
    p = add("show", admin.cmd_show, "print a card"); p.add_argument("card")
    p = add("validate", admin.cmd_validate, "check fields, refs, final first frames"); p.add_argument("cards", nargs="+")
    p = add("prompt", admin.cmd_prompt, "print the composed prompt"); p.add_argument("card")
    p = add("generate", produce.cmd_generate, "submit takes and (by default) wait for them")
    p.add_argument("cards", nargs="+"); p.add_argument("--model"); p.add_argument("--budget", type=int)
    p.add_argument("--approved", default=""); p.add_argument("--no-wait", action="store_true")
    p.add_argument("--timeout", type=float); p.add_argument("--post", action="append", default=[],
                                                            help="run this post hook on each new take")
    p = add("collect", produce.cmd_collect, "resume polling submitted jobs (never resubmits)")
    p.add_argument("cards", nargs="*"); p.add_argument("--timeout", type=float)
    p.add_argument("--post", action="append", default=[])
    p = add("import", produce.cmd_import, "bring a clip in: closes a handoff job, or a new take")
    p.add_argument("card"); p.add_argument("file"); p.add_argument("--model")
    p.add_argument("--move", action="store_true", help="delete the source file after a good import (default: copy, keep it)")
    p.add_argument("--copy", action="store_true", help=argparse.SUPPRESS)   # old flag: copying is the default now
    p.add_argument("--post", action="append", default=[])
    p = add("forget", produce.cmd_forget, "drop an unknown/failed job after a human checked it")
    p.add_argument("key"); p.add_argument("--why", required=True); p.add_argument("--force", action="store_true")
    add("status", admin.cmd_status, "cards, jobs, verdicts, picks")
    p = add("sheet", reviewing.cmd_sheet, "first/middle/last frame sheet of a take or all takes of a card"); p.add_argument("target")
    p = add("review", reviewing.cmd_review, "the AI's own verdict on a take")
    p.add_argument("take"); p.add_argument("verdict", choices=stations.VERDICTS); p.add_argument("--note", default="")
    p.add_argument("--by", default="ai")
    p = add("submit", reviewing.cmd_submit, "send takes to the review station"); p.add_argument("takes", nargs="+")
    p = add("sync", reviewing.cmd_sync, "pull human verdicts from the station (pass -> pick)"); p.add_argument("cards", nargs="*")
    p = add("pick", reviewing.cmd_pick, "pick / unpick takes by hand")
    p.add_argument("card"); p.add_argument("takes", nargs="+"); p.add_argument("--remove", action="store_true")
    p = add("feedback", reviewing.cmd_feedback, "record the human's feedback and its root cause")
    p.add_argument("card"); p.add_argument("verdict", choices=["accept", "reject", "revise"])
    p.add_argument("--reason", required=True); p.add_argument("--take", default="")
    p.add_argument("--root-cause", choices=["card", "model", "frame", "ref"]); p.add_argument("--by", default="")
    p = add("post", reviewing.cmd_post, "run a post hook (upscale ...) on a take"); p.add_argument("take"); p.add_argument("hook")
    p = add("register", delivery.cmd_register, "picked takes -> library drafts"); p.add_argument("card"); p.add_argument("takes", nargs="*")
    p = add("board", delivery.cmd_board, "canvas board: push approved takes / status")
    p.add_argument("action", choices=["push", "status"]); p.add_argument("cards", nargs="*")
    p.add_argument("--take", dest="takes", action="append")
    p = add("assemble", delivery.cmd_assemble, "cut list -> film"); p.add_argument("cut"); p.add_argument("--dry-run", action="store_true")
    p = add("providers", admin.cmd_providers, "models and their providers"); p.add_argument("--check", action="store_true")
    add("doctor", admin.cmd_doctor, "self-check")
    p = add("cloud", cloud.cmd_cloud, "cloud GPU box for the compshare provider: status / up / down")
    p.add_argument("action", choices=["status", "up", "down"]); p.add_argument("--approved", default="")
    p.add_argument("--create", action="store_true", help="up: create the box from video.cloud.create if none is recorded")
    p.add_argument("--price-ok", action="store_true",
                   help="up --create: the user has seen the dry-run price and agreed; without it only the plan is printed")
    return parser


def _command_line(args):
    argv = sys.argv[1:]
    if args.command in argv:
        argv = argv[argv.index(args.command):]
    return " ".join(argv)


def main(argv=None):
    args = build_parser().parse_args(argv)
    cfg = Config(args.config)
    if cfg.missing and args.command != "init":
        die(f"config not found: {cfg.path} (run `init` first)")
    writes = args.command in WRITE_COMMANDS and not (args.command == "board" and args.action == "status")
    autosave = cfg.rules.get("autosave", True) is not False
    if writes:
        versioning.save(cfg.path.parent, f"手动改动（{args.command} 前自动存）", autosave)
    try:
        args.func(cfg, args)
    except (ConfigError, refmod.RefError, providers.ProviderError, stations.StationError,
            library.LibraryError, media.MediaError, jobs.JobError, cloud.CloudError) as error:
        die(str(error))
    finally:
        if writes or args.command == "init":
            versioning.save(cfg.path.parent, f"自动存：{_command_line(args)}", autosave)


if __name__ == "__main__":
    main()
