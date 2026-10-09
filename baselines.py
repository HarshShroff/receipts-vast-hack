"""Comparison baselines. Same interface as Receipts:
    system.answer(store, question_text, question_key, as_of) -> dict
(question_key is unused by baselines; they only see the question text.)
They read raw per-clip observations and never honour supersession."""
import re

from claims import parse_t

STOP = {"is", "the", "a", "an", "of", "in", "at", "to", "still", "now", "what", "where", "are",
        "was", "does", "do", "can", "people", "get", "out", "it", "on", "there", "currently"}


def tokens(text):
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t and t not in STOP}


def _score(question, o):
    return len(tokens(question) & tokens(f"{o['entity']} {o['attribute']} {o['value']} {o['location']}"))


def _result(o, retrieved=()):
    if o is None:
        return {"answer": None, "claim_id": None, "clip_id": None, "t_start": None, "t_end": None,
                "stale": False, "retrieved": []}
    return {"answer": o["value"], "claim_id": o["claim_id"], "clip_id": o["clip_id"],
            "t_start": o["t_start"], "t_end": o["t_end"], "stale": False,
            "retrieved": [x["claim_id"] for x in retrieved]}


def _best(question, obs, tiebreak="newest"):
    """Top-k ranked by keyword overlap; ties broken by recency ('newest') or insertion order ('oldest')."""
    scored = [(_score(question, o), o) for o in obs]
    scored = [s for s in scored if s[0] > 0]
    sign = 1 if tiebreak == "newest" else -1
    scored.sort(key=lambda s: (s[0], sign * s[1]["observed_at"]), reverse=True)
    return [o for _, o in scored]


class NoMemory:
    """B1: answers from the single most recent clip only."""
    name = "B1 no-memory (latest clip)"

    def answer(self, store, question, key, as_of):
        as_of = parse_t(as_of)
        r = store.db.execute("SELECT clip_id FROM clips WHERE t_start<=? ORDER BY t_start DESC LIMIT 1",
                             (as_of,)).fetchone()
        obs = [o for o in store.observations(as_of) if r and o["clip_id"] == r["clip_id"]]
        ranked = _best(question, obs)
        return _result(ranked[0] if ranked else None, ranked[:1])


class RagAll:
    """B2: naive retrieval over every observation, supersession ignored, top-k cited."""
    def __init__(self, k=3, tiebreak="oldest"):
        self.k, self.tiebreak = k, tiebreak
        self.name = f"B2 RAG-all (ties->{tiebreak})"

    def answer(self, store, question, key, as_of):
        ranked = _best(question, store.observations(parse_t(as_of)), self.tiebreak)[: self.k]
        return _result(ranked[0] if ranked else None, ranked)


class LastN:
    """B3: observations from the last N clips only."""
    def __init__(self, n=2):
        self.n = n
        self.name = f"B3 last-{n}-clips window"

    def answer(self, store, question, key, as_of):
        as_of = parse_t(as_of)
        ids = [r["clip_id"] for r in store.db.execute(
            "SELECT clip_id FROM clips WHERE t_start<=? ORDER BY t_start DESC LIMIT ?", (as_of, self.n))]
        obs = [o for o in store.observations(as_of) if o["clip_id"] in ids]
        ranked = _best(question, obs)
        return _result(ranked[0] if ranked else None, ranked[:1])


ALL = [NoMemory(), RagAll(tiebreak="oldest"), RagAll(tiebreak="newest"), LastN(2)]
