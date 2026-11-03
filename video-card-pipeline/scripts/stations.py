"""Review stations -- where the human watches takes and gives verdicts, notes and time marks.

The pipeline hands takes to a station and later pulls verdicts back (`sync`).
A station never decides for the human; it only stores what the human said.
The review protocol (whole take / segments, good-bad-discard + liked / disliked,
token + actor + revision gate, activity log) is card-core's ``core.station``,
shared by every station.

    station:
      kind: builtin                 # skill's own review page (review_web.py), no service needed
      auto_submit: true             # send every new take to the station right away

    station:
      kind: plugin                  # your own screening room
      module: templates/station_plugin.example.py   # template: a folder people review in
      ...                           # anything else is passed to the plugin

A plugin module defines ``make_station(cfg, spec)`` returning an object with:

    submit(take) -> dict            # take = the take's meta; returns a station ref (stored on the take)
    fetch(take, ref) -> dict|None   # core.station.aggregate(...) output:
                                    # {verdict: pass|fail|borderline, discard, note, marks: [{t, text}], by, at, segments}
    describe() -> str
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from core import station as protocol
from core.jsonio import append_jsonl, locked, now, read_json, write_json

VERDICTS = ("pass", "fail", "borderline")          # the AI's own check (`review`); humans use protocol.VERDICTS
StationError = protocol.StationError
FORMAT = "video-card-pipeline/station-v2"
_V1 = {"pass": "good", "fail": "bad"}                # v1 "borderline" has no v2 verdict: kept as legacy


def take_segments(take):
    """Segments 1..n of a take: one per shot of an assembled film, else just the whole take."""
    shots = take.get("shots") or []
    if len(shots) > 1:
        return [{"n": i, "label": Path(s.get("file", "")).stem[-28:] or f"第{i}段",
                 "start": s.get("start", 0), "end": s.get("end", 0)} for i, s in enumerate(shots, 1)]
    duration = (take.get("probe") or {}).get("duration") or 0
    return [{"n": 1, "label": take.get("title") or take.get("card", ""), "start": 0, "end": duration}]


class BuiltinStation:
    """Takes and reviews in <output>/station/station.json; the page is review_web.py."""

    def __init__(self, cfg, spec):
        self.cfg = cfg
        self.spec = spec
        self.dir = cfg.output_dir / "station"
        self.path = self.dir / "station.json"
        self.log = self.dir / "activity-log.jsonl"

    def load(self):
        data = read_json(self.path, {"format": FORMAT, "revision": 0, "takes": {}})
        if data.get("format") != FORMAT:                       # station-v1: one verdict per take
            for entry in data.get("takes", {}).values():
                if entry.get("verdict") or entry.get("discard"):
                    verdict = "discard" if entry.get("discard") else _V1.get(entry["verdict"], "")
                    review = {"verdict": verdict, "liked": "", "disliked": "",
                              "by": entry.get("by", ""), "at": entry.get("at", "")}
                    if not verdict:
                        review["legacy"] = entry["verdict"]
                    entry.setdefault("reviews", {})["0"] = review
                for key in ("verdict", "discard"):
                    entry.pop(key, None)
            data.update(format=FORMAT, revision=data.get("revision", 0))
        return data

    def save(self, data):
        write_json(self.path, data)

    def describe(self):
        return f"builtin review page (python3 scripts/review_web.py --config {self.cfg.path}) · {self.path}"

    def submit(self, take):
        with locked(self.path):     # across processes: generate in one session, the review page in another
            data = self.load()
            entry = data["takes"].setdefault(take["name"], {"submitted_at": now(), "reviews": {}})
            entry.update({"card": take["card"], "file": take["file"], "title": take.get("title", ""),
                          "group": take.get("group", ""), "segments": take_segments(take)})
            self.save(data)        # revision guards reviews only: a new take must not 409 someone mid-review
        return {"station": "builtin", "key": take["name"]}

    def fetch(self, take, ref):
        entry = self.load()["takes"].get(ref.get("key") or take["name"]) or {}
        reviews = entry.get("reviews") or {}
        segments = entry.get("segments") or []
        parts = [(s["n"], reviews.get(str(s["n"]))) for s in segments] if len(segments) > 1 else []
        return protocol.aggregate(reviews.get("0"), parts, marks=entry.get("marks") or [], note=entry.get("note") or "")

    def record(self, name, body, *, token, expected_token, actor):
        """Called by the review page (the human), never by the pipeline. body: {revision, segment, verdict,
        liked, disliked[, note, marks]} -- note and time marks belong to the whole take (segment 0)."""
        with locked(self.path):
            data = self.load()
            actor = protocol.check_write(token=token, expected_token=expected_token, actor=actor,
                                         revision=body.get("revision"), current_revision=data["revision"])
            entry = data["takes"].get(name)
            if entry is None:
                raise StationError(f"take {name} was not submitted to this station")
            segment = int(body.get("segment") or 0)
            if segment and segment not in {s["n"] for s in entry.get("segments") or []}:
                raise StationError(f"take {name} has no segment {segment}")
            review = protocol.clean_review(body)
            reviews = entry.setdefault("reviews", {})
            previous = reviews.get(str(segment))
            if previous:
                entry.setdefault("history", []).append({"segment": segment, **previous})
            reviews[str(segment)] = {**review, "by": actor, "at": now()}
            if segment == 0:
                if "note" in body:
                    entry["note"] = str(body.get("note") or "")[:4000]
                if "marks" in body:
                    entry["marks"] = [{"t": round(float(m["t"]), 2), "text": str(m.get("text", ""))[:500]}
                                      for m in body.get("marks") or []]
            data["revision"] += 1
            self.save(data)
            append_jsonl(self.log, {"at": now(), "actor": actor, "take": name, "segment": segment, **review,
                                    "note": entry.get("note", "") if segment == 0 else "",
                                    "marks": len(entry.get("marks") or []) if segment == 0 else 0,
                                    "revision": data["revision"]})
            return entry, data["revision"]


def load_plugin(cfg, spec):
    module_path = cfg.resolve(spec["module"]) if not Path(spec["module"]).is_absolute() else Path(spec["module"])
    if not module_path.is_file():
        from config import SKILL_ROOT
        module_path = SKILL_ROOT / spec["module"]
    if not module_path.is_file():
        raise StationError(f"station plugin not found: {spec['module']}")
    loader = importlib.util.spec_from_file_location(f"station_{module_path.stem}", module_path)
    module = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(module)
    if not hasattr(module, "make_station"):
        raise StationError(f"{module_path} has no make_station(cfg, spec)")
    return module.make_station(cfg, spec)


def make(cfg):
    spec = cfg.station or {"kind": "builtin"}
    kind = spec.get("kind", "builtin")
    if kind == "builtin":
        return BuiltinStation(cfg, spec)
    if kind == "plugin":
        return load_plugin(cfg, spec)
    if kind == "none":
        return None
    raise StationError(f"unknown station kind '{kind}'")
