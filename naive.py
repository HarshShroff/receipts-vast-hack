"""Retrieval-only baseline: what a typical video agent answers.

Ranks every indexed caption by keyword overlap with the question, ignoring time
completely (no as_of, no supersession), and answers from the best-ranked caption
that states the asked (entity, attribute). Same caption->claim extractor as
Receipts, so the only difference between the two is time awareness.

Ties are broken by clip_id (an arbitrary hash order), not by recency, so the
baseline is neither helped nor hurt by caption order.
"""
import re

from vss_source import caption_to_claims

_STOP = {
    "a", "an", "the", "is", "are", "was", "were", "be", "in", "on", "at", "of", "to",
    "this", "that", "it", "any", "there", "right", "now", "currently", "does", "do",
    "what", "which", "who", "how", "camera", "cam", "zone", "aisle",
}
_SYNONYMS = {
    "moving": {"moving", "moves", "driving", "drives", "travels", "motion"},
    "parked": {"parked", "stationary", "stopped", "still", "idle"},
    "worker": {"worker", "workers", "person", "people", "individual", "man", "woman"},
    "blocked": {"blocked", "obstructed", "obstruction", "blocking"},
}


def _tokens(text):
    out = set()
    for w in re.findall(r"[a-z]+", (text or "").lower()):
        if w in _STOP:
            continue
        out.add(w)
        for root, syns in _SYNONYMS.items():
            if w in syns:
                out.add(root)
    return out


def rank_clips(store, question):
    """All clips, best keyword match first. No time filter."""
    q = _tokens(question)
    rows = store.db.execute(
        "SELECT clip_id, path, t_start, t_end, description FROM clips").fetchall()
    scored = [(len(q & _tokens(r["description"])), r["clip_id"], r) for r in rows]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [(score, r) for score, _cid, r in scored]


def naive_answer(store, question, entity, attribute, camera_id=""):
    """Answer from the best-matching caption that states (entity, attribute)."""
    for score, r in rank_clips(store, question):
        claims = caption_to_claims(
            r["description"], camera_id=camera_id, source_uri=r["path"] or r["clip_id"],
            t_start=r["t_start"], t_end=r["t_end"])
        for c in claims:
            if c["entity"] == entity and c["attribute"] == attribute:
                return {
                    "answer": c["value"],
                    "clip_id": r["clip_id"],
                    "path": r["path"],
                    "t_start": r["t_start"],
                    "t_end": r["t_end"],
                    "caption": r["description"],
                    "match_score": score,
                    "method": "keyword retrieval over all captions, no time awareness (simulated baseline)",
                }
    return {"answer": None, "clip_id": None, "caption": None,
            "method": "keyword retrieval over all captions, no time awareness (simulated baseline)"}
