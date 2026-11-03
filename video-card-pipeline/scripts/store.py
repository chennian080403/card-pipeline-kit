"""On-disk records of a video project: the job manifest, takes, review files."""

from __future__ import annotations

import json

from common import die, read_json, write_json
from core import jobs, jsonio

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


def take_name(card_id, alias, n):
    return f"{card_id}_{alias}_{n}"


def take_meta_path(cfg, name):
    return cfg.takes_dir / f"{name}.json"


def load_take(cfg, name):
    path = take_meta_path(cfg, name)
    if not path.is_file():
        matches = sorted(cfg.takes_dir.glob(f"{name}*.json"))
        if not matches:
            die(f"no take {name} in {cfg.takes_dir}")
        path = matches[0]
    return json.loads(path.read_text(encoding="utf-8"))


def save_take(cfg, meta):
    write_json(take_meta_path(cfg, meta["name"]), meta)


def card_takes(cfg, card_id):
    takes = []
    for path in sorted(cfg.takes_dir.glob(f"{card_id}_*.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        if meta.get("card") == card_id:
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

