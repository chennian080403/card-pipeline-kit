"""Making takes: generate / collect / import / forget, finalising a take, post hooks, station hand-off."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import media
import providers
import refs as refmod
import stations
from cards import card_fingerprint, collect_refs, compose_prompt, load_card, pick_mode, save_card, validate, v2_card_problems
from common import die, now
from config import SKILL_ROOT
from core import jobs, jsonio, naming
from store import (_refs_record, job_key, load_manifest, load_take, manifest_path, save_manifest, save_take,
                   take_meta_path, take_name, update_job, update_manifest)


def _free_take_name(cfg, name):
    """Never overwrite a clip: when <name>.mp4 / .json already exist (a forgotten job submitted again,
    an import onto a used number ...) the new take becomes <name>-2, -3 ..."""
    candidate, k = name, 1
    while (cfg.takes_dir / f"{candidate}.mp4").exists() or take_meta_path(cfg, candidate).exists():
        k += 1
        candidate = f"{name}-{k}"
    return candidate


def _reserve_take(cfg, card, model, manifest):
    card_id = card["id"]
    seq = max(naming.next_seq(manifest["jobs"], card_id, model),
              naming.next_seq(manifest.get("forgotten", ()), card_id, model),
              naming.next_seq((p.with_suffix(".mp4") for p in cfg.takes_dir.glob("*") if p.suffix in {".json", ".mp4"}),
                              card_id, model))
    name = take_name(card_id, model, seq, v2=True, policy=cfg.naming_policy,
                     legacy_card=card.get("schema_version") != 2)
    return {"card_id": card_id, "naming_version": 2, "canonical_model": naming.name_key(model),
            "seq": seq, "media_name": name + ".mp4"}


def finalize_take(cfg, key, info, card, *, post=()):
    """A provider said ok: put the file in takes/, describe it, sheet it, hand it to the station.

    The file is fetched into a hidden .part next to takes/ first (no lock held); then, under the
    manifest lock, the job is re-read -- when another session already finished it, this copy is
    dropped -- the clip gets a name nothing else uses, and only this one job is written back.
    Returns the take's meta, or None when someone else got there first."""
    job = load_manifest(cfg)["jobs"].get(key)
    if not job:
        print(f"  · {key} is no longer in the manifest (forgotten meanwhile?): nothing saved")
        return None
    cfg.takes_dir.mkdir(parents=True, exist_ok=True)
    base = Path(job["media_name"]).stem if job.get("media_name") else take_name(card["id"], job["model"], job["n"])
    staged = cfg.takes_dir / f".{base}.{os.getpid()}.part"
    try:
        if info.get("url"):
            media.download(info["url"], staged)
        else:
            source = Path(info["path"])
            if str(source).startswith(str(cfg.output_dir / "runs")):
                shutil.move(str(source), staged)
            else:
                shutil.copy2(source, staged)
        probe = media.probe(cfg.ffmpeg, staged)
        digest = refmod.sha256(staged)
        with jsonio.locked(manifest_path(cfg)):
            manifest = load_manifest(cfg)
            job = manifest["jobs"].get(key) or {}
            if job.get("state") not in ("submitted", "waiting_import"):
                print(f"  · {key} is already {job.get('state') or 'gone'} (another run finished it): this copy dropped")
                return None
            name = base if job.get("naming_version") == 2 else _free_take_name(cfg, base)
            if name != base:
                print(f"  ! {base}.mp4 already exists: this take is saved as {name} (nothing overwritten)")
            out = cfg.takes_dir / f"{name}.mp4"
            if job.get("naming_version") == 2:
                if take_meta_path(cfg, name).exists():
                    die(f"take metadata already exists: {name}")
                os.link(staged, out)
            else:
                os.replace(staged, out)
            meta = {
                "name": name, "file": str(out), "card": card["id"], "title": card.get("title", ""),
                "group": card.get("group", ""), "model": job["model"], "provider": job["provider"],
                "mode": info.get("mode") or job.get("mode"), "prompt": job["prompt"], "refs": job["refs"],
                "card_fingerprint": job.get("card_fingerprint"), "probe": probe, "sha256": digest,
                "run_dir": job.get("run_dir"), "at": now(), "derived_from": None, "sheet": None,
            }
            if job.get("naming_version") == 2:
                meta.update({field: job[field] for field in ("card_id", "canonical_model", "naming_version", "seq")}, job_key=key)
            save_take(cfg, meta)
            job.update(state="ok", take=name, finished_at=now())
            job.pop("error", None)
            save_manifest(cfg, manifest)
    finally:
        staged.unlink(missing_ok=True)
    sheet = media.frame_sheet(cfg.ffmpeg, out, cfg.output_dir / "sheets" / f"{name}.jpg", probe)
    if sheet:
        meta["sheet"] = str(sheet)
        save_take(cfg, meta)
    print(f"  ✓ take {name}  {probe['duration']}s {probe['width']}x{probe['height']}"
          + (f"  sheet {sheet}" if sheet else ""))
    if card.get("status") in (None, "draft", "confirmed"):
        card["status"] = "generated"
        save_card(card)
    takes = [meta] + [run_hook(cfg, meta, hook) for hook in post]
    for item in takes:
        if item and (cfg.station or {}).get("auto_submit", True):
            submit_to_station(cfg, item)
    return meta


def submit_to_station(cfg, meta):
    station = stations.make(cfg)
    if station is None:
        return None
    try:
        ref = station.submit(meta)
    except Exception as error:   # noqa: BLE001 -- the take is safe on disk; report and go on
        print(f"  ! station submit failed for {meta['name']}: {error}")
        return None
    meta["station"] = ref
    save_take(cfg, meta)
    print(f"  → station: {meta['name']} ({ref.get('key', '')})")
    return ref


# --------------------------------------------------------------------------- #
# hooks (post-processing: upscale, interpolation, denoise ... any command)
# --------------------------------------------------------------------------- #

def hook_spec(cfg, name):
    for hook in cfg.hooks.get("post") or []:
        if hook.get("name") == name:
            return hook
    die(f"no post hook '{name}' in video.hooks.post")


def run_hook(cfg, meta, hook):
    hook = hook_spec(cfg, hook) if isinstance(hook, str) else hook
    src = Path(meta["file"])
    name = f"{meta['name']}__{hook['name']}"
    out = cfg.takes_dir / f"{name}.mp4"
    if out.is_file():
        print(f"  {name} exists, not redone")
        return load_take(cfg, name)
    values = {"in": str(src), "out": str(out), "skill": str(SKILL_ROOT), "ffmpeg": cfg.ffmpeg,
              "config_dir": str(cfg.path.parent)}
    template = hook["command"]
    argv = [part.format(**values) for part in (template if isinstance(template, list) else shlex.split(template))]
    print(f"  ▶ hook {hook['name']}: {' '.join(argv)[:160]}")
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0 or not out.is_file():
        print(f"  ! hook {hook['name']} failed: {(proc.stderr or proc.stdout)[-400:]}")
        return None
    probe = media.probe(cfg.ffmpeg, out)
    derived = dict(meta, name=name, file=str(out), probe=probe, sha256=refmod.sha256(out), at=now(),
                   derived_from=meta["name"], hook=hook["name"], station=None)
    sheet = media.frame_sheet(cfg.ffmpeg, out, cfg.output_dir / "sheets" / f"{name}.jpg", probe)
    derived["sheet"] = str(sheet) if sheet else None
    save_take(cfg, derived)
    print(f"  ✓ derived take {name}  {probe['width']}x{probe['height']}")
    return derived if hook.get("submit", True) else None

def _new_run_dir(cfg, stem):
    """A fresh run folder per submission: a resubmit within the same second never shares the old record."""
    base = cfg.output_dir / "runs"
    base.mkdir(parents=True, exist_ok=True)
    for k in range(1, 1000):
        run_dir = base / (stem if k == 1 else f"{stem}-{k}")
        try:
            run_dir.mkdir()
            return run_dir
        except FileExistsError:
            continue
    die(f"too many runs named {stem}")


def _approval(cfg, provider, alias, args):
    try:
        return update_manifest(cfg, lambda manifest: jobs.approve(
            manifest, free=provider.free, required=cfg.rules.get("require_approval", True),
            approved=args.approved, record={"model": alias, "cards": args.cards, "budget": args.budget}))
    except jobs.JobError as error:
        die(f"{alias}: {error}")


def _claim(cfg, key, record, card, model):
    """Under the manifest lock: re-read the slot and, if it is still free to submit, write the record
    as state=submitting before anything is sent. Returns the step that stopped us, or None when claimed."""
    def change(manifest):
        step = jobs.next_step(manifest["jobs"].get(key))
        if step == "submit":
            if model:
                record.update(_reserve_take(cfg, card, model, manifest))
            manifest["jobs"][key] = dict(record, state="submitting")
            return None
        return step
    return update_manifest(cfg, change)


def cmd_generate(cfg, args):
    pending = []
    last_submit = 0.0
    for row, card_id in enumerate(args.cards):
        card = load_card(cfg, card_id)
        alias = args.model or card.get("model") or cfg.default_model()
        problems, notes, refs = validate(cfg, card, alias)
        if problems:
            die(f"{card_id} does not validate on {alias}: {problems}")
        spec = cfg.model_spec(alias)
        model = naming.resolve_model(alias, cfg.vocab, "video") if cfg.v2(card) else None
        if model:
            take_name(card["id"], model, 1, v2=True, policy=cfg.naming_policy,
                      legacy_card=card.get("schema_version") != 2)
        provider = providers.make(spec, cfg)
        approval = _approval(cfg, provider, alias, args)
        budget = args.budget or int(card.get("takes") or 1)
        prompt = compose_prompt(cfg, card)
        mode = pick_mode(card, refs)
        print(f"▶ {card_id} · {alias} ({spec['kind']}) · mode {mode} · budget {budget}")
        for note in notes:
            print(f"  note: {note}")
        for n in range(1, budget + 1):
            key = job_key(card_id, alias, n)
            job = load_manifest(cfg)["jobs"].get(key) or {}
            step = jobs.next_step(job)
            if step == "skip":
                print(f"  #{n} ok already ({job.get('take')}), skipped")
                continue
            if step == "wait":
                print(f"  #{n} {job['state']}: not resubmitted (use collect / import)")
                if job["state"] == "submitted":
                    pending.append(key)
                continue
            if step == "stop":
                _say_stopped(n, key, job)
                continue
            gap = float(spec.get("min_gap", 0))
            if gap and last_submit and time.time() - last_submit < gap:
                wait = gap - (time.time() - last_submit)
                print(f"  free tier pacing: waiting {wait:.0f}s")
                time.sleep(wait)
            run_dir = _new_run_dir(cfg, f"{naming.normalize_name(card_id)}-{uuid.uuid4().hex}" if model
                                   else f"{card_id}-{alias}-{n}-{time.strftime('%Y%m%d-%H%M%S')}")
            job_input = {"card": {k: v for k, v in card.items() if not k.startswith("_")}, "n": n, "row": row,
                         "prompt": prompt, "mode": mode, "refs": refs, "run_dir": str(run_dir),
                         "duration": card.get("duration", 5), "aspect": str(card.get("aspect", "16:9")),
                         "resolution": card.get("resolution")}
            (run_dir / "job.json").write_text(json.dumps({**job_input, "model": alias, "spec_kind": spec["kind"]},
                                                         ensure_ascii=False, indent=1, default=str), encoding="utf-8")
            record = {"card": card_id, "model": alias, "provider": spec["name"], "n": n, "mode": mode,
                      "prompt": prompt, "refs": _refs_record(refs),
                      "card_fingerprint": card_fingerprint(card, cfg), "run_dir": str(run_dir), "submitted_at": now(),
                      "approval": approval}
            taken = _claim(cfg, key, record, card, model)
            if taken:                      # another session took this slot between our read and the lock
                shutil.rmtree(run_dir, ignore_errors=True)
                print(f"  #{n} taken by another run meanwhile ({taken}): not submitted")
                continue
            try:
                handle = provider.submit(job_input)
            except providers.ProviderError as error:
                update_job(cfg, key, state="unknown" if error.uncertain else "failed", error=str(error),
                           finished_at=now())
                print(f"  #{n} {'UNKNOWN' if error.uncertain else 'failed'}: {error}")
                if error.uncertain:
                    break
                continue
            except BaseException as error:  # timeout, network, bad reply, Ctrl-C ...: maybe billed -> unknown, never resubmitted
                update_job(cfg, key, state="unknown", error=f"{type(error).__name__}: {error}", finished_at=now())
                print(f"  #{n} UNKNOWN ({type(error).__name__}: {error}): may have been submitted or billed. "
                      f"Look at {run_dir}; `forget {key} --why ...` only after checking.")
                raise
            last_submit = time.time()
            state, _ = provider.poll(handle) if spec["kind"] == "handoff" else ("submitted", None)
            update_job(cfg, key, state=state, handle=handle)
            if state == "waiting_import":
                print(f"  #{n} handoff package {handle['package']}\n     after generating on the site: "
                      f"import {card_id} <file.mp4> --model {alias}")
            else:
                print(f"  #{n} submitted")
                pending.append(key)
    if pending and not args.no_wait:
        collect(cfg, pending, timeout=args.timeout, post=args.post)
    elif pending:
        print(f"{len(pending)} job(s) running: `collect` later picks them up (nothing is resubmitted)")


def _say_stopped(n, key, job):
    if job.get("state") == "submitting":
        print(f"  #{n} SUBMITTING since {job.get('submitted_at')}: another run is submitting it now, or that run "
              f"was killed mid-submit (may be billed). Look at {job.get('run_dir')}; `forget {key} --why ...` "
              f"only when no run is busy with it. Not resubmitting.")
    else:
        print(f"  #{n} UNKNOWN: may have been produced or billed. Look at {job.get('run_dir')}; "
              f"`forget {key}` only after checking. Not resubmitting.")


def collect(cfg, keys=None, timeout=None, post=()):
    """Poll submitted jobs until done. Holds no copy of the manifest: each job is re-read before it is
    polled and written back alone, so a long collect never undoes what another session wrote meanwhile."""
    jobs_now = load_manifest(cfg)["jobs"]
    keys = keys or [k for k, j in jobs_now.items() if j.get("state") == "submitted"]
    keys = [k for k in keys if jobs_now.get(k, {}).get("state") == "submitted"]
    if not keys:
        print("nothing running")
        return
    timeout = float(timeout or cfg.rules.get("poll_timeout", 1200))
    deadline = time.time() + timeout
    cache = {}
    while keys and time.time() < deadline:
        intervals = []
        jobs_now = load_manifest(cfg)["jobs"]
        for key in list(keys):
            job = jobs_now.get(key) or {}
            if job.get("state") != "submitted":          # finished / forgotten by another run meanwhile
                keys.remove(key)
                continue
            try:
                if job["model"] not in cache:
                    cache[job["model"]] = providers.make(cfg.model_spec(job["model"]), cfg)
                provider = cache[job["model"]]
                intervals.append(provider.interval)
                state, info = provider.poll(job["handle"])
            except Exception as error:   # noqa: BLE001 -- transient poll errors keep the job alive
                state, info = "running", {"status": f"poll error: {error}"}
            if state == "running":
                continue
            keys.remove(key)
            if state == "ok":
                try:
                    finalize_take(cfg, key, info, load_card(cfg, job["card"]), post=post or ())
                except (Exception, SystemExit) as error:   # noqa: BLE001 -- one bad take must not stop the others
                    update_job(cfg, key, state="unknown", error=f"finalize failed: {type(error).__name__}: {error}",
                               finished_at=now())
                    print(f"  ✗ {key}: the provider finished but the take could not be saved ({error}); marked "
                          f"UNKNOWN (paid, file may still be at {info.get('url') or info.get('path')}). Going on.")
            else:
                update_job(cfg, key, state=state, error=info.get("error"), finished_at=now())
                print(f"  ✗ {key} {state}: {info.get('error')}")
        if keys:
            time.sleep(max(1, min(intervals or [5])))
    if keys:
        print(f"{len(keys)} job(s) still running after {timeout:.0f}s -- left as submitted, run `collect` again "
              f"(never resubmitted): {', '.join(keys)}")


def cmd_collect(cfg, args):
    manifest = load_manifest(cfg)
    keys = [k for k, j in manifest["jobs"].items() if j.get("state") == "submitted"
            and (not args.cards or j["card"] in args.cards)]
    collect(cfg, keys, timeout=args.timeout, post=args.post)


def _import_slot(cfg, card, alias_given):
    """Which job an import closes: --model X -> X's oldest waiting_import job; no --model -> the card's
    only waiting_import job (several -> the human picks with --model); none -> a new take."""
    manifest = load_manifest(cfg)
    controlled = naming.has_model_vocab(cfg.vocab, "video")
    canonical = naming.resolve_model(alias_given, cfg.vocab, "video") if alias_given and cfg.v2(card) else None
    def matches(job):
        if not alias_given or job["model"] == alias_given:
            return True
        if not canonical:
            return False
        try:
            return canonical == (job.get("canonical_model") or naming.resolve_model(job["model"], cfg.vocab, "video"))
        except naming.NamingError:
            return False

    waiting = sorted((j["n"], k, j["model"]) for k, j in manifest["jobs"].items()
                     if j["card"] == card["id"] and j.get("state") == "waiting_import" and matches(j))
    if alias_given:
        exact = [item for item in waiting if item[2] == alias_given]
        if exact:
            waiting = exact
        elif len({item[2] for item in waiting}) > 1:
            routes = ", ".join(sorted({item[2] for item in waiting}))
            die(f"{card['id']}: model {alias_given!r} matches several waiting routes ({routes}); give the route with --model")
        return (waiting[0][2], waiting[0][1]) if waiting else (alias_given, None)
    models = sorted({m for _, _, m in waiting})
    if len(models) > 1 or len(waiting) > 1:
        die(f"{card['id']} has {len(waiting)} handoff jobs waiting ({', '.join(k for _, k, _ in waiting)}): "
            f"say which with --model")
    if waiting:
        return waiting[0][2], waiting[0][1]
    if controlled:
        die(f"{card['id']}: no handoff job is waiting; --model is required and must be in the model vocabulary")
    return card.get("model") or cfg.default_model(), None


def cmd_import(cfg, args):
    """Close a handoff job (web platform), or bring in any outside clip as a new take.

    The source file is copied and left where it is (--move deletes it after a good import);
    files inside the project's output folder are refused (they belong to the pipeline already)."""
    card = load_card(cfg, args.card)
    problems = v2_card_problems(cfg, card)
    if problems:
        die("; ".join(problems))
    src = Path(args.file).expanduser()
    if not src.is_file():
        die(f"no such file {src}")
    output = cfg.output_dir.resolve()
    if output == src.resolve() or output in src.resolve().parents:
        die(f"{src} is inside the output folder {cfg.output_dir}: import a downloaded copy from elsewhere")
    alias, key = _import_slot(cfg, card, args.model)
    model = naming.resolve_model(alias or "import", cfg.vocab, "video") if cfg.v2(card) else None
    created = key is None
    if created:
        refs, _ = collect_refs(cfg, card, strict_final=False)
        spec = cfg.model_spec(alias) if alias in cfg.model_aliases() else {"name": "import"}
        record = {"card": card["id"], "model": alias, "provider": spec.get("name", "import"),
                  "mode": "import", "prompt": compose_prompt(cfg, card), "refs": _refs_record(refs),
                  "card_fingerprint": card_fingerprint(card, cfg), "submitted_at": now(), "state": "waiting_import"}

        def add(manifest):           # number picked under the lock: two imports never share one
            used = [j["n"] for j in manifest["jobs"].values() if j["card"] == card["id"] and j["model"] == alias]
            n = max(used, default=0) + 1
            fields = _reserve_take(cfg, card, model, manifest) if model else {}
            key = job_key(card["id"], f"import:{fields['canonical_model']}", fields["seq"]) if fields else job_key(card["id"], alias, n)
            manifest["jobs"][key] = {**record, **fields, "n": n}
            return key
        key = update_manifest(cfg, add)
    def mark(manifest):
        job = manifest["jobs"].get(key)
        if not job or job.get("state") != "waiting_import":
            die(f"{key} is no longer waiting for import")
        if model and not job.get("naming_version"):
            job.update(_reserve_take(cfg, card, model, manifest))
        job["imported_from"] = str(src)
        return dict(job)
    job = update_manifest(cfg, mark)
    try:
        meta = finalize_take(cfg, key, {"path": str(src), "mode": job.get("mode")}, card, post=args.post or ())
    except BaseException:
        if created:
            update_manifest(cfg, lambda manifest: manifest["jobs"].pop(key, None))
        raise
    if meta and args.move:
        src.unlink()
        print(f"  moved: {src} deleted")


def cmd_forget(cfg, args):
    def change(manifest):
        jobs.forget(manifest, args.key, args.why, force=args.force)
    try:
        update_manifest(cfg, change)
    except jobs.JobError as error:
        die(str(error))
    print(f"forgot {args.key} (kept in manifest.forgotten). The next generate may submit it again.")
