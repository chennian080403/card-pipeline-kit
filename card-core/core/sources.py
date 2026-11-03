"""True-source kinds (ecosystem contract v1.4): which sections sources.yaml has, declared in project.yaml.

    sources:
      kinds:
        scenes: {label: 场景, as: scene}                     # a new kind: cards write scenes: [<名>]
        moods:  {label: 镜头语言, as: lens, text: true}      # text: the entry also carries a description

Defaults are characters / props / styles; the project's ``sources.kinds`` adds kinds or relabels them
(it cannot remove the defaults). The key is both the sources.yaml section and the card field.

- ``label``: the Chinese name shown on pages and used as the top-level pack.
- ``as``: the singular used by ``promote --as <as>:<名>`` (the section key works too).
- ``text``: the entry carries a ``prompt`` description that is spliced into prompts. Video sends
  text kinds as words only; image sends the sample image as well.
- ``role``: default reference-image role; ``{name}`` is replaced by the entry's name.

``characters`` is special in both skills (card items are dicts with name/lookup/position, a face
close-up, subject lines); every other kind is a plain list of names.
"""

from __future__ import annotations

DEFAULT_KINDS = {
    "characters": {"label": "人物", "as": "character", "text": False, "role": "identity, costume and look of {name}"},
    "props": {"label": "道具", "as": "prop", "text": False, "role": "the {name}"},
    "styles": {"label": "风格", "as": "style", "text": True,
               "role": "lighting, color and atmosphere reference only - ignore its people, objects and location"},
}


class KindError(ValueError):
    pass


def kinds(raw):
    """{section: {label, as, text, role}} -- defaults first, then the project's own, in file order."""
    out = {key: dict(value) for key, value in DEFAULT_KINDS.items()}
    declared = ((raw or {}).get("sources") or {}).get("kinds") or {}
    if not isinstance(declared, dict):
        raise KindError("sources.kinds must be a mapping {section: {label, as, text}}")
    for key, value in declared.items():
        key = str(key).strip()
        if not key or "/" in key or ":" in key:
            raise KindError(f"sources.kinds: bad section name {key!r}")
        value = dict(value or {})
        merged = dict(out.get(key) or {"label": key, "as": key, "text": False, "role": "the {name}"})
        merged.update({k: v for k, v in value.items() if v is not None})
        merged["text"] = bool(merged.get("text"))
        out[key] = merged
    seen = {}
    for key, value in out.items():
        token = str(value.get("as") or key)
        if token in seen and seen[token] != key:
            raise KindError(f"sources.kinds: {seen[token]} and {key} both use as: {token}")
        seen[token] = key
    return out


def section_for(kind_map, token):
    """``promote --as`` token (singular ``as`` or the section key) -> section, or None."""
    for key, value in kind_map.items():
        if token in (key, value.get("as")):
            return key
    return None


def role_for(kind_map, section, name, entry=None):
    role = (entry or {}).get("role") if isinstance(entry, dict) else None
    template = role or (kind_map.get(section) or {}).get("role") or "the {name}"
    return str(template).replace("{name}", str(name))


def card_names(card, section):
    """Names a card lists under one kind (items may be plain names or {name: …, lookup: …})."""
    out = []
    for item in card.get(section) or []:
        name = (item.get("lookup") or item.get("name")) if isinstance(item, dict) else item
        if name:
            out.append(str(name))
    return out
