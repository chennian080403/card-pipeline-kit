#!/usr/bin/env python3
"""image-card-pipeline -- one card drives one image, end to end.

Stages:  validate -> compose -> generate -> sheet -> review -> keep (= pass + pick + register)

The human shows up three times: at the start (confirm card, tick references,
approve the batch), when supplementing (one reason on rejection), and at
acceptance (finalise in the library). Everything between is automatic.

Nothing here is project-specific: every path, provider, model and endpoint comes
from a project config (see project.example.yaml). Credentials come from the
environment only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fileio
from core import keyframes as keyframemod
from core import sources as sourcekinds
import hosts
import library
import providers
from providers import ProviderError, sniff_image

SKILL_ROOT = Path(__file__).resolve().parent.parent
CARD_TEMPLATE = SKILL_ROOT / "templates" / "card.template.yaml"


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

class Config:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.missing = not self.path.is_file()
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) if not self.missing else {}
        self.raw = _expand(raw or {})
        self.name = (self.raw.get("project") or {}).get("name") or self.path.stem
        base = (self.raw.get("project") or {}).get("root")
        self.root = (self.path.parent / base).resolve() if base else self.path.parent
        self.paths = self.raw.get("paths") or {}
        self.providers = (self.raw.get("providers") or {}).get("entries") or {}
        self.default_provider = (self.raw.get("providers") or {}).get("default")
        self.models = self.raw.get("models") or {}
        self.host = self.raw.get("image_host") or {"kind": "none"}
        self.library = dict(self.raw.get("library") or {"kind": "none"})
        if self.library.get("root") and not Path(str(self.library["root"])).expanduser().is_absolute():
            self.library["root"] = str((self.root / str(self.library["root"])).resolve())
        self.rules = self.raw.get("rules") or {}

    def dir(self, key, default):
        value = self.paths.get(key, default)
        path = Path(os.path.expanduser(str(value)))
        return path if path.is_absolute() else (self.root / path).resolve()

    @property
    def kinds(self):
        """True-source kinds (ecosystem contract v1.4): defaults + project.yaml ``sources.kinds``."""
        try:
            return sourcekinds.kinds(self.raw)
        except sourcekinds.KindError as error:
            raise SystemExit(str(error)) from error

    @property
    def prompt_zone(self):
        return self.dir("prompt_zone", "prompt-zone")

    @property
    def cards_dir(self):
        return self.dir("cards", "cards")

    @property
    def blocks_dir(self):
        return self.dir("blocks", "blocks")

    @property
    def output_dir(self):
        return self.dir("output", "output")

    @property
    def inspiration_dir(self):
        return self.dir("inspiration", "inspiration")

    @property
    def promoted_dir(self):
        return self.dir("promoted", "refs")

    @property
    def sources_file(self):
        value = self.paths.get("sources", "sources.yaml")
        path = Path(os.path.expanduser(str(value)))
        return path if path.is_absolute() else (self.root / path).resolve()

    def model_aliases(self):
        """Only the alias -> {provider, model, ...} entries; 'default' and friends are not aliases."""
        return {alias: entry for alias, entry in self.models.items() if isinstance(entry, dict)}

    def model_spec(self, alias):
        """Resolve a model alias to a full provider spec."""
        entry = self.models.get(alias)
        if not isinstance(entry, dict):
            raise ProviderError(f"model alias '{alias}' is not defined in models:")
        entry = dict(entry)
        provider_name = entry.pop("provider", None) or self.default_provider
        if not provider_name or provider_name not in self.providers:
            raise ProviderError(f"model alias '{alias}' points at unknown provider '{provider_name}'")
        spec = dict(self.providers[provider_name])
        spec["name"] = provider_name
        # per-alias overrides win over provider-level defaults
        spec.update(entry)
        spec.setdefault("model", alias)
        return spec

    def default_model(self):
        return self.models.get("default") or next(iter(self.models), None)


def _expand(value):
    """Recursively expand ${ENV} / ~ in config strings."""
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, str):
        return os.path.expanduser(os.path.expandvars(value))
    return value


# --------------------------------------------------------------------------- #
# prompt zone + blocks
# --------------------------------------------------------------------------- #

PROMPT_PARA = re.compile(r"^[A-Za-z(\"'{]")


def _body_paragraphs(text):
    """Keep only latin body paragraphs; Chinese notes, tables and headings stay out."""
    return "\n\n".join(p.strip() for p in text.split("\n\n") if PROMPT_PARA.match(p.strip()))


def prompt_templates(zone):
    """Scan <zone>/**/*.md -> [{id, group, name, variables, order, body}].

    Each ``## `` section becomes its own entry (id ``<file>#<section>``) so ticking
    one line never drags a whole variant table into the prompt. Rescanned on every
    call: edit a template, no restart.
    """
    rows = []
    zone = Path(zone)
    if not zone.is_dir():
        return rows
    for path in sorted(zone.rglob("*.md")):
        if path.name == "README.md" or any(part.startswith(("_", ".")) for part in path.relative_to(zone).parts):
            continue
        text = path.read_text(encoding="utf-8")
        meta, body = {}, text
        if text.startswith("---"):
            head, _, body = text[3:].partition("\n---")
            for line in head.strip().splitlines():
                key, _, value = line.partition(":")
                if key.strip():
                    meta[key.strip()] = value.strip()
        rel = path.relative_to(zone).as_posix()
        variables = [v.strip() for v in meta.get("变量", "").replace("，", ",").split(",") if v.strip()]
        try:
            order = int(meta.get("顺序", "50"))
        except ValueError:
            order = 50
        name = meta.get("名称") or path.stem
        base = {"group": rel.rsplit("/", 1)[0] if "/" in rel else "", "order": order}
        main, *sections = re.split(r"\n## ", "\n" + body)
        main_body = _body_paragraphs(main)
        if main_body or not sections:
            rows.append({**base, "id": rel[:-3], "name": name, "variables": variables, "body": main_body or body.strip()})
        for section in sections:
            heading, _, sec = section.partition("\n")
            sec_body = _body_paragraphs(sec)
            if not sec_body:
                continue
            short = re.split(r"[（(]", heading, 1)[0].strip()
            rows.append({**base, "id": f"{rel[:-3]}#{short}", "name": f"{name} · {short}",
                         "variables": [v for v in variables if "{{" + v + "}}" in sec_body], "body": sec_body})
    rows.sort(key=lambda row: (row["order"], row["id"]))
    return rows


def load_blocks(cfg):
    """blocks/*.yaml -> {stem: {key: english}} plus blocks/*.txt -> {stem: text}."""
    blocks = {}
    directory = cfg.blocks_dir
    if not directory.is_dir():
        return blocks
    for path in sorted(directory.glob("*.yaml")):
        blocks[path.stem] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for path in sorted(directory.glob("*.txt")):
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")]
        blocks[path.stem] = "\n".join(lines).strip()
    return blocks


def _block(blocks, section, key):
    table = blocks.get(section) or {}
    if isinstance(table, dict) and key in table:
        return str(table[key]).strip()
    return str(key).strip() if key else ""


# --------------------------------------------------------------------------- #
# cards
# --------------------------------------------------------------------------- #

def load_card(cfg, card_id):
    """The card whose ``id`` is exactly ``card_id``; only when none has it, the file named ``<card_id>_*.yaml``
    (a card that lost its id line). Exact first: ``s-a`` must never land on ``s-a_b``'s file."""
    card_id = str(card_id)
    by_name = None
    for path in sorted(cfg.cards_dir.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        try:
            card = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as error:
            if path.stem.split("_")[0] == card_id:
                raise SystemExit(f"card {path.name}: invalid YAML: {error}")
            print(f"warning: skipping {path.name}: invalid YAML", file=sys.stderr)   # one broken card must not block the rest
            continue
        if not isinstance(card, dict):
            continue
        if str(card.get("id")) == card_id:
            card["_path"] = path
            return card
        if by_name is None and path.stem.split("_")[0] == card_id and not card.get("id"):
            by_name = (path, card)
    if by_name:
        path, card = by_name
        card["_path"] = path
        return card
    raise SystemExit(f"no card with id '{card_id}' under {cfg.cards_dir}")


# libyaml 的 C 版解析器快十几倍（审核页每次刷新都要读全部卡）；没装就退回纯 Python 版
_FAST_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def all_cards(cfg):
    cards = []
    for path in sorted(cfg.cards_dir.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        try:
            card = yaml.load(path.read_text(encoding="utf-8"), Loader=_FAST_LOADER) or {}
        except yaml.YAMLError:
            print(f"warning: skipping {path.name}: invalid YAML (run `validate` on it)", file=sys.stderr)
            continue
        card["_path"] = path
        cards.append(card)
    return cards


# 资产包：大包/小包/子包。只是归类路径，不搬文件。
DEFAULT_PACK_CATEGORIES = ["人物", "道具", "场景", "风格", "测试"]


def pack_categories(cfg):
    return [str(c) for c in ((cfg.raw.get("packs") or {}).get("categories") or DEFAULT_PACK_CATEGORIES)]


def pack_parts(value):
    return [part.strip() for part in str(value or "").replace("\\", "/").split("/") if part.strip()]


def card_pack(card, kind_map=None):
    """Card -> pack path. An explicit `pack:` wins; otherwise 人物/<角色>/<分组>, 道具/<道具>/<分组>
    (any image kind of sources.kinds: <label>/<名>/<分组>), 测试/<分组>."""
    kind_map = kind_map or sourcekinds.DEFAULT_KINDS
    explicit = pack_parts(card.get("pack"))
    if explicit:
        return explicit
    group = card.get("group") or ("短效草图" if card.get("kind") == "sketch" else "未分组")
    for item in card.get("characters") or []:
        name = item.get("name") if isinstance(item, dict) else item
        if name:
            return [kind_map["characters"]["label"], str(name), group]
    for section, kind in kind_map.items():
        if section == "characters" or kind["text"]:
            continue
        for item in card.get(section) or []:
            name = item.get("name") if isinstance(item, dict) else item
            if name:
                return [kind["label"], *pack_parts(name), group]
    return ["测试", group]


def source_pack(section, name, entry, kind_map=None):
    """Registered source -> pack path: 人物/<名>/长相, 道具/<名>/长相, 风格/<名字前缀> (files only in leaves).

    Image kinds go to <label>/<名>/长相 (it is already the 真源区, so the leaf says what it is),
    text kinds (styles …) to <label>/<名字前缀>."""
    explicit = pack_parts((entry or {}).get("pack"))
    if explicit:
        return explicit
    kind = (kind_map or sourcekinds.DEFAULT_KINDS).get(section) or {"label": section, "text": False}
    if not kind["text"]:
        return [kind["label"], *pack_parts(name), "长相"]
    for sep in ("_", "·"):
        if sep in name:
            return [kind["label"], name.split(sep, 1)[0]]
    return [kind["label"], name]


PROMOTE_LOOKS = {"画风": "paint", "氛围": "mood"}


def parse_promote(value, kind_map=None):
    """Card `promote:` -> {want, as, name, look}; None when empty. Raises ValueError on a bad value.

    Written when the card is made (``sketch --promote``) so the review page knows what this card
    is for: ``character:主角`` / ``prop:主角/剑`` / ``style:黄昏夕阳 @氛围`` / ``template:城门回头``.
    ``want`` is the page's "喜欢它的什么" button: look / paint / mood / comp / t:<as>."""
    text = str(value or "").strip()
    if not text:
        return None
    kind_map = kind_map or sourcekinds.DEFAULT_KINDS
    look = ""
    if "@" in text:
        text, _, look = text.rpartition("@")
        text, look = text.strip(), look.strip()
    kind, _, name = text.partition(":")
    kind, name = kind.strip(), name.strip()
    if not name:
        raise ValueError(f"promote must be <kind>:<name>, got {value!r}")
    if kind == "template":
        token, want = "template", "comp"
    else:
        section = sourcekinds.section_for(kind_map, kind)
        if not section:
            choices = ", ".join(str(v.get("as") or k) for k, v in kind_map.items())
            raise ValueError(f"promote: unknown kind {kind!r} (use {choices} or template)")
        entry = kind_map[section]
        token = str(entry.get("as") or section)
        if not entry["text"]:
            want = "look"
        elif section == "styles":
            if look not in PROMOTE_LOOKS:
                raise ValueError(f"promote: a style says what it is for -- add @画风 or @氛围 ({value!r})")
            want = PROMOTE_LOOKS[look]
        else:
            want = f"t:{token}"
    if look and want not in ("paint", "mood"):
        raise ValueError(f"promote: @{look} only goes with a style ({value!r})")
    return {"want": want, "as": token, "name": name, "look": look}


def promote_defaults(card, kind_map=None, registry=None):
    """What the review page pre-fills when a picture of this card is promoted.

    The card's own `promote:` wins (and picks the button); the rest is derived:
    长相 from the pack / character; 画风 from the card's first style registered as 画风, 氛围 from its
    first other style (a style with no `look` is an atmosphere reference); either falls back to the
    title segment that is not a character name; 构图 from the last title segment."""
    kind_map = kind_map or sourcekinds.DEFAULT_KINDS
    pack = card_pack(card, kind_map)
    top = pack[0] if pack else ""
    chars = [c for c in card.get("characters") or [] if isinstance(c, dict) and c.get("name")]
    subject = chars[0]["name"] if chars else ""
    lookup = next((c["lookup"] for c in chars if c.get("lookup")), "")
    outfit = card.get("outfit") or ""
    title = str(card.get("title") or card.get("id") or "")
    bare = title[2:] if title.startswith("s-") else title
    image_kind = next((v for k, v in kind_map.items() if not v["text"] and v["label"] == top), None)
    person = kind_map["characters"]["label"]
    # 道具按「角色/道具」两级起名（主角/剑），免得和人物重名；人物有造型时带上造型（主角/婚服）
    who = pack[1] if len(pack) > 1 else subject
    if image_kind and image_kind is not kind_map["characters"] and len(pack) > 2:
        look_name = "/".join(pack[1:3])
    elif top == person and lookup:
        look_name = lookup
    elif top == person and outfit and who:
        look_name = f"{who}/{outfit}"
    elif image_kind and len(pack) > 1:
        look_name = pack[1]
    else:
        look_name = subject or bare.split("·")[0]
    styles = sourcekinds.card_names(card, "styles")
    segments = [seg for seg in bare.split("·") if seg] or [bare]
    # 角色 / 道具主人的名字不当画风名：卡里的角色，加上 人物·道具 包的第二级（草图卡没有 characters）
    names = [c["name"] for c in chars] + [c["lookup"] for c in chars if c.get("lookup")] + (pack[1:2] if image_kind else [])
    from_title = next((seg for seg in segments if not any(n in seg for n in names)), segments[-1])
    looks = {name: (((registry or {}).get("styles") or {}).get(name) or {}).get("look") for name in styles}
    out = {"want": "look", "look_as": image_kind["as"] if image_kind else "character", "look_name": look_name,
           "paint": next((n for n in styles if looks[n] == "画风"), from_title),
           "mood": next((n for n in styles if looks[n] != "画风"), from_title),
           "comp": title.split("·")[-1], "text": {}, "declared": False}
    try:
        declared = parse_promote(card.get("promote"), kind_map)
    except ValueError:
        declared = None                     # validate reports it; the page falls back to the derived values
    if declared:
        out["want"], out["declared"] = declared["want"], True
        if declared["want"] == "look":
            out["look_as"], out["look_name"] = declared["as"], declared["name"]
        elif declared["want"] in ("paint", "mood"):
            out[declared["want"]] = declared["name"]
        elif declared["want"] == "comp":
            out["comp"] = declared["name"]
        else:
            out["text"][declared["as"]] = declared["name"]
    return out


def inspiration_pack(item):
    explicit = pack_parts(item.get("pack"))
    if explicit:
        return explicit
    tags = item.get("tags") or []
    return ["风格", tags[0] if tags else "未分类"]


# 流水线自己写的字段：存盘时放到卡的末尾，和人写的内容分开；也不进指纹
SYSTEM_KEYS = ("status", "hold", "created", "slug", "feedback", "sources")


def _is_empty(value):
    return value is None or value is False or value == "" or value == [] or value == {}


def prune_empty(value):
    """Drop '', [], {}, None and false from a card (recursively): unset fields stay unwritten."""
    if isinstance(value, dict):
        out = {key: prune_empty(item) for key, item in value.items()}
        # an explicit `transparent: false` must survive: a portrait card defaults to transparent
        return {key: item for key, item in out.items() if not _is_empty(item) or (key == "transparent" and item is False)}
    if isinstance(value, list):
        out = [prune_empty(item) for item in value]
        return [item for item in out if not _is_empty(item)]
    return value


# 等于默认值的字段不写进卡（background 历史字段，已无代码读取）
CARD_DEFAULTS = {"prompt_mode": "lean", "candidates": {"default": 1}, "background": True}


def clean_card(card):
    """Card fields that actually say something: no private keys, no empties, no defaults."""
    payload = prune_empty({key: value for key, value in card.items() if not key.startswith("_")})
    return {key: value for key, value in payload.items() if CARD_DEFAULTS.get(key, object()) != value}


def save_card(card):
    """Write only what is set: the human part first, then the pipeline's own record under a divider."""
    path = card["_path"]
    payload = clean_card(card)
    mine = {key: value for key, value in payload.items() if key not in SYSTEM_KEYS}
    record = {key: payload[key] for key in SYSTEM_KEYS if key in payload}
    text = yaml.safe_dump(mine, allow_unicode=True, sort_keys=False)
    if record:
        text += "\n# ---- 以下由流水线记录（状态 / 反馈 / 来源），不用手改 ----\n"
        text += yaml.safe_dump(record, allow_unicode=True, sort_keys=False)
    with fileio.locked(path):
        fileio.atomic_write(path, text)


def update_card(cfg, card_id, change):
    """锁住卡 → 重新读 → change(card) → 写回。出图要几分钟，期间审核页可能往卡里写了反馈，不能拿开头读的旧卡整份盖回去。"""
    path = load_card(cfg, card_id)["_path"]
    with fileio.locked(path):
        card = load_card(cfg, card_id)
        change(card)
        save_card(card)
    return card


# --------------------------------------------------------------------------- #
# reference resolution
# --------------------------------------------------------------------------- #

def load_sources(cfg):
    if cfg.sources_file.is_file():
        return yaml.safe_load(cfg.sources_file.read_text(encoding="utf-8")) or {}
    return {}


def _resolve_path(cfg, value):
    if providers.is_url(value):
        return value
    path = Path(os.path.expanduser(str(value)))
    return path if path.is_absolute() else (cfg.root / path).resolve()


STYLE_ROLE = "lighting, color and atmosphere reference only - ignore its people, objects and location"
BASE_ROLE = "the image to edit - everything not listed under Change stays exactly as it is here"

# card kinds besides the plain scene card; their wording can be overridden by blocks/rerender.txt / blocks/portrait.txt
CARD_KINDS = ("card", "scene", "sketch", "rerender", "portrait")   # scene = card (older name, still accepted)
DEFAULT_RERENDER = ("Edit Image 1. Keep exactly as Image 1: {{keep}}\n"
                    "Change ONLY this: {{change}}\n"
                    "Anything not named under Change - faces, features, costume, pose, camera, framing, background, "
                    "light and painting style - stays identical to Image 1. Do not redraw it from scratch.")
DEFAULT_PORTRAIT = ("Composition: a single-figure full-body character sheet, {{view}}, the whole body from head to feet "
                    "inside the frame with a little margin, centred, nothing else in the picture.\n"
                    "Pose: {{pose}}")


def resolve_refs(cfg, card):
    """Collect this card's reference images in order. Returns (refs, missing)."""
    items, missing = collect_refs(cfg, card)
    return [ref for ref, _role in items], missing


def collect_refs(cfg, card):
    """Collect this card's reference images in order. Returns ([(ref, default_role)], missing).

    The default role comes from the registry entry's ``role`` (or a sensible
    default per kind); a card's own ``refs.roles`` still wins in compose.

    Sources, in order: registry lookup by character/prop name, explicit card
    ``refs.files``, refs confirmed by the human, then extra URLs. Anything that
    cannot be found is reported, never silently dropped.
    """
    registry = load_sources(cfg)
    chars_reg = (registry.get("characters") or {})
    refs, missing = [], []

    def add(value, label, role="reference"):
        if not value:
            return
        if not isinstance(value, str):     # e.g. a library id {asset: <id>} / {path: …}: not understood here
            missing.append({"label": label, "path": str(value),
                            "reason": f"unsupported source form {value!r} (this skill takes a file path or URL)"})
            return
        if providers.is_url(value):
            refs.append((value, role))
            return
        path = _resolve_path(cfg, value)
        if Path(path).is_file():
            refs.append((str(path), role))
        else:
            missing.append({"label": label, "path": str(path), "reason": "file-not-found"})

    base = card.get("base")
    if base:      # rerender: the image being edited is always Image 1; a candidate name works too
        if isinstance(base, str) and not providers.is_url(base) and not Path(_resolve_path(cfg, base)).is_file():
            found = sorted((cfg.output_dir / "candidates").glob(f"{Path(base).stem}.*"))
            base = str(found[0]) if len(found) == 1 else base
        add(base, "base", BASE_ROLE)
    # a rerender with a base sends that one image only: an extra identity image pulls the face away from it
    base_only = card.get("kind") == "rerender" and bool(refs)

    for character in [] if base_only else card.get("characters") or []:
        character = character or {}
        name = character.get("name") or ""
        lookup = character.get("lookup") or name
        if not lookup and not character.get("source"):
            continue        # the template's blank `- name: ''` row: no character, nothing missing
        entry = chars_reg.get(lookup) or {}
        source = character.get("source") or entry.get("source")
        if not source:
            missing.append({"label": f"character:{lookup}", "reason": "no source registered"})
            continue
        add(source, f"character:{lookup}", entry.get("role") or f"identity, costume and look of {name}")
        if card.get("prompt_mode", "lean") == "lean" and entry.get("face"):
            add(entry["face"], f"face:{lookup}", f"face close-up of {name}")

    # every other kind of sources.kinds (props, styles, and what the project adds), in declared order;
    # a rerender with a base keeps only the text kinds (a style sample does not pull the face away)
    kind_map = cfg.kinds
    for section, kind in kind_map.items():
        if section == "characters" or (base_only and not kind["text"]):
            continue
        reg = registry.get(section) or {}
        tag = kind.get("as") or section
        for name in sourcekinds.card_names(card, section):
            entry = reg.get(name) or {}
            if not entry.get("source"):
                missing.append({"label": f"{tag}:{name}", "reason": "no source registered"})
                continue
            add(entry["source"], f"{tag}:{name}", sourcekinds.role_for(kind_map, section, name, entry))

    refs_section = card.get("refs") or {}
    for value in refs_section.get("files") or []:
        add(value, "refs.files")
    for value in refs_section.get("confirmed") or []:
        add(value, "refs.confirmed")
    for value in refs_section.get("urls") or []:
        add(value, "refs.urls")

    seen, unique = set(), []
    for ref, role in refs:
        key = ref if providers.is_url(ref) else hashlib.sha256(Path(ref).read_bytes()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        unique.append((ref, role))
    return unique, missing


# --------------------------------------------------------------------------- #
# prompt composition
# --------------------------------------------------------------------------- #

def compose_prompt(cfg, card, blocks):
    parts = []
    kind = card.get("kind")
    bare = bool(card.get("skip_blocks"))      # a look test must not inherit the project's look
    style = blocks.get("style")
    if isinstance(style, str) and style and not bare and kind != "rerender":   # rerender: the look comes from Image 1
        parts.append(style)

    for template_id in card.get("prompt_templates") or []:
        for template in prompt_templates(cfg.prompt_zone):
            if template["id"] == template_id:
                parts.append(template["body"])
                break
        else:
            parts.append(f"[missing template: {template_id}]")

    # 画风 / 氛围真源 = 样张 + 一段描述（真源只分「图」和「字」）；描述跟着名字走，卡里只写 styles: [名]
    # 契约：sources.kinds 里 text: true 的种类都这样拼（默认只有 styles）
    for text in kind_texts(cfg, card).values():
        if text:
            parts.append(text)

    if card.get("aspect"):
        parts.append(f"Aspect ratio: {card['aspect']}")

    items, _missing = collect_refs(cfg, card)
    refs = [ref for ref, _role in items]
    if refs:
        roles = (card.get("refs") or {}).get("roles") or []
        lines = []
        for index, (_ref, default_role) in enumerate(items, start=1):
            given = roles[index - 1] if index - 1 < len(roles) else ""
            if index == 1 and kind == "rerender" and not given:
                default_role = BASE_ROLE
            lines.append(f"Image {index}: {given or default_role}")
        parts.append("Input images:\n" + "\n".join(lines))

    if kind == "rerender":
        edit = blocks.get("rerender") if isinstance(blocks.get("rerender"), str) and blocks.get("rerender") else DEFAULT_RERENDER
        parts.append(edit.replace("{{keep}}", str(card.get("keep") or "").strip())
                         .replace("{{change}}", str(card.get("change") or "").strip()))

    environment = _block(blocks, "environments", card.get("environment"))
    if environment:
        parts.append(f"Environment: {environment}")
    composition = _block(blocks, "compositions", card.get("composition"))
    if composition:
        parts.append(f"Composition: {composition}")

    if kind == "portrait":
        sheet = blocks.get("portrait") if isinstance(blocks.get("portrait"), str) and blocks.get("portrait") else DEFAULT_PORTRAIT
        parts.append(sheet.replace("{{view}}", str(card.get("view") or "front three-quarter view, face fully visible").strip())
                          .replace("{{pose}}", str(card.get("pose") or "standing naturally, weight even, arms relaxed").strip()))
        if card.get("costume"):
            parts.append("Costume: " + str(card["costume"]).strip())

    for character in card.get("characters") or []:
        if kind == "rerender":     # identity is Image 1; only an expression change is spelled out
            if character.get("expression"):
                parts.append(f"Expression of {character.get('name') or 'the subject'}: {character['expression']}. "
                             "Change the expression only; facial structure, eyes, nose, mouth and hairline stay exactly as Image 1.")
            continue
        bits = [character.get("name") or "character"]
        if character.get("position"):
            bits.append(f"positioned {character['position']}")
        if character.get("expression"):
            bits.append(f"expression {character['expression']}")
        if card.get("prompt_mode") == "long" and character.get("identity_note"):
            bits.append(character["identity_note"])
        parts.append("Subject: " + ", ".join(bits))

    if card.get("action"):
        parts.append("Action: " + str(card["action"]).strip())

    if card.get("transparent", kind == "portrait"):    # a character sheet is a cut-out unless the card says otherwise
        parts.append("Background: fully transparent, isolated subject, no backdrop.")

    for rule in card.get("hard_rules") or []:
        parts.append(str(rule).strip())

    tail = blocks.get("tail")
    if isinstance(tail, str) and tail and not bare:
        if not refs:    # "keep the face as in the reference images" with no image sent misleads the model
            tail = "\n".join(" ".join(sentence for sentence in re.split(r"(?<=[.!?])\s+", line)
                                       if not re.search(r"reference image|\bImage \d", sentence, re.I))
                             for line in tail.splitlines())
        parts.append(tail)

    if card.get("extra_prompt"):
        parts.append(str(card["extra_prompt"]).strip())

    text = "\n\n".join(part.strip() for part in parts if part and str(part).strip())
    return text, refs


# --------------------------------------------------------------------------- #
# manifest (four-state run record)
# --------------------------------------------------------------------------- #

def manifest_path(cfg):
    return cfg.output_dir / "manifest.json"


def _new_manifest():
    return {"format": "image-card-pipeline/manifest-v1", "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "jobs": {}, "approvals": {}}


def load_manifest(cfg):
    # 写入一律整份替换，读到的总是完整的一版
    return fileio.read_json(manifest_path(cfg), None) or _new_manifest()


def update_manifest(cfg, change):
    """几个 generate 并发时唯一的写法：锁住 → 重新读 → change(manifest) → 原子写回。返回 change 的返回值。"""
    _data, result = fileio.update_json(manifest_path(cfg), _new_manifest, change)
    return result


def set_job(cfg, key, job):
    update_manifest(cfg, lambda manifest: manifest["jobs"].__setitem__(key, job))


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError):
        return isinstance(pid, int)
    return True


def job_key(card_id, model, index):
    return f"{card_id}|{model}|{index}"


def kind_texts(cfg, card):
    """{key: description} of the card's text kinds (styles, and any declared ``text: true`` kind).

    Keys are the bare name for styles (so older fingerprints stay valid) and <section>/<name> otherwise."""
    out, registry = {}, None
    for section, kind in cfg.kinds.items():
        if not kind["text"]:
            continue
        for name in sourcekinds.card_names(card, section):
            if registry is None:
                registry = load_sources(cfg)
            entry = (registry.get(section) or {}).get(name) or {}
            text = str((entry.get("prompt") if isinstance(entry, dict) else "") or "").strip()
            out[name if section == "styles" else f"{section}/{name}"] = text
    return out


def card_fingerprint(cfg, card):
    """Hash of everything that decides the prompt: the card body plus every block.

    The pipeline's own record (SYSTEM_KEYS) is excluded because it changes as the card advances;
    ``pack`` is only where the card is filed and ``promote`` only what the review page pre-fills; empty or default fields count the same as absent ones.
    """
    payload = {key: value for key, value in clean_card(card).items() if key not in (*SYSTEM_KEYS, "pack", "promote")}
    blocks = load_blocks(cfg)
    # the resolved references count too: a promoted source changes what the card sends
    refs, _missing = resolve_refs(cfg, card)
    ref_keys = [ref if providers.is_url(ref) else hashlib.sha256(Path(ref).read_bytes()).hexdigest() for ref in refs]
    # 画风 / 氛围真源的描述也会拼进提示词：改了描述，旧提示词就算过期
    style_texts = kind_texts(cfg, card)
    basis = {"card": payload, "blocks": blocks, "refs": ref_keys}
    if any(style_texts.values()):           # 没有描述的卡指纹不变，老卡不会被误判过期
        basis["style_texts"] = style_texts
    blob = json.dumps(basis, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def prompt_meta_path(cfg, card_id):
    return cfg.output_dir / "prompt" / f"{card_id}.meta.json"


def _apply_vars(text, pairs):
    for pair in pairs or []:
        name, _, value = str(pair).partition("=")
        text = re.sub(r"\{\{\s*" + re.escape(name.strip()) + r"\s*\}\}", value.strip(), text)
    return text


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

# ---- 版本：项目文件夹用 git 存版本，不再生成 .bak 文件 -------------------------
# 写命令（命令行和审核页都一样）动手前先存一次（收住人或 AI 的手改），做完再存一次。
# 项目不是 git 仓库、或 rules.autosave: false 时直接跳过。找回用 `版本 历史 / 取 / 回退`。
WRITE_COMMANDS = {"init", "new-card", "compose", "generate", "forget", "review", "pick", "keep", "pack",
                  "unregister", "unreview", "note", "demote", "register", "feedback", "inspire", "sketch", "promote"}
_VERSION_LOCK = threading.Lock()
PROJECT_GITIGNORE = """# image-card-pipeline 项目：这些是派生物或重复件，不进版本
output/runs/*/result.*
output/runs/**/response.json
output/refs-sent/
output/sheets/
*.mp4
*.mov
*.webm
*.part
*.log
.*.lock
.*.tmp
.DS_Store
__pycache__/
"""


def _git(root, *argv):
    return subprocess.run(["git", "-C", str(root), *argv], capture_output=True, text=True)


def version_tracked(cfg):
    """项目目录自己就是一个 git 仓库的根才算（同 core/versioning.tracked）：放在别的仓库里面时，不往外层仓库提交。"""
    root = cfg.path.parent
    if not root.is_dir():
        return False
    top = _git(root, "rev-parse", "--show-toplevel")
    return top.returncode == 0 and Path(top.stdout.strip()).resolve() == root.resolve()


def save_version(cfg, message):
    """把项目文件夹（project.yaml 所在目录）现在的样子存成一个版本。有改动才存，返回是否存了。"""
    if (cfg.raw.get("rules") or {}).get("autosave", True) is False or not version_tracked(cfg):
        return False
    root = cfg.path.parent
    git_dir = _git(root, "rev-parse", "--absolute-git-dir").stdout.strip() or str(root / ".git")
    # 几个命令同时收尾时都会 git add / commit，跨进程排队，否则撞 index.lock 白白漏存
    with _VERSION_LOCK, fileio.locked(Path(git_dir) / "icp-version"):
        if _git(root, "add", "-A", "--", ".").returncode != 0:
            return False
        if _git(root, "diff", "--cached", "--quiet", "--", ".").returncode == 0:
            return False
        done = _git(root, "-c", "user.name=AI", "-c", "user.email=ai@local", "commit", "-q",
                    "-m", message[:200], "--", ".")
        return done.returncode == 0


def _command_line(args):
    argv = sys.argv[1:]
    if args.command in argv:
        argv = argv[argv.index(args.command):]
    return " ".join(argv)


_PLACEHOLDER = re.compile(r"\.\.\.|…|<[^>]*>|\bx{3,}\b|\bTODO\b|待填|占位", re.IGNORECASE)


def approval_problem(text):
    """批准必须是人的原话：空、过短、像占位符的一律不收。返回问题描述，没问题返回 None。"""
    text = (text or "").strip()
    if not text:
        return "missing"
    if _PLACEHOLDER.search(text):
        return f"looks like a placeholder, not what the user said: {text!r}"
    if len(re.findall(r"[0-9A-Za-z\u4e00-\u9fff]", text)) < 4:
        return f"too short to be the user's words: {text!r}"
    return None


def cmd_init(cfg, args):
    """Scaffold a project from the skill templates."""
    if (cfg.path.exists() and not args.force):
        raise SystemExit(f"{cfg.path} already exists; pass --force to overwrite the scaffold files")
    import shutil

    cfg.root.mkdir(parents=True, exist_ok=True)
    (cfg.cards_dir).mkdir(parents=True, exist_ok=True)
    (cfg.blocks_dir).mkdir(parents=True, exist_ok=True)
    (cfg.prompt_zone).mkdir(parents=True, exist_ok=True)
    (cfg.output_dir).mkdir(parents=True, exist_ok=True)

    wrote = []

    def copy(src, dst):
        if dst.exists() and not args.force:
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        wrote.append(str(dst))

    copy(SKILL_ROOT / "templates" / "blocks" / "style.txt", cfg.blocks_dir / "style.txt")
    copy(SKILL_ROOT / "templates" / "blocks" / "tail.txt", cfg.blocks_dir / "tail.txt")
    copy(SKILL_ROOT / "templates" / "blocks" / "compositions.example.yaml", cfg.blocks_dir / "compositions.yaml")
    copy(SKILL_ROOT / "templates" / "blocks" / "environments.example.yaml", cfg.blocks_dir / "environments.yaml")
    copy(SKILL_ROOT / "templates" / "sources.example.yaml", cfg.sources_file)
    for example in (SKILL_ROOT / "templates" / "prompt-zone").rglob("*.md"):
        rel = example.relative_to(SKILL_ROOT / "templates" / "prompt-zone")
        copy(example, cfg.prompt_zone / rel)
    if not cfg.path.exists():
        body = (SKILL_ROOT / "project.example.yaml").read_text(encoding="utf-8")
        cfg.path.write_text(body, encoding="utf-8")
        wrote.append(str(cfg.path))

    ignore = cfg.path.parent / ".gitignore"
    if not ignore.exists():
        ignore.write_text(PROJECT_GITIGNORE, encoding="utf-8")
        wrote.append(str(ignore))
    if not version_tracked(cfg):
        if _git(cfg.path.parent, "init", "-q").returncode == 0:
            wrote.append(f"{cfg.path.parent}/.git  (version history: 版本 历史 / 取 / 回退)")
        else:
            print("note: git not available -- this project has no version history")

    for path in wrote:
        print(f"created {path}")
    print()
    print("next steps:")
    print(f"  1. edit {cfg.path}: point providers at your endpoint, keep secrets in env vars")
    print(f"  2. put identity images under {cfg.root} and list them in {cfg.sources_file}")
    print(f"  3. python3 {Path(__file__).name} --config {cfg.path} doctor")
    print(f"  4. python3 {Path(__file__).name} --config {cfg.path} new-card 01-01 'first shot'")


def cmd_new_card(cfg, args):
    depth = cfg.cards_dir
    depth.mkdir(parents=True, exist_ok=True)
    kind = getattr(args, "kind", None) or "card"
    template = (CARD_TEMPLATE if kind == "card" else CARD_TEMPLATE.with_name(f"card.{kind}.yaml")).read_text(encoding="utf-8")
    stamp = time.strftime("%Y%m%d")
    slug = re.sub(r"[^a-z0-9]+", "-", (args.title or "").lower()).strip("-") or "card"
    path = depth / f"{args.card_id}_{slug}.yaml"
    if path.exists():
        raise SystemExit(f"{path} already exists")
    body = template.replace("__ID__", str(args.card_id)).replace("__TITLE__", args.title or "")
    body = body.replace("__SLUG__", slug).replace("__DATE__", stamp)
    path.write_text(body, encoding="utf-8")
    print(f"created {path}")
    print("next: fill it in, then `validate`")


def cmd_cards(cfg, args):
    for card in all_cards(cfg):
        hold = f"  HOLD: {card['hold']}" if card.get("hold") else ""
        print(f"{card.get('id'):<8} {card.get('status', 'draft'):<10} {card.get('title', '')}{hold}")


def cmd_show(cfg, args):
    card = load_card(cfg, args.card)
    print(yaml.safe_dump({k: v for k, v in card.items() if not k.startswith("_")}, allow_unicode=True, sort_keys=False))


def card_problems(cfg, card):
    """(problems, notes) for a card. Problems block compose; notes are informational."""
    problems, notes = [], []

    if card.get("hold"):
        problems.append(f"card is on hold: {card['hold']}")
    for field in ("id", "title"):
        if not card.get(field):
            problems.append(f"missing field: {field}")
    kind = card.get("kind") or "card"
    if kind not in CARD_KINDS:
        problems.append(f"unknown kind '{kind}' (use one of: {', '.join(CARD_KINDS)})")
    elif kind == "rerender":
        for field in ("keep", "change"):
            if not str(card.get(field) or "").strip():
                problems.append(f"rerender card needs `{field}`")
    elif kind == "portrait":
        if len([c for c in card.get("characters") or [] if (c or {}).get("name")]) != 1:
            problems.append("portrait card takes exactly one character")
    elif not card.get("action") and not card.get("extra_prompt"):
        problems.append("card has no action and no extra_prompt -- nothing to draw")
    try:
        parse_promote(card.get("promote"), cfg.kinds)
    except ValueError as error:
        problems.append(str(error))
    if card.get("aspect") and not re.fullmatch(r"\d+x\d+", str(card["aspect"])):
        problems.append(f"aspect must be WxH, got {card['aspect']!r}")

    _refs, missing = resolve_refs(cfg, card)
    if kind == "rerender" and not _refs:
        problems.append("rerender card has no image to edit: set `base:` (a path or candidate name) or a character source")
    for item in missing:    # a named source / file that cannot be found would just be left out of a paid request
        problems.append(f"reference not found: {item.get('label')} ({item.get('reason')})"
                        + (" -- register it (promote) or take it off the card" if item.get("reason") == "no source registered" else ""))
    if not (card.get("characters") or card.get("props") or (card.get("refs") or {}).get("files")):
        notes.append("no characters, props or explicit refs -- pure text-to-image")

    for template_id in card.get("prompt_templates") or []:
        known = {t["id"] for t in prompt_templates(cfg.prompt_zone)}
        if template_id not in known:
            problems.append(f"prompt template not found: {template_id}")

    for name in sorted(set(re.findall(r"\{\{\s*([^}]+?)\s*\}\}", compose_prompt(cfg, card, load_blocks(cfg))[0]))):
        notes.append(f"prompt still has an unfilled variable {{{{{name}}}}} -- fill it via `compose --var {name}=...`")
    return problems, notes


def cmd_validate(cfg, args):
    card = load_card(cfg, args.card)
    problems, notes = card_problems(cfg, card)
    for line in notes:
        print(f"note:    {line}")
    for line in problems:
        print(f"PROBLEM: {line}")
    if problems:
        raise SystemExit(f"{len(problems)} problem(s); fix them before composing")
    update_card(cfg, card["id"], lambda fresh: fresh.__setitem__(
        "status", "confirmed" if fresh.get("status") in (None, "draft") else fresh.get("status")))
    print(f"OK {card['id']}: {len(notes)} note(s), 0 problems")


def cmd_compose(cfg, args):
    card = load_card(cfg, args.card)
    if card.get("hold"):
        raise SystemExit(f"card is on hold: {card['hold']}")
    problems, _notes = card_problems(cfg, card)
    if problems and not args.force:
        for line in problems:
            print(f"PROBLEM: {line}")
        raise SystemExit(f"{len(problems)} problem(s); fix the card (see `validate`), or pass --force")
    text, refs = compose_prompt(cfg, card, load_blocks(cfg))
    text = _apply_vars(text, args.var)
    remaining = sorted(set(re.findall(r"\{\{\s*([^}]+?)\s*\}\}", text)))
    directory = cfg.output_dir / "prompt"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{card['id']}.txt"
    fileio.atomic_write(path, text)
    fileio.atomic_write(prompt_meta_path(cfg, card["id"]), json.dumps({
        "fingerprint": card_fingerprint(cfg, card), "vars": dict(
            (str(pair).partition("=")[0].strip(), str(pair).partition("=")[2].strip()) for pair in args.var or []),
        "chars": len(text), "refs": [str(r) for r in refs], "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, ensure_ascii=False, indent=2))

    rules = cfg.rules.get("max_prompt_chars") or {}
    warn, reject = int(rules.get("warn", 2400)), int(rules.get("reject", 3200))
    size = len(text)
    verdict = "ok"
    if reject and size > reject:
        verdict = "reject"
    elif warn and size > warn:
        verdict = "warn"
    print(f"prompt -> {path}")
    print(f"chars: {size}  ({verdict}; warn>{warn}, reject>{reject})")
    print(f"refs: {len(refs)}" + (f" -> {', '.join(Path(r).name if not providers.is_url(r) else r for r in refs)}" if refs else ""))
    if remaining:
        print(f"unfilled variables: {', '.join(remaining)}  (pass --var name=value)")
    if verdict == "reject" and not args.force:
        raise SystemExit(f"prompt is {size} chars (> {reject}); shorten the card, or pass --force")
    update_card(cfg, card["id"], lambda fresh: fresh.__setitem__("status", "composed"))


def cmd_generate(cfg, args):
    card = load_card(cfg, args.card)
    if card.get("hold"):
        raise SystemExit(f"card is on hold: {card['hold']}")
    # 花钱前先过 validate：缺真源、缺文件的卡出了也是白出（--force 不放行这一条）
    problems, _notes = card_problems(cfg, card)
    if problems:
        for line in problems:
            print(f"PROBLEM: {line}")
        raise SystemExit(f"{len(problems)} problem(s); fix the card (see `validate {card['id']}`) before paying for it")

    alias = args.model or card.get("model") or cfg.default_model()
    if not alias:
        raise SystemExit("no model: pass --model or set models.default in the config")
    spec = cfg.model_spec(alias)

    budget = int(args.budget or (card.get("candidates") or {}).get("default") or 1)
    require_approval = cfg.rules.get("require_approval", True)
    if require_approval and approval_problem(args.approved):
        raise SystemExit("paid generation needs the user's approval, copied word for word: "
                         f'--approved "用户 10-23：01-01 出 2 张" ({approval_problem(args.approved)})')

    text, refs = compose_prompt(cfg, card, load_blocks(cfg))
    prompt_path = cfg.output_dir / "prompt" / f"{card['id']}.txt"
    meta_path = prompt_meta_path(cfg, card["id"])
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    current = card_fingerprint(cfg, card)
    if meta.get("fingerprint") == current and prompt_path.is_file():
        # the composed prompt is still current: send exactly those bytes (variables included)
        text = prompt_path.read_text(encoding="utf-8")
    elif not prompt_path.is_file():
        # nothing composed yet: a first compose cannot be stale, so just do it
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        fileio.atomic_write(prompt_path, text)
        fileio.atomic_write(meta_path, json.dumps({"fingerprint": current, "vars": {}, "chars": len(text),
                                         "at": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False, indent=2))
        print(f"no prompt on disk yet -- composed it ({len(text)} chars) and recorded its fingerprint")
    elif not args.refresh:
        raise SystemExit(f"prompt on disk is stale (card or blocks changed). re-run `compose {card['id']}`, or pass --refresh")
    else:
        text = _apply_vars(text, [f"{k}={v}" for k, v in (meta.get("vars") or {}).items()])
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        fileio.atomic_write(prompt_path, text)
        fileio.atomic_write(meta_path, json.dumps({"fingerprint": current, "vars": meta.get("vars") or {}, "chars": len(text),
                                         "at": time.strftime("%Y-%m-%dT%H:%M:%S")}, ensure_ascii=False, indent=2))

    rules = cfg.rules.get("max_prompt_chars") or {}
    reject = int(rules.get("reject", 3200))
    if reject and len(text) > reject and not args.force:
        raise SystemExit(f"prompt is {len(text)} chars (> {reject}); pass --force to send it anyway")

    ready, reason = providers.provider_ready(spec)
    if not ready:
        raise SystemExit(f"provider '{spec['name']}' not ready: {reason}")

    sent_refs = _prepare_refs(cfg, spec, refs)    # 有一张发不出去就整批不发，绝不悄悄少带图

    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    if require_approval:
        update_manifest(cfg, lambda manifest: manifest["approvals"].__setitem__(
            f"{card['id']}|{alias}", {"approved": args.approved, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}))

    def claim(manifest, key):
        """锁内认领一个序号：返回它原来的状态；可以出就当场记成 running（带进程号），别的进程看到就会跳过。"""
        job = manifest["jobs"].get(key) or {}
        state = job.get("status")
        if state == "running" and job.get("pid") != os.getpid() and _pid_alive(job.get("pid")):
            return "busy"
        if state == "running":
            # 认领它的进程已经没了：请求可能已经发出去、扣了费，只是没活到记结果。记 unknown，不重发
            manifest["jobs"][key] = {**job, "status": "unknown", "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                     "error": f"left 'running' by process {job.get('pid')}, which is gone -- "
                                              "the request may have gone out"}
            return "orphaned"
        if state in ("ok", "unknown"):
            return state
        manifest["jobs"][key] = {"status": "running", "pid": os.getpid(), "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
        return state or "new"

    produced, skipped, stale_done, busy, unknown = [], [], [], [], []
    for index in range(1, max(1, budget) + 1):
        key = job_key(card["id"], alias, index)
        state = update_manifest(cfg, lambda manifest: claim(manifest, key))
        if state == "busy":
            busy.append(key)             # 另一个进程正在出这一张，不重复花钱
            print(f"{key}: being generated by another process -- skipped")
            continue
        if state == "ok":
            skipped.append(key)          # budget is a total, so a re-run never doubles the spend
            old_req = cfg.output_dir / "runs" / f"{card['id']}-{alias}-{index}" / "request.json"
            try:
                if json.loads(old_req.read_text(encoding="utf-8")).get("prompt") != text:
                    stale_done.append(key)
            except (OSError, ValueError):
                pass
            continue
        if state in ("unknown", "orphaned"):
            run_hint = cfg.output_dir / "runs" / f"{card['id']}-{alias}-{index}"
            why = "was left 'running' by a process that is gone" if state == "orphaned" else "state 'unknown'"
            print(f"{key}: {why} -- a paid run may already exist. Inspect {run_hint} (response.json / error.txt) "
                  f"and the provider's console; this command will not re-send it. "
                  f"Only after checking: `forget {card['id']} {index} --model {alias} --why \"…\"`")
            unknown.append(key)
            continue
        run_dir = cfg.output_dir / "runs" / f"{card['id']}-{alias}-{index}"
        run_dir.mkdir(parents=True, exist_ok=True)
        for stale in ("response.json", "error.txt"):     # 上一次失败（failed）留下的，别和这次的混在一起
            (run_dir / stale).unlink(missing_ok=True)
        request_record = {
            "card": card["id"], "model_alias": alias, "provider": spec["name"], "upstream_model": spec["model"],
            "size": args.size or card.get("aspect"), "refs": [str(r) for r in sent_refs], "prompt": text,
        }
        (run_dir / "request.json").write_text(json.dumps(request_record, ensure_ascii=False, indent=2), encoding="utf-8")

        started = time.time()
        try:
            # 拿到响应先把原文落进 run 目录，再去解析 / 下载：后面哪一步断了，人都还能对账
            raw, message = providers.generate(
                spec, text, refs=sent_refs, size=args.size or card.get("aspect"),
                quality=args.quality, background="transparent" if card.get("transparent") else None,
                record=lambda body, run_dir=run_dir: (run_dir / "response.json").write_bytes(body),
            )
        except Exception as error:  # noqa: BLE001 - record the failure state, never guess
            # 只有「确定没跑」（没连上、本地就错了、4xx 拒收）才是 failed，下次会重发；其余一律 unknown，不重发
            state = "failed" if isinstance(error, providers.NotRun) else "unknown"
            set_job(cfg, key, {"status": state, "error": f"{type(error).__name__}: {error}",
                               "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
            (run_dir / "error.txt").write_text(f"{type(error).__name__}: {error}\n", encoding="utf-8")
            print(f"{key}: {state} -- {error}")
            if state == "unknown":
                unknown.append(key)
                print(f"'unknown' = the request went out and may have been billed; it will NOT be re-sent. "
                      f"Check {run_dir} and the provider's console, then if nothing was made: "
                      f"`forget {card['id']} {index} --model {alias} --why \"…\"`")
            continue

        ext, _mime, width, height, has_alpha = sniff_image(raw)
        (run_dir / f"result.{ext}").write_bytes(raw)
        candidates = cfg.output_dir / "candidates"
        candidates.mkdir(parents=True, exist_ok=True)
        candidate = candidates / f"{card['id']}_{alias}_{index}.{ext}"
        fileio.atomic_write(candidate, raw)      # 审核页随时在读候选图，不能看到半张
        (run_dir / "run.json").write_text(json.dumps(
            {"message": message, "seconds": round(time.time() - started, 2), "width": width, "height": height,
             "has_alpha": has_alpha}, ensure_ascii=False, indent=2), encoding="utf-8")
        set_job(cfg, key, {"status": "ok", "candidate": str(candidate.relative_to(cfg.output_dir)),
                           "message": message, "seconds": round(time.time() - started, 2),
                           "width": width, "height": height, "has_alpha": has_alpha,
                           "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        produced.append(candidate)
        print(f"{key}: ok -> {candidate}  {width}x{height} alpha={has_alpha}  ({message})")

    if produced:
        update_card(cfg, card["id"], lambda fresh: fresh.__setitem__("status", "generated"))
    print(f"done: {len(produced)} new, {len(skipped)} already ok (budget {budget} = total for this card+model)"
          + (f", {len(busy)} in progress elsewhere" if busy else "")
          + (f", {len(unknown)} unknown (not re-sent)" if unknown else ""))
    if skipped:
        print("to add more, raise --budget (it is a total, not an increment)")
    if stale_done and not produced:
        print(f"note: {len(stale_done)} of those were made from an OLDER prompt (card/blocks/sources changed since); "
              f"nothing was made from the current one. --budget {budget + len(stale_done)} would add that many new ones")


def cmd_forget(cfg, args):
    """A human checked an 'unknown' job (run dir, provider console) and nothing usable was made: drop it so the
    next generate may send that number again. The record moves to manifest ``forgotten`` with the reason;
    the run dir moves to output/runs/_forgotten/ so the next attempt does not write over the evidence."""
    card = load_card(cfg, args.card)
    if "|" in args.job:
        key = args.job
    else:
        alias = args.model or card.get("model") or cfg.default_model()
        if not str(args.job).isdigit() or not alias:
            raise SystemExit("give the job as its number (with --model if it is not the card's model) or as <card>|<model>|<n>")
        key = job_key(card["id"], alias, int(args.job))
    if key.split("|")[0] != str(card["id"]):
        raise SystemExit(f"{key} does not belong to card {card['id']}")
    why = (args.why or "").strip()
    if len(re.findall(r"[0-9A-Za-z一-鿿]", why)) < 4:
        raise SystemExit('say what you checked (--why "…"): forgetting lets the next generate send -- and pay for -- it again')
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")

    def drop(manifest):
        job = manifest["jobs"].get(key)
        if not job:
            raise SystemExit(f"no job {key} (see output/manifest.json)")
        if job.get("status") != "unknown":
            raise SystemExit(f"{key} is {job.get('status')!r}; only 'unknown' jobs need forgetting "
                             "('failed' ones are sent again on their own)")
        manifest.setdefault("forgotten", []).append({**job, "key": key, "forgotten_at": stamp, "why": why})
        del manifest["jobs"][key]
        return job

    update_manifest(cfg, drop)
    run_dir = cfg.output_dir / "runs" / key.replace("|", "-")
    if run_dir.is_dir():
        kept = cfg.output_dir / "runs" / "_forgotten" / f"{run_dir.name}-{time.strftime('%Y%m%d-%H%M%S')}"
        kept.parent.mkdir(parents=True, exist_ok=True)
        run_dir.rename(kept)
        print(f"run dir kept as {kept}")
    print(f"forgot {key} (kept in manifest.forgotten: {why}). The next generate may send it again.")


def _prepare_refs(cfg, spec, refs):
    """Every reference reaches the model, or nothing is sent. Returns the list to send (same order -- the
    prompt's "Image N" lines follow it); raises SystemExit naming the image that cannot go.

    Inline first; over ``max_inline_bytes`` the big local images are shrunk (output/refs-sent/, JPEG unless
    they carry alpha); still over, or over the count, they are published through ``image_host`` -- and one
    failed upload stops the whole batch. ``max_refs`` (chat_completions) is a hard cap, never a silent cut.
    """
    if not refs:
        return []
    kind = spec.get("kind", "mock")
    name = spec.get("name") or kind
    if kind == "chat_completions" and len(refs) > int(spec.get("max_refs", 4)):
        cap = int(spec.get("max_refs", 4))
        raise SystemExit(f"{len(refs)} references, but {name} takes at most {cap} (max_refs); these would not be sent: "
                         + ", ".join(_ref_label(r) for r in refs[cap:])
                         + " -- take some off the card, or raise max_refs if the model really takes more")
    max_inline = int(spec.get("max_inline_refs", 8))
    max_bytes = int(spec.get("max_inline_bytes", 12 * 1024 * 1024))
    sent = list(refs)
    local = [r for r in sent if not providers.is_url(r)]
    if local and sum(Path(r).stat().st_size for r in local) > max_bytes:
        share = max_bytes // len(local)
        sent = [r if providers.is_url(r) or Path(r).stat().st_size <= share else _shrink_ref(cfg, r, share) for r in sent]
        local = [r for r in sent if not providers.is_url(r)]
    total = sum(Path(r).stat().st_size for r in local)
    mixed = kind == "openai_images" and local and len(local) < len(sent)    # 这个接口一次只能全是文件或全是 URL
    if total <= max_bytes and (kind == "chat_completions" or len(local) <= max_inline) and not mixed:
        return sent
    why = (f"{total / 1e6:.1f}MB of inline references (max_inline_bytes {max_bytes / 1e6:.1f}MB, even after shrinking)"
           if total > max_bytes else f"{len(local)} inline references (max_inline_refs {max_inline})"
           if len(local) > max_inline and kind != "chat_completions" else "URL and local references mixed")
    if (cfg.host or {}).get("kind", "none") == "none":
        biggest = sorted(local, key=lambda r: -Path(r).stat().st_size)[:5]
        raise SystemExit(f"references cannot all be sent: {why}, and no image_host is configured to publish them. "
                         "Biggest: " + ", ".join(f"{_ref_label(r)} ({Path(r).stat().st_size / 1e6:.1f}MB)" for r in biggest)
                         + " -- use fewer / smaller references, or configure image_host")
    published, failures = hosts.upload_many(cfg.host, local)
    if failures or not all(published):
        bad = failures or [{"path": r, "error": "no URL returned"} for r, u in zip(local, published) if not u]
        raise SystemExit("references cannot all be sent -- publishing failed for: "
                         + "; ".join(f"{_ref_label(f['path'])} ({f['error']})" for f in bad) + ". Nothing was sent.")
    urls = dict(zip(local, published))
    print(f"refs: {why} -- published {len(local)} through image_host ({cfg.host.get('kind')})")
    return [urls.get(r, r) for r in sent]


def _ref_label(ref):
    return ref if providers.is_url(ref) else Path(ref).name


def _shrink_ref(cfg, ref, limit):
    """A smaller copy of a local reference (under ``limit`` bytes if it can be done), kept in output/refs-sent/.
    No Pillow, or it will not go small enough: the original comes back and the caller decides."""
    try:
        from PIL import Image
    except ImportError:
        return ref
    raw = Path(ref).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()[:8]
    try:
        image = Image.open(ref)
        image.load()
    except Exception:  # noqa: BLE001 - not an image Pillow reads: leave it to the size check
        return ref
    alpha = image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info)
    image = image.convert("RGBA" if alpha else "RGB")
    out_dir = cfg.output_dir / "refs-sent"
    side = max(image.size)
    for target in (2048, 1536, 1024, 768, 512):
        if target > side and target != 2048:
            continue
        copy = image.copy()
        copy.thumbnail((target, target))
        out = out_dir / f"{digest}_{Path(ref).stem}_{max(copy.size)}.{'png' if alpha else 'jpg'}"
        out_dir.mkdir(parents=True, exist_ok=True)
        if alpha:
            copy.save(out, optimize=True)
        else:
            copy.save(out, quality=88)
        if out.stat().st_size <= limit:
            print(f"refs: {Path(ref).name} {len(raw) / 1e6:.1f}MB -> {out.name} {out.stat().st_size / 1e6:.1f}MB (to fit max_inline_bytes)")
            return str(out)
        out.unlink()
    return ref


def cmd_sheet(cfg, args):
    card = load_card(cfg, args.card)
    candidates = sorted((cfg.output_dir / "candidates").glob(f"{card['id']}_*"))
    if not candidates:
        raise SystemExit(f"no candidates for {card['id']}; run generate first")
    refs, _missing = resolve_refs(cfg, card)
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("Pillow not installed: contact sheet skipped (candidates are still on disk)")
        for candidate in candidates:
            print(f"  {candidate}")
        return

    cell, pad = 512, 12
    tiles = []
    for path in candidates:
        image = Image.open(path).convert("RGBA")
        image.thumbnail((cell, cell))
        tiles.append((path.name, image))
    ref_tiles = []
    for ref in refs[:8]:
        if providers.is_url(ref):
            continue
        try:
            image = Image.open(ref).convert("RGBA")
        except Exception:  # noqa: BLE001 - a broken ref must not kill the sheet
            continue
        image.thumbnail((cell // 2, cell // 2))
        ref_tiles.append((Path(ref).name, image))

    columns = max(1, min(len(tiles), 3))
    rows = (len(tiles) + columns - 1) // columns
    strip_h = (cell // 2 + pad) if ref_tiles else 0
    width = columns * (cell + pad) + pad
    height = rows * (cell + pad) + pad + strip_h
    sheet = Image.new("RGB", (width, height), (19, 23, 34))
    draw = ImageDraw.Draw(sheet)
    for index, (name, image) in enumerate(tiles):
        x = pad + (index % columns) * (cell + pad)
        y = pad + (index // columns) * (cell + pad)
        sheet.paste(image, (x, y), image)
        draw.text((x + 4, y + 4), name, fill=(255, 255, 255))
    for index, (name, image) in enumerate(ref_tiles):
        x = pad + index * (cell // 2 + pad)
        y = pad + rows * (cell + pad)
        sheet.paste(image, (x, y), image)
        draw.text((x + 2, y + 2), "ref: " + name, fill=(180, 200, 255))
    directory = cfg.output_dir / "sheets"
    directory.mkdir(parents=True, exist_ok=True)
    out = directory / f"{card['id']}_preview.jpg"
    sheet.save(out, quality=88)
    print(f"sheet -> {out}  ({len(tiles)} candidates, {len(ref_tiles)} refs)")


def _review_path(cfg, card_id):
    return cfg.output_dir / "review" / f"{card_id}.json"


def update_review(cfg, card_id, change):
    """审核记录的唯一写法：锁住 → 重新读 → change(review) → 原子写回。返回改完的记录。"""
    review, _ = fileio.update_json(_review_path(cfg, card_id),
                                   lambda: {"card": card_id, "candidates": {}, "pick": []}, change)
    return review


def candidate_name(cfg, name):
    """The candidate's real file name, so `01-01_sun_1` and `01-01_sun_1.png` land on the same review key
    (the review page looks results up by file name)."""
    name = Path(str(name)).name
    directory = cfg.output_dir / "candidates"
    if (directory / name).is_file():
        return name
    matches = sorted(p.name for p in directory.glob(f"{name}.*") if p.is_file())
    if len(matches) == 1:
        return matches[0]
    raise SystemExit(f"candidate not found: {name}" + (f" (ambiguous: {', '.join(matches)})" if matches else ""))


DEFAULT_REVIEWERS = ["claude", "human"]   # 看图结论只认视觉主力和人（分工表，SKILL.md）


def cmd_review(cfg, args):
    by = (getattr(args, "by", "") or "claude").strip().lower()
    allowed = [str(x).lower() for x in ((cfg.raw.get("rules") or {}).get("reviewers") or DEFAULT_REVIEWERS)]
    if by not in allowed:
        raise SystemExit(f"review --by {by}: 看图结论只能由 {' / '.join(allowed)} 给（rules.reviewers）。"
                         "文字类杂活可以交给别的模型，看图判断不行。")
    card = load_card(cfg, args.card)
    args.candidate = candidate_name(cfg, args.candidate)
    fields = {"verdict": args.verdict, "by": by, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if (args.note or "").strip():   # 不写备注就不动原来的备注；清空备注只走 `note`（留旧版）
        fields["note"] = args.note.strip()
    update_review(cfg, card["id"], lambda review: _set_note_kept(review["candidates"].setdefault(args.candidate, {}), fields))
    print(f"reviewed {args.candidate}: {args.verdict}" + (f" ({args.note})" if args.note else ""))


def cmd_pick(cfg, args):
    card = load_card(cfg, args.card)
    # 挑出和结论（通过/待定/不通过）互相独立；--remove 取消挑出
    names = [candidate_name(cfg, name) for name in args.candidates]
    stems = {Path(name).stem for name in names}
    args.candidates = names

    def change(review):
        # 保持加入顺序：契约 §3 的 image:<卡> 取 pick 的第一张，先挑的那张不能被字母序挤掉
        if getattr(args, "remove", False):
            review["pick"] = [name for name in review.get("pick") or [] if Path(name).stem not in stems]
            return
        picks = list(review.get("pick") or [])
        for name in names:
            same = [n for n, old in enumerate(picks) if Path(old).stem == Path(name).stem]
            if same:                         # 已经挑过（含不带扩展名的旧写法）：原位换成正式文件名，不挪位置
                picks[same[0]] = name
                for n in reversed(same[1:]):
                    del picks[n]
            else:
                picks.append(name)
        review["pick"] = picks

    review = update_review(cfg, card["id"], change)
    if review["pick"]:
        update_card(cfg, card["id"], lambda fresh: fresh.__setitem__("status", "reviewed"))
    print(f"pick: {', '.join(review['pick']) or '(none)'}")
    print("next: `register` to hand these to the library, or show the human the sheet")


def cmd_register(cfg, args):
    if not library.enabled(cfg.library):
        raise SystemExit("no library configured (set library.kind: local or http in the project config)")
    card = load_card(cfg, args.card)
    path = _review_path(cfg, card["id"])
    review = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    picks = args.candidates or review.get("pick") or []
    if not picks:
        raise SystemExit("nothing picked: run `pick` first or pass candidate names")

    root = Path(cfg.library["root"]).expanduser() if cfg.library.get("root") else None
    candidates_dir = cfg.output_dir / "candidates"
    files = []
    manifest = load_manifest(cfg)
    sent_prompts = {}                      # candidate file name -> the prompt actually sent for it
    for key, job in manifest["jobs"].items():
        if job.get("candidate"):
            request = cfg.output_dir / "runs" / key.replace("|", "-") / "request.json"
            try:
                sent_prompts[Path(job["candidate"]).name] = json.loads(request.read_text(encoding="utf-8")).get("prompt", "")
            except (OSError, ValueError):
                pass
    for name in picks:
        candidate = candidates_dir / name
        if not candidate.is_file():
            matches = sorted(candidates_dir.glob(f"{name}*"))
            if not matches:
                raise SystemExit(f"candidate not found: {name}")
            candidate = matches[0]
        if root and str(candidate).startswith(str(root)):
            files.append(str(candidate.relative_to(root)))
        else:
            files.append(str(candidate))

    prompt_file = cfg.output_dir / "prompt" / f"{card['id']}.txt"
    fallback = prompt_file.read_text(encoding="utf-8") if prompt_file.is_file() else ""
    # one ingest per image so each entry keeps the prompt that actually produced it (not the card's current one)
    reply = {"ok": True, "added": [], "already": []}
    for item in files:
        part = library.ingest(
            cfg.library, title=f"{card['id']} {card.get('title', '')}", files=[item],
            prompt=sent_prompts.get(Path(item).name) or fallback,
            model=card.get("model") or cfg.default_model(), batch=card.get("group") or card["id"],
        )
        for field in ("added", "already"):
            reply[field] += part.get(field) or []
        reply.update({k: v for k, v in part.items() if k not in ("added", "already")})
    update_card(cfg, card["id"], lambda fresh: fresh.__setitem__("status", "registered"))
    print(json.dumps(reply, ensure_ascii=False, indent=2))
    print("registered as drafts: only the human finalises in the library")


def cmd_unregister(cfg, args):
    """Undo a registration: the image leaves the library and goes back to where it came from
    (trial / review zone), or with --discard straight to the rejects (verdict fail; the reason is optional)."""
    if cfg.library.get("kind") != "local":
        raise SystemExit("unregister works on a local library only")
    card = load_card(cfg, args.card)
    name = Path(args.candidate).stem   # `01-01_sun_1` and `01-01_sun_1.png` both work, like review/pick
    entries = [e for e in library.local_load(cfg.library)["entries"]
               if Path(e["file"]).stem == name and str(e.get("title", "")).split(" ", 1)[0] == card["id"]]
    if not entries:
        raise SystemExit(f"{name} is not in the library")
    final = [e["id"] for e in entries if e.get("status") == "final"]
    if final:   # 定稿只归人：命令行不替人撤回，先在审核页把定稿撤回（点「草稿 / 待定」）再移
        raise SystemExit(f"{name} is finalised in the library ({', '.join(final)}): only the human takes that back -- "
                         "on the review page press 「待定」 to un-finalise it first, then move it")
    for entry in entries:
        library.local_remove(cfg.library, entry["id"])
    if args.discard:
        cmd_review(cfg, argparse.Namespace(card=card["id"], candidate=name, verdict="fail", note=args.note,
                                            by="human"))   # 从素材库放弃是人的决定
        cmd_pick(cfg, argparse.Namespace(card=card["id"], candidates=[name], remove=True))   # a later bare `register` must not bring it back
        if args.note:   # 不通过 / 丢弃可以不写原因
            cmd_feedback(cfg, argparse.Namespace(card=card["id"], verdict="revise", reason=f"{name}: {args.note}",
                                                 root_cause="", by="human"))
        print(f"{name}: out of the library -> rejects")
    else:
        cmd_pick(cfg, argparse.Namespace(card=card["id"], candidates=[name], remove=True))
        _clear_verdict(cfg, card["id"], name)   # 回到待定（未审），不留「旧·通过」
        print(f"{name}: out of the library -> back where it came from")
    def back(fresh):
        if fresh.get("status") == "registered":
            fresh["status"] = "reviewed"

    update_card(cfg, card["id"], back)


def _clear_verdict(cfg, card_id, candidate):
    """Drop a candidate's verdict so it is 未审 again (the note stays); returns whether there was one."""
    stem = Path(candidate).stem
    cleared = []

    def change(review):
        for key in [k for k in review["candidates"] if Path(k).stem == stem]:   # with or without extension
            entry = review["candidates"][key]
            cleared.append(entry.get("verdict"))
            for field in ("verdict", "by", "at"):
                entry.pop(field, None)
            if not entry.get("note") and not entry.get("note_history"):
                del review["candidates"][key]

    update_review(cfg, card_id, change)
    return any(cleared)


NOTE_HISTORY = 10   # 每张图留最近这么多版旧备注，手滑删了能找回


def _set_note_kept(entry, fields, keep=True):
    """entry.update(fields), but a note that changes goes into note_history first (newest last).
    keep=False: same edit still going on (the page autosaves while typing) -- overwrite, no new version."""
    old = entry.get("note") or ""
    if keep and "note" in fields and old and old != fields["note"]:
        history = entry.setdefault("note_history", [])
        history[:] = [h for h in history if h.get("note") != old]   # 同一段字只留一份（翻版本时不越翻越多）
        history.append({"note": old, "at": entry.get("note_at") or entry.get("at") or ""})
        del history[:-NOTE_HISTORY]
    if "note" in fields:
        fields = {**fields, "note_at": fields.get("note_at") or time.strftime("%Y-%m-%dT%H:%M:%S")}
    entry.update(fields)


def cmd_note(cfg, args):
    """Write (or clear) a candidate's note; the verdict stays as it is. The review page autosaves into it.
    The note it replaces goes into note_history (last 10), so a slip can be undone."""
    card = load_card(cfg, args.card)
    name = candidate_name(cfg, args.candidate)
    text = (args.note or "").strip()
    update_review(cfg, card["id"], lambda review: _set_note_kept(review["candidates"].setdefault(name, {}), {"note": text},
                                                                 keep=not getattr(args, "no_history", False)))
    print(f"{name}: note {'saved' if text else 'cleared'} (the old one is kept in note_history)")


def cmd_unreview(cfg, args):
    """Take a verdict back: a rejected image leaves the rejects and is 未审 again in the trial / review zone."""
    card = load_card(cfg, args.card)
    name = candidate_name(cfg, args.candidate)
    if not _clear_verdict(cfg, card["id"], name):
        raise SystemExit(f"{name} has no verdict to take back")
    print(f"{name}: verdict cleared -> back in the {'trial' if card.get('kind') == 'sketch' else 'review'} zone (未审)")


def cmd_demote(cfg, args):
    """Undo a promotion (one step back): the previous image in history comes back, or with no history the
    entry goes; the image it was promoted from returns to the library (a sketch's stays in the trial zone),
    or with --discard goes to the rejects."""
    kind, _, name = args.as_.partition(":")
    section = sourcekinds.section_for(cfg.kinds, kind)
    if not section or not name:
        raise SystemExit("--as must be <kind>:<name>, e.g. character:主角")
    with fileio.locked(cfg.sources_file):
        data, rt = _load_sources_rt(cfg.sources_file)
        table = data.get(section) or {}
        entry = table.get(name)
        if not isinstance(entry, dict) or not entry.get("source"):
            raise SystemExit(f"{section}.{name} is not registered")
        users = _cards_using(cfg, section, name)
        history = entry.get("history") or []
        if not history and users and not args.force:
            raise SystemExit(f"{section}.{name} has no earlier version and cards use it ({', '.join(users)}); "
                             "removing it leaves them without a source -- pass --force")
        source, origin = str(entry["source"]), entry.get("from")
        if history:
            old = history.pop()
            entry["source"] = old["source"]
            entry["approved"] = old.get("approved", "")
            for key in ("from", "at") + IMAGE_BOUND:
                if old.get(key):
                    entry[key] = old[key]
                elif key in entry:
                    del entry[key]
            if not history:
                del entry["history"]
        else:
            del table[name]
        _dump_sources_rt(cfg.sources_file, data, rt)
    log = cfg.output_dir / "promotions.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": "demote", "as": args.as_,
                                 "approved": "移回", "target": f"{cfg.sources_file}:{section}.{name}", "stored": source,
                                 "candidate": origin or "", "restored": str(entry["source"]) if name in table else ""},
                                ensure_ascii=False) + "\n")
    print(f"{section}.{name}: " + (f"back to {entry['source']}" if name in table else "removed (no earlier version)"))
    if not origin:
        print("note: this entry does not record which image it came from -- nothing goes back to the library")
        return
    try:
        _, job = _find_candidate(cfg, origin)
    except SystemExit:
        print(f"note: {origin} is no longer in output/candidates -- nothing goes back to the library")
        return
    card_id = job.get("key", "").split("|")[0]
    if not card_id:
        return
    finalised = cfg.library.get("kind") == "local" and any(
        Path(e["file"]).stem == Path(origin).stem and e.get("status") == "final"
        for e in library.local_load(cfg.library)["entries"])
    if finalised:   # 原图在素材库里已经定稿：不动它（定稿只归人）
        print(f"note: {origin} is finalised in the library -- it stays there; un-finalise it on the review page to move it")
        return
    if args.discard:   # 3：撤掉真源，图直接进废稿区（在素材区的也一起移出）
        try:
            cmd_unregister(cfg, argparse.Namespace(card=card_id, candidate=origin, discard=True, note=""))
        except SystemExit:
            cmd_review(cfg, argparse.Namespace(card=card_id, candidate=origin, verdict="fail", note="", by="human"))
            cmd_pick(cfg, argparse.Namespace(card=card_id, candidates=[origin], remove=True))
            print(f"{origin}: -> rejects")
    elif load_card(cfg, card_id).get("kind") == "sketch":   # 短效的上一步是试样区：图本来就留在那里
        _clear_verdict(cfg, card_id, origin)
        print(f"{origin}: back in the trial zone")
    else:
        cmd_keep(cfg, argparse.Namespace(card=card_id, candidate=origin, note="", by="human"))
        print(f"{origin}: back in the library")


def video_cards_dir(config):
    """生态契约 §5：视频卡在哪（只读）。同一份 project.yaml 带 ``video:`` 段，或 ``link.video_config`` 指向视频项目。"""
    link = (config.raw.get("link") or {}).get("video_config")
    if link:
        path = Path(os.path.expanduser(str(link)))
        path = path if path.is_absolute() else (config.root / path).resolve()
        if not path.is_file():
            return None
        raw = _expand(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
        base = (raw.get("project") or {}).get("root")
        root = (path.parent / base).resolve() if base else path.parent
    elif config.raw.get("video"):
        raw, root = config.raw, config.root
    else:
        return None
    cards = Path(os.path.expanduser(str(((raw.get("video") or {}).get("paths") or {}).get("cards", "video-cards"))))
    return root, (cards if cards.is_absolute() else (root / cards).resolve())


def video_keyframe_uses(config, entries):
    """{素材库条目 id: [{card, title, at, slot}]}：哪些视频卡拿这条素材当关键帧。只读视频卡，不改。

    认法和视频解析引用一致：library:<id>；image:<卡>（那张卡 pick 的第一张）或 image:<卡>/<候选>；
    文件路径按内容（sha256）比，定稿副本也算。"""
    found = video_cards_dir(config)
    if not found or not entries:
        return {}
    root, cards_dir = found
    if not cards_dir.is_dir():
        return {}
    stem = lambda name: Path(str(name)).stem
    by_stem = {}
    for entry in entries:
        by_stem.setdefault((entry["card"], stem(entry["candidate"])), []).append(entry)
    by_sha = {}
    for entry in entries:
        if entry.get("sha256"):
            by_sha.setdefault(entry["sha256"], []).append(entry)
    picks, digests, uses = {}, {}, {}

    def first_pick(card_id):
        if card_id not in picks:
            path = _review_path(config, card_id)
            review = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            picks[card_id] = (review.get("pick") or [None])[0]
        return picks[card_id]

    for path in sorted(cards_dir.glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        try:
            card = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        card_id = str(card.get("id") or path.stem.split("_")[0])
        for value, item in keyframemod.values(card):
            hits = []
            if value.startswith("library:"):
                hits = [e for e in entries if e["id"] == value[len("library:"):]]
            elif value.startswith("image:"):
                image_card, _, cand = value[len("image:"):].partition("/")
                if cand:              # 写了候选名：和视频一样按前缀认
                    hits = [e for (c, s), es in by_stem.items() if c == image_card and s.startswith(stem(cand)) for e in es]
                elif first_pick(image_card):
                    hits = by_stem.get((image_card, stem(first_pick(image_card))), [])
            elif not value.startswith(("source:", "take:", "http://", "https://", "data:")):
                file = Path(os.path.expanduser(value))
                file = file if file.is_absolute() else (root / file).resolve()
                if file.is_file():
                    if file not in digests:
                        digests[file] = hashlib.sha256(file.read_bytes()).hexdigest()
                    hits = by_sha.get(digests[file], [])
            for entry in hits:
                use = {"card": card_id, "title": str(card.get("title") or ""), "at": item["at"], "slot": item["slot"]}
                if use not in uses.setdefault(entry["id"], []):
                    uses[entry["id"]].append(use)
    return uses


def project_state(config):
    """全局一份：卡、候选与结论、真源、模板、素材库、灵感、真源记录。审核页 /api/state 和 `state` 命令共用。"""
    manifest = load_manifest(config)
    by_candidate = {Path(job["candidate"]).name: {"key": key, **job}
                    for key, job in manifest["jobs"].items() if job.get("candidate")}
    registered = set()                    # candidate file names already in the local library
    if config.library.get("kind") == "local" and config.library.get("root"):
        registered = {Path(e["file"]).name for e in library.local_load(config.library)["entries"]}
    cards = []
    registry = load_sources(config)    # 定真源默认值要看 styles 登记的是画风还是氛围；下面真源区也用它
    raw_cards = all_cards(config)      # 读一次，下面真源的「被哪些卡引用」也用它
    for card in raw_cards:
        card_id = str(card.get("id"))
        review_path = _review_path(config, card_id)
        review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.is_file() else {}
        candidates = []
        for path in sorted((config.output_dir / "candidates").glob(f"{card_id}_*")):
            job = by_candidate.get(path.name, {})
            if job and job["key"].split("|")[0] != card_id:
                continue                      # prefix collision: belongs to another card
            reviews = review.get("candidates") or {}
            verdict = reviews.get(path.name) or reviews.get(path.stem) or {}   # older CLI runs keyed it without extension
            sent = ""                         # the prompt actually sent for this image (runs/<card>-<alias>-<n>/request.json)
            if job:
                request = config.output_dir / "runs" / job["key"].replace("|", "-") / "request.json"
                try:
                    sent = json.loads(request.read_text(encoding="utf-8")).get("prompt", "")
                except (OSError, ValueError):
                    pass
            candidates.append({"sent_prompt": sent,
                "name": path.name, "file": _relative_to_root(config, path), "model": job.get("key", "||").split("|")[1],
                "width": job.get("width"), "height": job.get("height"), "at": job.get("at", ""), "alpha": bool(job.get("has_alpha")),
                "verdict": verdict.get("verdict", ""), "note": verdict.get("note", ""),
                "note_history": verdict.get("note_history") or [],
                "picked": bool({path.name, path.stem} & set(review.get("pick") or [])), "registered": path.name in registered,
            })
        prompt_file = config.output_dir / "prompt" / f"{card_id}.txt"
        cards.append({
            "id": card_id, "title": card.get("title", ""), "kind": card.get("kind", "card"),
            "group": card.get("group") or ("短效草图" if card.get("kind") == "sketch" else "未分组"),
            "pack": card_pack(card, config.kinds),
            "subject": next((c.get("name") for c in (card.get("characters") or []) if isinstance(c, dict) and c.get("name")), ""),
            "lookup": next((c.get("lookup") for c in (card.get("characters") or []) if isinstance(c, dict) and c.get("lookup")), ""),
            "outfit": card.get("outfit", ""),
            "promote": promote_defaults(card, config.kinds, registry),
            "status": card.get("status", "draft"), "hold": card.get("hold", ""),
            "idea": card.get("extra_prompt") or card.get("action") or "",
            "prompt": prompt_file.read_text(encoding="utf-8") if prompt_file.is_file() else "",
            "feedback": card.get("feedback") or [], "candidates": candidates,
        })

    sources = []
    kind_map = config.kinds
    # declared kinds first; a section that is in sources.yaml but no longer declared still shows (nothing is lost)
    sections = list(kind_map) + [k for k in registry if k not in kind_map and isinstance(registry.get(k), dict)]
    for section in sections:
        kind = kind_map.get(section) or {"label": section, "as": section, "text": False}
        label = kind["label"]
        for name, entry in (registry.get(section) or {}).items():
            entry = entry or {}
            history = []
            for item in entry.get("history") or []:
                history.append({**item, "source": str(item.get("source", ""))})
            source = entry.get("source")
            sources.append({
                "section": section, "label": label, "name": name, "pack": source_pack(section, name, entry, kind_map), "text": kind["text"],
                "source": str(source) if source else "", "face": str(entry.get("face", "") or ""),
                "exists": bool(source) and (str(source).startswith(("http://", "https://")) or (config.root / str(source)).is_file()),
                "approved": entry.get("approved", ""), "at": str(entry.get("at", "")),
                "note": entry.get("note", ""), "history": history, "from": str(entry.get("from", "") or ""),
                "look": entry.get("look", ""), "prompt": entry.get("prompt", ""),
                "used_by": _cards_using(config, section, name, raw_cards),
            })

    templates = [t for t in prompt_templates(config.prompt_zone)]
    lib = {"kind": config.library.get("kind", "none"), "entries": []}
    if config.library.get("kind") == "local" and config.library.get("root"):
        root = Path(config.library["root"])
        lib["root"] = _relative_to_root(config, root)
        packs = {c["id"]: c["pack"] for c in cards}
        for entry in library.local_load(config.library)["entries"]:
            owner = str(entry.get("title", "")).split(" ", 1)[0]
            lib["entries"].append({**entry, "file": _relative_to_root(config, root / entry["file"]),
                                   "card": owner, "candidate": Path(entry["file"]).name,
                                   "pack": packs.get(owner) or ["测试", str(entry.get("batch") or "未分组")]})
        uses = video_keyframe_uses(config, lib["entries"])
        for entry in lib["entries"]:
            entry["keyframe_of"] = uses.get(entry["id"], [])

    inspiration = []
    for key, item in load_inspiration(config)["items"].items():
        inspiration.append({"id": key, **item, "pack": inspiration_pack(item),
                            "file": _relative_to_root(config, config.inspiration_dir / item["file"])})

    promotions = []
    log = config.output_dir / "promotions.jsonl"
    if log.is_file():
        promotions = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _file(value):                     # log paths are absolute or project-relative; the page wants project-relative
        if not value:
            return ""
        path = Path(value) if Path(value).is_absolute() else config.root / value
        return _relative_to_root(config, path) if path.is_file() else ""
    for p in promotions:
        p["file"] = _file(p.get("stored")) or _file(p.get("candidate"))
        p["previous_file"] = _file(p.get("previous"))

    return {"project": config.name, "root": str(config.root), "today": time.strftime("%Y-%m-%d"),
            "categories": pack_categories(config),
            "kinds": [{"section": k, "label": v["label"], "as": v.get("as") or k, "text": v["text"]} for k, v in config.kinds.items()],
            "cards": cards, "sources": sources, "templates": [{"id": t["id"], "name": t["name"], "body": t.get("body", "")} for t in templates],
            "library": lib, "inspiration": inspiration, "promotions": promotions[-50:][::-1]}


def cmd_keep(cfg, args):
    """「要」：通过 + 记进 pick + 登记成素材库草稿，一步到位（审核页的「要」就是它）。
    pick 也要写：视频卡首帧写 image:<卡> 读的就是它（生态契约 §3）。先 pick 再登记，卡状态停在 registered。"""
    cmd_review(cfg, argparse.Namespace(card=args.card, candidate=args.candidate, verdict="pass",
                                       note=args.note or "", by=args.by))
    cmd_pick(cfg, argparse.Namespace(card=args.card, candidates=[args.candidate], remove=False))
    cmd_register(cfg, argparse.Namespace(card=args.card, candidates=[args.candidate]))


def cmd_pack(cfg, args):
    """改卡的资产包路径（只归类，不搬文件）；路径给空就回到默认规则。"""
    card = update_card(cfg, args.card, lambda fresh: fresh.__setitem__("pack", "/".join(pack_parts(args.pack))))
    print(f"{args.card} -> {card['pack'] or '（按默认规则归类）'}")


def cmd_state(cfg, args):
    data = project_state(cfg)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(f"project : {data['project']}  ({data['root']})")
    for card in data["cards"]:
        counts = {}
        for c in card["candidates"]:
            zone = "registered" if c["registered"] else (c["verdict"] or "unreviewed")
            counts[zone] = counts.get(zone, 0) + 1
        summary = "  ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "no candidates"
        print(f"  {card['id']:<14} {card['status']:<10} {summary:<26} {'/'.join(card['pack'])}")
    print(f"sources : {len(data['sources'])}  templates: {len(data['templates'])}  "
          f"library: {len(data['library']['entries'])} ({data['library']['kind']})  inspiration: {len(data['inspiration'])}")
    print("full data: `state --json` (same as the review page's /api/state)")


def cmd_feedback(cfg, args):
    card = load_card(cfg, args.card)
    entry = {"by": args.by or "human", "verdict": args.verdict,
             "reason": args.reason or "", "root_cause": args.root_cause or ""}
    with fileio.locked(card["_path"]):
        card = load_card(cfg, args.card)       # 锁内重新读：别的进程可能刚改过这张卡
        log = card.setdefault("feedback", [])
        last = log[-1] if log else {}
        if all(last.get(k, "") == v for k, v in entry.items()):
            # the same note saved twice (double click / re-save) is one piece of feedback
            print(f"feedback unchanged on {card['id']}: {args.verdict}")
            return
        log.append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry})
        if args.verdict == "reject":
            card["status"] = "draft"
        save_card(card)
    print(f"feedback recorded on {card['id']}: {args.verdict}")


# --------------------------------------------------------------------------- #
# short-term system: inspiration -> sketch -> promote
#
# The short line settles things once (a face, a product, a look, a phrasing);
# the long line reuses them for months. `promote` is the hand-off: a picked
# candidate becomes a registry entry or a prompt template, with the human's
# approval and the previous value kept in `history`.
# --------------------------------------------------------------------------- #

def _inspiration_index_path(cfg):
    return cfg.inspiration_dir / "index.json"


def load_inspiration(cfg):
    path = _inspiration_index_path(cfg)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"format": "image-card-pipeline/inspiration-v1", "items": {}}


def _resolve_inspiration(cfg, value):
    """An inspiration id (or its prefix) -> stored file; anything else is a path/URL."""
    items = load_inspiration(cfg)["items"]
    hits = [key for key in items if key.startswith(str(value))]
    if len(hits) == 1:
        return str(cfg.inspiration_dir / items[hits[0]]["file"])
    return value


def cmd_inspire(cfg, args):
    import shutil

    index = load_inspiration(cfg)
    if args.action == "list":
        rows = index["items"].items()
        if args.tag:
            rows = [(key, item) for key, item in rows if args.tag in (item.get("tags") or [])]
        for key, item in rows:
            tags = f"  [{', '.join(item['tags'])}]" if item.get("tags") else ""
            print(f"{key}  {item['file']}{tags}  {item.get('note', '')}")
        if not index["items"]:
            print("inspiration is empty: `inspire add <file…> --note …`")
        return

    if not args.files:
        raise SystemExit("inspire add needs at least one file")
    cfg.inspiration_dir.mkdir(parents=True, exist_ok=True)
    with fileio.locked(_inspiration_index_path(cfg)):
        _inspire_add(cfg, args, load_inspiration(cfg))
    print("next: `sketch <name> --idea … --ref <id>` to try it out")


def _inspire_add(cfg, args, index):
    import shutil

    for value in args.files:
        source = Path(os.path.expanduser(value)).resolve()
        if not source.is_file():
            raise SystemExit(f"not a file: {value}")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:8]
        if digest in index["items"]:
            print(f"{digest}  already in inspiration ({index['items'][digest]['file']})")
            continue
        name = f"{digest}_{source.name}"
        shutil.copy2(source, cfg.inspiration_dir / name)
        index["items"][digest] = {"file": name, "from": str(source), "note": args.note or "",
                                  "tags": [t for t in (args.tag or "").replace("，", ",").split(",") if t.strip()],
                                  **({"pack": "/".join(pack_parts(args.pack))} if args.pack else {}),
                                  "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        print(f"{digest}  {name}")
    fileio.write_json(_inspiration_index_path(cfg), index)


def cmd_sketch(cfg, args):
    """A throwaway card: one idea + a few references, straight to candidates."""
    card_id = args.name if args.name.startswith("s-") else f"s-{args.name}"
    try:
        parse_promote(args.promote, cfg.kinds)
    except ValueError as error:
        raise SystemExit(str(error))
    cfg.cards_dir.mkdir(parents=True, exist_ok=True)
    # 只认 id 完全相同的那张：按文件名前缀找，`s-a` 会撞上 `s-a_b` 的卡并把它覆盖掉
    existing = [card["_path"] for card in all_cards(cfg) if str(card.get("id")) == card_id]
    fresh = cfg.cards_dir / f"{card_id}_sketch.yaml"
    if not existing and fresh.exists():
        raise SystemExit(f"{fresh.name} exists but belongs to another card (its id is not {card_id}); pick another name")
    refs = [_resolve_inspiration(cfg, value) for value in args.ref or []]
    if existing and not args.idea and not refs:
        print(f"re-using sketch {existing[0].name}")
    else:
        card = {
            "id": card_id, "title": args.title or args.name, "slug": card_id, "kind": "sketch",
            "pack": "/".join(pack_parts(args.pack)), "outfit": args.outfit or "", "promote": args.promote or "",
            "status": "draft", "hold": "", "created": time.strftime("%Y%m%d"),
            "model": args.model or "", "aspect": args.aspect or "1024x1024",
            "transparent": bool(args.transparent), "prompt_mode": "lean", "skip_blocks": bool(args.bare),
            "styles": args.style or [], "characters": [], "props": [],
            "extra_prompt": args.idea or "",
            "refs": {"roles": args.role or [], "files": refs, "confirmed": [], "urls": []},
            "feedback": [], "sources": [f"sketch {time.strftime('%Y-%m-%d')}"],
        }
        if existing:
            previous = yaml.safe_load(existing[0].read_text(encoding="utf-8")) or {}
            card["feedback"] = previous.get("feedback") or []
            card["pack"] = card.get("pack") or previous.get("pack") or ""
            card["outfit"] = card.get("outfit") or previous.get("outfit") or ""
            card["promote"] = card.get("promote") or previous.get("promote") or ""
            card["_path"] = existing[0]
        else:
            card["_path"] = fresh
        if not card["extra_prompt"]:
            raise SystemExit("a sketch needs --idea (the inspiration in one paragraph, English works best)")
        save_card(card)
        print(f"sketch card -> {card['_path']}")
        if not card["promote"]:
            print("note: no --promote -- the review page will guess what this sketch is for "
                  "(say it up front: --promote character:<名> | prop:<名> | \"style:<名> @画风|@氛围\" | template:<名>)")

    compose_args = argparse.Namespace(card=card_id, var=args.var, force=args.force)
    cmd_compose(cfg, compose_args)
    generate_args = argparse.Namespace(card=card_id, model=args.model, size=None, quality=None, budget=args.budget,
                                       approved=args.approved, refresh=True, force=args.force)
    cmd_generate(cfg, generate_args)
    print(f"next: `sheet {card_id}`, then `promote <candidate> --as character:<name>|prop:<name>|style:<name>|template:<name>`")


def _find_candidate(cfg, name):
    """Candidate file name (or prefix) -> (path, job record or {})."""
    candidates_dir = cfg.output_dir / "candidates"
    path = candidates_dir / name
    if not path.is_file():
        matches = sorted(candidates_dir.glob(f"{name}*"))
        if not matches:
            path = Path(os.path.expanduser(name))
            if not path.is_file():
                raise SystemExit(f"candidate not found: {name}")
            return path.resolve(), {}
        path = matches[0]
    for key, job in load_manifest(cfg)["jobs"].items():
        if job.get("candidate") and Path(job["candidate"]).name == path.name:
            return path, {"key": key, **job}
    return path, {}


def _update_sources(cfg, section, name, entry_update):
    """Set sources.yaml <section>.<name>, keeping comments when ruamel.yaml is available.

    The previous source moves into ``history`` so a promotion is always reversible.
    """
    with fileio.locked(cfg.sources_file):
        return _update_sources_locked(cfg, section, name, entry_update)


def _load_sources_rt(path):
    """sources.yaml -> (data, rt); rt is None when ruamel.yaml is missing (comments get lost on write)."""
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    try:
        from ruamel.yaml import YAML

        rt = YAML()
        rt.preserve_quotes = True
        rt.width = 4096
        data = rt.load(text) if text.strip() else None
    except ImportError:
        rt, data = None, yaml.safe_load(text) if text.strip() else None
    return (data if data is not None else {}), rt


def _dump_sources_rt(path, data, rt):
    path.parent.mkdir(parents=True, exist_ok=True)
    if rt is not None:
        import io

        buffer = io.StringIO()
        rt.dump(data, buffer)
        fileio.atomic_write(path, buffer.getvalue())
    else:
        fileio.atomic_write(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


# 换图时跟着旧图一起进 history 的字段：脸部特写是那张图的脸；role 是登记那张图时写的用法
IMAGE_BOUND = ("face", "role")


def _update_sources_locked(cfg, section, name, entry_update):
    path = cfg.sources_file
    data, rt = _load_sources_rt(path)
    keeps_comments = rt is not None
    table = data.get(section)
    if table is None:
        data[section] = {}
        table = data[section]
    entry = table.get(name)
    previous = None
    if entry is None:
        table[name] = {}
        entry = table[name]
    else:
        previous = entry.get("source")
    if previous and str(previous) != str(entry_update["source"]):
        history = entry.get("history")
        if history is None:
            entry["history"] = []
            history = entry["history"]
        old = {"source": previous, "replaced": time.strftime("%Y-%m-%d"), "approved": entry.get("approved", "")}
        for key in ("from", "at") + IMAGE_BOUND:   # demote 移回时靠它们把旧版原样恢复
            if entry.get(key):
                old[key] = entry[key]
        history.append(old)
        for key in IMAGE_BOUND:      # 跟着旧图走的：新图没一起给，就从现条目清掉（旧的已进 history）
            if key not in entry_update and key in entry:
                del entry[key]
    for key, value in entry_update.items():
        entry[key] = value

    _dump_sources_rt(path, data, rt)
    return previous, keeps_comments


def _cards_using(cfg, section, name, cards=None):
    users = []
    for card in all_cards(cfg) if cards is None else cards:
        if name in sourcekinds.card_names(card, section):
            users.append(str(card.get("id")))
    return users


def cmd_describe(cfg, args):
    """Write (or clear) the description of a registered text-kind source, e.g. a style; the image stays."""
    if approval_problem(args.approved):
        raise SystemExit("the description is spliced into every card that uses it -- pass the user's words: "
                         f'--approved "用户 10-24：描述这样写" ({approval_problem(args.approved)})')
    kind, _, name = args.as_.partition(":")
    section = sourcekinds.section_for(cfg.kinds, kind)
    if not section or not name:
        raise SystemExit("--as must be <kind>:<name>, e.g. style:墨白")
    if not cfg.kinds[section]["text"]:
        raise SystemExit(f"{section} entries carry no description (only kinds with text: true do)")
    entry = (load_sources(cfg).get(section) or {}).get(name)
    if not isinstance(entry, dict) or not entry.get("source"):
        raise SystemExit(f"{section}.{name} is not registered -- promote an image first")
    text, before = (args.text or "").strip(), str(entry.get("prompt") or "").strip()
    _update_sources(cfg, section, name, {"source": entry["source"], "prompt": text})
    # 和定真源记同一本账（真源区「记录」）：谁批的、哪天、描述从什么改成什么；stored 只为记录里显示样张
    log = cfg.output_dir / "promotions.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": "describe", "as": args.as_,
                                 "approved": args.approved, "target": f"{cfg.sources_file}:{section}.{name}",
                                 "stored": entry["source"], "before": before, "text": text}, ensure_ascii=False) + "\n")
    users = _cards_using(cfg, section, name)
    print(f"{section}.{name}: description {'set' if text else 'cleared'} ({len(text)} chars)"
          + (f"; cards using it need compose again: {', '.join(users)}" if users else ""))


def cmd_promote(cfg, args):
    """Hand a short-line pick to the long line: registry entry or prompt template."""
    import shutil

    if approval_problem(args.approved):
        raise SystemExit("promoting sets what every later card reuses -- pass the user's words: "
                         f'--approved "用户 10-23：就这张" ({approval_problem(args.approved)})')
    kind, _, name = args.as_.partition(":")
    kind_map = cfg.kinds
    section = sourcekinds.section_for(kind_map, kind) if kind != "template" else None
    if (kind != "template" and not section) or not name:
        choices = ", ".join(f"{v.get('as') or k}:<name>" for k, v in kind_map.items())
        raise SystemExit(f"--as must be {choices} or template:<name> (kinds come from sources.kinds in project.yaml)")

    parts = name.replace("\\", "/").split("/")
    if name.startswith(("/", "~")) or Path(name).is_absolute() or any(part in ("", ".", "..") for part in parts):
        raise SystemExit(f"bad name {name!r}: use plain names, '/' only between parts (主角/剑) -- no '..', no absolute path")
    base_dir = cfg.prompt_zone if kind == "template" else cfg.promoted_dir
    target_dir = (base_dir / name).resolve()
    if not target_dir.is_relative_to(base_dir.resolve()):
        raise SystemExit(f"bad name {name!r}: it points outside {base_dir}")

    candidate, job = _find_candidate(cfg, args.candidate)
    card_id = job.get("key", "").split("|")[0] or None
    stamp = time.strftime("%Y-%m-%d")
    log_entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "as": args.as_, "candidate": str(candidate),
                 "card": card_id, "approved": args.approved, "note": args.note or ""}

    if kind == "template":
        card = load_card(cfg, card_id) if card_id else {}
        body = (args.text or "").strip()
        if not body and card.get("kind") != "sketch":
            # a scene card: keep the whole scene phrasing, never identity (that lives in the registry)
            blocks = load_blocks(cfg)
            lines = []
            for label, section, key in (("Environment", "environments", "environment"),
                                        ("Composition", "compositions", "composition")):
                value = _block(blocks, section, card.get(key))
                if value:
                    lines.append(f"{label}: {value}")
            if card.get("action"):
                lines.append("Action: " + str(card["action"]).strip())
            if card.get("extra_prompt"):
                lines.append(str(card["extra_prompt"]).strip())
            body = "\n".join(lines)
        if not body:
            body = (card.get("extra_prompt") or card.get("action") or "").strip()
        if not body:
            raise SystemExit("nothing to save: the card has no extra_prompt/action; pass --text")
        if not PROMPT_PARA.match(body):
            print("note: prompt-zone keeps only paragraphs starting with a latin letter; "
                  "a Chinese idea will be dropped at compose time -- pass an English --text")
        rel = name if name.endswith(".md") else f"{name}.md"
        target = cfg.prompt_zone / rel
        if target.exists() and not args.force:
            raise SystemExit(f"{target} exists; pass --force to overwrite (the old one stays in version history)")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"---\n名称: {Path(rel).stem}\n顺序: 50\n---\n\n"
            f"沉淀自 {candidate.name}（{stamp}，{args.approved}）{('：' + args.note) if args.note else ''}\n\n"
            f"{body}\n", encoding="utf-8")
        log_entry["target"] = str(target)
        print(f"template -> {target}")
        print(f"use it: add \"{rel[:-3]}\" to a card's prompt_templates")
    else:
        destination = cfg.promoted_dir / name
        destination.mkdir(parents=True, exist_ok=True)
        stored = destination / f"{time.strftime('%Y%m%d')}_{candidate.name}"
        shutil.copy2(candidate, stored)
        update = {"source": _relative_to_root(cfg, stored), "approved": args.approved,
                  "from": candidate.name, "at": stamp}
        if args.face:
            face = Path(os.path.expanduser(args.face)).resolve()
            if not face.is_file():
                raise SystemExit(f"face file not found: {args.face}")
            face_stored = destination / f"{time.strftime('%Y%m%d')}_face_{face.name}"
            shutil.copy2(face, face_stored)
            update["face"] = _relative_to_root(cfg, face_stored)
        if args.note:
            update["note"] = args.note
        if getattr(args, "role", None):
            update["role"] = args.role
        if getattr(args, "look", None):
            update["look"] = args.look          # 画风 / 氛围：页面上按它分组
        if (args.text or "").strip():
            update["prompt"] = args.text.strip()   # 字：compose 会把它拼进引用这条的卡
        previous, kept = _update_sources(cfg, section, name, update)
        log_entry.update({"target": f"{cfg.sources_file}:{section}.{name}", "stored": str(stored),
                          "previous": str(previous) if previous else None})
        if previous and str(previous) != str(update["source"]):
            tail = f"  (was {previous}, kept in history)"
        elif previous:
            tail = "  (same image as before -- approval updated, nothing added to history)"
        else:
            tail = ""
        print(f"{section}.{name} -> {update['source']}{tail}")
        if not kept:
            print("note: ruamel.yaml not installed -- sources.yaml was rewritten without comments (the previous text is in version history)")
        users = _cards_using(cfg, section, name)
        if users:
            print(f"cards that now use it: {', '.join(users)}")
        else:
            hint = f"characters: [{{name: {name}}}]" if section == "characters" else f"{section}: [{name}]"
            print(f"no card uses it yet; reference it with `{hint}`")

    if card_id:
        try:
            def kept(card):
                card.setdefault("feedback", []).append({"at": log_entry["at"], "by": "human", "verdict": "accept",
                                                        "reason": f"promoted {candidate.name} as {args.as_}: {args.approved}",
                                                        "root_cause": ""})
                card["status"] = "kept"

            update_card(cfg, card_id, kept)
        except SystemExit:
            pass
    log = cfg.output_dir / "promotions.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(log_entry, ensure_ascii=False) + "\n")


def _relative_to_root(cfg, path):
    try:
        return str(Path(path).resolve().relative_to(cfg.root))
    except ValueError:
        return str(path)


def cmd_providers(cfg, args):
    print(f"default model: {cfg.default_model()}  (provider default: {cfg.default_provider})")
    for alias, entry in cfg.model_aliases().items():
        try:
            spec = cfg.model_spec(alias)
        except ProviderError as error:
            print(f"  {alias:<20} BROKEN: {error}")
            continue
        ready, reason = providers.provider_ready(spec)
        flag = "ready" if ready else "NOT READY"
        print(f"  {alias:<20} -> {spec['name']}/{spec['model']}  [{spec['kind']}]  {flag} ({reason})")


def cmd_doctor(cfg, args):
    print(f"project : {cfg.name}  ({cfg.path})")
    print(f"root    : {cfg.root}")
    for label, path in (("cards", cfg.cards_dir), ("prompt_zone", cfg.prompt_zone),
                        ("blocks", cfg.blocks_dir), ("output", cfg.output_dir), ("sources", cfg.sources_file)):
        exists = "ok" if Path(path).exists() else "missing"
        print(f"{label:<11}: {exists}  {path}")
    print(f"templates: {len(prompt_templates(cfg.prompt_zone))}")
    cards = all_cards(cfg)
    print(f"cards    : {len(cards)}")
    # paths being there is not enough: the cards must actually compose and find their references
    blocks, broken, unresolved = load_blocks(cfg), [], []
    for card in cards:
        if card.get("hold"):
            continue
        try:
            problems, _notes = card_problems(cfg, card)
            _refs, missing = resolve_refs(cfg, card)
        except Exception as error:  # noqa: BLE001 - a broken card must not hide the rest of the report
            problems, missing = [f"{type(error).__name__}: {error}"], []
        if problems:
            broken.append((card.get("id"), problems[0]))
        if missing:
            unresolved.append((card.get("id"), missing[0].get("reason") or missing[0].get("label")))
    held = sum(1 for card in cards if card.get("hold"))
    bad = {card_id for card_id, _ in broken} | {card_id for card_id, _ in unresolved}
    print(f"  usable : {len(cards) - held - len(bad)}  (on hold {held}, with problems {len(broken)}, missing refs {len(unresolved)})")
    for card_id, line in broken[:5]:
        print(f"  PROBLEM {card_id}: {line}")
    for card_id, line in unresolved[:5]:
        print(f"  no ref  {card_id}: {line}")
    if len(broken) > 5 or len(unresolved) > 5:
        print("  ... (run `validate <card>` for the full list)")
    if version_tracked(cfg):
        auto = (cfg.raw.get("rules") or {}).get("autosave", True) is not False
        print(f"version : git  (auto-save {'on' if auto else 'off'}; 版本 历史 / 取 / 回退)")
    else:
        print(f"version : NONE -- no history for {cfg.path.parent}; run `git init` there (or `init`) so overwrites can be undone")
    print(f"image_host: {cfg.host.get('kind', 'none')}")
    print(f"library : {cfg.library.get('kind', 'none')}" + (f"  {cfg.library.get('url') or cfg.library.get('root')}" if library.enabled(cfg.library) else ""))
    cmd_providers(cfg, args)


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #

def build_parser():
    parser = argparse.ArgumentParser(prog="pipeline", description="card-driven image generation pipeline")
    parser.add_argument("--config", default=os.getenv("IMAGE_CARD_CONFIG", "project.yaml"),
                        help="project config (default: $IMAGE_CARD_CONFIG or ./project.yaml)")
    parser.add_argument("--root", help="override the project root from the config")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="scaffold a project (config, blocks, prompt zone, sources)")
    p.add_argument("--force", action="store_true", help="overwrite existing scaffold files")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("new-card", help="scaffold a card from the template")
    p.add_argument("card_id")
    p.add_argument("title", nargs="?", default="")
    p.add_argument("--kind", choices=("card", "rerender", "portrait"), default="card",
                   help="card = scene; rerender = edit one image (keep/change); portrait = one-figure character sheet")
    p.set_defaults(func=cmd_new_card)

    sub.add_parser("cards", help="list cards").set_defaults(func=cmd_cards)

    p = sub.add_parser("show", help="print one card")
    p.add_argument("card")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("validate", help="check card fields and references")
    p.add_argument("card")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("compose", help="build the prompt and report its size")
    p.add_argument("card")
    p.add_argument("--var", action="append", metavar="NAME=VALUE", help="fill a {{variable}} (repeatable)")
    p.add_argument("--force", action="store_true", help="compose even with validate problems or over the reject threshold")
    p.set_defaults(func=cmd_compose)

    p = sub.add_parser("generate", help="call the provider and record the run")
    p.add_argument("card")
    p.add_argument("--model", help="model alias from the config")
    p.add_argument("--size")
    p.add_argument("--quality")
    p.add_argument("--budget", type=int, help="max images this invocation")
    p.add_argument("--approved", help="who/when approved this paid batch (required unless relaxed)")
    p.add_argument("--refresh", action="store_true", help="accept a prompt recomputed from a changed card")
    p.add_argument("--force", action="store_true", help="send even if over the reject threshold")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("forget", help="drop an 'unknown' job after a human checked nothing was made, so it may be sent again")
    p.add_argument("card")
    p.add_argument("job", help="the job's number (1, 2 …) or its full key <card>|<model>|<n>")
    p.add_argument("--model", help="model alias of the job (default: the card's / the project default)")
    p.add_argument("--why", required=True, help="what you checked (run dir, provider console) -- kept with the record")
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("sheet", help="contact sheet: candidates + a reference strip")
    p.add_argument("card")
    p.set_defaults(func=cmd_sheet)

    p = sub.add_parser("review", help="record a verdict on one candidate")
    p.add_argument("card")
    p.add_argument("candidate")
    p.add_argument("verdict", choices=["pass", "fail", "borderline"])
    p.add_argument("--note", default="")
    p.add_argument("--by", default="claude", help="who looked at the image: claude | human (rules.reviewers)")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("pick", help="promote candidates for the human")
    p.add_argument("card")
    p.add_argument("candidates", nargs="+")
    p.add_argument("--remove", action="store_true", help="un-pick these candidates")
    p.set_defaults(func=cmd_pick)

    p = sub.add_parser("keep", help="「要」: pass + pick + register as a library draft, in one go")
    p.add_argument("card")
    p.add_argument("candidate")
    p.add_argument("--note", default="")
    p.add_argument("--by", default="claude", help="who decided: claude | human (the review page records human)")
    p.set_defaults(func=cmd_keep)

    p = sub.add_parser("pack", help="set a card's asset pack path (empty = back to the default rule); no files move")
    p.add_argument("card")
    p.add_argument("pack", nargs="?", default="")
    p.set_defaults(func=cmd_pack)

    p = sub.add_parser("state", help="everything the review page sees, in one go")
    p.add_argument("--json", action="store_true", help="the full data, same as /api/state")
    p.set_defaults(func=cmd_state)

    p = sub.add_parser("unregister", help="move a registered image back out of the library (or --discard it)")
    p.add_argument("card")
    p.add_argument("candidate")
    p.add_argument("--discard", action="store_true", help="send it to the rejects instead (needs --note)")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_unregister)

    p = sub.add_parser("unreview", help="take a verdict back: a rejected image goes back to the trial / review zone (未审)")
    p.add_argument("card")
    p.add_argument("candidate")
    p.set_defaults(func=cmd_unreview)

    p = sub.add_parser("note", help="write or clear one candidate's note (the verdict stays)")
    p.add_argument("card")
    p.add_argument("candidate")
    p.add_argument("--note", default="", help="empty clears it")
    p.add_argument("--no-history", action="store_true", help="same edit as the last save: overwrite without keeping a version")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("demote", help="undo a promotion: earlier version back (or entry gone), the image back to the library")
    p.add_argument("--as", dest="as_", required=True, help="<kind>:<name>, e.g. character:主角")
    p.add_argument("--force", action="store_true", help="remove it even though cards use it and there is no earlier version")
    p.add_argument("--discard", action="store_true", help="send the image to the rejects instead of the library")
    p.set_defaults(func=cmd_demote)

    p = sub.add_parser("register", help="register picked candidates as library drafts")
    p.add_argument("card")
    p.add_argument("candidates", nargs="*")
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("feedback", help="record human feedback on a card")
    p.add_argument("card")
    p.add_argument("verdict", choices=["accept", "reject", "revise"])
    p.add_argument("--reason", default="")
    p.add_argument("--root-cause", default="")
    p.add_argument("--by", default="")
    p.set_defaults(func=cmd_feedback)

    p = sub.add_parser("inspire", help="short line: keep found material in the inspiration area")
    p.add_argument("action", choices=["add", "list"])
    p.add_argument("files", nargs="*")
    p.add_argument("--note", default="")
    p.add_argument("--tag", default="", help="comma-separated tags (add), or one tag to filter (list)")
    p.add_argument("--pack", default="", help="asset pack path, e.g. 风格/雪 (default: 风格/<first tag>)")
    p.set_defaults(func=cmd_inspire)

    p = sub.add_parser("sketch", help="short line: one idea + references -> candidates, no full card")
    p.add_argument("name")
    p.add_argument("--idea", default="", help="the inspiration as one prompt paragraph")
    p.add_argument("--pack", default="", help="asset pack path 大包/小包/子包, e.g. 人物/主角/画风对比")
    p.add_argument("--outfit", default="", help="造型名，如 婚服；网页沉淀人物真源时默认名带上它（主角/婚服）")
    p.add_argument("--promote", default="", help="这张草图是来定哪条真源的：character:<名> / prop:<名> / "
                                                  "\"style:<名> @画风|@氛围\" / template:<名>；审核页按 D 直接停在这一项")
    p.add_argument("--ref", action="append", help="inspiration id or image path (repeatable)")
    p.add_argument("--role", action="append", help="role of each --ref, in order (repeatable)")
    p.add_argument("--style", action="append", help="registered style to apply (repeatable)")
    p.add_argument("--title", default="")
    p.add_argument("--aspect", default="")
    p.add_argument("--transparent", action="store_true")
    p.add_argument("--bare", action="store_true", help="skip blocks/style.txt and tail.txt (for testing a new look)")
    p.add_argument("--model")
    p.add_argument("--budget", type=int, help="total candidates for this sketch+model")
    p.add_argument("--approved", help="who/when approved this paid batch")
    p.add_argument("--var", action="append", metavar="NAME=VALUE")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_sketch)

    p = sub.add_parser("promote", help="hand a pick to the long line: registry entry or prompt template")
    p.add_argument("candidate", help="candidate file name (or prefix), or an image path")
    p.add_argument("--as", dest="as_", required=True,
                   help="character:<name> | prop:<name> | style:<name> | <kind>:<name> (sources.kinds) | template:<path>")
    p.add_argument("--approved", help="who approved it, when, in their words (required)")
    p.add_argument("--face", help="character only: a face close-up to register with it")
    p.add_argument("--text", help="template: the prompt paragraph to save (default: the card's idea); "
                                  "style: its description, spliced into every card that uses it")
    p.add_argument("--look", help="styles: 画风 or 氛围 (how the review page groups it)")
    p.add_argument("--role", help="registry entries: how cards should use this image (default per kind)")
    p.add_argument("--note", default="")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("describe", help="write or clear a registered style's (text kind's) description; the image stays")
    p.add_argument("--as", dest="as_", required=True, help="style:<name> | <kind>:<name> for kinds with text: true")
    p.add_argument("--text", default="", help="the English description; empty clears it")
    p.add_argument("--approved", help="who approved it, when, in their words (required)")
    p.set_defaults(func=cmd_describe)

    sub.add_parser("providers", help="list models and whether their credentials are present").set_defaults(func=cmd_providers)
    sub.add_parser("doctor", help="check config, paths and providers").set_defaults(func=cmd_doctor)
    return parser


def main():
    args = build_parser().parse_args()
    cfg = Config(args.config)
    if cfg.missing and args.command != "init":
        raise SystemExit(f"config not found: {cfg.path}\nrun `{Path(__file__).name} --config {args.config} init` first")
    if args.root:
        cfg.root = Path(args.root).expanduser().resolve()
    writes = args.command in WRITE_COMMANDS and not (args.command == "inspire" and args.action == "list")
    if writes:
        save_version(cfg, f"手动改动（{args.command} 前自动存）")
    try:
        args.func(cfg, args)
    except ProviderError as exc:
        raise SystemExit(f"error: {exc}")
    finally:
        if writes:
            save_version(Config(cfg.path) if cfg.missing else cfg, f"自动存：{_command_line(args)}")


if __name__ == "__main__":
    main()
