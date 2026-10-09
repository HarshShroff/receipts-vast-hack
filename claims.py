"""SQLite claim store for Receipts. Standard library only.

Time model: every timestamp is float seconds on ONE shared timeline (seconds
since midnight in the synthetic demo; use stream-epoch seconds on real footage).
A claim says "entity.attribute at location had value V, seen in clip C during
[t_start, t_end]". observed_at is when the footage showed it (default t_end).
Every sighting of a claim (first sighting + later corroborations) is one row in
`evidence`, so answers can cite the most recent clip that confirmed the claim.
"""
import re
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips(
  clip_id TEXT PRIMARY KEY, path TEXT, t_start REAL NOT NULL, t_end REAL NOT NULL,
  description TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS claims(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity TEXT NOT NULL, attribute TEXT NOT NULL, value TEXT NOT NULL,
  location TEXT NOT NULL DEFAULT '',
  clip_id TEXT NOT NULL, t_start REAL NOT NULL, t_end REAL NOT NULL,
  observed_at REAL NOT NULL, confidence REAL NOT NULL DEFAULT 0.8,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded','stale')),
  superseded_by INTEGER REFERENCES claims(id), rule_fired TEXT, source TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS evidence(
  id INTEGER PRIMARY KEY AUTOINCREMENT, claim_id INTEGER NOT NULL REFERENCES claims(id),
  clip_id TEXT NOT NULL, t_start REAL NOT NULL, t_end REAL NOT NULL, observed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT NOT NULL, claim_id INTEGER,
  other_claim_id INTEGER, detail TEXT);
CREATE INDEX IF NOT EXISTS claims_key ON claims(entity, attribute, location);
-- Keys whose current claim has not been re-observed while footage kept rolling.
CREATE VIEW IF NOT EXISTS data_gaps AS
SELECT c.entity, c.attribute, c.location, c.id AS claim_id,
  (SELECT MAX(observed_at) FROM evidence e WHERE e.claim_id = c.id) AS last_observed,
  (SELECT MAX(t_end) FROM clips) AS footage_ends,
  (SELECT MAX(t_end) FROM clips) - (SELECT MAX(observed_at) FROM evidence e WHERE e.claim_id = c.id)
    AS unobserved_seconds
FROM claims c WHERE c.status != 'superseded';
"""


def parse_t(x):
    """'10:42' / '10:42:30' / number -> float seconds.

    Bare numerics (query strings, JSON, slider values) are already seconds.
    Only colon forms are treated as clock times.
    """
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", s):
        return float(s)
    parts = [float(p) for p in s.split(":")]
    while len(parts) < 3:
        parts.append(0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def fmt_t(s):
    s = int(round(s))
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h:02d}:{m:02d}" + (f":{sec:02d}" if sec else "")


def norm(v):
    return re.sub(r"\s+", " ", str(v).strip().lower())


def parse_key(key):
    """('exit','state','north_exit') or 'exit|state|north_exit' -> tuple of 3."""
    parts = key.split("|") if isinstance(key, str) else list(key)
    return tuple(norm(p) for p in (parts + [""])[:3])


class Store:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def add_clip(self, clip_id, t_start, t_end, description="", path=""):
        self.db.execute("INSERT OR REPLACE INTO clips VALUES(?,?,?,?,?)",
                        (clip_id, path, parse_t(t_start), parse_t(t_end), description))
        self.db.commit()

    def log(self, rule_id, claim_id=None, other=None, detail=""):
        self.db.execute("INSERT INTO audit(rule_id,claim_id,other_claim_id,detail) VALUES(?,?,?,?)",
                        (rule_id, claim_id, other, detail))

    def audit(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM audit ORDER BY id")]

    def add_evidence(self, claim_id, clip_id, t_start, t_end, observed_at):
        self.db.execute("INSERT INTO evidence(claim_id,clip_id,t_start,t_end,observed_at) VALUES(?,?,?,?,?)",
                        (claim_id, clip_id, t_start, t_end, observed_at))

    def coverage_end(self, as_of):
        """Latest footage time we hold at or before as_of (None if no footage yet)."""
        r = self.db.execute("SELECT MAX(t_end) m FROM clips WHERE t_start <= ?", (as_of,)).fetchone()
        return None if r["m"] is None else min(r["m"], as_of)

    def is_superseded_at(self, claim_id, as_of):
        r = self.db.execute("""SELECT 1 FROM claims c JOIN claims s ON s.id = c.superseded_by
                               WHERE c.id = ? AND s.observed_at <= ?""", (claim_id, as_of)).fetchone()
        return r is not None

    def observations(self, as_of):
        """Raw per-clip sightings up to as_of (what a memory-less retriever sees)."""
        rows = self.db.execute("""SELECT c.id AS claim_id, c.entity, c.attribute, c.value, c.location,
                    e.clip_id, e.t_start, e.t_end, e.observed_at
                    FROM evidence e JOIN claims c ON c.id = e.claim_id
                    WHERE e.observed_at <= ? ORDER BY e.observed_at, e.id""", (as_of,))
        return [dict(r) for r in rows]
