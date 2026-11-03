# card-core copy -- do not edit here: edit card-core/core (next to this skill), then run card-core/sync.py
"""Keyframes of a video card (ecosystem contract v1.6): the few finalised images a shot is built on.

    first_frame: image:01-01            # shorthand: the keyframe at 0s
    keyframes:                          # any number, in time order
      - image:01-02                     # no `at`: spread evenly between its neighbours
      - {ref: library:3fa2c1d0, at: 3}  # `at` in seconds
    last_frame: image:01-03             # shorthand: the keyframe at the end (duration)

``first_frame`` / ``last_frame`` are just the two ends of the same list. Without them the first
item of ``keyframes`` sits at 0s and (with two or more) the last at the end. Each keyframe is a
key reference image: when ``video.rules.keyframes: final`` every one must be finalised in the library.

Video uses this to resolve and send keyframes; image reads video cards with it (read-only) to know
which library entries are some shot's keyframe.
"""

from __future__ import annotations


class KeyframeError(ValueError):
    pass


def _value(item):
    if isinstance(item, dict):
        return item.get("ref") or item.get("image") or ""
    return item


def timeline(card):
    """[{value, at, slot, label}] in time order; slot = first | middle | last. Raises KeyframeError."""
    duration = float(card.get("duration") or 5)
    items = []
    if card.get("first_frame"):
        items.append({"value": str(card["first_frame"]).strip(), "at": 0.0, "label": "first_frame"})
    listed = card.get("keyframes") or []
    if not isinstance(listed, list):
        raise KeyframeError("keyframes must be a list (each item a ref, or {ref, at})")
    for index, item in enumerate(listed):
        value = str(_value(item) or "").strip()
        if not value:
            raise KeyframeError(f"keyframes[{index}] has no ref")
        at = item.get("at") if isinstance(item, dict) else None
        try:
            at = None if at in (None, "") else float(at)
        except (TypeError, ValueError) as error:
            raise KeyframeError(f"keyframes[{index}].at must be seconds, got {at!r}") from error
        items.append({"value": value, "at": at, "label": f"keyframes[{index}]"})
    if card.get("last_frame"):
        items.append({"value": str(card["last_frame"]).strip(), "at": duration, "label": "last_frame"})
    if not items:
        return []
    if items[0]["at"] is None:
        items[0]["at"] = 0.0
    if len(items) > 1 and items[-1]["at"] is None:
        items[-1]["at"] = duration
    # the rest without `at`: evenly between the nearest known neighbours
    n = 0
    while n < len(items):
        if items[n]["at"] is not None:
            n += 1
            continue
        end = next(k for k in range(n, len(items)) if items[k]["at"] is not None)
        start_at, end_at, gap = items[n - 1]["at"], items[end]["at"], end - n + 1
        for k in range(n, end):
            items[k]["at"] = round(start_at + (end_at - start_at) * (k - n + 1) / gap, 3)
        n = end
    for before, after in zip(items, items[1:]):
        if after["at"] <= before["at"]:
            raise KeyframeError(f"keyframes out of order: {after['label']} at {after['at']:g}s "
                                f"is not after {before['label']} at {before['at']:g}s")
    for item in items:
        if not 0 <= item["at"] <= duration:
            raise KeyframeError(f"{item['label']} at {item['at']:g}s is outside the shot (0–{duration:g}s)")
        item["slot"] = "first" if item["at"] == 0 else "last" if item["at"] == duration else "middle"
    return items


def _raw_items(card):
    """Every ref a card names as a keyframe, without checking the timeline."""
    found = []
    if card.get("first_frame"):
        found.append({"value": str(card["first_frame"]).strip(), "label": "first_frame"})
    listed = card.get("keyframes")
    for index, item in enumerate(listed if isinstance(listed, list) else []):
        value = str(_value(item) or "").strip()
        if value:
            found.append({"value": value, "label": f"keyframes[{index}]"})
    if card.get("last_frame"):
        found.append({"value": str(card["last_frame"]).strip(), "label": "last_frame"})
    return found


def values(card):
    """Just the refs, for readers that only need to know which images a card uses as keyframes.

    A card whose timeline is broken still lists its refs (with ``error`` set on each item), so the image
    side keeps marking and warning about them instead of silently forgetting the card.
    """
    try:
        return [(item["value"], item) for item in timeline(card)]
    except KeyframeError as error:
        return [(item["value"], {**item, "error": str(error)}) for item in _raw_items(card)]
