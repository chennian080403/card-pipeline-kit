"""Generation jobs (card-core): the state machine behind "never pay twice".

    jobs[<key>] = {state, ...}     state: submitted | waiting_import | ok | failed | unknown

- ok              done: skipped on the next run
- submitted /     still running at the provider / waiting for a human to bring the file in:
  waiting_import  only polled (or imported), never submitted again
- unknown         may have produced or billed: never resubmitted until a human `forget`s it
- failed          the provider said no for sure: may be submitted again

Paid entries need an approval ("who / when") before anything is submitted.
"""

from __future__ import annotations

import re

from .jsonio import now, read_json, write_json

LIVE = ("submitted", "waiting_import")
FORGETTABLE = ("unknown", "submitting", "failed", "waiting_import")


class JobError(RuntimeError):
    pass


def load(path, fmt):
    return read_json(path, {"format": fmt, "jobs": {}, "approvals": []})


def save(path, manifest):
    write_json(path, manifest)


def next_step(job):
    """What to do with a job slot: 'skip' (ok) | 'wait' (live) | 'stop' (unknown / submitting) | 'submit'.

    ``submitting`` is written just before the request goes out: if the process dies there, nobody knows
    whether the provider took (and billed) it, so it stops like ``unknown`` until a human forgets it."""
    state = (job or {}).get("state")
    if state == "ok":
        return "skip"
    if state in LIVE:
        return "wait"
    if state in ("unknown", "submitting"):
        return "stop"
    return "submit"


_PLACEHOLDER = re.compile(r"\.\.\.|…|<[^>]*>|\bx{3,}\b|\bTODO\b|待填|占位", re.IGNORECASE)


def approval_problem(text):
    """Why an --approved text is not a real approval (None = fine): empty, a copied placeholder, or too short."""
    text = str(text or "").strip()
    if not text:
        return "paid entry: add --approved \"<who / when>\" (rules.require_approval)"
    if _PLACEHOLDER.search(text):
        return f"--approved \"{text}\" looks like a placeholder: write who approved and when"
    if len(re.findall(r"[0-9A-Za-z\u4e00-\u9fff]", text)) < 4:
        return f"--approved \"{text}\" is too short: write who approved and when"
    return None


def approve(manifest, *, free, required, approved, record):
    """Gate for paid entries. Returns the approval record (or None when free / not required)."""
    if free or not required:
        return None
    problem = approval_problem(approved)
    if problem:
        raise JobError(problem)
    entry = {"at": now(), "by": approved, **record}
    manifest.setdefault("approvals", []).append(entry)
    return entry


def forget(manifest, key, why, force=False):
    """A human looked at an unknown / failed / waiting job: drop it so it may run again (kept in `forgotten`)."""
    job = manifest["jobs"].get(key)
    if not job:
        raise JobError(f"no job {key}")
    if job.get("state") not in FORGETTABLE and not force:
        raise JobError(f"{key} is {job.get('state')}; only {' / '.join(FORGETTABLE)} jobs can be forgotten")
    if not str(why or "").strip():
        raise JobError("say why (--why): forgetting lets the next run submit and maybe pay again")
    manifest.setdefault("forgotten", []).append({**job, "key": key, "forgotten_at": now(), "why": why})
    del manifest["jobs"][key]
    return job
