#!/usr/bin/env python3
"""Provider adapters for image-card-pipeline.

Three adapter kinds, all pure standard library:

- ``openai_images``    POST {base}/images/generations or /images/edits
                        (multipart inline refs, or JSON ``image`` URL array)
- ``chat_completions`` POST {base}/chat/completions; the image comes back in
                        ``choices[0].message.images[0].image_url.url``
- ``mock``             returns a synthetic PNG; no network, for dry runs

Credentials are read from environment variables only -- never from config files
that could be committed. A config entry names the variable (``api_key_env``),
and may name a fallback key file (``api_key_file``) for launchd-style hosts.

Paid calls: every failure is sorted into one of two kinds, because the caller
must never pay twice for one image --

- ``NotRun``          certainly did not run (bad local input, could not connect,
                      or the server refused it with a 4xx): safe to send again
- anything else       the request went out and broke afterwards (timeout, cut
                      connection, 5xx, non-JSON, 200 with no image, the result
                      download failed): it may have run and been billed. The
                      caller records it as ``unknown`` and never re-sends it
                      on its own.
"""

from __future__ import annotations

import base64
import http.client
import json
import mimetypes
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path


class ProviderError(RuntimeError):
    """Base class. On its own it means: the outcome of a sent request is not known."""


class NotRun(ProviderError):
    """The provider certainly did not run this request -- it never left the machine, or the
    server refused it outright (4xx). Safe to send again."""


class NotSent(NotRun):
    """Failed before the connection was up: nothing reached the server. The only error retried automatically."""


class OutcomeUnknown(ProviderError):
    """Sent, then something broke (timeout, cut connection, 5xx, non-JSON, no image, download failed).
    It may have run and been billed: record ``unknown``, never re-send on your own."""


# --------------------------------------------------------------------------- #
# image sniffing (ext / mime / dimensions / alpha) -- no Pillow required
# --------------------------------------------------------------------------- #

_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def sniff_image(raw):
    """Return (ext, mime, width, height, has_alpha) for PNG/JPEG/WEBP/other."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        width = height = None
        has_alpha = False
        if raw[12:16] == b"IHDR":
            width = int.from_bytes(raw[16:20], "big")
            height = int.from_bytes(raw[20:24], "big")
            color_type = raw[25] if len(raw) > 25 else 0
            has_alpha = color_type in (4, 6) or (color_type == 3 and b"tRNS" in raw)
        return "png", "image/png", width, height, has_alpha
    if raw[:2] == b"\xff\xd8":
        i = 2
        while i < len(raw) - 9:
            if raw[i] != 0xFF:
                i += 1
                continue
            marker = raw[i + 1]
            if marker in _JPEG_SOF:
                height = int.from_bytes(raw[i + 5:i + 7], "big")
                width = int.from_bytes(raw[i + 7:i + 9], "big")
                return "jpg", "image/jpeg", width, height, False
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            i += 2 + int.from_bytes(raw[i + 2:i + 4], "big")
        return "jpg", "image/jpeg", None, None, False
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        chunk = raw[12:16]
        if chunk == b"VP8X":
            flags = raw[20]
            width = int.from_bytes(raw[24:27], "little") + 1
            height = int.from_bytes(raw[27:30], "little") + 1
            return "webp", "image/webp", width, height, bool(flags & 0x10)
        if chunk == b"VP8 ":
            width = int.from_bytes(raw[26:28], "little") & 0x3FFF
            height = int.from_bytes(raw[28:30], "little") & 0x3FFF
            return "webp", "image/webp", width, height, False
        if chunk == b"VP8L":
            bits = int.from_bytes(raw[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return "webp", "image/webp", width, height, bool((bits >> 28) & 1)
        return "webp", "image/webp", None, None, False
    return "bin", "application/octet-stream", None, None, False


def is_url(value):
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def can_inline(value):
    return isinstance(value, str) and value.startswith("data:")


# --------------------------------------------------------------------------- #
# multipart helper
# --------------------------------------------------------------------------- #

def build_multipart(fields, files):
    """files: list of (field_name, filename, bytes, mime). Returns (boundary, body)."""
    boundary = "----imagecardpipeline" + base64.b16encode(os.urandom(8)).decode()
    out = bytearray()
    for key, value in fields.items():
        if value is None:
            continue
        out += b"--" + boundary.encode() + b"\r\n"
        out += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        out += str(value).encode("utf-8") + b"\r\n"
    for field, filename, raw, mime in files:
        out += b"--" + boundary.encode() + b"\r\n"
        out += f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode()
        out += f"Content-Type: {mime}\r\n\r\n".encode()
        out += raw + b"\r\n"
    out += b"--" + boundary.encode() + b"--\r\n"
    return boundary, bytes(out)


def data_url_for(path):
    raw = Path(path).read_bytes()
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode())


def read_credential(spec):
    """Resolve a provider credential from an env var name, then an optional key file.

    ``api_key_field`` reads one field out of a JSON key file, so an existing
    private config can be used in place without copying the key anywhere.
    """
    env_name = spec.get("api_key_env")
    if env_name and os.getenv(env_name):
        return os.environ[env_name].strip()
    key_file = spec.get("api_key_file")
    if key_file:
        path = Path(os.path.expanduser(key_file))
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            field = spec.get("api_key_field")
            if field:
                try:
                    value = json.loads(text).get(field)
                except (ValueError, AttributeError):
                    return None
                return str(value).strip() if value else None
            return text.strip()
    return None


# --------------------------------------------------------------------------- #
# adapters
# --------------------------------------------------------------------------- #

def _download(url, timeout):
    """Fetch the finished image from a result URL. The image was already made (and paid for) by then,
    so any failure here is OutcomeUnknown -- the URL is in the run's response.json."""
    request = urllib.request.Request(url, headers={"User-Agent": "image-card-pipeline/1.0", "Accept": "image/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except Exception as error:  # noqa: BLE001 - every way this breaks means the same thing
        raise OutcomeUnknown(f"the image was made but downloading it failed ({type(error).__name__}: {error}); "
                             "the result URL is in response.json") from error


def _connection(url, timeout):
    """(connection, request target) for ``url``, honouring http(s)_proxy / no_proxy like urlopen does."""
    parts = urllib.parse.urlsplit(url)
    https = parts.scheme == "https"
    host, port = parts.hostname, parts.port or (443 if https else 80)
    target = (parts.path or "/") + ("?" + parts.query if parts.query else "")
    loopback = host in ("localhost", "::1") or str(host).startswith("127.")   # 本机地址不走系统代理：代理替它回的 502/503 分不清到底发没发出去
    proxy = None if loopback or urllib.request.proxy_bypass(host or "") else urllib.request.getproxies().get(parts.scheme)
    if not proxy:
        make = http.client.HTTPSConnection if https else http.client.HTTPConnection
        return make(host, port, timeout=timeout), target
    via = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
    auth = {}
    if via.username:
        token = base64.b64encode(f"{urllib.parse.unquote(via.username)}:{urllib.parse.unquote(via.password or '')}".encode()).decode()
        auth = {"Proxy-Authorization": "Basic " + token}
    if https:      # CONNECT tunnel through the proxy, TLS to the real host
        conn = http.client.HTTPSConnection(via.hostname, via.port or 80, timeout=timeout)
        conn.set_tunnel(host, port, headers=auth)
        return conn, target
    return http.client.HTTPConnection(via.hostname, via.port or 80, timeout=timeout), url   # plain http: absolute URL to the proxy


def _post(url, body, headers, timeout, record=None):
    """POST ``body``; return the parsed JSON reply. Failures come out sorted (see the module docstring).

    http.client instead of urlopen: urlopen reports "could not connect" (nothing sent) and "sent, then the
    connection broke" as the same URLError, and only the first one is safe to send again.
    ``record(raw)`` gets the raw reply body before anything else is done with it (the run keeps it)."""
    conn, target = _connection(url, timeout)
    try:
        conn.connect()
    except (OSError, http.client.HTTPException) as error:
        conn.close()
        raise NotSent(f"could not connect to {url} ({type(error).__name__}: {error}) -- nothing was sent") from error
    try:
        conn.request("POST", target, body=body, headers={"User-Agent": "image-card-pipeline/1.0", **headers})
        response = conn.getresponse()
        raw = response.read()
    except Exception as error:  # noqa: BLE001 - once the bytes are out, every failure means the same thing
        raise OutcomeUnknown(f"request sent, then {type(error).__name__}: {error} -- it may have run") from error
    finally:
        conn.close()
    if record:
        record(raw)
    status = response.status
    if 400 <= status < 500:     # the server said no outright: it did not run
        raise NotRun(f"HTTP {status}: {raw.decode('utf-8', 'replace')[:300]}")
    if not 200 <= status < 300:
        raise OutcomeUnknown(f"HTTP {status}: {raw.decode('utf-8', 'replace')[:300]} -- it may have run")
    try:
        return json.loads(raw)
    except ValueError as error:
        raise OutcomeUnknown(f"HTTP {status} but the reply is not JSON: {raw[:200]!r} -- it may have run") from error


def generate_openai_images(spec, prompt, refs=None, size=None, quality=None, background=None, record=None):
    """refs: list of local paths and/or http(s) URLs.

    Local paths and data URLs go inline as multipart to /images/edits.
    Pure URL lists go as a JSON ``image`` array. No refs -> /images/generations.
    A mix of the two cannot be sent in one request (NotRun; the pipeline publishes the local ones first).
    """
    base = str(spec["base_url"]).rstrip("/")
    key = read_credential(spec)
    if not key:
        raise NotRun(f"provider '{spec.get('name')}': missing credential ({spec.get('api_key_env')})")
    model = spec["model"]
    timeout = int(spec.get("timeout", 300))
    fields = {"model": model, "prompt": prompt, "quality": quality or spec.get("quality") or "auto"}
    if size:
        if spec.get("accepts_size", True):
            fields["size"] = size
    if background:
        fields["background"] = background
    refs = refs or []
    headers = {"Authorization": "Bearer " + key}

    if refs and all(is_url(r) for r in refs):
        payload = dict(fields)
        payload["image"] = list(refs)
        url, body = base + "/images/edits", json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
        message = f"{spec.get('name')}: {len(refs)} refs via URL"
    elif refs:
        if any(is_url(r) for r in refs):
            raise NotRun(f"{spec.get('name')}: URL and local references cannot go in one request -- publish the local ones too")
        files = []
        field = "image[]" if len(refs) > 1 else "image"
        try:
            for ref in refs:
                if can_inline(ref):
                    mime = ref.split(";", 1)[0].split(":", 1)[1]
                    raw = base64.b64decode(ref.split(",", 1)[1])
                    files.append((field, "ref." + mime.split("/")[-1], raw, mime))
                else:
                    path = Path(ref)
                    files.append((field, path.name, path.read_bytes(),
                                  mimetypes.guess_type(path.name)[0] or "image/png"))
        except (OSError, ValueError) as error:
            raise NotRun(f"{spec.get('name')}: could not read a reference ({error})") from error
        boundary, body = build_multipart(fields, files)
        url = base + "/images/edits"
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        message = f"{spec.get('name')}: {len(refs)} refs inline (multipart, never left the machine)"
    else:
        url, body = base + "/images/generations", json.dumps(fields).encode()
        headers["Content-Type"] = "application/json"
        message = f"{spec.get('name')}: text-to-image only"

    # timing split: where the seconds go (send+server wait vs. fetching the result URL)
    started = time.time()
    result = _post(url, body, headers, timeout, record=record)
    waited = time.time() - started
    message += f" [request {len(body) / 1e6:.1f}MB, wait {waited:.0f}s"
    item = ((result.get("data") if isinstance(result, dict) else None) or [{}])[0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"]), message + "]"
    if item.get("url"):
        started = time.time()
        raw = _download(item["url"], timeout)
        return raw, message + f", download {time.time() - started:.0f}s] (downloaded)"
    raise OutcomeUnknown(f"{spec.get('name')}: HTTP 200 but the reply carried no image (kept in response.json): "
                         f"{json.dumps(result)[:300]}")


def generate_chat_completions(spec, prompt, refs=None, size=None, quality=None, background=None, record=None):
    """OpenAI-compatible chat endpoint whose reply carries images in message.images[].

    Retried only when the connection could not even be made (nothing sent). A 200 with no image,
    a read timeout or a cut connection may already have been billed: OutcomeUnknown, no retry.
    More references than ``max_refs`` is refused (NotRun) -- never cut silently.
    """
    base = str(spec["base_url"]).rstrip("/")
    key = read_credential(spec)
    if not key:
        raise NotRun(f"provider '{spec.get('name')}': missing credential ({spec.get('api_key_env')})")
    timeout = int(spec.get("timeout", 240))
    attempts = max(1, int(spec.get("attempts", 3)))
    max_refs = int(spec.get("max_refs", 4))
    refs = list(refs or [])
    if len(refs) > max_refs:
        raise NotRun(f"{spec.get('name')}: {len(refs)} references but this model takes at most {max_refs} (max_refs); "
                     f"these would be dropped: {', '.join(str(r) for r in refs[max_refs:])}")

    content = [{"type": "text", "text": prompt}]
    try:
        for ref in refs:
            url = ref if is_url(ref) or can_inline(ref) else data_url_for(ref)
            content.append({"type": "image_url", "image_url": {"url": url}})
    except OSError as error:
        raise NotRun(f"{spec.get('name')}: could not read a reference ({error})") from error
    body = json.dumps({"model": spec["model"], "messages": [{"role": "user", "content": content}]}).encode()
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}

    for attempt in range(1, attempts + 1):
        try:
            result = _post(base + "/chat/completions", body, headers, timeout, record=record)
            break
        except NotSent as error:
            if attempt == attempts:
                raise NotSent(f"{spec.get('name')}: {attempts} attempts could not connect, last: {error}") from error
            time.sleep(1.5 * attempt)
    message = ((result.get("choices") if isinstance(result, dict) else None) or [{}])[0].get("message") or {}
    images = message.get("images") or []
    text = message.get("content")
    if images:
        url = images[0]["image_url"]["url"]
        raw = base64.b64decode(url.split(",", 1)[1]) if url.startswith("data:") else _download(url, timeout)
        return raw, f"{spec.get('name')}: image in message.images[] (attempt {attempt}, {len(content) - 1} refs)"
    # some upstreams return markdown image links in content instead
    if isinstance(text, str) and "data:image" in text:
        chunk = text.split("data:image", 1)[1].split(")", 1)[0].split('"', 1)[0]
        payload = chunk.split(",", 1)[1] if "," in chunk else chunk
        return base64.b64decode(payload), f"{spec.get('name')}: image in content (attempt {attempt})"
    raise OutcomeUnknown(f"{spec.get('name')}: HTTP 200 but no image in the reply (kept in response.json) -- "
                         "it may still have been billed, so it is not retried")


def _swatch_png(side=512):
    """A plain RGBA square in a random soft colour: visible on the review page, no imaging library needed."""
    import struct, zlib
    r, g, b = (96 + byte % 128 for byte in os.urandom(3))
    row = b"\x00" + bytes((r, g, b, 255)) * side

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * side, 9)) + chunk(b"IEND", b""))


def generate_mock(spec, prompt, refs=None, size=None, quality=None, background=None, record=None):
    if spec.get("delay"):            # 自检并发用：模拟上游要等一会儿
        time.sleep(float(spec["delay"]))
    return _swatch_png(), f"mock provider: no API called ({len(refs or [])} refs resolved)"


_ADAPTERS = {
    "openai_images": generate_openai_images,
    "chat_completions": generate_chat_completions,
    "mock": generate_mock,
}


def generate(spec, prompt, refs=None, size=None, quality=None, background=None, record=None):
    """-> (image bytes, message). ``record(raw)`` receives the raw reply body as soon as it arrives."""
    kind = spec.get("kind", "mock")
    if kind not in _ADAPTERS:
        raise NotRun(f"unknown provider kind '{kind}' (have: {', '.join(_ADAPTERS)})")
    return _ADAPTERS[kind](spec, prompt, refs=refs, size=size, quality=quality, background=background, record=record)


def provider_ready(spec):
    """(ready, reason). A mock provider is always ready."""
    kind = spec.get("kind", "mock")
    if kind == "mock":
        return True, "mock"
    if kind not in _ADAPTERS:
        return False, f"unknown kind {kind}"
    if read_credential(spec):
        return True, "credential found"
    return False, f"missing env {spec.get('api_key_env')}" + (f" or file {spec.get('api_key_file')}" if spec.get("api_key_file") else "")
