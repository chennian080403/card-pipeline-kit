"""Project and card housekeeping: init, new-card, cards, show, validate, prompt, status, providers, doctor."""

from __future__ import annotations

import shutil
import time

import yaml

from core import library
import media
import providers
import refs as refmod
import stations
from cards import (all_cards, card_files, collect_refs, compose_prompt, keyframe_report, load_card, pick_mode, save_card,
                   validate)
from common import canvas_plugin, die
from config import SKILL_ROOT, Config, ConfigError
from core import versioning
from store import load_manifest, load_review

CARD_TEMPLATE = SKILL_ROOT / "templates" / "card.template.yaml"


PROJECT_EXAMPLE = SKILL_ROOT / "project.example.yaml"


PROJECT_GITIGNORE = """# video-card-pipeline 项目：片子和派生物不进版本（片子只追加、从不覆盖；记录 json 进版本）
*.mp4
*.mov
*.webm
*.part
video-output/sheets/
video-output/handoff/
video-output/final/
.DS_Store
__pycache__/
"""


MEDIA_IGNORES = ("*.mp4", "*.mov", "*.webm", "*.part")


def _ignore_media(folder):
    """An image project getting a video section: make its .gitignore keep clips out (only adds missing lines)."""
    path = folder / ".gitignore"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    have = {line.strip() for line in text.splitlines()}
    missing = [line for line in MEDIA_IGNORES if line not in have]
    if missing:
        head = "" if not text or text.endswith("\n") else "\n"
        path.write_text(text + head + "# video-card-pipeline: 片子不进版本\n" + "\n".join(missing) + "\n", encoding="utf-8")
    return missing


def cmd_init(cfg, args):
    def copy(src, dst):
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            print(f"  + {dst}")

    example = PROJECT_EXAMPLE.read_text(encoding="utf-8")
    if cfg.missing:
        cfg.path.parent.mkdir(parents=True, exist_ok=True)
        crowded = [p for p in cfg.path.parent.iterdir() if p.name not in (".DS_Store", ".git", ".gitignore")]
        cfg.path.write_text(example, encoding="utf-8")
        print(f"  + {cfg.path}")
        if crowded:
            # versioning a folder that already holds other things would commit all of them
            print(f"  · {cfg.path.parent} already holds {len(crowded)} other item(s): no git repository made "
                  f"(versions are saved only when this folder is its own repository; `git init` it yourself if you want)")
        else:
            for line in versioning.init(cfg.path.parent, PROJECT_GITIGNORE):
                print(f"  + {line}")
    elif not cfg.video:
        # an image-card-pipeline project: append the video section, keep everything else.
        # No .bak: when the project is its own git repository the current file is saved first.
        saved = versioning.save(cfg.path.parent, "加 video: 段前自动存",
                                (cfg.raw.get("rules") or {}).get("autosave", True) is not False)
        section = example[example.index("\nvideo:") + 1:]
        with cfg.path.open("a", encoding="utf-8") as handle:
            handle.write("\n\n# ==== video-card-pipeline (added by `vpipeline.py init`) ====\n" + section)
        print(f"  ~ appended video: section to {cfg.path}"
              + (" (previous version saved: 版本 历史)" if saved else " (note: project has no git history)"))
        added = _ignore_media(cfg.path.parent)
        if added:
            print(f"  ~ .gitignore: + {' '.join(added)} (clips never go into versions)")
    cfg = Config(cfg.path)
    copy(SKILL_ROOT / "templates" / "blocks" / "motion_tail.txt", cfg.blocks_dir / "motion_tail.txt")
    copy(SKILL_ROOT / "templates" / "cut.template.yaml", cfg.cuts_dir / "_template.yaml")
    copy(SKILL_ROOT / "templates" / "sources.example.yaml", cfg.sources_file)
    cfg.cards_dir.mkdir(parents=True, exist_ok=True)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    print("next: new-card <id> <title> --first-frame <path|image:<card>>, then validate / generate")


def cmd_new_card(cfg, args):
    cfg.cards_dir.mkdir(parents=True, exist_ok=True)
    title = args.title or args.card_id
    path = cfg.cards_dir / f"{args.card_id}_{title}.yaml"
    if path.exists():
        die(f"exists: {path}")
    text = CARD_TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("id: XX-XX", f"id: {args.card_id}").replace("title: 镜头标题", f"title: {title}")
    text = text.replace("created: ''", f"created: '{time.strftime('%Y-%m-%d')}'")
    if args.first_frame:
        text = text.replace("first_frame: ''", f"first_frame: '{args.first_frame}'")
    if args.group:
        text = text.replace("group: ''", f"group: '{args.group}'")
    path.write_text(text, encoding="utf-8")
    print(f"✓ {path}")


def cmd_cards(cfg, args):
    for card in all_cards(cfg):
        print(f"{card['id']:12} {card.get('status', 'draft'):10} {card.get('group', ''):18} {card.get('title', '')}"
              + (f"  ⛔ {card['hold']}" if card.get("hold") else ""))


def cmd_show(cfg, args):
    card = load_card(cfg, args.card)
    print(yaml.safe_dump({k: v for k, v in card.items() if not k.startswith("_")}, allow_unicode=True, sort_keys=False))


def cmd_validate(cfg, args):
    bad = 0
    for card_id in args.cards:
        card = load_card(cfg, card_id)
        problems, notes, refs = validate(cfg, card)
        mode = pick_mode(card, refs)
        print(f"{'✗' if problems else '✓'} {card_id}  mode={mode}  model={card.get('model') or cfg.default_model()}")
        for p in problems:
            print(f"    PROBLEM {p}")
        for n in notes:
            print(f"    note    {n}")
        for r in refs.get("keyframes") or []:
            slot = {"first": "first_frame", "last": "last_frame"}.get(r["slot"], "keyframe")
            print(f"    {slot:11} {r['at']:>5g}s  {r.get('path') or r.get('url')}  [{r['origin'].get('kind')}]"
                  + ("  (final copy)" if r.get("final_copy") else ""))
        for bucket in ("images", "videos", "audios"):
            for r in refs[bucket]:
                print(f"    {bucket[:-1]:11} {r.get('path') or r.get('url')}  ({r['label']})")
        bad += bool(problems)
        if not problems and card.get("status") in (None, "draft"):
            card["status"] = "confirmed"
            save_card(card)
    if bad:
        raise SystemExit(1)


def cmd_prompt(cfg, args):
    card = load_card(cfg, args.card)
    prompt = compose_prompt(cfg, card)
    print(prompt)
    print(f"\n({len(prompt)} chars)")
    # what of the keyframes reaches the card's model (the prompt alone does not show it)
    alias = card.get("model") or cfg.default_model()
    try:
        spec = cfg.model_spec(alias)
    except ConfigError:
        spec = {}
    refs, _ = collect_refs(cfg, card, strict_final=False)
    problems, notes = keyframe_report(refs, spec, alias)
    for line in problems + notes:
        print(f"keyframes: {line}")


def cmd_status(cfg, args):
    manifest = load_manifest(cfg)
    for card in all_cards(cfg):
        jobs = {k: j for k, j in manifest["jobs"].items() if j["card"] == card["id"]}
        review = load_review(cfg, card["id"])
        counts = {}
        for job in jobs.values():
            counts[job.get("state")] = counts.get(job.get("state"), 0) + 1
        verdicts = {}
        for item in review["takes"].values():
            v = (item.get("human") or {}).get("verdict")
            if v:
                verdicts[v] = verdicts.get(v, 0) + 1
        print(f"{card['id']:12} {card.get('status', 'draft'):10} jobs={counts or '-'} human={verdicts or '-'} "
              f"pick={','.join(review.get('pick') or []) or '-'}")

def cmd_providers(cfg, args):
    for alias in cfg.model_aliases():
        try:
            spec = cfg.model_spec(alias)
            provider = providers.make(spec, cfg)
            ok, why = provider.credentials_ok() if args.check else (None, "")
        except (ConfigError, providers.ProviderError) as error:
            print(f"✗ {alias:22} {error}")
            continue
        tag = "free" if provider.free else "paid"
        state = "" if ok is None else ("✓ " if ok else "✗ ") + why
        print(f"  {alias:22} {spec['kind']:9} {spec.get('model', ''):28} {tag:5} {state}")


def cmd_doctor(cfg, args):
    problems = 0

    def line(ok, label, detail=""):
        nonlocal problems
        problems += not ok
        print(f"{'✓' if ok else '✗'} {label:18} {detail}")

    line(not cfg.missing, "config", str(cfg.path))
    line(bool(cfg.video), "video section", "present" if cfg.video else "missing: run `init`")
    line(cfg.cards_dir.is_dir(), "cards", f"{cfg.cards_dir} ({len(card_files(cfg)) if cfg.cards_dir.is_dir() else 0})")
    line(media.ffmpeg_ok(cfg.ffmpeg), "ffmpeg", cfg.ffmpeg)
    tracked, why = versioning.describe(cfg.path.parent)
    print(f"{'✓' if tracked else '·'} {'version':18} {why}")
    print(f"{'✓' if cfg.sources_file.is_file() else '·'} {'sources':18} {cfg.sources_file}"
          + ("" if cfg.sources_file.is_file() else " (optional)"))
    try:
        default = cfg.default_model()
        cfg.model_spec(default)
        line(True, "default model", default)
    except ConfigError as error:
        line(False, "default model", str(error))
    image_cfg = cfg.image_config_path()
    if image_cfg:
        try:
            project = refmod.ImageProject(image_cfg)
            line(project.output.is_dir() or True, "image link", f"{image_cfg} (output {project.output})")
        except Exception as error:   # noqa: BLE001
            line(False, "image link", str(error))
        skill = cfg.image_skill_dir()
        print(f"{'✓' if skill else '·'} {'image skill':18} {skill or 'not found (frame feedback stays manual)'}")
    else:
        print(f"· {'image link':18} none (standalone: first frames are file paths / source:)")
    if library.enabled(cfg.library):
        if cfg.library.get("kind") == "http":
            try:
                library.catalog_token(cfg.library, timeout=5)
                line(True, "library", f"http {cfg.library['url']}")
            except Exception as error:   # noqa: BLE001
                line(False, "library", f"{cfg.library['url']}: {error}")
        else:
            line(True, "library", f"local {cfg.library['root']}")
    else:
        print(f"· {'library':18} none (register disabled)")
    try:
        station = stations.make(cfg)
        line(True, "station", station.describe() if station else "none")
    except Exception as error:   # noqa: BLE001
        line(False, "station", str(error))
    if cfg.board.get("kind") == "toonflow":
        try:
            ok, why = canvas_plugin(cfg.board).wait_ready(cfg.board)
        except RuntimeError as error:
            ok, why = False, str(error)
        print(f"{'✓' if ok else '·'} {'board':18} toonflow (plugin) · {why}")
    for alias in cfg.model_aliases():
        try:
            provider = providers.make(cfg.model_spec(alias), cfg)
            ok, why = provider.credentials_ok()
            print(f"{'✓' if ok else '·'} {'model ' + alias:18} {why}")
        except Exception as error:   # noqa: BLE001
            print(f"· {'model ' + alias:18} {error}")
    print("doctor: ok" if not problems else f"doctor: {problems} problem(s)")
    if problems:
        raise SystemExit(1)

