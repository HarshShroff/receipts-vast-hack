"""call_model(prompt, video_path) -> str for NVIDIA Cosmos Reason via the OpenAI-compatible endpoint.

Stdlib only (urllib, subprocess+ffmpeg); no openai package needed. Key from env NVIDIA_API_KEY (never logged).

Sources (NIM for VLMs, Cosmos Reason2 API page, docs.nvidia.com/nim/vision-language-models/1.7.0/examples/cosmos-reason2/api.html):
  - model ids nvidia/cosmos-reason2-2b, nvidia/cosmos-reason2-8b
  - video part: {"type":"video_url","video_url":{"url":"data:video/mp4;base64,..."}} (or a public URL)
  - sampling via extra_body media_io_kwargs {"video":{"fps":N}} (default 4 fps; fps above the file's own fps -> 400)
  - ~2 fps keeps timestamps within ~+-0.25 s; best at <=16k multimodal tokens
UNVERIFIED (docs I could reach do not say): hosted base URL https://integrate.api.nvidia.com/v1 (third-party listing only),
  whether the hosted endpoint accepts video_url at all, max request/body size, max duration, hosted rate limit.
  Hence the clip is transcoded to ~2 fps, 640 px wide, no audio, to keep the base64 body small. Override via env:
  COSMOS_BASE_URL, COSMOS_MODEL, COSMOS_FPS, COSMOS_MAX_WIDTH, COSMOS_MAX_TOKENS.
"""
import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("COSMOS_BASE_URL", "https://integrate.api.nvidia.com/v1")
MODEL = os.environ.get("COSMOS_MODEL", "nvidia/cosmos-reason2-8b")
FPS = float(os.environ.get("COSMOS_FPS", "2"))
MAX_WIDTH = int(os.environ.get("COSMOS_MAX_WIDTH", "640"))
MAX_TOKENS = int(os.environ.get("COSMOS_MAX_TOKENS", "2048"))
MAX_RETRIES = 5
TIMEOUT = 180
WARN_BYTES = 20 * 1024 * 1024


def _api_key():
    key = os.environ.get("NVIDIA_API_KEY", "").strip()
    if not key:
        raise RuntimeError("NVIDIA_API_KEY is not set. Create a key at build.nvidia.com and export it before calling call_model().")
    return key


def transcode(video_path, out_path, fps=FPS, max_width=MAX_WIDTH):
    """Resample to ~fps, downscale, drop audio (ffmpeg must be on PATH)."""
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", video_path, "-an",
           "-vf", f"fps={fps},scale='min({max_width},iw)':-2",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p", out_path]
    subprocess.run(cmd, check=True, capture_output=True)


def build_payload(prompt, b64_video):
    return {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": [
                {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + b64_video}},
                {"type": "text", "text": prompt},
            ]},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.2,
        "stream": False,
        # Passed through as extra_body fields by the OpenAI SDK; the file is already at FPS, so this is a no-op guard.
        "media_io_kwargs": {"video": {"fps": FPS}},
    }


def _post(payload, key, sleep=time.sleep):
    req = urllib.request.Request(
        BASE_URL.rstrip("/") + "/chat/completions", data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "application/json"})
    delay = 2.0
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < MAX_RETRIES:
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    wait = float(ra) if ra else delay
                except ValueError:
                    wait = delay
                sleep(wait)
                delay = min(delay * 2, 60)
                continue
            body = e.read().decode(errors="replace")[:500]
            raise RuntimeError(f"Cosmos API HTTP {e.code}: {body}") from None


def call_model(prompt: str, video_path: str) -> str:
    key = _api_key()
    if not os.path.isfile(video_path):
        raise FileNotFoundError(video_path)
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "clip_sampled.mp4")
        transcode(video_path, out)
        size = os.path.getsize(out)
        if size > WARN_BYTES:
            raise RuntimeError(f"Sampled clip is {size/1e6:.1f} MB; too large to inline as base64. Trim the clip or lower COSMOS_MAX_WIDTH/COSMOS_FPS.")
        with open(out, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    data = _post(build_payload(prompt, b64), key)
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("Unexpected Cosmos response shape: " + json.dumps(data)[:300]) from None
