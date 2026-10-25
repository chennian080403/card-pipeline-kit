"""Video cards: load / save, references, prompt, mode, validation."""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path

import yaml

import refs as refmod
from common import die
from core import keyframes as keyframemod
from core import sources as sourcekinds
from config import ConfigError

SYSTEM_KEYS = ("status", "hold", "created", "feedback", "sources")


def card_files(cfg):
    return [p for p in sorted(cfg.cards_dir.glob("*.yaml")) if not p.name.startswith("_")]


def load_card(cfg, card_id):
    hits = [p for p in card_files(cfg) if p.stem == card_id or p.stem.startswith(card_id + "_")]
    if not hits:
        die(f"no video card {card_id} in {cfg.cards_dir}")
    if len(hits) > 1:
        exact = [p for p in hits if (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("id") == card_id]
        hits = exact or hits
        if len(hits) > 1:
            die(f"{card_id} matches several cards: {[h.name for h in hits]}")
    card = yaml.safe_load(hits[0].read_text(encoding="utf-8")) or {}
    card.setdefault("id", card_id)
    if card.get("act") and not card.get("group"):      # older cards call the group "act"
        card["group"] = card["act"]
    card["_path"] = hits[0]
    return card


def all_cards(cfg):
    out = []
    for path in card_files(cfg):
        card = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        card.setdefault("id", path.stem.split("_")[0])
        card["_path"] = path
        out.append(card)
    return out


def _empty(value):
    return value is None or value == "" or value == [] or value == {}


def save_card(card):
    """Human part first, the pipeline's record under a divider (same layout as image-card-pipeline)."""
    payload = {k: v for k, v in card.items() if not k.startswith("_") and not _empty(v)}
    mine = {k: v for k, v in payload.items() if k not in SYSTEM_KEYS}
    record = {k: payload[k] for k in SYSTEM_KEYS if k in payload}
    text = yaml.safe_dump(mine, allow_unicode=True, sort_keys=False, width=110)
    if record:
        text += "\n# ---- 以下由流水线记录（状态 / 反馈 / 来源），不用手改 ----\n"
        text += yaml.safe_dump(record, allow_unicode=True, sort_keys=False, width=110)
    card["_path"].write_text(text, encoding="utf-8")


def card_fingerprint(card, cfg=None):
    payload = {k: v for k, v in card.items() if not k.startswith("_") and k not in SYSTEM_KEYS and k != "pack"}
    # 画风 / 氛围真源的描述会拼进提示词：描述改了，指纹跟着变（没有描述的卡指纹不变）
    texts = kind_texts(cfg, card)[0] if cfg is not None else {}
    if any(texts.values()):
        payload = dict(payload, _style_texts=texts)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]

# --------------------------------------------------------------------------- #
# refs + prompt + mode
# --------------------------------------------------------------------------- #

def collect_refs(cfg, card, strict_final=True):
    """Returns (refs, problems). refs = {keyframes, first_frame, last_frame, images, videos, audios}.

    keyframes: every keyframe in time order (each ref carries ``at`` / ``slot``); first_frame / last_frame
    are the ones at 0s / at the end, kept as their own keys because most providers only take those two."""
    out = {"keyframes": [], "first_frame": None, "last_frame": None, "images": [], "videos": [], "audios": []}
    problems = []

    def get(value, label, role=None):
        try:
            return refmod.resolve(cfg, value, label, role)
        except refmod.RefError as error:
            problems.append(str(error))
            return None

    # 关键帧：首帧、中间几张、尾帧是同一条时间线；只认定稿时每一张都要是定稿
    # 新名 keyframes、旧名 first_frame 任一写了 final 就按 final（不让 keyframes: any 的默认值盖掉旧配置）
    policy = "final" if "final" in (cfg.rules.get("keyframes"), cfg.rules.get("first_frame")) else "any"
    try:
        timeline = keyframemod.timeline(card)
    except keyframemod.KeyframeError as error:
        problems.append(str(error))
        timeline = []
    for item in timeline:
        role = {"first": "first frame", "last": "last frame"}.get(item["slot"], f"keyframe at {item['at']:g}s")
        ref = get(item["value"], item["label"], role)
        if not ref:
            continue
        if policy == "final" and strict_final:
            ok, why, copy = refmod.final_status(cfg, ref)
            if not ok:
                problems.append(f"{item['label']} is not a final asset ({why}): finalise it in the library first, "
                                f"or set video.rules.keyframes: any")
            elif copy and Path(copy) != Path(ref["path"]):
                ref = dict(ref, path=str(copy), final_copy=True)
        ref = dict(ref, at=item["at"], slot=item["slot"])
        out["keyframes"].append(ref)
        if item["slot"] in ("first", "last"):
            out[item["slot"] + "_frame"] = ref
    kind_map = cfg.kinds
    for name in card.get("characters") or []:
        name = name.get("lookup") or name.get("name") if isinstance(name, dict) else name
        ref = get(f"source:characters/{name}", f"character:{name}", f"identity and costume of {name}")
        if ref:
            out["images"].append(ref)
    # other image kinds (props, and any kind a project declares) go out as reference images;
    # text kinds (styles …) only add their description to the prompt -- see kind_texts
    for section, kind in kind_map.items():
        if section == "characters" or kind["text"]:
            continue
        for name in sourcekinds.card_names(card, section):
            ref = get(f"source:{section}/{name}", f"{kind.get('as') or section}:{name}",
                      sourcekinds.role_for(kind_map, section, name))
            if ref:
                out["images"].append(ref)
    section = card.get("refs") or {}
    for bucket in ("images", "videos", "audios"):
        for index, value in enumerate(section.get(bucket) or []):
            ref = get(value, f"refs.{bucket}[{index}]")
            if ref:
                out[bucket].append(ref)
    return out, problems


MODES = ("auto", "keyframes", "first_frame", "reference", "text")


def pick_mode(card, refs):
    """auto: a first frame plus one or more keyframes -> keyframes; a first frame alone -> first_frame;
    references only -> reference; nothing -> text. Keyframes that miss the 0s slot are a validate PROBLEM
    (see keyframe_report), so they never silently turn a shot into text-to-video."""
    mode = card.get("mode") or "auto"
    if mode != "auto":
        return mode
    if refs.get("first_frame") and len(refs.get("keyframes") or []) >= 2:
        return "keyframes"
    if refs.get("first_frame"):
        return "first_frame"
    if refs.get("images") or refs.get("videos"):
        return "reference"
    return "text"


DEFAULT_TAIL = ("Keep faces, costumes and art style exactly as in the first frame. "
                "No text, no subtitles, no watermark, no camera shake.")


def _block_text(cfg, name):
    path = cfg.blocks_dir / name
    if not path.is_file():
        return ""
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#")]
    return " ".join(" ".join(lines).split())


def kind_texts(cfg, card):
    """({name: prompt}, problems) for the card's text kinds -- contract v1.3/v1.4: sources.yaml <section>.<name>.prompt.

    Keys are the bare name for ``styles`` and ``<section>/<name>`` for other text kinds."""
    texts, problems = {}, []
    registry = None
    for section, kind in cfg.kinds.items():
        if not kind["text"]:
            continue
        for name in sourcekinds.card_names(card, section):
            if registry is None:
                registry = refmod.load_sources(cfg)
            entry = (registry.get(section) or {}).get(name)
            if entry is None:
                problems.append(f"{section}: {name} is not registered in {cfg.sources_file}")
                continue
            key = name if section == "styles" else f"{section}/{name}"
            texts[key] = str((entry.get("prompt") if isinstance(entry, dict) else "") or "").strip()
    return texts, problems


def compose_prompt(cfg, card):
    """style block + text kinds' descriptions (styles …) + motion + hard rules + tail. Dialogue never goes in: subtitles belong to the cut list."""
    parts = [_block_text(cfg, "style.txt"), *kind_texts(cfg, card)[0].values(), str(card.get("motion") or "").strip()]
    parts += [str(rule).strip() for rule in card.get("hard_rules") or []]
    tail = card.get("motion_tail") or _block_text(cfg, "motion_tail.txt") or DEFAULT_TAIL
    parts.append(tail.strip())
    return " ".join(p for p in parts if p).strip()


KEYFRAME_LIMIT = {"handoff": None, "command": None}   # None = takes them all; other kinds default to 2 (first + last)

# top-level card fields the pipeline reads (plus every section in sources.kinds); anything else gets a note
CARD_FIELDS = {"id", "title", "group", "act", "model", "first_frame", "keyframes", "last_frame", "characters", "styles",
               "refs", "mode", "duration", "aspect", "resolution", "takes", "motion", "hard_rules", "motion_tail",
               "subtitle", "audio", "hold", "created", "pack", "chosen", *SYSTEM_KEYS}


def keyframe_limit(spec):
    limit = spec.get("max_keyframes", KEYFRAME_LIMIT.get(spec.get("kind"), 2))
    return None if limit is None else int(limit)


def keyframe_report(refs, spec, alias=""):
    """What of the card's keyframes actually reaches this model (contract §5). Returns (problems, notes).

    - keyframes that do not start at 0s (only `at: 3`, only a last frame, at 1s / 4s ...) are a PROBLEM:
      most models need a first frame, and without one the shot would quietly become text-to-video;
    - a model that takes fewer keyframes (default: first + last) is told plainly that the middle ones
      are NOT sent -- they are not smuggled in as reference images either (no model entry reads them there
      in keyframes mode). handoff / command entries get every keyframe."""
    frames = refs.get("keyframes") or []
    if not frames:
        return [], []
    problems, notes = [], []
    if not refs.get("first_frame"):
        problems.append(f"keyframes start at {frames[0]['at']:g}s ({frames[0].get('label')}), not at 0s: give a "
                        f"first_frame (or drop `at` on the first keyframe), or move these images to refs.images")
        return problems, notes
    limit = keyframe_limit(spec)
    middle = [r for r in frames if r.get("slot") == "middle"]
    who = alias or "this model"
    if limit is not None and middle and (limit <= 2 or len(frames) > limit):   # two-frame models take only the 0s / end slots
        times = ", ".join(f"{r['at']:g}s" for r in middle)
        taken = "the first + last keyframes" if limit == 2 else f"{limit} keyframes"
        notes.append(f"{who} uses only {taken}: the {len(middle)} in the middle "
                     f"({times}) are NOT sent "
                     f"(set max_keyframes on the model if it takes more, or use a handoff / command entry)")
        if not refs.get("last_frame"):
            notes.append(f"no last_frame: {who} runs this shot from the first frame alone")
    return problems, notes


def validate(cfg, card, alias=None):
    """(problems, notes, refs) for this card on model ``alias`` (default: the card's model, then
    models.default) -- generate --model X validates against X, so its limits are the ones checked."""
    problems, notes = [], []
    for key in ("id", "motion"):
        if _empty(card.get(key)) or str(card.get(key)).strip() == "...":
            problems.append(f"missing {key}")
    if card.get("hold"):
        problems.append(f"hold: {card['hold']}")
    if card.get("mode") not in (None, "", *MODES):
        problems.append(f"mode {card['mode']!r} is not one of {' / '.join(MODES)}")
    known = CARD_FIELDS | set(cfg.kinds)
    for key in card:
        if not str(key).startswith("_") and key not in known:
            close = difflib.get_close_matches(str(key), sorted(known), n=1)
            notes.append(f"unknown field {key!r}" + (f" (did you mean {close[0]!r}?)" if close else "")
                         + ": the pipeline does not read it")
    alias = alias or card.get("model") or cfg.default_model()
    try:
        spec = cfg.model_spec(alias)
    except ConfigError as error:
        problems.append(str(error))
        spec = {}
    refs, ref_problems = collect_refs(cfg, card)
    problems += ref_problems
    frame_problems, frame_notes = keyframe_report(refs, spec, alias)
    problems += frame_problems
    notes += frame_notes
    texts, kind_problems = kind_texts(cfg, card)
    problems += kind_problems
    notes += [f"{name if '/' in name else 'styles/' + name} has no prompt in sources.yaml (adds nothing to the prompt)"
              for name, text in texts.items() if not text]
    duration = float(card.get("duration", 5))
    if spec.get("min_seconds") and duration < float(spec["min_seconds"]):
        problems.append(f"duration {duration}s below {alias} minimum {spec['min_seconds']}s")
    if spec.get("max_seconds") and duration > float(spec["max_seconds"]):
        problems.append(f"duration {duration}s above {alias} maximum {spec['max_seconds']}s")
    if "subtitle" in str(card.get("motion", "")).lower():
        notes.append("motion mentions subtitles: dialogue and subtitles belong to the cut list")
    limit = (cfg.rules.get("max_prompt_chars") or {})
    prompt = compose_prompt(cfg, card)
    if limit.get("reject") and len(prompt) > int(limit["reject"]):
        problems.append(f"prompt {len(prompt)} chars > reject {limit['reject']}")
    elif limit.get("warn") and len(prompt) > int(limit["warn"]):
        notes.append(f"prompt {len(prompt)} chars > warn {limit['warn']}")
    return problems, notes, refs

