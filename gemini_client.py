"""Gemini (AI Studio) client for the video-QA baselines. Stdlib only (urllib); no SDK.

Key: env GEMINI_API_KEY, else the GEMINI_API_KEY= line in ~/.config/receipts/secrets.env.
Deliberately never reads GOOGLE_CLOUD_* or gcloud application-default credentials: this
talks to the AI Studio endpoint only, so it cannot bill a Vertex project by accident.
The key is sent as the x-goog-api-key header and is never logged or cached.

Every response is cached under eval/out/gemini_cache/<sha256>.json, keyed by everything that
can change the answer (model, prompt, video identity, schema, config, repeat index), so a
rerun of the eval costs nothing and works without a key.
"""
import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request

API = "https://generativelanguage.googleapis.com/v1beta"
SECRETS = os.path.expanduser("~/.config/receipts/secrets.env")
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "eval", "out", "gemini_cache")
TIMEOUT = 300
MAX_RETRIES = 5


class GeminiError(RuntimeError):
    pass


class QuotaExhausted(GeminiError):
    """Daily/long-window quota (e.g. free tier: 20 requests/day per model); retrying is pointless."""


class CacheMiss(GeminiError):
    """Offline mode and no cached answer."""


def load_key():
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key and os.path.exists(SECRETS):
        with open(SECRETS) as f:
            for line in f:
                if line.startswith("GEMINI_API_KEY="):
                    key = line.split("=", 1)[1].strip()
    if not key:
        raise GeminiError(f"no Gemini key: set GEMINI_API_KEY or add GEMINI_API_KEY=... to {SECRETS}")
    return key


def _request(method, path, key, body=None):
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(MAX_RETRIES):
        req = urllib.request.Request(API + path, data=data, method=method,
                                     headers={"x-goog-api-key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code == 429 and re.search(r"retry in (\d+h|\d+m\d)", detail):
                raise QuotaExhausted(f"HTTP 429 on {path}: quota exhausted, {detail[detail.find('retry in'):][:40]}") from None
            if e.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise GeminiError(f"HTTP {e.code} on {path}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
            raise GeminiError(f"network error on {path}: {getattr(e, 'reason', e)!r}") from None


def list_models(key=None):
    return _request("GET", "/models?pageSize=200", key or load_key()).get("models", [])


_STABLE_FLASH = re.compile(r"^models/gemini-(\d+(?:\.\d+)?)-flash$")


def resolve_model(prefer=None, key=None):
    """`prefer` (or env GEMINI_MODEL) if the API lists it; else the newest stable full Flash
    (gemini-X.Y-flash, no lite/image/tts/preview)."""
    prefer = prefer or os.environ.get("GEMINI_MODEL")
    models = list_models(key)
    names = {m["name"] for m in models if "generateContent" in m.get("supportedGenerationMethods", [])}
    if prefer:
        name = prefer if prefer.startswith("models/") else "models/" + prefer
        if name not in names:
            raise GeminiError(f"model {prefer!r} not available for generateContent")
        return name.split("/", 1)[1]
    stable = [(float(m.group(1)), n) for n in names if (m := _STABLE_FLASH.match(n))]
    if not stable:
        raise GeminiError("no stable gemini-*-flash model listed")
    return max(stable)[1].split("/", 1)[1]


def generate(model, parts, schema=None, temperature=0.0, key=None):
    """One generateContent call -> (text, model_version, usage)."""
    cfg = {"temperature": temperature}
    if schema is not None:
        cfg.update(responseMimeType="application/json", responseSchema=schema)
    body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": cfg}
    d = _request("POST", f"/models/{model}:generateContent", key or load_key(), body)
    cands = d.get("candidates") or []
    if not cands:
        raise GeminiError(f"no candidates: {json.dumps(d.get('promptFeedback', d))[:300]}")
    text = "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []) if not p.get("thought"))
    return text, d.get("modelVersion", model), d.get("usageMetadata", {})


def video_part(path, fps=None):
    with open(path, "rb") as f:
        part = {"inlineData": {"mimeType": "video/mp4", "data": base64.b64encode(f.read()).decode()}}
    if fps:
        part["videoMetadata"] = {"fps": fps}
    return part


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class VideoQA:
    """Interface for a model that answers a prompt about a video with JSON.
    ask(video_path, video_id, prompt, schema, repeat) -> {"data": dict, "model_version", "cached"}."""
    model_id = None

    def ask(self, video_path, video_id, prompt, schema, repeat=0):
        raise NotImplementedError


class GeminiVideoQA(VideoQA):
    """Cached Gemini video QA. `video_id` identifies the video content (source hash + cut) so cache
    keys do not depend on re-encoded bytes. offline=True raises on a cache miss instead of calling."""

    def __init__(self, model=None, fps=2, cache_dir=CACHE_DIR, offline=False, temperature=0.0):
        self.fps, self.cache_dir, self.offline, self.temperature = fps, cache_dir, offline, temperature
        self._model = model
        self.calls = 0

    @property
    def model_id(self):
        if self._model is None:
            self._model = resolve_model()
        return self._model

    def cache_key(self, video_id, prompt, schema, repeat):
        material = {"model": self.model_id, "video": video_id, "fps": self.fps, "prompt": prompt,
                    "schema": schema, "temperature": self.temperature, "repeat": repeat}
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest(), material

    def ask(self, video_path, video_id, prompt, schema, repeat=0):
        digest, material = self.cache_key(video_id, prompt, schema, repeat)
        return self._cached(digest, material, lambda: [video_part(video_path, self.fps), {"text": prompt}], schema)

    def ask_parts(self, parts, prompt, schema, repeat=0):
        """Several labeled videos in one request. parts: [{"text": str} | {"video": path, "id": video_id}],
        sent in order, followed by the prompt. Cache key uses video ids, never bytes."""
        desc = [p if "text" in p else {"video": p["id"]} for p in parts]
        digest, material = self.cache_key(desc, prompt, schema, repeat)
        build = lambda: [video_part(p["video"], self.fps) if "video" in p else {"text": p["text"]} for p in parts] \
            + [{"text": prompt}]  # noqa: E731
        return self._cached(digest, material, build, schema)

    def _cached(self, digest, material, build_parts, schema):
        path = os.path.join(self.cache_dir, digest + ".json")
        if os.path.exists(path):
            with open(path) as f:
                entry = json.load(f)
            return {"data": json.loads(entry["text"]), "model_version": entry["model_version"], "cached": True}
        if self.offline:
            raise CacheMiss(f"cache miss in offline mode ({digest[:12]}, repeat {material['repeat']})")
        text, version, usage = generate(self.model_id, build_parts(), schema, self.temperature)
        self.calls += 1
        data = json.loads(text)
        os.makedirs(self.cache_dir, exist_ok=True)
        with open(path, "w") as f:
            json.dump({"request": material, "text": text, "model_version": version, "usage": usage,
                       "created": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, f, indent=1, sort_keys=True)
        return {"data": data, "model_version": version, "cached": False}
