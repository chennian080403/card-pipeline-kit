"""Project config for video-card-pipeline.

One ``project.yaml`` may serve both skills of the ecosystem: the top-level keys
``project`` / ``paths.sources`` / ``library`` are shared with image-card-pipeline,
everything video-specific lives under ``video:`` (image-card-pipeline ignores it).
A video-only project simply has no image keys. See references/ecosystem-contract.md.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from core import sources as sourcekinds

SKILL_ROOT = Path(__file__).resolve().parents[1]


class ConfigError(RuntimeError):
    pass


def _expand(value):
    """Recursively expand ${ENV} / ~ in config strings."""
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, str):
        return os.path.expanduser(os.path.expandvars(value))
    return value


class Config:
    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.missing = not self.path.is_file()
        raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) if not self.missing else {}
        self.raw = _expand(raw or {})
        project = self.raw.get("project") or {}
        self.name = project.get("name") or self.path.stem
        base = project.get("root")
        self.root = (self.path.parent / base).resolve() if base else self.path.parent
        self.video = self.raw.get("video") or {}
        self.paths = self.video.get("paths") or {}
        self.providers = (self.video.get("providers") or {}).get("entries") or {}
        self.default_provider = (self.video.get("providers") or {}).get("default")
        self.models = self.video.get("models") or {}
        self.rules = self.video.get("rules") or {}
        self.hooks = self.video.get("hooks") or {}
        self.station = self.video.get("station") or {"kind": "builtin"}
        self.board = self.video.get("board") or {"kind": "none"}
        self.link = self.video.get("link") or {}
        self.tools = self.video.get("tools") or {}
        self.library = dict(self.raw.get("library") or {"kind": "none"})
        if self.library.get("root"):
            self.library["root"] = str(self.resolve(self.library["root"]))

    # ------------------------------------------------------------------ paths
    def resolve(self, value):
        path = Path(os.path.expanduser(str(value)))
        return path if path.is_absolute() else (self.root / path).resolve()

    def dir(self, key, default):
        return self.resolve(self.paths.get(key, default))

    @property
    def cards_dir(self):
        return self.dir("cards", "video-cards")

    @property
    def cuts_dir(self):
        return self.dir("cuts", "cuts")

    @property
    def blocks_dir(self):
        return self.dir("blocks", "video-blocks")

    @property
    def output_dir(self):
        return self.dir("output", "video-output")

    @property
    def takes_dir(self):
        return self.output_dir / "takes"

    @property
    def sources_file(self):
        """Shared registry (top-level paths.sources), the same file image-card-pipeline promotes into."""
        value = (self.raw.get("paths") or {}).get("sources") or self.paths.get("sources") or "sources.yaml"
        return self.resolve(value)

    @property
    def ffmpeg(self):
        return self.tools.get("ffmpeg") or "ffmpeg"

    # ----------------------------------------------------------------- models
    def model_aliases(self):
        return {alias: entry for alias, entry in self.models.items() if isinstance(entry, dict)}

    def default_model(self):
        return self.models.get("default") or next(iter(self.model_aliases()), None)

    def model_spec(self, alias):
        entry = self.models.get(alias)
        if not isinstance(entry, dict):
            raise ConfigError(f"model alias '{alias}' is not defined in video.models")
        entry = dict(entry)
        provider_name = entry.pop("provider", None) or self.default_provider
        if not provider_name or provider_name not in self.providers:
            raise ConfigError(f"model alias '{alias}' points at unknown provider '{provider_name}'")
        spec = dict(self.providers[provider_name])
        spec["name"] = provider_name
        spec["alias"] = alias
        spec.update(entry)
        spec.setdefault("model", alias)
        return spec

    # ----------------------------------------------------------- image link
    def image_config_path(self):
        """The image-card-pipeline project this video project reads from, or None.

        ``video.link.image_config`` wins; otherwise the same file counts as linked
        when it also carries image keys (top-level ``paths.cards``)."""
        value = self.link.get("image_config")
        if value:
            path = self.resolve(value)
            return path if path.is_file() else None
        if (self.raw.get("paths") or {}).get("cards"):
            return self.path
        return None

    @property
    def kinds(self):
        """True-source kinds (contract v1.4): ``sources.kinds`` here, else in the linked image project.yaml."""
        raw = self.raw
        if not (raw.get("sources") or {}).get("kinds"):
            linked = self.image_config_path()
            if linked and linked != self.path:
                raw = yaml.safe_load(linked.read_text(encoding="utf-8")) or {}
        try:
            return sourcekinds.kinds(raw)
        except sourcekinds.KindError as error:
            raise ConfigError(str(error)) from error

    def image_skill_dir(self):
        value = self.link.get("image_skill") or os.getenv("IMAGE_CARD_SKILL")
        if value:
            return Path(os.path.expanduser(value))
        guess = SKILL_ROOT.parent / "image-card-pipeline"     # installed side by side
        return guess if (guess / "scripts" / "pipeline.py").is_file() else None
