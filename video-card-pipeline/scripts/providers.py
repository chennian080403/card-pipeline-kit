"""Video providers. Every provider is asynchronous by contract:

    submit(job)    -> handle (dict, saved in the manifest so `collect` can resume after a crash)
    poll(handle)   -> (state, info)   state: running | waiting_import | ok | failed | unknown

``ok`` info carries ``path`` (local file) or ``url`` (download). ``unknown`` means
"the provider may have produced / billed something" -- the pipeline stops and never
resubmits on its own. Kinds:

    mock      local ffmpeg synthesis, no network (default: run the whole chain dry)
    agnes     example: Agnes video API (flash keyframe/reference, v2.0 image/keyframes)
    toonflow  optional canvas plugin (plugins/画布 next to the skill), loaded only when configured
    handoff   web platforms: write a package (frames + prompt), human generates, `import` closes the job
    command   any CLI: a command template that writes {out}
    compshare example: own cloud GPU box (MiniMax H3 via vLLM-Omni or ComfyUI); box lifecycle is `cloud up/down`
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import media


class ProviderError(RuntimeError):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class Provider:
    interval = 15

    def __init__(self, spec, cfg):
        self.spec = spec
        self.cfg = cfg
        self.interval = int(spec.get("poll_interval", self.interval))

    @property
    def free(self):
        return bool(self.spec.get("free"))

    def credentials_ok(self):
        return True, "no credentials needed"

    def submit(self, job):
        raise NotImplementedError

    def poll(self, handle):
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# mock
# --------------------------------------------------------------------------- #

class MockProvider(Provider):
    interval = 1

    @property
    def free(self):
        return True

    def submit(self, job):
        out = Path(job["run_dir"]) / "result.mp4"
        first = job["refs"].get("first_frame")
        media.synth_mock(self.cfg.ffmpeg, out, first_frame=first["path"] if first and "path" in first else None,
                         duration=float(self.spec.get("duration", 2)), label=job["card"]["id"])
        return {"path": str(out), "ready_at": time.time() + float(self.spec.get("delay", 0)), "mode": job["mode"]}

    def poll(self, handle):
        if time.time() < handle.get("ready_at", 0):
            return "running", {}
        return "ok", {"path": handle["path"], "mode": handle.get("mode")}


# --------------------------------------------------------------------------- #
# Agnes (request shapes checked against the live API; do not "fix" them from the vendor README)
# --------------------------------------------------------------------------- #

def _split_keys(raw):
    return [k.strip() for k in re.split(r"[,\n\r]+", raw or "") if k.strip()]


class AgnesProvider(Provider):
    interval = 15

    def _keys(self):
        env = self.spec.get("api_key_env", "AGNES_API_KEYS")
        raw = os.getenv(env, "")
        if not raw and shutil.which("launchctl"):
            raw = subprocess.run(["launchctl", "getenv", env], capture_output=True, text=True).stdout.strip()
        key_file = self.spec.get("api_key_file")
        if not raw and key_file and Path(os.path.expanduser(key_file)).is_file():
            for line in Path(os.path.expanduser(key_file)).read_text().splitlines():
                if line.strip().startswith(f"{env}="):
                    raw = line.split("=", 1)[1].strip().strip("\"'")
        keys = _split_keys(raw)
        db = self.spec.get("newapi_db")
        if not keys and db and Path(os.path.expanduser(db)).is_file():
            import sqlite3
            with sqlite3.connect(f"file:{os.path.expanduser(db)}?mode=ro", uri=True) as conn:
                keys = [k for (value,) in conn.execute(
                    "select key from channels where name like ? and status=1 order by id",
                    (self.spec.get("newapi_channel", "AGNES%"),)) for k in _split_keys(value)]
        return keys

    def credentials_ok(self):
        keys = self._keys()
        return (True, f"{len(keys)} key(s)") if keys else (False, f"set {self.spec.get('api_key_env', 'AGNES_API_KEYS')}")

    @property
    def base(self):
        return str(self.spec.get("base_url") or "https://apihub.agnes-ai.com").rstrip("/")

    def _http(self, method, url, key, body=None, timeout=120):
        request = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                         headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            text = error.read().decode(errors="replace")
            try:
                return error.code, json.loads(text)
            except ValueError:
                return error.code, {"raw": text[:500]}

    def payloads(self, job):
        model = self.spec["model"]
        prompt, refs = job["prompt"], job["refs"]
        first, last = refs.get("first_frame"), refs.get("last_frame")
        images = [r for r in refs.get("images", []) if "path" in r]
        secs = int(job["duration"])
        if "flash" in model:
            seconds = str(max(4, min(12, secs)))
            out = []
            if first:
                keyframe = {"model": model, "prompt": prompt, "mode": "keyframe", "size": job.get("resolution_tag") or "720P",
                            "seconds": seconds, "first_frame": media.data_url(first["path"])}
                if last:
                    keyframe["last_frame"] = media.data_url(last["path"])
                out.append(("keyframe", keyframe))
            pictures = ([first] if first else []) + images
            if pictures:
                tags = " ".join(f"<Picture {i + 1}>" for i in range(min(5, len(pictures))))
                out.append(("reference", {"model": model, "prompt": f"{tags} {prompt}", "mode": "reference",
                                          "size": "720P", "seconds": seconds,
                                          "images": [media.data_url(p["path"]) for p in pictures[:5]]}))
            if not out:
                out.append(("text", {"model": model, "prompt": prompt, "mode": "text", "size": "720P", "seconds": seconds}))
            return out
        frames = max(1, round(secs * 24 / 8)) * 8 + 1          # num_frames must be 8n+1
        w, h = (1152, 768) if job["aspect"] == "16:9" else (768, 1152)
        payload = {"model": model, "prompt": prompt, "width": w, "height": h, "num_frames": min(frames, 441),
                   "frame_rate": 24}
        if first and last:
            payload.update(mode="keyframes", extra_body={"image": [media.data_url(first["path"]), media.data_url(last["path"])]})
            return [("keyframes", payload)]
        if first:
            payload["image"] = media.data_url(first["path"])
            return [("image", payload)]
        return [("text", payload)]

    def submit(self, job):
        keys = self._keys()
        if not keys:
            raise ProviderError("no Agnes key")
        run_dir = Path(job["run_dir"])
        attempts, wait = int(self.spec.get("attempts", 8)), int(self.spec.get("retry_wait", 90))
        for label, payload in self.payloads(job):
            shown = {k: (v[:40] + "...") if isinstance(v, str) and v.startswith("data:") else v for k, v in payload.items()}
            (run_dir / f"request_{label}.json").write_text(json.dumps(shown, ensure_ascii=False, indent=1))
            for attempt in range(attempts):
                key_index = attempt % len(keys)
                try:
                    code, body = self._http("POST", f"{self.base}/v1/videos", keys[key_index], payload)
                except (OSError, ValueError) as error:   # timeout / connection lost / not JSON: it may have been accepted
                    raise ProviderError(f"create ({label}) got no clear answer ({type(error).__name__}: {error}); "
                                        f"may have been accepted -- check {run_dir}", uncertain=True) from error
                (run_dir / f"create_{label}_{attempt}.json").write_text(json.dumps({"http": code, "body": body}, ensure_ascii=False, indent=1))
                video_id = body.get("id") or body.get("video_id") or (body.get("data") or {}).get("id")
                if code == 200 and video_id:
                    return {"video_id": video_id, "model": payload["model"], "mode": label, "key_index": key_index}
                if code in (429, 503):                      # free tier: ~1/min, queue full -> back off, rotate keys
                    print(f"  {label}: HTTP {code}, retry in {wait}s ({attempt + 1}/{attempts})")
                    time.sleep(wait)
                    continue
                if code >= 500:
                    raise ProviderError(f"HTTP {code} on create ({label}); may have been accepted -- check {run_dir}", uncertain=True)
                print(f"  {label}: HTTP {code} {json.dumps(body, ensure_ascii=False)[:200]} -> next mode")
                break
        raise ProviderError("no mode was accepted (see run dir)")

    def poll(self, handle):
        keys = self._keys()
        key = keys[handle.get("key_index", 0) % len(keys)]
        code, body = self._http("GET", f"{self.base}/agnesapi?video_id={handle['video_id']}&model_name={handle['model']}", key)
        status = str(body.get("status") or (body.get("data") or {}).get("status") or "").lower()
        url = body.get("url") or (body.get("data") or {}).get("url")
        if url and status in ("", "completed", "succeeded", "success", "done"):
            return "ok", {"url": url, "mode": handle.get("mode"), "raw": body}
        if status in ("failed", "fail", "error", "cancelled"):
            return "failed", {"error": json.dumps(body, ensure_ascii=False)[:400]}
        return "running", {"status": status or f"http {code}"}


# --------------------------------------------------------------------------- #
# handoff (web platforms) and command (any CLI)
# --------------------------------------------------------------------------- #

class HandoffProvider(Provider):
    @property
    def free(self):
        return True          # the human spends the credits by hand on the website

    def submit(self, job):
        card = job["card"]
        package = self.cfg.output_dir / "handoff" / f"{card['id']}_{self.spec['alias']}_{job['n']}"
        package.mkdir(parents=True, exist_ok=True)
        # 关键帧按时间排好号：关键帧1_0s_首帧.png … 关键帧4_5s_尾帧.png（网页上几张关键帧就传几张）
        frames = [r for r in job["refs"].get("keyframes", []) if "path" in r]
        slot_name = {"first": "_首帧", "last": "_尾帧"}
        for index, ref in enumerate(frames, 1):
            shutil.copy2(ref["path"], package / f"关键帧{index}_{ref['at']:g}s{slot_name.get(ref['slot'], '')}{Path(ref['path']).suffix}")
        timeline = "、".join(f"{r['at']:g}s" for r in frames) or "无"
        for index, ref in enumerate(job["refs"].get("images", []), 1):
            if "path" in ref:
                shutil.copy2(ref["path"], package / f"参考{index}_{Path(ref['path']).name}")
        (package / "提示词.txt").write_text(job["prompt"] + "\n", encoding="utf-8")
        url = self.spec.get("url", "")
        (package / "参数.txt").write_text(
            f"平台：{url}\n模型：{self.spec.get('model')}\n模式：{job['mode']}\n时长：{job['duration']}s\n画幅：{job['aspect']}\n"
            f"关键帧：{len(frames)} 张（{timeline}）\n"
            f"收回：vpipeline.py import {card['id']} <下载的mp4> --model {self.spec['alias']}\n", encoding="utf-8")
        if self.spec.get("open"):
            subprocess.run(["open", str(package)], check=False)
            if url:
                subprocess.run(["open", url], check=False)
        return {"package": str(package), "mode": job["mode"]}

    def poll(self, handle):
        return "waiting_import", {"package": handle["package"]}


class CommandProvider(Provider):
    """command: "dreamina image2video --image {first_frame} --prompt-file {prompt_file} --out {out}"

    All keyframes: {keyframes} (paths, comma-separated, in time order) and {keyframes_json}
    (a file: [{"path", "at", "slot"}]) -- for tools that take more than first + last."""

    def submit(self, job):
        run_dir = Path(job["run_dir"])
        out = run_dir / "result.mp4"
        prompt_file = run_dir / "prompt.txt"
        prompt_file.write_text(job["prompt"], encoding="utf-8")
        refs = job["refs"]
        frames = [{"path": r["path"], "at": r["at"], "slot": r["slot"]} for r in refs.get("keyframes", []) if "path" in r]
        keyframes_json = run_dir / "keyframes.json"
        keyframes_json.write_text(json.dumps(frames, ensure_ascii=False, indent=1), encoding="utf-8")
        values = {"out": str(out), "prompt_file": str(prompt_file), "prompt": job["prompt"],
                  "keyframes": ",".join(f["path"] for f in frames), "keyframes_json": str(keyframes_json),
                  "first_frame": (refs.get("first_frame") or {}).get("path", ""),
                  "last_frame": (refs.get("last_frame") or {}).get("path", ""),
                  "duration": str(job["duration"]), "aspect": job["aspect"], "model": self.spec.get("model", ""),
                  "mode": job["mode"]}
        template = self.spec["command"]
        argv = [part.format(**values) for part in (template if isinstance(template, list) else shlex.split(template))]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=int(self.spec.get("timeout", 1800)))
        except subprocess.TimeoutExpired as error:   # the tool may have sent the job already: never resubmit blindly
            raise ProviderError(f"command timed out after {error.timeout}s; may have produced or billed -- check {run_dir}",
                                uncertain=True) from error
        (run_dir / "command.log").write_text(f"$ {' '.join(argv)}\n{proc.stdout}\n{proc.stderr}", encoding="utf-8")
        if proc.returncode != 0:
            raise ProviderError(f"command exited {proc.returncode}: {proc.stderr[-300:]}",
                                uncertain=bool(self.spec.get("billed_on_failure")))
        if not out.is_file():
            raise ProviderError(f"command succeeded but wrote no {out}", uncertain=True)
        return {"path": str(out), "mode": job["mode"]}

    def poll(self, handle):
        return "ok", {"path": handle["path"], "mode": handle.get("mode")}


# --------------------------------------------------------------------------- #
# compshare (own cloud GPU box running MiniMax H3; lifecycle via `cloud up/down`)
# --------------------------------------------------------------------------- #

class CompshareProvider(Provider):
    """One take = upload a job folder -> durable remote job (scripts/remote/h3_run.py) -> download.

    The box bills by the hour and is approved at `cloud up`, so takes themselves are free here;
    submit refuses while the box is not running and never starts one."""
    interval = 20

    @property
    def free(self):
        return True

    def credentials_ok(self):
        import cloud
        return cloud.ready(self.cfg)

    def submit(self, job):
        import cloud
        ok, message = cloud.ready(self.cfg)
        if not ok:
            raise ProviderError(message)
        settings = cloud.settings(self.cfg)
        run_dir = Path(job["run_dir"])
        stage = run_dir / "cloud_job"
        stage.mkdir(exist_ok=True)
        refs = job["refs"]
        mode = job["mode"]
        if mode == "keyframes" and not (refs.get("last_frame") or {}).get("path"):
            mode = "first_frame"          # first + middle frames, no last: H3 runs from the first frame (validate says so)
        comfyui = {**(settings.get("comfyui") or {}), **(self.spec.get("comfyui") or {})}   # a model may bring its own preset
        workflows = dict(comfyui.get("workflows") or {})
        for task, value in workflows.items():
            local = self.cfg.resolve(value)
            if local.is_file():         # a workflow kept in the project rides along with each take
                shutil.copy2(local, stage / f"workflow_{task}.json")
                workflows[task] = f"workflow_{task}.json"
        comfyui["workflows"] = workflows
        remote_job = {"backend": settings["backend"], "mode": mode, "duration": job["duration"],
                      "aspect": job["aspect"], "vllm": settings.get("vllm") or {}, "comfyui": comfyui}
        for key in ("width", "height", "steps", "seed", "short_edge"):
            if self.spec.get(key):
                remote_job[key] = self.spec[key]
        for key in ("first_frame", "last_frame"):
            ref = refs.get(key)
            if ref and "path" in ref:
                name = key + Path(ref["path"]).suffix
                shutil.copy2(ref["path"], stage / name)
                remote_job[key] = name
        images = []
        for index, ref in enumerate(r for r in refs.get("images", []) if "path" in r):
            name = f"ref{index + 1}{Path(ref['path']).suffix}"
            shutil.copy2(ref["path"], stage / name)
            images.append(name)
        remote_job["images"] = images
        (stage / "prompt.txt").write_text(job["prompt"], encoding="utf-8")
        (stage / "job.json").write_text(json.dumps(remote_job, ensure_ascii=False, indent=1), encoding="utf-8")
        iid = cloud.instance_id(self.cfg)
        remote = f"{settings['remote_root']}/runs/{run_dir.name}"
        job_id = "vcp-" + re.sub(r"[^A-Za-z0-9._-]", "_", run_dir.name)[:60]
        try:
            cloud.cp(settings, iid, str(stage), f":{remote}")
            cloud.job_submit(settings, iid, job_id, ["python3", f"{settings['remote_root']}/h3_run.py", remote],
                             name=f"{job['card']['id']} #{job['n']}")
        except cloud.CloudError as error:
            raise ProviderError(f"cloud submit: {error}") from error
        for name in [remote_job.get("first_frame"), remote_job.get("last_frame"), *images]:
            if name:                    # copies only; refs in the take record keep origin + sha256
                (stage / name).unlink(missing_ok=True)
        return {"instance": iid, "job_id": job_id, "remote": remote, "mode": mode, "run_dir": str(run_dir)}

    def poll(self, handle):
        import cloud
        settings = cloud.settings(self.cfg)
        iid, job_id, run_dir = handle["instance"], handle["job_id"], Path(handle["run_dir"])
        try:
            state = str(cloud.job_show(settings, iid, job_id).get("State") or "")
        except cloud.CloudError as error:
            return "running", {"status": f"cloud unreachable: {error}"}
        if state in ("Starting", "Running", "Cancelling", ""):
            return "running", {"status": state or "?"}
        (run_dir / "remote.log").write_text(cloud.job_tail(settings, iid, job_id, 400), encoding="utf-8")
        if state != "Succeeded":
            return "failed", {"error": f"remote job {state}: {(run_dir / 'remote.log').read_text()[-600:]}"}
        try:
            cloud.cp(settings, iid, f":{handle['remote']}/result.mp4", str(run_dir / "result.mp4"))
            cloud.cp(settings, iid, f":{handle['remote']}/done.json", str(run_dir / "done.json"))
        except cloud.CloudError as error:
            return "running", {"status": f"download retry: {error}"}
        return "ok", {"path": str(run_dir / "result.mp4"), "mode": handle.get("mode")}


KINDS = {"mock": MockProvider, "agnes": AgnesProvider,
         "handoff": HandoffProvider, "command": CommandProvider, "compshare": CompshareProvider}


def make(spec, cfg):
    kind = spec.get("kind", "mock")
    if kind == "toonflow":                # optional canvas plugin, loaded only when a provider asks for it
        from common import canvas_plugin
        try:
            canvas_plugin(spec, cfg.board)
        except RuntimeError as error:
            raise ProviderError(str(error)) from error
        from toonflow_provider import ToonflowProvider
        return ToonflowProvider(spec, cfg)
    if kind not in KINDS:
        raise ProviderError(f"unknown provider kind '{kind}' (known: {', '.join(KINDS)})")
    return KINDS[kind](spec, cfg)
