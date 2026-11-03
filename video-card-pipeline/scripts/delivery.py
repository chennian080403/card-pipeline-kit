"""Delivering approved takes: library drafts, the optional canvas board, cut list -> film."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import yaml

from core import library
import media
import refs as refmod
from cards import all_cards, load_card, save_card
from common import canvas_plugin, die, now
from produce import submit_to_station
from store import load_manifest, load_review, load_take, save_take


def cmd_register(cfg, args):
    if not library.enabled(cfg.library):
        die("no library configured (library.kind: local or http)")
    card = load_card(cfg, args.card)
    review = load_review(cfg, card["id"])
    picks = args.takes or review.get("pick") or []
    if not picks:
        die("nothing picked: sync the station or `pick` first")
    for name in picks:
        meta = load_take(cfg, name)
        human = (review["takes"].get(meta["name"]) or {}).get("human")
        frames = meta["refs"].get("keyframes") or [meta["refs"].get("first_frame"), meta["refs"].get("last_frame")]
        seen, lineage = set(), []
        for r in [*frames, *meta["refs"].get("images", [])]:   # 旧片子的中间关键帧曾折成参考图（两边都有），只记一次
            if r and (r.get("sha256") or r.get("label")) not in seen:
                seen.add(r.get("sha256") or r.get("label"))
                lineage.append(r)
        extra = {"derived_from": [{"label": r.get("label"), "origin": r.get("origin"), "sha256": r.get("sha256")}
                                  for r in lineage] + ([{"take": meta["derived_from"]}] if meta.get("derived_from") else []),
                 "review": human, "card": card["id"],
                 "reason": f"video take {meta['name']}" + (f"; station verdict {human.get('verdict')} by {human.get('by')}"
                                                           if human else "")}
        reply = library.ingest(cfg.library, title=f"{card['id']} {card.get('title', '')} {meta['model']}",
                               files=[meta["file"]], prompt=meta["prompt"], model=meta["model"],
                               batch=card.get("group") or card["id"], extra=extra,
                               operation_id=f"video-{meta['sha256'][:16]}")
        meta["library"] = {k: reply.get(k) for k in ("added", "already", "asset_id", "existing") if k in reply}
        save_take(cfg, meta)
        print(f"  {meta['name']} → library: {json.dumps(meta['library'], ensure_ascii=False)}")
    card["status"] = "registered"
    save_card(card)
    print("registered as drafts: only the human finalises in the library")


def cmd_board(cfg, args):
    spec = cfg.board
    if spec.get("kind") != "toonflow":
        if args.action == "status":
            print("· no board (optional canvas plugin, not installed)")
            return
        die("no board configured: the canvas is an optional plugin (video.board.kind: toonflow)")
    try:
        canvas = canvas_plugin(spec)
    except RuntimeError as error:
        die(str(error))
    if args.action == "status":
        ok, why = canvas.wait_ready(spec)
        print(("✓ " if ok else "✗ ") + why)
        return
    running = [k for k, j in load_manifest(cfg)["jobs"].items() if j.get("state") == "submitted"
               and cfg.providers.get(j.get("provider"), {}).get("kind") == "toonflow"]
    if running:
        die(f"canvas generation running ({', '.join(running)}): switching canvas would cancel it -- "
            f"`collect` first, then push")
    client = canvas.Canvas(spec)
    client.ensure_canvas(spec.get("canvas") or "成片看板")
    types = client.node_types()
    order = {c["id"]: i for i, c in enumerate(all_cards(cfg))}
    for card_id in args.cards:
        card = load_card(cfg, card_id)
        review = load_review(cfg, card_id)
        names = args.takes or review.get("pick") or []
        if not names:
            print(f"  {card_id}: nothing picked, skipped (the board shows approved takes only)")
            continue
        y = 420 * order.get(card_id, 0)
        prefix = f"{card_id} {card.get('title', '')}".strip()
        first = None
        for name in names:
            meta = load_take(cfg, name)
            first = first or (meta["refs"].get("first_frame") or {}).get("path")
        if first:
            client.upsert_media("image", f"{prefix} · 首帧", first, {"x": 0, "y": y}, types)
        for index, name in enumerate(names):
            meta = load_take(cfg, name)
            node = client.upsert_media("video", f"{prefix} · 成片 · {meta['name']}", meta["file"],
                                       {"x": 400 + index * 420, "y": y}, types)
            meta.setdefault("board", {})["node"] = node
            save_take(cfg, meta)
            print(f"  {card_id}: {meta['name']} → canvas node {node}")


# --------------------------------------------------------------------------- #
# assemble: cut list -> film (crossfades, music, burned subtitles)
# --------------------------------------------------------------------------- #

def _ass_time(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def cmd_assemble(cfg, args):
    path = cfg.cuts_dir / f"{args.cut}.yaml"
    if not path.is_file():
        die(f"no cut list {path} (copy cuts/_template.yaml)")
    cut = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    W, H, FPS = int(cut.get("width", 1920)), int(cut.get("height", 1080)), int(cut.get("fps", 24))
    if cfg.rules.get("fps") and FPS != int(cfg.rules["fps"]):
        die(f"cut fps={FPS}, but video.rules.fps={cfg.rules['fps']} (no frame interpolation in this project)")
    xf = float(cut.get("crossfade", 0.8))
    shots = []
    for index, shot in enumerate(cut.get("shots") or [], 1):
        if shot.get("card"):
            picks = load_review(cfg, shot["card"]).get("pick") or []
            if not picks:
                die(f"card {shot['card']} has no picked take")
            if not shot.get("take") and len(picks) > 1:
                print(f"  · shot {index}: card {shot['card']} has {len(picks)} picked takes ({', '.join(picks)}): "
                      f"using the first picked, {picks[0]} (write `take:` on the shot to choose)")
            file = Path(load_take(cfg, shot.get("take") or picks[0])["file"])
        elif shot.get("take"):
            file = Path(load_take(cfg, shot["take"])["file"])
        else:
            file = cfg.resolve(shot["file"])
        if not file.is_file():
            die(f"shot file missing {file}")
        info = media.probe(cfg.ffmpeg, file)
        real = float(info["duration"] or 0)
        t_in = float(shot.get("in", 0))
        t_out = float(shot.get("out", real))
        if real and t_out > real:
            print(f"  ! shot {index}: out {t_out:g}s is past the end of {file.name} ({real:g}s) -- cut at {real:g}s")
            t_out = real
        if t_out <= t_in:
            die(f"shot {index} ({file.name}): in {t_in:g}s is not before out {t_out:g}s (clip is {real:g}s)")
        shots.append({"file": file, "in": t_in, "len": t_out - t_in, "sub": shot.get("subtitle"),
                      "speed": float(shot.get("speed", 1.0)), "n": index, "name": file.name, "audio": info["audio"]})
    if not shots:
        die("cut list has no shots")
    if len(shots) > 1:
        for s in shots:
            if s["len"] / s["speed"] <= xf:
                die(f"shot {s['n']} ({s['name']}) lasts {s['len'] / s['speed']:.2f}s, not longer than the "
                    f"crossfade {xf:g}s: lengthen it or lower `crossfade`")
    inputs, graph, starts = [], [], []
    for i, s in enumerate(shots):
        inputs += ["-i", str(s["file"])]
        s["out_len"] = s["len"] / s["speed"]
        graph.append(f"[{i}:v]trim=start={s['in']}:duration={s['len']},setpts=(PTS-STARTPTS)/{s['speed']},"
                     f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},format=yuv420p,setsar=1[v{i}]")
    prev, t = "v0", shots[0]["out_len"]
    starts.append(0.0)
    for i in range(1, len(shots)):
        offset = t - xf
        starts.append(offset)
        graph.append(f"[{prev}][v{i}]xfade=transition={cut.get('transition', 'fade')}:duration={xf}:offset={offset:.3f}[x{i}]")
        prev, t = f"x{i}", offset + shots[i]["out_len"]
    total = t
    fade_in, fade_out = float(cut.get("fade_in", 0.6)), float(cut.get("fade_out", 1.5))
    graph.append(f"[{prev}]fade=t=in:st=0:d={fade_in},fade=t=out:st={max(total - fade_out, 0):.3f}:d={fade_out}[vf]")
    subs = []
    for s, st in zip(shots, starts):
        for sub in ([s["sub"]] if isinstance(s["sub"], (str, dict)) else (s["sub"] or [])):
            sub = {"text": sub} if isinstance(sub, str) else sub
            a0 = st + float(sub.get("at", 0.5))
            subs.append((a0, a0 + float(sub.get("dur", min(3.0, s["out_len"] - 0.8))), sub["text"]))
    vlast = "vf"
    tmpdir = Path(tempfile.mkdtemp())
    if subs:
        ass = tmpdir / "subs.ass"
        font = cut.get("font", "PingFang SC")
        ass.write_text(
            "[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\n\n[V4+ Styles]\n"
            "Format: Name,Fontname,Fontsize,PrimaryColour,OutlineColour,BackColour,Bold,Italic,BorderStyle,Outline,"
            "Shadow,Alignment,MarginL,MarginR,MarginV\nStyle: D,%s,%d,&H00FFFFFF,&H00000000,&H64000000,0,0,1,2,1,2,40,40,%d\n\n"
            "[Events]\nFormat: Layer,Start,End,Style,Text\n" % (W, H, font, int(cut.get("font_size", H // 15)), H // 12)
            + "".join(f"Dialogue: 0,{_ass_time(x)},{_ass_time(y)},D,{{\\fad(300,300)}}{txt}\n" for x, y, txt in subs),
            encoding="utf-8")
        graph.append(f"[vf]subtitles='{ass.as_posix()}'[vs]")
        vlast = "vs"
    music = cut.get("music")
    alast = "am"
    if cut.get("shot_audio"):     # keep each clip's own sound (e.g. H3's native audio), crossfaded with the picture
        vol = float(cut.get("shot_volume", 1.0))
        for i, s in enumerate(shots):
            if s["audio"]:
                tempo = f",atempo={s['speed']}" if s["speed"] != 1 else ""
                graph.append(f"[{i}:a]atrim=start={s['in']}:duration={s['len']},asetpts=PTS-STARTPTS{tempo},"
                             f"aresample=48000,aformat=channel_layouts=stereo,volume={vol}[a{i}]")
            else:
                graph.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={s['out_len']:.3f}[a{i}]")
        aprev = "a0"
        for i in range(1, len(shots)):
            graph.append(f"[{aprev}][a{i}]acrossfade=d={xf}:c1=tri:c2=tri[ax{i}]")
            aprev = f"ax{i}"
        graph.append(f"[{aprev}]afade=t=in:st=0:d={fade_in},afade=t=out:st={max(total - fade_out, 0):.3f}:d={fade_out}[as]")
        alast = "as"
    if music:
        mf = cfg.resolve(music["file"])
        if not mf.is_file():
            die(f"music missing {mf}")
        inputs += ["-i", str(mf)]
        mi = len(shots)
        graph.append(f"[{mi}:a]atrim=start={float(music.get('start', 0))}:duration={total:.3f},asetpts=PTS-STARTPTS,"
                     f"volume={music.get('volume', 1.0)},afade=t=in:st=0:d={music.get('fade_in', 1.0)},"
                     f"afade=t=out:st={max(total - float(music.get('fade_out', 2.5)), 0):.3f}:d={music.get('fade_out', 2.5)}[am]")
        if alast == "as":
            graph.append("[as][am]amix=inputs=2:duration=first:normalize=0[amx]")
            alast = "amx"
    elif alast == "am":
        graph.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={total:.3f}[am]")
    out_dir = cfg.output_dir / "final"
    out_dir.mkdir(parents=True, exist_ok=True)
    k = 1
    while (out_dir / f"{args.cut}_v{k:02d}.mp4").exists():
        k += 1
    out = out_dir / f"{args.cut}_v{k:02d}.mp4"
    cmd = [cfg.ffmpeg, "-v", "error", "-y", *inputs, "-filter_complex", ";".join(graph), "-map", f"[{vlast}]",
           "-map", f"[{alast}]", "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]
    if args.dry_run:
        print(" ".join(cmd))
        return
    print(f"▶ assemble {len(shots)} shots · {total:.1f}s · {W}x{H}@{FPS}")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        die(f"ffmpeg failed: {proc.stderr[-600:]}")
    probe = media.probe(cfg.ffmpeg, out)
    meta = {"name": out.stem, "file": str(out), "card": f"cut:{args.cut}", "title": cut.get("title", args.cut),
            "group": cut.get("group", ""), "model": "assemble", "provider": "ffmpeg", "mode": "assemble",
            "prompt": "", "refs": {"first_frame": None, "last_frame": None, "images": [], "videos": [], "audios": []},
            "shots": [{"file": str(s["file"]), "start": round(st, 2), "end": round(st + s["out_len"], 2)}
                      for s, st in zip(shots, starts)],
            "probe": probe, "sha256": refmod.sha256(out), "at": now(), "kind": "final", "cut": str(path)}
    save_take(cfg, meta)
    print(f"✓ {out}")
    if (cfg.station or {}).get("auto_submit", True):
        submit_to_station(cfg, meta)

