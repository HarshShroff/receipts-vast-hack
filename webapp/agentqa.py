"""The event's own VSS agent (agent-qa) as a live comparison card.

Uses the retrieval/agent-qa skill endpoints. Prefer POST /api/v1/agent/ask
({"question","top_k"}); if that fails, fall back to POST
/api/v1/agent/search-and-answer ({"query","top_k"}) which is the same skill's
filterable path. agent-qa has no as_of cutoff. Results are cached per question.
"""
import time
import urllib.error

from vss_source import _post_json, filename_timestamp_seconds, login

_CACHE = {}
_TTL_S = 600


def clear_cache():
    _CACHE.clear()


def _evidence_rows(data):
    ev = data.get("evidence") or []
    if isinstance(ev, dict):
        ev = ev.get("chunks") or ev.get("segments") or []
    return ev if isinstance(ev, list) else []


def _call_agent(backend, token, question, top_k):
    """Skill: ask first (question+top_k), then search-and-answer (query+top_k)."""
    try:
        return _post_json(
            backend, token, "/api/v1/agent/ask",
            {"question": question, "top_k": top_k},
        )
    except urllib.error.HTTPError:
        return _post_json(
            backend, token, "/api/v1/agent/search-and-answer",
            {"query": question, "top_k": top_k},
        )


def agentqa_answer(question, top_k=10):
    q = (question or "").strip()
    if not q:
        return {"ok": False, "error": "empty question"}
    hit = _CACHE.get(q)
    if hit and time.time() - hit[0] < _TTL_S:
        return hit[1]
    try:
        backend, token = login()
        data = _call_agent(backend, token, q, top_k)
    except urllib.error.HTTPError as e:
        out = {"ok": False, "error": f"agent-qa HTTP {e.code}"}
        _CACHE[q] = (time.time(), out)
        return out
    except Exception as e:  # network / auth: report, never fake an answer
        return {"ok": False, "error": f"agent-qa unavailable: {type(e).__name__}"}
    rows = _evidence_rows(data)
    top = rows[0] if rows else {}
    src = top.get("source") or top.get("original_video") or top.get("clip_id") or ""
    t0 = top.get("best_match_start_sec", top.get("segment_start_sec", top.get("t_start")))
    t1 = top.get("best_match_end_sec", top.get("segment_end_sec", top.get("t_end")))
    abs_t = None
    try:
        base = filename_timestamp_seconds(src) if src else None
        if base is not None and t0 is not None:
            abs_t = float(base) + float(t0)
    except Exception:
        abs_t = None
    synth = data.get("llm_synthesis")
    answer = data.get("answer")
    if not answer:
        out = {"ok": False, "error": "agent-qa returned empty answer"}
        _CACHE[q] = (time.time(), out)
        return out
    out = {
        "ok": True,
        "answer": answer,
        "evidence_source": src,
        "evidence_start_s": t0,
        "evidence_end_s": t1,
        "evidence_abs_t": abs_t,
        "n_evidence": len(rows),
        "model_id": synth.get("model") if isinstance(synth, dict) else None,
        "time_restricted": False,
        "tool_used": data.get("tool_used"),
        "label": "Event VSS agent (agent-qa): searches the whole archive, no as_of cutoff",
    }
    _CACHE[q] = (time.time(), out)
    return out
