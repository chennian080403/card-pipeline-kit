#!/usr/bin/env python3
"""Test double of the `compshare --json` CLI for selfcheck: one fake box on this machine.

Only the calls cloud.py makes are implemented, with the same JSON shapes as compshare-cli 0.5.6.
Remote paths (":/x") are plain local paths. Jobs run synchronously when submitted.
State lives in $FAKE_COMPSHARE_STATE (state.json + jobs/<id>.{out,err}).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

STATE_DIR = Path(os.environ["FAKE_COMPSHARE_STATE"])
STATE = STATE_DIR / "state.json"


def load():
    return json.loads(STATE.read_text()) if STATE.is_file() else {"hosts": {}, "jobs": {}, "calls": []}


def reply(data=None, error=None):
    body = {"ok": error is None, "schema_version": "1"}
    body.update({"data": data or {}} if error is None else {"error": {"code": "fake_error", "message": error}})
    print(json.dumps(body))
    sys.exit(0 if error is None else 1)


def main(argv):
    if argv and argv[0] == "--json":
        argv = argv[1:]
    state = load()
    state["calls"].append(argv[:3])
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    def save():
        STATE.write_text(json.dumps(state))

    group, action, rest = argv[0], argv[1], argv[2:]
    if group != "instance":
        reply(error=f"unsupported {group}")
    if action == "create":
        if "--dry-run" in rest:
            save()
            reply({"selection": {"GpuType": "fake"}, "price": {"PriceDetails": [{"ChargeType": "Postpay", "Instance": 0}]}})
        state["hosts"]["uhost-fake"] = "Running"
        state.setdefault("names", {})["uhost-fake"] = rest[rest.index("--name") + 1] if "--name" in rest else ""
        save()
        if (STATE_DIR / "fail_create").exists():       # selfcheck switch: the box is made, but create reports an error
            reply(error="wait timed out")
        reply({"instance": {"UHostIds": ["uhost-fake"]}})
    if action == "list":
        save()
        reply({"UHostSet": [{"UHostId": iid, "Name": state.get("names", {}).get(iid, ""), "State": st}
                            for iid, st in state["hosts"].items()]})
    sub = None
    if action in ("job", "schedule"):
        sub, rest = rest[0], rest[1:]
    iid = rest[0]
    if iid not in state["hosts"]:
        save()
        reply(error=f"instance {iid} not found")
    if action == "show":
        save()
        reply({"UHostSet": [{"UHostId": iid, "State": state["hosts"][iid]}]})
    if action in ("start", "stop"):
        state["hosts"][iid] = "Running" if action == "start" else "Stopped"
        save()
        reply({})
    if state["hosts"][iid] != "Running":
        save()
        reply(error=f"instance {iid} is not running")
    if action == "schedule":
        save()
        if (STATE_DIR / "fail_schedule").exists():     # selfcheck switch: the safety shutdown cannot be set
            reply(error="schedule refused")
        reply({})
    if action == "ssh":
        command = rest[rest.index("--") + 1:]
        proc = subprocess.run(command, capture_output=True, text=True)
        save()
        reply({"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr})
    if action == "cp":
        src, dst = (Path(p.lstrip(":")) for p in rest[1:3])
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        elif src.is_file():
            shutil.copy2(src, dst)
        else:
            save()
            reply(error=f"no such file: {src}")
        save()
        reply({})
    if action == "job":
        logs = STATE_DIR / "jobs"
        logs.mkdir(exist_ok=True)
        if sub == "submit":
            job_id = rest[rest.index("--job-id") + 1]
            if job_id in state["jobs"]:
                save()
                reply({"job": {"JobId": job_id, "State": state["jobs"][job_id], "Existing": True}})
            command = rest[rest.index("--") + 1:]
            proc = subprocess.run(command, capture_output=True, text=True)
            (logs / f"{job_id}.out").write_text(proc.stdout)
            (logs / f"{job_id}.err").write_text(proc.stderr)
            state["jobs"][job_id] = "Succeeded" if proc.returncode == 0 else "Failed"
            save()
            reply({"job": {"JobId": job_id, "State": "Running"}})
        job_id = rest[1]          # rest = [iid, job_id, ...]
        if job_id not in state["jobs"]:
            save()
            reply(error=f"job {job_id} not found")
        if sub == "show":
            save()
            reply({"job": {"JobId": job_id, "State": state["jobs"][job_id]}})
        if sub == "logs":
            save()
            reply({"logs": {"Stdout": (logs / f"{job_id}.out").read_text(), "Stderr": (logs / f"{job_id}.err").read_text()}})
    save()
    reply(error=f"unsupported {' '.join(argv[:3])}")


if __name__ == "__main__":
    main(sys.argv[1:])
