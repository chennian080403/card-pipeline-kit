#!/usr/bin/env python3
"""Runs ON the GPU box: one shot -> one mp4. Standard library only (python3.8+).

The `compshare` provider uploads a job folder (frames, reference images, prompt.txt,
job.json) and starts this script as a durable remote job. It talks to the model
service that is already running on the box and writes result.mp4 next to job.json.

    python3 h3_run.py /root/vcp/runs/<run>

job.json:
    backend      vllm | comfyui
    mode         keyframes | first_frame | reference | text   (pipeline modes)
    first_frame / last_frame / images   file names inside the job folder
    duration / aspect / width / height / seed / steps
    vllm         {url, fps, flow_shift, audio_flow_shift}
    comfyui      {url, workflow, nodes}   workflow = API-format json on the box;
                 nodes maps our fields to "<node id>.<input name>" (see providers.md)

Exit 0 only when result.mp4 exists. Everything said on stdout/stderr ends up in the
job log, which the provider copies back into the local run dir.
"""

from __future__ import annotations

import json
import mimetypes
import os
import random
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

TASKS = {"keyframes": "fl2va", "first_frame": "fl2va", "text": "t2va", "reference": "ref2va"}


def log(message):
    print(time.strftime("%H:%M:%S"), message, flush=True)


def _local_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


# --------------------------------------------------------------------------- #
# vLLM-Omni: POST /v1/videos/sync returns the mp4 body
# --------------------------------------------------------------------------- #

def run_vllm(job, folder, out):
    cfg = job.get("vllm") or {}
    if job["mode"] == "keyframes" and not job.get("last_frame"):
        job["mode"] = "first_frame"       # no last frame came up: run from the first frame instead of failing
    task = TASKS[job["mode"]]
    extra = {"task": task, "duration": float(job["duration"]),
             "audio_flow_shift": float(cfg.get("audio_flow_shift", 3.0))}
    fields = [("prompt", (folder / "prompt.txt").read_text(encoding="utf-8")),
              ("fps", str(cfg.get("fps", 24))),
              ("num_inference_steps", str(job.get("steps") or cfg.get("steps", 50))),
              ("flow_shift", str(cfg.get("flow_shift", 12))),
              ("seed", str(job["seed"]))]
    files = []
    if job["mode"] == "keyframes":
        extra["frame_indices"] = [0, -1]
        files = [("input_references", folder / job["first_frame"]), ("input_references", folder / job["last_frame"])]
    elif job["mode"] == "first_frame":
        files = [("input_reference", folder / job["first_frame"])]
    elif job["mode"] == "reference":
        names = ([job["first_frame"]] if job.get("first_frame") else []) + list(job.get("images") or [])
        files = [("input_reference", folder / name) for name in names[:9]]
        fields += [("aspect_ratio", "adaptive"), ("short_edge", str(job.get("short_edge", 768)))]
    if job.get("width") and job.get("height"):
        fields += [("width", str(job["width"])), ("height", str(job["height"]))]
    if job.get("aspect") and job["mode"] == "text":
        fields.append(("aspect_ratio", job["aspect"]))
    fields.append(("extra_params", json.dumps(extra)))
    argv = ["curl", "-sS", "--fail-with-body", "-X", "POST", cfg.get("url", "http://127.0.0.1:8091/v1/videos/sync"),
            "-o", str(out), "--max-time", str(int(cfg.get("timeout", 14400)))]
    for key, value in fields:
        argv += ["-F", f"{key}={value}" if key != "prompt" else f"prompt=<{folder / 'prompt.txt'}"]
    for key, path in files:
        argv += ["-F", f"{key}=@{path};type={mimetypes.guess_type(str(path))[0] or 'image/png'}"]
    log(f"vllm {task}: {len(files)} image(s), {job['duration']}s, seed {job['seed']}")
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = proc.stdout[-800:] + proc.stderr[-800:]
        if out.is_file() and out.stat().st_size < 65536:
            detail += out.read_text(errors="replace")[-800:]
            out.unlink()
        raise SystemExit(f"vllm request failed ({proc.returncode}): {detail}")


# --------------------------------------------------------------------------- #
# ComfyUI: upload images, patch an API-format workflow, queue, wait, download
# --------------------------------------------------------------------------- #

def _comfy_upload(base, path):
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{path.name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode() + path.read_bytes() + \
        f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(f"{base}/upload/image", data=body, method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with _local_opener().open(request, timeout=120) as response:
        return json.loads(response.read())["name"]


def run_comfyui(job, folder, out):
    cfg = job.get("comfyui") or {}
    base = cfg.get("url", "http://127.0.0.1:8188").rstrip("/")
    task = TASKS[job["mode"]]
    workflow_path = Path((cfg.get("workflows") or {}).get(task) or "")
    nodes = (cfg.get("nodes") or {}).get(task) or {}
    if not workflow_path.is_file() or not nodes:
        raise SystemExit(f"comfyui: no workflow/nodes configured for {task} (video.cloud.comfyui.workflows / nodes)")
    graph = json.loads(workflow_path.read_text(encoding="utf-8"))
    values = {"prompt": (folder / "prompt.txt").read_text(encoding="utf-8"), "seed": job["seed"],
              "duration": job["duration"], "frames": int(round(float(job["duration"]) * int(cfg.get("fps", 24)))) + 1,
              "width": job.get("width"), "height": job.get("height"), "steps": job.get("steps")}
    for key in ("first_frame", "last_frame"):
        if job.get(key):
            values[key] = _comfy_upload(base, folder / job[key])
    for index, name in enumerate(job.get("images") or [], 1):
        values[f"image{index}"] = _comfy_upload(base, folder / name)
    for field, target in nodes.items():
        if field == "output" or values.get(field) in (None, ""):
            continue
        node_id, _, input_name = target.partition(".")
        graph[node_id]["inputs"][input_name] = values[field]
    request = urllib.request.Request(f"{base}/prompt", method="POST", headers={"Content-Type": "application/json"},
                                     data=json.dumps({"prompt": graph, "client_id": uuid.uuid4().hex}).encode())
    with _local_opener().open(request, timeout=60) as response:
        prompt_id = json.loads(response.read())["prompt_id"]
    log(f"comfyui {task}: queued {prompt_id}")
    deadline = time.time() + int(cfg.get("timeout", 14400))
    while time.time() < deadline:
        time.sleep(5)
        with _local_opener().open(f"{base}/history/{prompt_id}", timeout=30) as response:
            history = json.loads(response.read()).get(prompt_id)
        if not history:
            continue
        status = history.get("status") or {}
        if status.get("status_str") == "error":
            raise SystemExit(f"comfyui error: {json.dumps(status.get('messages'), ensure_ascii=False)[-1500:]}")
        outputs = history.get("outputs") or {}
        wanted = nodes.get("output")
        for node_id, result in outputs.items():
            if wanted and node_id != str(wanted):
                continue
            for item in result.get("gifs", []) + result.get("videos", []) + result.get("images", []):
                if str(item.get("filename", "")).endswith((".mp4", ".webm", ".mov")):
                    query = urllib.parse.urlencode({k: item.get(k, "") for k in ("filename", "subfolder", "type")})
                    with _local_opener().open(f"{base}/view?{query}", timeout=600) as response:
                        out.write_bytes(response.read())
                    return
        if status.get("completed"):
            raise SystemExit(f"comfyui finished without a video output: {list(outputs)}")
    raise SystemExit("comfyui: timed out waiting for the result")


# --------------------------------------------------------------------------- #
# mock: lets the whole remote chain be rehearsed without a model (selfcheck)
# --------------------------------------------------------------------------- #

def run_mock(job, folder, out):
    frame = folder / job["first_frame"] if job.get("first_frame") else None
    argv = ["ffmpeg", "-y", "-loglevel", "error"]
    argv += ["-loop", "1", "-i", str(frame)] if frame else ["-f", "lavfi", "-i", "testsrc=size=640x360:rate=24"]
    argv += ["-t", str(min(float(job["duration"]), 2)), "-r", "24", "-vf", "scale=640:-2,format=yuv420p", str(out)]
    subprocess.run(argv, check=True)


def main():
    folder = Path(sys.argv[1]).resolve()
    job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
    job.setdefault("seed", random.randint(1, 2**31 - 1))
    out = folder / "result.mp4"
    if out.exists():
        out.unlink()
    started = time.time()
    {"vllm": run_vllm, "comfyui": run_comfyui, "mock": run_mock}[job.get("backend", "vllm")](job, folder, out)
    if not out.is_file() or out.stat().st_size == 0:
        raise SystemExit("no result.mp4 was written")
    (folder / "done.json").write_text(json.dumps({"seconds": round(time.time() - started, 1), "seed": job["seed"],
                                                  "bytes": out.stat().st_size}), encoding="utf-8")
    log(f"done in {time.time() - started:.0f}s -> {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    os.environ.pop("http_proxy", None)
    main()
