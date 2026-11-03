"""Review-station protocol (card-core) -- one review station, many front ends.

The human reviews a take whole or segment by segment. Each review is

    {verdict: good | bad | discard | "", liked: "...", disliked: "...", by, at}

``good`` = 满意, ``bad`` = 不满意, ``discard`` = 放弃 (drop it from the next cut), "" = withdraw.
Segment 0 is the whole take; segments 1..n come from the take's ``segments``
(an assembled film has one per shot; a plain take has just one).

Writes go through a gate, the same as the screening room and the asset library:

- the write token handed out by the catalog (``X-Review-Token``)         -> 403 when missing / wrong
- an actor (``X-Actor``): ``user@browser`` or ``human:<name>``          -> 400 when missing / malformed,
  agents (``claude-code:…``, ``agent:…``) are refused: verdicts are the human's      403
- the catalog revision the page was looking at                          -> 409 when someone wrote since
- every accepted write is appended to an activity log (never rewritten)

``aggregate`` turns the stored reviews into what the pipeline reads back on `sync`:

    {verdict: pass | fail | borderline, discard, note, marks, by, at, segments: [{n, verdict, liked, disliked}]}
"""

from __future__ import annotations

import re
import secrets

VERDICTS = ("good", "bad", "discard")
PIPELINE = {"good": "pass", "bad": "fail", "discard": "fail"}
HUMAN_ACTOR = re.compile(r"^(user@browser|human:[\w.\-一-鿿]{1,40})$")
AGENT_ACTOR = re.compile(r"^(claude-code|agent|ai|codex|pi)[:@]", re.I)


class StationError(RuntimeError):
    """Bad request (400)."""
    code = 400


class Forbidden(StationError):
    code = 403


class Conflict(StationError):
    code = 409


def new_token():
    return secrets.token_urlsafe(18)


def check_write(*, token, expected_token, actor, revision, current_revision):
    """Raise the matching error when a write must be refused."""
    if not token or token != expected_token:
        raise Forbidden("missing or wrong X-Review-Token (reload the page)")
    actor = (actor or "").strip()
    if AGENT_ACTOR.match(actor):
        raise Forbidden(f"actor {actor}: verdicts come from the human only; agents record their own check with `review`")
    if not HUMAN_ACTOR.match(actor):
        raise StationError("missing or malformed X-Actor (user@browser or human:<name>)")
    try:
        revision = int(revision)
    except (TypeError, ValueError):
        raise StationError("revision missing") from None
    if revision != int(current_revision):
        raise Conflict(f"revision {revision} is stale (now {current_revision}): someone reviewed meanwhile, reload")
    return actor


def clean_review(body):
    verdict = str(body.get("verdict") or "")
    if verdict and verdict not in VERDICTS:
        raise StationError(f"verdict must be one of {VERDICTS} or empty")
    return {"verdict": verdict, "liked": str(body.get("liked") or "")[:2000],
            "disliked": str(body.get("disliked") or "")[:2000]}


def aggregate(overall, parts, *, marks=(), note="", dropped=False):
    """Fold whole-take and segment reviews into one pipeline verdict.

    overall: review of the whole take (segment 0) or None; parts: [(n, review)] for segments 1..n.
    The whole-take verdict wins; otherwise all good -> pass, bad without good -> fail, mixed -> borderline.
    A review may carry ``legacy`` (a pipeline verdict from an older format, e.g. "borderline") that stands
    when it has no verdict of its own. Liked / disliked text comes back even without a verdict.
    Returns None while the human has said nothing."""
    def said(r):
        return bool(r and (r.get("verdict") or r.get("legacy") or r.get("liked") or r.get("disliked")))

    overall = overall if said(overall) else None
    parts = [(n, r) for n, r in parts if said(r)]
    if not overall and not parts and not dropped and not note and not marks:
        return None
    judged = [(n, r) for n, r in parts if r.get("verdict")]
    if overall and overall.get("verdict"):
        verdict = PIPELINE[overall["verdict"]]
    elif judged:
        found = {PIPELINE[r["verdict"]] for _, r in judged}
        verdict = "pass" if found == {"pass"} else ("fail" if "pass" not in found else "borderline")
    elif overall and overall.get("legacy"):
        verdict = overall["legacy"]
    elif dropped:
        verdict = "fail"
    else:
        verdict = None
    lines, out_marks = [], list(marks)
    for label, review in ([("整条", overall)] if overall else []) + [(f"第{n}段", r) for n, r in parts]:
        if review.get("liked"):
            lines.append(f"{label}满意：{review['liked']}")
        if review.get("disliked"):
            lines.append(f"{label}不满意：{review['disliked']}")
    if note:
        lines.append(note)
    reviews = [r for r in [overall] + [r for _, r in parts] if r]
    latest = max(reviews, key=lambda r: r.get("at") or "", default={})
    discard = dropped or any(r.get("verdict") == "discard" for r in reviews)
    return {"verdict": verdict, "discard": bool(discard), "note": "\n".join(lines), "marks": out_marks,
            "by": latest.get("by") or "", "at": latest.get("at") or "",
            "segments": [{"n": n, "verdict": r.get("verdict", ""), "liked": r.get("liked", ""),
                          "disliked": r.get("disliked", "")} for n, r in parts]}
