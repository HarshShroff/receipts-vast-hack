"""Deterministic supersession rule engine. No model calls; every decision is
written to the audit table with a stable rule ID."""
from claims import norm, parse_t

RULES = {
    "FIRST_CLAIM": "no earlier claim for (entity, attribute, location): stored active",
    "SUPERSEDE_NEWER_CONTRADICTS": "later observation with a different value retires the older active claim (kept, status=superseded)",
    "CORROBORATE_SAME_VALUE": "same value seen again: one claim stays active, confidence rises, new clip linked as evidence",
    "OLDER_OR_TIED_CONTRADICTION": "different value but not newer than the active claim (late arrival or tie): stored as superseded",
    "STALE_MAX_AGE": "active claim not re-observed within max_age: status=stale (sweep_stale only)",
}


def _noisy_or(a, b):
    return min(0.99, 1 - (1 - a) * (1 - b))


def ingest_claim(store, entity, attribute, value, clip_id, t_start, t_end,
                 location="", confidence=0.8, source="", observed_at=None):
    """Insert one claim, applying the rules. Returns {rule_id, claim_id, superseded_ids}."""
    entity, attribute, location = norm(entity), norm(attribute), norm(location)
    t_start, t_end = parse_t(t_start), parse_t(t_end)
    obs = t_end if observed_at is None else parse_t(observed_at)
    db = store.db
    active = db.execute("""SELECT * FROM claims WHERE entity=? AND attribute=? AND location=?
                           AND status!='superseded' ORDER BY observed_at DESC, id DESC LIMIT 1""",
                        (entity, attribute, location)).fetchone()

    def insert(status, rule, superseded_by=None):
        cur = db.execute("""INSERT INTO claims(entity,attribute,value,location,clip_id,t_start,t_end,
                observed_at,confidence,status,superseded_by,rule_fired,source)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (entity, attribute, str(value), location, clip_id, t_start, t_end, obs,
                          confidence, status, superseded_by, rule, source))
        store.add_evidence(cur.lastrowid, clip_id, t_start, t_end, obs)
        return cur.lastrowid

    out = {"rule_id": None, "claim_id": None, "superseded_ids": []}
    if active is None:
        out.update(rule_id="FIRST_CLAIM", claim_id=insert("active", "FIRST_CLAIM"))
        store.log("FIRST_CLAIM", out["claim_id"], detail=f"{entity}/{attribute}/{location}={value}")
    elif norm(active["value"]) == norm(value):
        store.add_evidence(active["id"], clip_id, t_start, t_end, obs)
        db.execute("UPDATE claims SET confidence=?, status='active' WHERE id=?",
                   (_noisy_or(active["confidence"], confidence), active["id"]))
        out.update(rule_id="CORROBORATE_SAME_VALUE", claim_id=active["id"])
        store.log("CORROBORATE_SAME_VALUE", active["id"], detail=f"seen again in {clip_id}")
    elif obs > active["observed_at"]:
        new = insert("active", "SUPERSEDE_NEWER_CONTRADICTS")
        db.execute("UPDATE claims SET status='superseded', superseded_by=?, rule_fired=? WHERE id=?",
                   (new, "SUPERSEDE_NEWER_CONTRADICTS", active["id"]))
        out.update(rule_id="SUPERSEDE_NEWER_CONTRADICTS", claim_id=new, superseded_ids=[active["id"]])
        store.log("SUPERSEDE_NEWER_CONTRADICTS", new, active["id"],
                  f"{active['value']!r} (clip {active['clip_id']}) -> {value!r} (clip {clip_id})")
    else:
        new = insert("superseded", "OLDER_OR_TIED_CONTRADICTION", superseded_by=active["id"])
        out.update(rule_id="OLDER_OR_TIED_CONTRADICTION", claim_id=new)
        store.log("OLDER_OR_TIED_CONTRADICTION", new, active["id"], f"{value!r} not newer than active")
    db.commit()
    return out


def sweep_stale(store, as_of, max_age):
    """Mark active claims not re-observed within max_age as stale. Returns ids changed."""
    rows = store.db.execute("""SELECT c.id FROM claims c WHERE c.status='active' AND
        (SELECT MAX(observed_at) FROM evidence e WHERE e.claim_id=c.id AND e.observed_at<=?) < ?""",
                            (as_of, as_of - max_age)).fetchall()
    for r in rows:
        store.db.execute("UPDATE claims SET status='stale' WHERE id=?", (r["id"],))
        store.log("STALE_MAX_AGE", r["id"], detail=f"max_age={max_age}s at {as_of}")
    store.db.commit()
    return [r["id"] for r in rows]
