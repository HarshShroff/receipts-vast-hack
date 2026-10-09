#!/usr/bin/env python3
"""Local clip viewer server. Standard library only.

    python3 viewer/ingest_clips.py && python3 viewer/serve.py     # http://localhost:8765

Serves clips/*.mp4 with HTTP Range support (browsers need 206 responses to seek), the
static page, and a small JSON API that merges each clip's sidecar with claim status from
receipts.db. All page URLs are relative so the app also works behind a path prefix.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from answer import answer  # noqa: E402
from claims import Store, norm, parse_t  # noqa: E402
from viewer.ingest_clips import DEFAULT_CLIPS, DEFAULT_DB, build_store, discover_clips, scene_for  # noqa: E402
from viewer.sidecar import CREATING_RULES, clip_id_for, load_sidecar, sidecar_path, track_zone_intervals  # noqa: E402

PORT = int(os.environ.get("PORT", "8765"))
CLIPS_DIR = os.environ.get("VIEWER_CLIPS", DEFAULT_CLIPS)
# VIEWER_DB wins so this store stays separate from the warehouse RECEIPTS_DB.
DB_PATH = os.environ.get("VIEWER_DB") or os.environ.get("RECEIPTS_DB", DEFAULT_DB)
QUESTIONS_PATH = os.environ.get(
    "QUESTIONS_PATH", os.path.join(ROOT, "eval", "questions_real.json"))
_ensure_lock = threading.Lock()
STATIC_DIR = os.path.join(HERE, "static")
MAX_AGE_DEFAULT = 20.0  # seconds; the local timeline is ~85 s long
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".json": "application/json"}
CHUNK = 64 * 1024

_local = threading.local()
_meta_cache = {}


def ensure_local_db():
    """Build the local-clip store once clips are on disk. A missing folder is not an error:
    the browse page still lists warehouse segments, and a later copy of clips/ is picked up."""
    if os.path.exists(DB_PATH) or not os.path.isdir(CLIPS_DIR):
        return
    if not discover_clips(CLIPS_DIR):
        return
    with _ensure_lock:
        if os.path.exists(DB_PATH):
            return
        try:
            parent = os.path.dirname(os.path.abspath(DB_PATH))
            if parent:
                os.makedirs(parent, exist_ok=True)
            build_store(DB_PATH, CLIPS_DIR, write_back=False)
        except Exception as e:
            sys.stderr.write("viewer ingest skipped: %s\n" % e)


def get_store():
    """One sqlite connection per handler thread; reopened if ingest_clips.py rebuilt the file."""
    ensure_local_db()
    if not os.path.exists(DB_PATH):
        return None
    st = os.stat(DB_PATH)
    key = (DB_PATH, st.st_ino, st.st_mtime_ns)
    if getattr(_local, "key", None) != key:
        _local.store = Store(DB_PATH)
        _local.key = key
    return _local.store


def get_meta(clip_path):
    sp = sidecar_path(clip_path)
    if not os.path.exists(sp):
        return None
    mtime = os.stat(sp).st_mtime_ns
    cached = _meta_cache.get(sp)
    if cached is None or cached[0] != mtime:
        cached = (mtime, load_sidecar(sp))
        _meta_cache[sp] = cached
    return json.loads(json.dumps(cached[1]))  # private copy; handlers decorate it


def clip_paths():
    return {clip_id_for(p): p for p in discover_clips(CLIPS_DIR)}


# ---------------------------------------------------------------- API ------

def created_rule(store, claim_id):
    """Rule that created the claim (claims.rule_fired is rewritten when it is later superseded)."""
    r = store.db.execute(
        "SELECT rule_id FROM audit WHERE claim_id=? AND rule_id IN (?,?,?) ORDER BY id LIMIT 1",
        (claim_id,) + CREATING_RULES).fetchone()
    return r["rule_id"] if r else None


def db_rows(store):
    return {r["clip_id"]: dict(r) for r in store.db.execute("SELECT * FROM clips")} if store else {}


def api_questions():
    """Labeled questions (eval/questions_real.json). clip_origin is the clip clock start."""
    if not os.path.isfile(QUESTIONS_PATH):
        return {"questions": [], "clip_clock": {}}
    with open(QUESTIONS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    clock = data.get("clip_clock") or {}
    questions = []
    for q in data.get("questions") or []:
        item = dict(q)
        origin = clock.get(q.get("expected_clip_id"))
        if origin:
            item["clip_origin"] = origin
        questions.append(item)
    return {"questions": questions, "clip_clock": clock, "label": data.get("_label", "")}


RESULTS_PATH = os.environ.get("RESULTS_PATH", os.path.join(ROOT, "eval", "out", "results_real.json"))


def _allow_local_clip(cid):
    """Every clip in the folder. Skip the audio-stripped duplicate of person_moving."""
    c = (cid or "").lower()
    return bool(c) and not c.endswith("_noaudio")


def api_compare():
    """Model comparison: labeled questions + the committed eval results (eval/compare.py output).
    Read-only; never calls a model."""
    qs = api_questions()
    res = {}
    if os.path.isfile(RESULTS_PATH):
        with open(RESULTS_PATH, encoding="utf-8") as f:
            res = json.load(f)
    clock = qs.get("clip_clock") or {}
    clip_id = next(iter(clock), "person_moving")
    path = clip_paths().get(clip_id)
    return {"questions": qs["questions"], "label": qs.get("label", ""),
            "clip_id": clip_id, "clip_url": ("clips/" + os.path.basename(path)) if path else None,
            "clip_start": parse_t(clock[clip_id]) if clip_id in clock else 0.0,
            "summaries": res.get("summaries", {}), "per_question": res.get("per_question", {}),
            "notes": res.get("notes", []), "results_file": os.path.relpath(RESULTS_PATH, ROOT)}


def api_clips():
    """Every mp4 under the clips folder. Bridge and garage stay in the list with person_moving."""
    store = get_store()
    rows = db_rows(store)
    out = []
    for cid, path in clip_paths().items():
        if not _allow_local_clip(cid):
            continue
        meta = get_meta(path)
        row = rows.get(cid)
        tl = (meta or {}).get("timeline") or {}
        video = (meta or {}).get("video") or {}
        t_start = row["t_start"] if row else tl.get("t_start")
        t_end = row["t_end"] if row else tl.get("t_end")
        out.append({"clip_id": cid, "url": "clips/" + os.path.basename(path),
                    "scene": (meta or {}).get("scene") or scene_for(cid),
                    "description": (meta or {}).get("description", ""),
                    "t_start": t_start, "t_end": t_end, "duration": video.get("duration"),
                    "fps": video.get("fps"), "width": video.get("width"), "height": video.get("height"),
                    "has_sidecar": meta is not None, "in_db": row is not None,
                    "n_segments": len((meta or {}).get("segments") or []),
                    "n_tracks": len((meta or {}).get("tracks") or [])})
    starts = [c["t_start"] for c in out if c["t_start"] is not None]
    ends = [c["t_end"] for c in out if c["t_end"] is not None]
    return {"db": store is not None, "t_min": min(starts) if starts else 0.0,
            "t_max": max(ends) if ends else 1.0, "max_age_default": MAX_AGE_DEFAULT, "clips": out}


def claim_db_info(store, clip_id, clip_t0, c):
    """Match one sidecar claim (clip-relative times) to its evidence row + claim; None if not ingested."""
    if store is None or clip_t0 is None:
        return None
    r = store.db.execute("""
        SELECT c.*, e.t_start AS e_t_start, s.clip_id AS sb_clip, s.value AS sb_value
        FROM evidence e JOIN claims c ON c.id = e.claim_id LEFT JOIN claims s ON s.id = c.superseded_by
        WHERE e.clip_id=? AND ABS(e.t_start-?)<0.005 AND ABS(e.t_end-?)<0.005
          AND c.entity=? AND c.attribute=? AND c.location=? AND c.value=?""",
        (clip_id, clip_t0 + c["t_start"], clip_t0 + c["t_end"],
         norm(c["entity"]), norm(c["attribute"]), norm(c["location"]), str(c["value"]))).fetchone()
    if r is None:
        return None
    first = r["clip_id"] == clip_id and abs(r["t_start"] - r["e_t_start"]) < 0.005
    return {"claim_id": r["id"], "status": r["status"].upper(), "rule_fired": r["rule_fired"],
            "created_rule": created_rule(store, r["id"]), "confidence": r["confidence"],
            "superseded_by": r["superseded_by"], "superseded_by_clip": r["sb_clip"],
            "superseded_by_value": r["sb_value"], "sighting": "first" if first else "corroboration",
            "source": r["source"]}


def api_clip_meta(clip_id):
    path = clip_paths().get(clip_id)
    if path is None:
        return None
    meta = get_meta(path)
    if meta is None:
        return {"clip_id": clip_id, "has_sidecar": False}
    store = get_store()
    row = db_rows(store).get(clip_id)
    t0 = row["t_start"] if row else None
    scene = meta.get("scene") or clip_id
    for seg in meta.get("segments") or []:
        for c in seg.get("claims") or []:
            full = {"entity": c["entity"], "attribute": c["attribute"], "value": c["value"],
                    "location": c.get("location", scene),
                    "t_start": c.get("t_start", seg["t_start"]), "t_end": c.get("t_end", seg["t_end"])}
            c["db"] = claim_db_info(store, clip_id, t0, full)
    zones = meta.get("zones") or {}
    track_claims = []
    for tr in meta.get("tracks") or []:
        for iv in track_zone_intervals(tr, zones):
            full = {"entity": f"{iv['label']}_{iv['track_id']}", "attribute": "zone", "value": iv["zone"],
                    "location": scene, "t_start": iv["t_start"], "t_end": iv["t_end"]}
            track_claims.append(dict(iv, entity=full["entity"], db=claim_db_info(store, clip_id, t0, full)))
    meta["track_claims"] = track_claims
    meta["has_sidecar"] = True
    meta["in_db"] = row is not None
    return meta


def api_claims(clip_id=None, status=None):
    store = get_store()
    if store is None:
        return []
    sql = ("SELECT c.*, e.clip_id AS sighting_clip, e.t_start AS sighting_t_start, e.t_end AS sighting_t_end "
           "FROM evidence e JOIN claims c ON c.id = e.claim_id")
    where, args = [], []
    if clip_id:
        where.append("e.clip_id=?")
        args.append(clip_id)
    if status:
        where.append("c.status=?")
        args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY e.observed_at, e.id"
    return [dict(r) for r in store.db.execute(sql, args)]


def api_keys():
    store = get_store()
    if store is None:
        return []
    return [dict(r) for r in store.db.execute(
        "SELECT entity, attribute, location, COUNT(*) AS n FROM claims GROUP BY 1,2,3 ORDER BY 1,2,3")]


def seconds(x):
    """Query values are seconds ('20', '20.5'); only 'H:M[:S]' strings go through parse_t,
    which would read a bare '20' as 20 hours."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return parse_t(x)


def ask(entity, attribute, location, as_of, max_age=MAX_AGE_DEFAULT):
    """answer() in shared-timeline seconds (the slider's unit), with clip-relative citation times.
    claim_status and stale are separate: a claim active as of as_of can still be stale."""
    store = get_store()
    if store is None:
        return {"error": "no receipts.db; run viewer/ingest_clips.py"}
    as_of = seconds(as_of)
    r = answer(store, (entity, attribute, location), as_of, max_age)
    rows = db_rows(store)

    def cite(clip_id, t_start, t_end):
        row = rows.get(clip_id)
        t0 = row["t_start"] if row else None
        return {"clip_id": clip_id, "clip_url": ("clips/" + os.path.basename(row["path"])) if row and row["path"] else None,
                "clip_t_start": t0, "t_start": t_start, "t_end": t_end,
                "rel_t_start": None if t0 is None else t_start - t0,
                "rel_t_end": None if t0 is None else t_end - t0}

    out = {"answer": r["answer"], "claim_status": "ACTIVE" if r["claim_id"] else "NO_EVIDENCE",
           "stale": bool(r["stale"]), "stale_reason": r["stale_reason"],
           "data_gap_note": r["data_gap_note"], "claim_id": r["claim_id"],
           "created_rule": created_rule(store, r["claim_id"]) if r["claim_id"] else None,
           "current_status": None, "as_of": as_of, "max_age": max_age,
           "key": {"entity": norm(entity), "attribute": norm(attribute), "location": norm(location)},
           "history": []}
    if r["claim_id"]:
        row = store.db.execute("SELECT status FROM claims WHERE id=?", (r["claim_id"],)).fetchone()
        out["current_status"] = row["status"].upper() if row else None
        out.update(cite(r["clip_id"], r["t_start"], r["t_end"]))
    for h in r.get("history") or []:
        out["history"].append(dict(cite(h["clip_id"], h["t_start"], h["t_end"]),
                                   claim_id=h["claim_id"], value=h["value"], status="SUPERSEDED",
                                   created_rule=created_rule(store, h["claim_id"])))
    return out


# ------------------------------------------------------------- HTTP --------

def parse_range(header, size):
    """'bytes=a-b' | 'bytes=a-' | 'bytes=-n' -> (start, end) inclusive, clamped. None = whole file.
    Raises ValueError when the range cannot be satisfied (-> 416)."""
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    spec = header[len("bytes="):].strip()
    a, sep, b = spec.partition("-")
    if not sep:
        return None
    try:
        if a == "" and b != "":
            n = int(b)
            if n <= 0:
                raise ValueError("empty suffix range")
            return max(0, size - n), size - 1
        start = int(a)
        end = int(b) if b != "" else size - 1
    except ValueError:
        if a == "" and b == "":
            return None
        raise
    if start >= size or start > end or start < 0:
        raise ValueError("unsatisfiable range")
    return start, min(end, size - 1)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _headers(self, code, ctype, length, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()

    def _send(self, code, body, ctype="text/plain; charset=utf-8", head=False):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self._headers(code, ctype, len(data))
        if not head:
            self.wfile.write(data)

    def _json(self, code, obj, head=False):
        self._send(code, json.dumps(obj), "application/json", head)

    def _static(self, name, head):
        path = os.path.join(STATIC_DIR, os.path.basename(name))
        ext = os.path.splitext(path)[1]
        if ext not in MIME or not os.path.isfile(path):
            return self._send(404, "not found", head=head)
        with open(path, "rb") as f:
            self._send(200, f.read(), MIME[ext], head)

    def _video(self, name, head):
        base = os.path.basename(unquote(name))
        path = os.path.join(CLIPS_DIR, base)
        if not base.lower().endswith(".mp4") or not os.path.isfile(path):
            return self._send(404, "not found", head=head)
        size = os.path.getsize(path)
        try:
            rng = parse_range(self.headers.get("Range"), size)
        except ValueError:
            self._headers(416, "text/plain", 0, [("Content-Range", f"bytes */{size}")])
            return
        start, end = rng if rng else (0, size - 1)
        extra = [("Accept-Ranges", "bytes")]
        if rng:
            extra.append(("Content-Range", f"bytes {start}-{end}/{size}"))
        self._headers(206 if rng else 200, "video/mp4", end - start + 1, extra)
        if head:
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True  # browsers abort range requests all the time

    def do_HEAD(self):
        self._route(head=True)

    def do_GET(self):
        self._route(head=False)

    def _route(self, head):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path.rstrip("/") == "/browse" or path.startswith("/browse/"):
            path = path[len("/browse"):] or "/"
        path = path.rstrip("/") or "/"
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html", head)
            if path in ("/compare", "/compare.html"):
                return self._static("compare.html", head)
            if path == "/api/compare":
                return self._json(200, api_compare(), head)
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):], head)
            if path.startswith("/clips/"):
                return self._video(path[len("/clips/"):], head)
            if path == "/health":
                return self._json(200, {"ok": True, "db": os.path.exists(DB_PATH), "clips": len(clip_paths())}, head)
            if path == "/api/clips":
                return self._json(200, api_clips(), head)
            if path == "/api/questions":
                return self._json(200, api_questions(), head)
            if path.startswith("/api/clips/") and path.endswith("/meta"):
                meta = api_clip_meta(path[len("/api/clips/"):-len("/meta")])
                return self._json(200, meta, head) if meta else self._json(404, {"error": "no such clip"}, head)
            if path == "/api/claims":
                return self._json(200, api_claims(q.get("clip_id"), q.get("status")), head)
            if path == "/api/keys":
                return self._json(200, api_keys(), head)
            if path == "/api/answer":
                if "entity" not in q or "attribute" not in q:
                    return self._json(400, {"error": "entity and attribute are required"}, head)
                max_age = float(q.get("max_age", MAX_AGE_DEFAULT))
                return self._json(200, ask(q["entity"], q["attribute"], q.get("location", ""),
                                           q.get("as_of", api_clips()["t_max"]), max_age), head)
            return self._send(404, "not found", head=head)
        except Exception as e:  # keep the server up; surface the error to the page
            self.log_message("error on %s: %r", self.path, e)
            self._json(500, {"error": str(e)}, head)


def serve(port=PORT, host="127.0.0.1"):
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"viewer: http://{host}:{httpd.server_address[1]}  clips={CLIPS_DIR}  db={DB_PATH}", flush=True)
    return httpd


if __name__ == "__main__":
    serve().serve_forever()
