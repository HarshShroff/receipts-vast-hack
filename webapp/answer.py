"""Receipts answer(): latest verified claim + clip + time, with stale and data-gap flags."""
from claims import fmt_t, parse_key, parse_t

DEFAULT_MAX_AGE = 15 * 60  # seconds


def answer(store, question_key, as_of, max_age=DEFAULT_MAX_AGE):
    """question_key: (entity, attribute, location) or 'entity|attribute|location'.
    Only footage observed at or before as_of is used (the agent cannot see the future)."""
    as_of = parse_t(as_of)
    e, a, loc = parse_key(question_key)
    out = {"answer": None, "claim_id": None, "clip_id": None, "t_start": None, "t_end": None,
           "status": "no_evidence", "stale": False, "stale_reason": None,
           "data_gap_note": None, "history": []}
    cov = store.coverage_end(as_of)
    rows = store.db.execute("""
        SELECT c.* FROM claims c LEFT JOIN claims s ON s.id = c.superseded_by
        WHERE c.entity=? AND c.attribute=? AND c.location=? AND c.observed_at<=?
          AND (c.superseded_by IS NULL OR s.observed_at > ?)
        ORDER BY c.observed_at DESC, c.id DESC""", (e, a, loc, as_of, as_of)).fetchall()
    if not rows:
        out["data_gap_note"] = ("no footage yet" if cov is None
                                else f"no claim about {e}/{a}/{loc or '-'} in footage up to {fmt_t(cov)}")
        return out
    c = rows[0]
    ev = store.db.execute("""SELECT * FROM evidence WHERE claim_id=? AND observed_at<=?
                             ORDER BY observed_at DESC, id DESC LIMIT 1""", (c["id"], as_of)).fetchone()
    out.update(answer=c["value"], claim_id=c["id"], clip_id=ev["clip_id"], t_start=ev["t_start"],
               t_end=ev["t_end"], status="active")
    reasons = []
    age = as_of - ev["observed_at"]
    if age > max_age:
        reasons.append(f"last confirmed {fmt_t(ev['observed_at'])}, {age / 60:.0f} min before as_of "
                       f"(max_age {max_age / 60:.0f} min)")
        out["data_gap_note"] = (f"no newer observation of {e}/{a}/{loc or '-'} since "
                                f"{fmt_t(ev['observed_at'])}; answer may be stale")
    if cov is not None and cov < as_of:
        reasons.append(f"footage ends {fmt_t(cov)}, before as_of {fmt_t(as_of)}")
        out["data_gap_note"] = f"no footage after {fmt_t(cov)}, answer may be stale"
    if cov is None:
        out["data_gap_note"] = "no footage yet"
    if reasons:
        out.update(stale=True, status="stale", stale_reason="; ".join(reasons))
    out["history"] = [{"claim_id": r["id"], "value": r["value"], "clip_id": r["clip_id"],
                       "t_start": r["t_start"], "t_end": r["t_end"], "status": "superseded"}
                      for r in store.db.execute("""SELECT c.* FROM claims c JOIN claims s ON s.id = c.superseded_by
                      WHERE c.entity=? AND c.attribute=? AND c.location=? AND s.observed_at<=?
                      ORDER BY c.observed_at""", (e, a, loc, as_of))]
    return out
