"""On-disk records of a video project: the job manifest, takes, review files."""

from __future__ import annotations

import json
from pathlib import Path

from common import die, read_json, write_json
from core import jobs, jsonio, naming

MANIFEST_FORMAT = "video-card-pipeline/manifest-v1"


def manifest_path(cfg):
    return cfg.output_dir / "manifest.json"


def load_manifest(cfg):
    return jobs.load(manifest_path(cfg), MANIFEST_FORMAT)


def save_manifest(cfg, manifest):
    jobs.save(manifest_path(cfg), manifest)


def update_manifest(cfg, change):
    """Lock -> re-read -> change(manifest) -> write; returns what change returned.

    Every write to the manifest goes through here (one job at a time), so two sessions running
    generate / collect / import never overwrite each other's jobs with an older copy."""
    with jsonio.locked(manifest_path(cfg)):
        manifest = load_manifest(cfg)
        result = change(manifest)
        save_manifest(cfg, manifest)
        return result


def update_job(cfg, key, **fields):
    """Merge fields into one existing job under the manifest lock; returns the job (None when it is gone,
    e.g. forgotten by another session meanwhile -- never re-created as a half record)."""
    def change(manifest):
        job = manifest["jobs"].get(key)
        if job is None:
            return None
        job.update(fields)
        return dict(job)
    return update_manifest(cfg, change)


def job_key(card_id, alias, n):
    return f"{card_id}|{alias}|{n}"


def take_name(card_id, alias, n, *, v2=False, policy=None, legacy_card=False):
    if v2:
        return Path(naming.build_media_name(card_id, alias, n, ext=".mp4", policy=policy,
                                            legacy_card=legacy_card)).stem
    return f"{card_id}_{alias}_{n}"


def take_meta_path(cfg, name):
    return cfg.takes_dir / f"{name}.json"


def load_take(cfg, name, card_id=None):
    if Path(name).name != name:
        die(f"invalid take name {name!r}")
    exact = Path(name).stem if Path(name).suffix.lower() in {".mp4", ".json"} else name
    path = take_meta_path(cfg, exact)
    if not path.is_file():
        matches = [p for p in sorted(cfg.takes_dir.glob("*.json")) if p.stem.startswith(exact)]
        if len(matches) != 1:
            die(f"{'ambiguous' if matches else 'no'} take {name} in {cfg.takes_dir}")
        path = matches[0]
    meta = json.loads(path.read_text(encoding="utf-8"))
    owner = meta.get("card_id") or meta.get("card")
    if (card_id is not None and owner != card_id or not owner or not path.stem.startswith(f"{owner}_")
            or path.stem != exact and exact != owner and not exact.startswith(f"{owner}_")):
        die(f"take {path.stem} does not belong to card {card_id or exact}")
    return meta


def save_take(cfg, meta):
    write_json(take_meta_path(cfg, meta["name"]), meta)


def card_takes(cfg, card_id):
    takes = []
    for path in sorted(cfg.takes_dir.glob(f"{card_id}_*.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        if (meta.get("card_id") or meta.get("card")) == card_id:
            takes.append(meta)
    return takes


def review_path(cfg, card_id):
    return cfg.output_dir / "review" / f"{card_id}.json"


def load_review(cfg, card_id):
    return read_json(review_path(cfg, card_id), {"card": card_id, "takes": {}, "pick": []})


def save_review(cfg, review):
    write_json(review_path(cfg, review["card"]), review)


def update_review(cfg, card_id, change):
    """Lock -> re-read -> change(review) -> write (sync / pick / review from two sessions do not clobber)."""
    path = review_path(cfg, card_id)
    with jsonio.locked(path):
        review = load_review(cfg, card_id)
        result = change(review)
        save_review(cfg, review)
        return result


def _ref_record(ref):
    if not ref:
        return None
    keep = ("label", "role", "origin", "sha256", "media", "path", "url", "final_copy")
    return {k: ref[k] for k in keep if k in ref}


def _refs_record(refs):
    return {k: [_ref_record(x) for x in v] if isinstance(v, list) else _ref_record(v) for k, v in refs.items()}

