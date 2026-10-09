"""Two external baselines with the same answer() shape as baselines.py.

VssAgentBaseline calls the team VSS agent-qa endpoint. That API has no as_of
cutoff, so every result sets time_restricted=False and says so.

LlmCaptionBaseline sends captions whose end time is <= as_of to a chat model
and expects JSON {"answer", "cited_time_s"}.

Neither class opens a network connection unless you pass a live client.
"""
import json
import os
import re
import urllib.error
import urllib.request

from claims import parse_t


def _blank(extra=None):
    out = {
        "answer": None,
        "claim_id": None,
        "clip_id": None,
        "t_start": None,
        "t_end": None,
        "stale": False,
        "cited_time_s": None,
        "time_restricted": None,
        "model_id": None,
        "note": None,
    }
    if extra:
        out.update(extra)
    return out


def parse_llm_json(text):
    """Pull {"answer", "cited_time_s"} out of a model reply. Raises ValueError."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON object in model output")
    data = json.loads(m.group(0))
    if "answer" not in data:
        raise ValueError("model JSON missing answer")
    cited = data.get("cited_time_s")
    if cited is not None:
        cited = float(cited)
    return data.get("answer"), cited


class VssAgentBaseline:
    """B-VSS: team agent-qa. Footage cannot be cut at as_of."""

    name = "VSS agent-qa"

    def __init__(self, client):
        self.client = client

    def answer(self, store, question, key, as_of):
        raw = self.client.ask(question, parse_t(as_of))
        cited = raw.get("cited_time_s")
        t0 = raw.get("t_start")
        t1 = raw.get("t_end")
        if cited is None and t0 is not None and t1 is not None:
            cited = (float(t0) + float(t1)) / 2.0
        note = raw.get("note")
        if not raw.get("time_restricted", False):
            note = (note + "; " if note else "") + "agent-qa has no as_of cutoff; evidence was not time-restricted"
        return _blank({
            "answer": raw.get("answer"),
            "clip_id": raw.get("clip_id"),
            "t_start": t0,
            "t_end": t1,
            "cited_time_s": cited,
            "time_restricted": False,
            "model_id": raw.get("model_id"),
            "note": note,
        })


class LlmCaptionBaseline:
    """B-LLM: captions that have started by as_of (t_start <= as_of), then one chat completion."""

    name = "LLM-over-captions"

    def __init__(self, captions, llm, model_id="unknown"):
        """captions: list of {text, t_start, t_end}. llm(prompt) -> str."""
        self.captions = list(captions)
        self.llm = llm
        self.model_id = model_id

    def answer(self, store, question, key, as_of):
        as_of = parse_t(as_of)
        kept = [c for c in self.captions if parse_t(c["t_start"]) <= as_of]
        if not kept:
            return _blank({
                "time_restricted": True,
                "model_id": self.model_id,
                "note": "no captions at or before as_of",
            })
        lines = []
        for c in kept:
            lines.append(f"[{c['t_start']}-{c['t_end']}] {c['text']}")
        prompt = (
            "Answer the question using only the captions. Times are seconds.\n"
            "Reply with ONLY JSON {\"answer\": <string or null>, \"cited_time_s\": <number or null>}.\n"
            "cited_time_s must be a time that appears in a caption you used.\n\n"
            f"Question: {question}\n\nCaptions:\n" + "\n".join(lines)
        )
        text = self.llm(prompt)
        try:
            answer, cited = parse_llm_json(text)
        except (ValueError, json.JSONDecodeError) as e:
            return _blank({
                "time_restricted": True,
                "model_id": self.model_id,
                "note": f"unparsed model output: {e}",
            })
        return _blank({
            "answer": answer,
            "cited_time_s": cited,
            "t_start": cited,
            "t_end": cited,
            "time_restricted": True,
            "model_id": self.model_id,
        })


class VssAgentClient:
    """POST /api/v1/agent/ask. Reads INGRESS_URL, USERNAME, PASSWORD from the environment."""

    def __init__(self, ask_fn=None):
        self._ask_fn = ask_fn

    def ask(self, question, as_of):
        if self._ask_fn is not None:
            return self._ask_fn(question, as_of)
        from vss_source import login, _post_json
        backend, token = login()
        try:
            data = _post_json(backend, token, "/api/v1/agent/ask", {"question": question, "top_k": 8})
        except urllib.error.HTTPError as e:
            return {
                "answer": None,
                "time_restricted": False,
                "note": f"agent-qa HTTP {e.code}; person_moving.mp4 is not in the index and the call was not time-restricted",
            }
        evidence = data.get("evidence") or []
        if isinstance(evidence, dict):
            evidence = evidence.get("chunks") or evidence.get("segments") or []
        t0 = t1 = cited = clip_id = None
        if evidence:
            ev = evidence[0]
            t0 = ev.get("best_match_start_sec", ev.get("segment_start_sec", ev.get("t_start")))
            t1 = ev.get("best_match_end_sec", ev.get("segment_end_sec", ev.get("t_end")))
            clip_id = ev.get("source") or ev.get("original_video") or ev.get("clip_id")
            if t0 is not None and t1 is not None:
                cited = (float(t0) + float(t1)) / 2.0
            elif t0 is not None:
                cited = float(t0)
        return {
            "answer": data.get("answer"),
            "t_start": t0,
            "t_end": t1,
            "cited_time_s": cited,
            "clip_id": clip_id,
            "time_restricted": False,
            "model_id": (data.get("llm_synthesis") or {}).get("model") if isinstance(data.get("llm_synthesis"), dict) else None,
            "note": "asked the team archive; person_moving.mp4 is a local file and is not an indexed original_video",
        }


def _wandb_headers(key, extra=None):
    """W&B Inference bills usage to a team/project, sent as the OpenAI-Project header.
    Set WANDB_PROJECT_PATH="<team>/<project>" (or WANDB_ENTITY; project defaults to receipts-vast-hack)."""
    h = {"Authorization": "Bearer " + key}
    path = os.environ.get("WANDB_PROJECT_PATH", "").strip()
    if not path and os.environ.get("WANDB_ENTITY", "").strip():
        path = os.environ["WANDB_ENTITY"].strip() + "/" + os.environ.get("WANDB_PROJECT", "receipts-vast-hack")
    if path:
        h["OpenAI-Project"] = path
    h.update(extra or {})
    return h


def wandb_chat(prompt, model=None, base_url=None):
    """One W&B inference chat completion. Key from WANDB_API_KEY. Returns (text, model_id)."""
    key = os.environ.get("WANDB_API_KEY", "").strip()
    if not key:
        raise RuntimeError("WANDB_API_KEY is not set")
    model = model or os.environ.get("WANDB_MODEL", "").strip()
    base = (base_url or os.environ.get("WANDB_BASE_URL") or "https://api.inference.wandb.ai/v1").rstrip("/")
    if not model:
        req = urllib.request.Request(base + "/models", headers=_wandb_headers(key))
        with urllib.request.urlopen(req, timeout=60) as r:
            listing = json.loads(r.read().decode())
        rows = listing.get("data") or []
        if not rows:
            raise RuntimeError("W&B /models returned no ids")
        model = rows[0]["id"]
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 256,
        "temperature": 0,
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers=_wandb_headers(key, {"Content-Type": "application/json"}),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"W&B inference HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from None
    return data["choices"][0]["message"]["content"] or "", model
