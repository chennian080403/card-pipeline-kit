"""ffmpeg helpers: probe, frame sheets, mock synthesis, downloads, data URLs."""

from __future__ import annotations

import base64
import io
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path


class MediaError(RuntimeError):
    pass


def ffmpeg_ok(ffmpeg):
    return bool(shutil.which(ffmpeg) or Path(ffmpeg).is_file())


def probe(ffmpeg, path):
    """Read `ffmpeg -i` output (does not depend on ffprobe)."""
    err = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    v = re.search(r"Video:.*?, (\d{2,5})x(\d{2,5})", err)
    f = re.search(r"([\d.]+) fps", err)
    return {"duration": round(int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]), 2) if m else 0.0,
            "width": int(v[1]) if v else None, "height": int(v[2]) if v else None,
            "fps": float(f[1]) if f else None, "audio": "Audio:" in err}


def frame_sheet(ffmpeg, video, out, info=None, width=480):
    """First / middle / last frame side by side -- for the AI's self-check and the human's glance."""
    info = info or probe(ffmpeg, video)
    duration = max(info.get("duration") or 0.1, 0.1)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    times = [0.0, duration / 2, max(duration - 0.1, 0.0)]
    tiles = []
    for index, at in enumerate(times):
        tile = out.with_name(f"{out.stem}.{index}.jpg")
        subprocess.run([ffmpeg, "-v", "error", "-y", "-ss", f"{at:.2f}", "-i", str(video), "-frames:v", "1",
                        "-vf", f"scale={width}:-2", "-q:v", "4", str(tile)], check=False)
        if tile.is_file():
            tiles.append(tile)
    if not tiles:
        return None
    inputs = sum((["-i", str(t)] for t in tiles), [])
    subprocess.run([ffmpeg, "-v", "error", "-y", *inputs, "-filter_complex",
                    "".join(f"[{i}:v]" for i in range(len(tiles))) + f"hstack=inputs={len(tiles)}" if len(tiles) > 1 else "null",
                    "-frames:v", "1", "-q:v", "4", str(out)], check=False)
    for tile in tiles:
        tile.unlink(missing_ok=True)
    return out if out.is_file() else None


def synth_mock(ffmpeg, out, *, first_frame=None, duration=2, size=(640, 360), fps=24, label="mock"):
    """A real, playable mp4 without any provider: a slow zoom on the first frame, or a test pattern."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    w, h = size
    frames = int(duration * fps)
    if first_frame:
        vf = (f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},"
              f"zoompan=z='min(zoom+0.0015,1.2)':d={frames}:s={w}x{h}:fps={fps},format=yuv420p")
        cmd = [ffmpeg, "-v", "error", "-y", "-loop", "1", "-i", str(first_frame), "-vf", vf,
               "-frames:v", str(frames), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)]
    else:
        cmd = [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}",
               "-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out.is_file():
        raise MediaError(f"mock synthesis failed: {proc.stderr[-300:]}")
    return out


def download(url, dst, timeout=300):
    dst = Path(dst)
    tmp = dst.with_suffix(dst.suffix + ".part")
    with urllib.request.urlopen(url, timeout=timeout) as response, open(tmp, "wb") as handle:
        shutil.copyfileobj(response, handle)
    tmp.replace(dst)
    return dst


def data_url(path, width=1280, quality=92):
    """Image -> JPEG data URL (downscaled), so refs need not leave the machine via an image host."""
    try:
        from PIL import Image
    except ImportError:
        suffix = Path(path).suffix.lower().lstrip(".") or "png"
        return f"data:image/{'jpeg' if suffix == 'jpg' else suffix};base64," + base64.b64encode(Path(path).read_bytes()).decode()
    image = Image.open(path).convert("RGB")
    if image.width > width:
        image = image.resize((width, round(image.height * width / image.width)), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def mime_of(path):
    suffix = Path(path).suffix.lower()
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
            ".gif": "image/gif", ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
            ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4"}.get(suffix, "application/octet-stream")
