"""Asset references -- how a video card points at images, videos and registry entries.

Forms (ecosystem contract v1, see references/ecosystem-contract.md):

    path/to/file.png            relative to project root, or absolute
    https://...                 URL, passed through
    source:<section>/<name>     shared registry (sources.yaml); section = a kind of sources.kinds
                                (default characters | props | styles, contract v1.4)
    source:<name>               shorthand for source:characters/<name>
    image:<card>                image-card-pipeline: the first picked candidate of that card
    image:<card>/<candidate>    a specific candidate (name or prefix)
    library:<id>                an entry of a local library (library.json)
    take:<card>/<take>          a take of this video project (video-to-video references)

Each resolved ref carries its ``origin`` so the take records where every input came from.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import yaml

from core import library

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
VIDEO_EXT = {".mp4", ".webm", ".mov"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac"}


class RefError(RuntimeError):
    pass


def is_url(value):
    return isinstance(value, str) and value.startswith(("http://", "https://", "data:"))


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def media_kind(path):
    suffix = Path(str(path)).suffix.lower()
    if suffix in VIDEO_EXT:
        return "video"
    if suffix in AUDIO_EXT:
        return "audio"
    return "image"


def _ref(path, label, role, origin):
    path = Path(path)
    if not path.is_file():
        raise RefError(f"{label}: file not found {path}")
    return {"path": str(path), "label": label, "role": role or "reference", "origin": origin,
            "sha256": sha256(path), "media": media_kind(path)}


# --------------------------------------------------------------------------- #
# shared registry
# --------------------------------------------------------------------------- #

def load_sources(cfg):
    if cfg.sources_file.is_file():
        return yaml.safe_load(cfg.sources_file.read_text(encoding="utf-8")) or {}
    return {}


def source_entry(cfg, section, name):
    registry = load_sources(cfg)
    entry = (registry.get(section) or {}).get(name)
    if not entry:
        raise RefError(f"source:{section}/{name} is not registered in {cfg.sources_file}")
    if isinstance(entry, str):
        entry = {"source": entry}
    return entry


def _source_ref(cfg, value, label, role):
    body = value[len("source:"):]
    section, _, name = body.partition("/")
    if section not in cfg.kinds or not name:
        section, name = "characters", body
    entry = source_entry(cfg, section, name)
    if not entry.get("source"):
        raise RefError(f"source:{section}/{name} has no source file")
    if is_url(entry["source"]):
        return {"url": entry["source"], "label": label, "role": role or entry.get("role") or name,
                "origin": {"kind": "source", "section": section, "name": name}, "media": "image"}
    return _ref(cfg.resolve(entry["source"]), label, role or entry.get("role"),
                {"kind": "source", "section": section, "name": name})


# --------------------------------------------------------------------------- #
# image-card-pipeline link (read-only: we only read its files, never write them)
# --------------------------------------------------------------------------- #

class ImageProject:
    """Just enough of an image-card-pipeline project to read picks and candidates."""

    def __init__(self, path):
        self.path = Path(path)
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        base = (raw.get("project") or {}).get("root")
        self.root = (self.path.parent / os.path.expanduser(os.path.expandvars(base))).resolve() if base else self.path.parent
        paths = raw.get("paths") or {}
        self.output = self._abs(paths.get("output", "output"))
        self.cards = self._abs(paths.get("cards", "cards"))
        self.library = dict(raw.get("library") or {"kind": "none"})
        if self.library.get("root"):
            self.library["root"] = str(self._abs(self.library["root"]))

    def _abs(self, value):
        path = Path(os.path.expanduser(os.path.expandvars(str(value))))
        return path if path.is_absolute() else (self.root / path).resolve()

    def review(self, card_id):
        path = self.output / "review" / f"{card_id}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def candidate(self, name):
        directory = self.output / "candidates"
        exact = directory / name
        if exact.is_file():
            return exact
        matches = sorted(p for p in directory.glob(f"{name}*") if p.suffix.lower() in IMAGE_EXT)
        return matches[0] if matches else None

    def card_exists(self, card_id):
        return any(self.cards.glob(f"{card_id}_*.yaml")) or (self.cards / f"{card_id}.yaml").is_file()


def image_project(cfg):
    path = cfg.image_config_path()
    if not path:
        raise RefError("image:<card> needs a linked image-card-pipeline project "
                       "(video.link.image_config, or put the video section into the image project.yaml)")
    return ImageProject(path)


def _image_ref(cfg, value, label, role):
    body = value[len("image:"):]
    card_id, _, candidate = body.partition("/")
    project = image_project(cfg)
    if candidate:
        path = project.candidate(candidate)
        if not path:
            raise RefError(f"{value}: candidate not found in {project.output / 'candidates'}")
    else:
        picks = project.review(card_id).get("pick") or []
        if not picks:
            raise RefError(f"{value}: image card {card_id} has no pick yet (pick it in the image pipeline first)")
        path = project.candidate(picks[0])
        if not path:
            raise RefError(f"{value}: picked candidate {picks[0]} is missing on disk")
        candidate = picks[0]
    return _ref(path, label, role, {"kind": "image", "card": card_id, "candidate": Path(str(candidate)).stem,
                                    "project": str(project.path)})


def _library_ref(cfg, value, label, role):
    entry_id = value[len("library:"):]
    for spec in _libraries(cfg):
        if spec.get("kind") != "local":
            continue
        for entry in library.local_load(spec)["entries"]:
            if entry["id"] == entry_id:
                path = Path(spec["root"]) / entry["file"]
                return _ref(path, label, role, {"kind": "library", "id": entry_id, "status": entry.get("status")})
    raise RefError(f"{value}: no such entry in a local library (http libraries: use the file path of the final copy)")


def _take_ref(cfg, value, label, role):
    body = value[len("take:"):]
    _card, _, take = body.partition("/")
    take = take or _card
    matches = sorted(cfg.takes_dir.glob(f"{take}*.mp4"))
    if not matches:
        raise RefError(f"{value}: take not found in {cfg.takes_dir}")
    return _ref(matches[0], label, role, {"kind": "take", "take": matches[0].stem})


def resolve(cfg, value, label, role=None):
    if not value:
        raise RefError(f"{label}: empty reference")
    value = str(value).strip()
    if is_url(value):
        return {"url": value, "label": label, "role": role or "reference", "origin": {"kind": "url"},
                "media": media_kind(value.split("?")[0])}
    if value.startswith("source:"):
        return _source_ref(cfg, value, label, role)
    if value.startswith("image:"):
        return _image_ref(cfg, value, label, role)
    if value.startswith("library:"):
        return _library_ref(cfg, value, label, role)
    if value.startswith("take:"):
        return _take_ref(cfg, value, label, role)
    path = cfg.resolve(value)
    return _ref(path, label, role, {"kind": "file"})


# --------------------------------------------------------------------------- #
# "only finals may be first frames"
# --------------------------------------------------------------------------- #

def _libraries(cfg):
    specs = []
    if library.enabled(cfg.library):
        specs.append(cfg.library)
    image_path = cfg.image_config_path()
    if image_path and image_path != cfg.path:
        try:
            spec = ImageProject(image_path).library
            if library.enabled(spec) and spec not in specs:
                specs.append(spec)
        except (OSError, yaml.YAMLError):
            pass
    return specs


def _final_dirs(cfg):
    return [cfg.resolve(d) for d in (cfg.rules.get("final_dirs") or [])]


def final_status(cfg, ref):
    """(is_final, why, final_copy_path). Looks for the same bytes in a final place."""
    if "path" not in ref:
        return False, "URL refs cannot be checked for final status", None
    path = Path(ref["path"]).resolve()
    for directory in _final_dirs(cfg):
        if str(path).startswith(str(directory) + os.sep):
            return True, f"under final dir {directory}", path
    for spec in _libraries(cfg):
        if spec.get("kind") != "local":
            continue
        for entry in library.local_load(spec)["entries"]:
            if entry.get("sha256") == ref["sha256"] and entry.get("status") == "final":
                return True, f"local library entry {entry['id']} is final", Path(spec["root"]) / entry["file"]
    copy = find_copy(ref["sha256"], path.stat().st_size, _final_dirs(cfg))
    if copy:
        return True, f"same bytes finalised at {copy}", copy
    return False, "not finalised (no final library entry, not in rules.final_dirs)", None


def find_copy(digest, size, directories, limit=20000):
    """Find a file with these bytes under the given directories (size pre-filter keeps it cheap)."""
    seen = 0
    for directory in directories:
        if not directory.is_dir():
            continue
        for root, _dirs, files in os.walk(directory):
            for name in files:
                seen += 1
                if seen > limit:
                    return None
                candidate = Path(root) / name
                try:
                    if candidate.stat().st_size == size and sha256(candidate) == digest:
                        return candidate
                except OSError:
                    continue
    return None
