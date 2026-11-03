"""Review-station plugin template: hand takes to your own review tool, read the human's verdicts back.

Copy this file into your project, point the config at it and adapt submit / fetch to your tool:

    video:
      station:
        kind: plugin
        module: station_plugin.py        # relative to project.yaml (or templates/station_plugin.example.py as is)
        inbox: 待审片                     # this template: a plain folder people review in

What this template does (works as is, no server needed):
- submit copies the take into <inbox>/<group>/<file> and writes <file>.review.json with empty fields;
- a person opens the folder, watches the clip and fills the json:
      {"verdict": "good" | "bad" | "discard", "liked": "...", "disliked": "...", "by": "张三"}
- fetch reads that json back; card-core's review protocol folds it into pass / fail / borderline.

Rules every plugin keeps: never write the human's verdicts yourself, never overwrite a clip,
and return protocol.aggregate(...) (or None while the human has said nothing).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from core import station as protocol   # scripts/ is on sys.path when vpipeline loads this plugin

StationError = protocol.StationError


class FolderStation:
    def __init__(self, cfg, spec):
        self.inbox = cfg.resolve(spec.get("inbox") or "待审片")

    def describe(self):
        return f"folder station · {self.inbox} (fill <clip>.review.json next to each clip)"

    def submit(self, take):
        folder = self.inbox / (take.get("group") or "未分组")
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / Path(take["file"]).name
        if target.exists() and target.stat().st_size != Path(take["file"]).stat().st_size:
            raise StationError(f"{target} exists with other content; not overwriting")
        if not target.exists():
            shutil.copy2(take["file"], target)
        sheet = target.with_name(target.name + ".review.json")
        if not sheet.exists():
            sheet.write_text(json.dumps({"verdict": "", "liked": "", "disliked": "", "by": "",
                                         "_card": take["card"], "_prompt": take.get("prompt", "")},
                                        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"station": "folder", "key": target.relative_to(self.inbox).as_posix()}

    def fetch(self, take, ref):
        sheet = self.inbox / (ref["key"] + ".review.json")
        if not sheet.is_file():
            return None
        data = json.loads(sheet.read_text(encoding="utf-8"))
        if data.get("verdict") and data["verdict"] not in ("good", "bad", "discard"):
            raise StationError(f"{sheet}: verdict must be good / bad / discard, got {data['verdict']!r}")
        if protocol.AGENT_ACTOR.match(data.get("by") or ""):
            raise StationError(f"{sheet}: verdicts come from the human only, not {data['by']!r}")
        overall = {key: data.get(key) or "" for key in ("verdict", "liked", "disliked", "by")}
        return protocol.aggregate(overall, [])


def make_station(cfg, spec):
    return FolderStation(cfg, spec)
