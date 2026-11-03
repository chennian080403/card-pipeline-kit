"""Reviewing: frame sheets, the AI's own check, the station (submit / sync), picks, feedback, post hooks."""

from __future__ import annotations

import subprocess
import sys

import media
import stations
from cards import all_cards, load_card, save_card
from common import die, now
from core import keyframes as keyframemod
from produce import run_hook, submit_to_station
from store import card_takes, load_take, update_review


def cmd_sheet(cfg, args):
    names = [t["name"] for t in card_takes(cfg, args.target)] or [args.target]
    for name in names:
        meta = load_take(cfg, name)
        sheet = media.frame_sheet(cfg.ffmpeg, meta["file"], cfg.output_dir / "sheets" / f"{meta['name']}.jpg")
        print(f"{meta['name']}: {sheet}")


def cmd_review(cfg, args):
    """The AI's own check (first / middle / last frame). The human's verdict comes from the station."""
    meta = load_take(cfg, args.take)
    update_review(cfg, meta["card"], lambda review: review["takes"].setdefault(meta["name"], {}).update(
        ai={"verdict": args.verdict, "note": args.note or "", "by": args.by, "at": now()}))
    print(f"ai review {meta['name']}: {args.verdict}" + (f" ({args.note})" if args.note else ""))


def cmd_submit(cfg, args):
    for name in args.takes:
        submit_to_station(cfg, load_take(cfg, name))


def _sync_card(review, verdicts):
    """Fold the station's verdicts into one card's review. verdicts: [(take name, fetch() result)].

    pick keeps the order takes were added in (assemble uses the first). review["synced"] lists the picks
    that came from the station, so a withdrawn verdict (contract §9: empty = withdraw) unpicks only
    those -- takes the human picked by hand stay. Returns the lines to print."""
    picks = list(review.get("pick") or [])
    synced = set(review.get("synced") or [])
    lines = []
    for name, verdict in verdicts:
        entry = review["takes"].setdefault(name, {})
        if not verdict:                                  # nothing said (any more): withdrawn
            if entry.pop("human", None) is not None:
                lines.append(f"  {name}: verdict withdrawn")
        elif entry.get("human") != verdict:
            entry["human"] = verdict
            lines.append(f"  {name}: {verdict.get('verdict') or '(no verdict)'}"
                         + (f" — {verdict.get('note')}" if verdict.get("note") else "")
                         + (f" [{len(verdict.get('marks') or [])} marks]" if verdict.get("marks") else ""))
        verdict = verdict or {}
        if verdict.get("verdict") == "pass" and not verdict.get("discard"):
            if name not in picks:
                picks.append(name)
                synced.add(name)
        elif verdict.get("verdict") == "fail" or verdict.get("discard"):
            if name in picks:
                picks.remove(name)
            synced.discard(name)
        elif not verdict.get("verdict") and name in synced:   # verdict withdrawn: only the station's own pick goes
            if name in picks:
                picks.remove(name)
            synced.discard(name)
            lines.append(f"  {name}: unpicked (it was picked by an earlier station verdict)")
    review["pick"] = picks
    review["synced"] = [n for n in picks if n in synced]
    return lines


def cmd_sync(cfg, args):
    """Pull the human's verdicts from the station; passed takes are picked, failed / withdrawn ones unpicked."""
    station = stations.make(cfg)
    if station is None:
        die("no station configured")
    changed = 0
    cards = args.cards or [c["id"] for c in all_cards(cfg)]
    for card_id in cards:
        verdicts = [(meta["name"], station.fetch(meta, meta["station"]))
                    for meta in card_takes(cfg, card_id) if meta.get("station")]
        lines, picks = update_review(cfg, card_id, lambda review: (_sync_card(review, verdicts), review["pick"]))
        changed += len(lines)
        for line in lines:
            print(line)
        if picks:
            card = load_card(cfg, card_id)
            if card.get("status") in (None, "draft", "confirmed", "generated"):
                card["status"] = "reviewed"
                save_card(card)
    print(f"sync: {changed} change(s)")


def cmd_pick(cfg, args):
    """Pick / unpick by hand. Hand picks keep their order and are never undone by a withdrawn station verdict."""
    card = load_card(cfg, args.card)
    names = [load_take(cfg, t)["name"] for t in args.takes]

    def change(review):
        picks = list(review.get("pick") or [])
        synced = [n for n in review.get("synced") or [] if n not in names]
        if args.remove:
            picks = [n for n in picks if n not in names]
        else:
            picks += [n for n in names if n not in picks]
        review["pick"], review["synced"] = picks, [n for n in synced if n in picks]
        return picks
    picks = update_review(cfg, card["id"], change)
    print(f"pick: {', '.join(picks) or '(none)'}")


def cmd_feedback(cfg, args):
    card = load_card(cfg, args.card)
    card.setdefault("feedback", []).append({"at": now(), "by": args.by or "human", "verdict": args.verdict,
                                            "reason": args.reason, "root_cause": args.root_cause or "",
                                            "take": args.take or ""})
    if args.verdict == "reject":
        card["status"] = "draft"
    save_card(card)
    print(f"feedback recorded on {card['id']}: {args.verdict}")
    if args.root_cause == "frame":
        _feedback_to_image(cfg, card, args)
    hint = {"card": "edit motion / mode / duration on the card, then generate a new take",
            "model": "same card on another model, or raise --budget",
            "frame": "a keyframe itself is wrong: fix it on the image side",
            "ref": "a reference (source / refs) is wrong: fix sources.yaml or the card refs"}.get(args.root_cause)
    if hint:
        print(f"  next: {hint}")


def _feedback_to_image(cfg, card, args):
    """关键帧有问题：每张来自生图（image:<卡>）的关键帧，都回写到它那张图卡（同一张卡只写一次）。"""
    image_cards = []
    for value, _item in keyframemod.values(card):
        if value.startswith("image:"):
            image_card = value[len("image:"):].split("/")[0]
            if image_card not in image_cards:
                image_cards.append(image_card)
    if not image_cards:
        print("  no keyframe is an image:<card> ref -- nothing to send back")
        return
    skill = cfg.image_skill_dir()
    image_cfg = cfg.image_config_path()
    if not skill or not image_cfg:
        print(f"  image skill or image project not found -- tell the image side by hand: cards {', '.join(image_cards)}")
        return
    reason = f"[视频 {card['id']}{' ' + args.take if args.take else ''}] {args.reason}"
    for image_card in image_cards:
        proc = subprocess.run([sys.executable, str(skill / "scripts" / "pipeline.py"), "--config", str(image_cfg),
                               "feedback", image_card, "revise", "--reason", reason, "--root-cause", "frame",
                               "--by", "video-card-pipeline"], capture_output=True, text=True)
        if proc.returncode == 0:
            print(f"  ↩ sent to image card {image_card}: {proc.stdout.strip()}")
        else:
            print(f"  ! could not write image feedback to {image_card}: {(proc.stderr or proc.stdout)[-300:]}")


def cmd_post(cfg, args):
    meta = load_take(cfg, args.take)
    derived = run_hook(cfg, meta, args.hook)
    if derived and (cfg.station or {}).get("auto_submit", True):
        submit_to_station(cfg, derived)


