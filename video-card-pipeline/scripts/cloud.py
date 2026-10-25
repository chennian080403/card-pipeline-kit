"""Cloud GPU session for the `compshare` provider (CompShare / 优云智算, driven only through its CLI).

The GPU box costs money by the hour, not per clip, so its lifecycle is explicit and separate
from `generate`:

    cloud up --approved <the user's own words> [--create [--price-ok]]
                                                   start (or create) the box, safety shutdown, push the
                                                   runner, start the model service, wait healthy.
                                                   --create alone only prints the plan and price; the box
                                                   is created only after the user saw it (--price-ok)
    generate <cards> --model cloud-h3              each take = upload folder -> durable remote job -> download
    cloud down                                     stop the box (always do this after a batch)

`generate` refuses while the box is not up; it never starts or creates one by itself.
State lives in <output>/cloud/session.json (instance id, when it was started, server job).
Credentials stay with the CLI (`compshare config`); nothing here reads them.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from core import jobs, jsonio

RUNNER = Path(__file__).resolve().parent / "remote" / "h3_run.py"
SERVER_JOB = "vcp-model-server"


class CloudError(RuntimeError):
    pass


def settings(cfg):
    cloud = dict(cfg.video.get("cloud") or {})
    cloud.setdefault("cli", "compshare")
    cloud.setdefault("backend", "vllm")
    cloud.setdefault("remote_root", "/root/vcp")
    cloud.setdefault("safety_shutdown_hours", 3)
    return cloud


def session_path(cfg):
    return cfg.output_dir / "cloud" / "session.json"


def load_session(cfg):
    path = session_path(cfg)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def save_session(cfg, session):
    path = session_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_json(path, session)


def call(cloud, *args, timeout=900):
    """Run `<cli> --json <args>`; return `data` or raise CloudError with the CLI's own message."""
    argv = [cloud["cli"], "--json", *[str(a) for a in args]]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise CloudError(f"timed out after {timeout}s: {' '.join(argv[:5])} ...") from error
    except FileNotFoundError as error:
        raise CloudError(f"CLI not found: {cloud['cli']} (install compshare-cli, see SKILL.md)") from error
    try:
        body = json.loads(proc.stdout or "{}")
    except ValueError as error:
        raise CloudError(f"{' '.join(argv[2:5])}: not JSON: {(proc.stdout + proc.stderr)[-300:]}") from error
    if not body.get("ok"):
        err = body.get("error") or {}
        raise CloudError(f"{' '.join(argv[2:5])}: {err.get('code')}: {err.get('message') or proc.stderr[-300:]}")
    return body.get("data") or {}


def instance_id(cfg):
    return load_session(cfg).get("instance") or settings(cfg).get("instance") or ""


def instance_state(cloud, iid):
    data = call(cloud, "instance", "show", iid, "--status", timeout=120)
    hosts = data.get("UHostSet") or [{}]
    return str(hosts[0].get("State") or "Unknown")


def ssh(cloud, iid, script, timeout=300):
    data = call(cloud, "instance", "ssh", iid, "--", "sh", "-lc", script, timeout=timeout)
    return int(data.get("exit_code", 1)), str(data.get("stdout") or "")


def cp(cloud, iid, source, dest, timeout=1800):
    return call(cloud, "instance", "cp", iid, source, dest, timeout=timeout)


def job_submit(cloud, iid, job_id, command, cwd=None, name=None):
    args = ["instance", "job", "submit", iid, "--job-id", job_id]
    if cwd:
        args += ["--cwd", cwd]
    if name:
        args += ["--name", name]
    return (call(cloud, *args, "--", *command, timeout=300).get("job") or {})


def job_show(cloud, iid, job_id):
    return call(cloud, "instance", "job", "show", iid, job_id, timeout=120).get("job") or {}


def job_tail(cloud, iid, job_id, lines=80):
    try:
        logs = call(cloud, "instance", "job", "logs", iid, job_id, "--tail", lines, timeout=120).get("logs") or {}
    except CloudError as error:
        return f"(logs unavailable: {error})"
    return f"{logs.get('Stdout') or ''}\n{logs.get('Stderr') or ''}".strip()


def healthy(cloud, iid):
    url = (cloud.get("server") or {}).get("health")
    if not url:
        return True, "no health url configured"
    code, out = ssh(cloud, iid, f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 5 {url}", timeout=120)
    return out.strip() == "200", f"health {url} -> {out.strip() or code}"


def ready(cfg):
    """(ok, message): may `generate` use the box right now? Never changes anything."""
    cloud = settings(cfg)
    iid = instance_id(cfg)
    if not iid:
        return False, ("no cloud instance yet -- `cloud up --create` first "
                       "(with --approved: the user's own words, e.g. who approved and when)")
    try:
        state = instance_state(cloud, iid)
    except CloudError as error:
        return False, str(error)
    if state.lower() != "running":
        return False, (f"instance {iid} is {state} -- `cloud up` first "
                       "(with --approved: the user's own words, e.g. who approved and when)")
    return True, f"instance {iid} running"


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def _create(cfg, cloud, price_ok):
    spec = cloud.get("create") or {}
    need = ("region", "zone", "gpu", "cpu", "memory", "image")
    missing = [k for k in need if not spec.get(k)]
    if missing:
        raise CloudError(f"video.cloud.create is missing {missing}")
    args = ["instance", "create", "--region", spec["region"], "--zone", spec["zone"], "--gpu", spec["gpu"],
            "--count", spec.get("count", 1), "--cpu", spec["cpu"], "--memory", spec["memory"],
            "--image", spec["image"], "--image-source", spec.get("image_source", "platform"),
            "--charge", spec.get("charge", "Postpay"), "--max-count", 1,
            "--max-price", spec.get("max_price", 5), "--name", spec.get("name", f"vcp-{cfg.name}")[:60]]
    if spec.get("disk"):
        args += ["--disk", spec["disk"]]
    plan = call(cloud, *args, "--dry-run", timeout=300)
    price = (plan.get("price") or {}).get("PriceDetails")
    print(f"  plan: {json.dumps(plan.get('selection'), ensure_ascii=False)}\n  price/h: {json.dumps(price, ensure_ascii=False)}")
    if not price_ok:
        print("  plan only, nothing created: show this price to the user; after they agree, rerun with --price-ok")
        return None
    name = spec.get("name", f"vcp-{cfg.name}")[:60]
    try:
        data = call(cloud, *args, "--yes", "--wait", "--timeout", spec.get("timeout", 1200), timeout=1500)
    except CloudError as error:
        # the box may exist (and bill) even though create / its wait failed: look it up by name
        found = _find_by_name(cloud, name)
        if found:
            _remember(cfg, found)
            raise CloudError(_guard(cloud, found, f"create reported an error ({error})")) from error
        raise CloudError(f"create failed ({error}); no box named {name} found in `instance list` -- "
                         f"still check the console: a box may be BILLING") from error
    ids = (data.get("instance") or {}).get("UHostIds") or (data.get("instance") or {}).get("UHostId") or []
    ids = [ids] if isinstance(ids, str) else ids
    if not ids:
        found = _find_by_name(cloud, name)
        if found:
            _remember(cfg, found)
            raise CloudError(_guard(cloud, found, "create returned no instance id"))
        raise CloudError(f"create returned no instance id: {json.dumps(data, ensure_ascii=False)[:400]} -- "
                         f"check the console: a box may be BILLING")
    _remember(cfg, ids[0])
    return ids[0]


def _remember(cfg, iid):
    """Write the instance id into the session the moment it is known, so `cloud down` can always find it."""
    session = load_session(cfg)
    if session.get("instance") != iid:
        session.update(instance=iid, recorded_at=datetime.now().isoformat(timespec="seconds"))
        save_session(cfg, session)


def _find_by_name(cloud, name):
    """The id of the box called `name` (`instance list --all`), or "" -- never raises."""
    try:
        data = call(cloud, "instance", "list", "--all", timeout=120)
    except CloudError:
        return ""
    hosts = data.get("UHostSet") or data.get("instances") or data.get("items") or []
    for host in hosts if isinstance(hosts, list) else []:
        if isinstance(host, dict) and host.get("Name") == name and str(host.get("State", "")).lower() not in ("deleted",):
            return str(host.get("UHostId") or host.get("Id") or "")
    return ""


def _guard(cloud, iid, what):
    """Something failed while a box may be running: put the safety shutdown on it (or stop it) and
    return a loud message for the user."""
    hours = float(cloud.get("safety_shutdown_hours") or 3)
    at = (datetime.now().astimezone() + timedelta(hours=hours)).isoformat(timespec="seconds")
    try:
        call(cloud, "instance", "schedule", "set", iid, "--at", at, timeout=120)
        net = f"safety shutdown set for {at}"
    except CloudError as schedule_error:
        try:
            call(cloud, "instance", "stop", iid, "--yes", "--wait", "--timeout", 900, timeout=1000)
            net = f"safety shutdown could not be set ({schedule_error}), so it was stopped"
        except CloudError as stop_error:
            net = f"NEITHER safety shutdown ({schedule_error}) NOR stop ({stop_error}) worked -- stop it in the console NOW"
    return (f"!!! box {iid} MAY BE BILLING (可能有机器在计费): {what}. Recorded in the session; {net}. "
            f"`cloud status` to look, `cloud down` to stop.")


def cmd_status(cfg, args):
    cloud, session = settings(cfg), load_session(cfg)
    iid = instance_id(cfg)
    print(f"backend {cloud['backend']} · instance {iid or '(none)'}")
    if session.get("started_at"):
        print(f"started {session['started_at']} by {session.get('approved')}")
    if not iid:
        return
    state = instance_state(cloud, iid)
    print(f"state {state}")
    if state.lower() == "running":
        print(healthy(cloud, iid)[1])
        if (cloud.get("server") or {}).get("command"):
            job = job_show(cloud, iid, SERVER_JOB) if session.get("server_job") else {}
            print(f"model server job: {job.get('State', 'not started')}")


def cmd_up(cfg, args):
    cloud, session = settings(cfg), load_session(cfg)
    problem = jobs.approval_problem(args.approved)
    if problem:
        raise CloudError(f"the GPU box bills by the hour: {problem}")
    iid = instance_id(cfg)
    if not iid:
        if not args.create:
            raise CloudError("no instance recorded: add --create (uses video.cloud.create) or set video.cloud.instance")
        iid = _create(cfg, cloud, args.price_ok)
        if not iid:
            return
        session = load_session(cfg)          # _create already wrote the id into it
        print(f"  created {iid}")
    elif instance_state(cloud, iid).lower() != "running":
        _remember(cfg, iid)
        try:
            call(cloud, "instance", "start", iid, "--wait", "--timeout", 900, timeout=1000)
        except CloudError as error:     # it may be starting (and billing) all the same
            raise CloudError(_guard(cloud, iid, f"start reported an error ({error})")) from error
    session.update(instance=iid, started_at=datetime.now().isoformat(timespec="seconds"), approved=args.approved,
                   backend=cloud["backend"])
    save_session(cfg, session)
    print(f"✓ {iid} running")
    hours = float(cloud.get("safety_shutdown_hours") or 0)
    if hours:
        at = (datetime.now().astimezone() + timedelta(hours=hours)).isoformat(timespec="seconds")
        try:
            call(cloud, "instance", "schedule", "set", iid, "--at", at, timeout=120)
        except CloudError as error:     # never leave a billing box running without the safety net
            try:
                cmd_down(cfg, args)
            except CloudError as stop_error:
                raise CloudError(f"safety shutdown not set ({error}) and stop failed ({stop_error}); "
                                 f"box {iid} is still RUNNING -- stop it now") from error
            raise CloudError(f"safety shutdown not set ({error}); box stopped instead of running unguarded") from error
        print(f"  safety shutdown at {at} (`cloud down` earlier is still expected)")
    root = cloud["remote_root"]
    code, out = ssh(cloud, iid, f"mkdir -p {root}/runs && command -v python3 && command -v curl")
    if code != 0:
        raise CloudError(f"box is missing python3/curl: {out[-300:]}")
    cp(cloud, iid, str(RUNNER), f":{root}/h3_run.py", timeout=300)
    server = cloud.get("server") or {}
    if server.get("command"):
        job_submit(cloud, iid, SERVER_JOB, ["sh", "-lc", server["command"]], cwd=server.get("cwd"), name="model server")
        session["server_job"] = SERVER_JOB
        save_session(cfg, session)
    # nothing to start the service here (no server.command): it is either already up or never will be,
    # so probe briefly instead of sitting out the whole ready_timeout on a billing box
    wait = int(server.get("ready_timeout", 1800)) if server.get("command") else int(server.get("probe_timeout", 60))
    deadline = time.time() + wait
    while True:
        ok, message = healthy(cloud, iid)
        if ok or time.time() > deadline:
            break
        print(f"  waiting for the model service ({message})")
        time.sleep(min(int(server.get("poll", 20)), max(1, wait)))
    if not ok:
        if not server.get("command"):
            raise CloudError(f"model service not healthy after {wait}s ({message}) and video.cloud.server.command is "
                             f"empty, so nothing on the box starts it: start it by hand, or set server.command. "
                             f"Box is still RUNNING (billing) -- `cloud down` if giving up")
        tail = job_tail(cloud, iid, SERVER_JOB) if session.get("server_job") else ""
        raise CloudError(f"model service not healthy ({message}); box is still RUNNING -- `cloud down` if giving up\n{tail[-1500:]}")
    print(f"✓ ready: {message}")


def cmd_down(cfg, args):
    cloud, session = settings(cfg), load_session(cfg)
    iid = instance_id(cfg)
    if not iid:
        raise CloudError("no instance recorded")
    call(cloud, "instance", "stop", iid, "--yes", "--wait", "--timeout", 900, timeout=1000)
    session.update(stopped_at=datetime.now().isoformat(timespec="seconds"))
    session.pop("server_job", None)
    save_session(cfg, session)
    print(f"✓ {iid} stopped (disks are kept; `cloud up` resumes)")


def cmd_cloud(cfg, args):
    {"status": cmd_status, "up": cmd_up, "down": cmd_down}[args.action](cfg, args)
