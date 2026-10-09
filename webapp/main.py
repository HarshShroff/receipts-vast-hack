#!/usr/bin/env python3
"""Receipts ops board — dark control-room UI. Stdlib only. Routes at / (Ingress strips /app)."""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from answer import answer  # noqa: E402
from claims import Store, fmt_t, parse_t  # noqa: E402
from ingest_adapter import ingest_clip  # noqa: E402
from vss_source import VssSource, fetch_warehouse_segments, login  # noqa: E402

PORT = int(os.environ.get("PORT", "8080"))
CAMERA = os.environ.get("CAMERA_ID", "sdg_warehouse_cam-2")
LOCATION = os.environ.get("VSS_LOCATION", "warehouse3")
DB_PATH = os.environ.get("RECEIPTS_DB", "/tmp/receipts.db")
LIVE_INTERVAL = float(os.environ.get("LIVE_INTERVAL_SEC", "2"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL_SEC", "45"))
ZONES = ("left_aisle", "center_aisle", "right_aisle", "loading_area", "wall_area")
STALE_BANNER_GAP = 120.0  # seconds of no footage → STALE banner

STATE = {
    "store": None,
    "token": None,
    "backend": None,
    "t_min": 0.0,
    "t_max": 1.0,
    "error": None,
    "segment_count": 0,
    "ready": False,
    "known_clips": set(),
    "event_log": [],  # chronological replay events
    "default_as_of": None,  # first forklift moving→parked supersede
    "default_question": "Is the forklift on cam-2 moving?",
    "frame_paths": {},
    "lock": threading.RLock(),
}
SUBSCRIBERS: list[queue.Queue] = []
SUB_LOCK = threading.Lock()


def _publish(event):
    with SUB_LOCK:
        dead = []
        for q in SUBSCRIBERS:
            try:
                q.put_nowait(event)
            except queue.Full:
                dead.append(q)
        for q in dead:
            SUBSCRIBERS.remove(q)


def _subscribe():
    q = queue.Queue(maxsize=256)
    with SUB_LOCK:
        SUBSCRIBERS.append(q)
    return q


def _unsubscribe(q):
    with SUB_LOCK:
        if q in SUBSCRIBERS:
            SUBSCRIBERS.remove(q)


def public_backend():
    return (os.environ.get("PUBLIC_VSS_URL")
            or os.environ.get("INGRESS_URL")
            or STATE.get("backend")
            or "").rstrip("/")


def stream_url(source, clip_id=None):
    """Browser-facing clip URL — proxied through this app (avoids dead cross-origin players)."""
    if clip_id:
        from urllib.parse import quote
        return f"api/clip?clip_id={quote(clip_id, safe='')}"
    if source:
        from urllib.parse import quote
        return f"api/clip?source={quote(source, safe='')}"
    return None


def _vss_stream_request(source):
    """Build authenticated request to VSS /videos/stream (token is a required query param)."""
    import urllib.request
    from urllib.parse import quote
    backend = (STATE.get("backend") or "").rstrip("/")
    token = STATE.get("token")
    if not backend or not token or not source:
        return None
    url = (f"{backend}/api/v1/videos/stream?"
           f"source={quote(source, safe='')}&token={quote(token, safe='')}")
    return urllib.request.Request(url)


FRAME_CACHE = os.environ.get("FRAME_CACHE", "/tmp/receipts_frames")


def _resolve_source(clip_id=None, source=None):
    with STATE["lock"]:
        store = STATE["store"]
        if clip_id and store:
            row = store.db.execute(
                "SELECT path FROM clips WHERE clip_id=?", (clip_id,)).fetchone()
            if row and row["path"]:
                return row["path"]
    return source


def _download_segment_bytes(source, limit=None):
    """Fetch segment bytes from VSS; optional byte limit for frame extract."""
    import urllib.request
    req = _vss_stream_request(source)
    if not req:
        raise RuntimeError("vss unavailable")
    with urllib.request.urlopen(req, timeout=120) as upstream:
        if limit is None:
            return upstream.read()
        chunks = []
        got = 0
        while got < limit:
            block = upstream.read(min(64 * 1024, limit - got))
            if not block:
                break
            chunks.append(block)
            got += len(block)
        return b"".join(chunks)


def extract_frames(clip_id, source, n=5):
    """Extract n JPEG frames with ffmpeg; cache under FRAME_CACHE. Returns file paths."""
    import hashlib
    import subprocess
    import tempfile
    safe = hashlib.sha1((clip_id or source or "x").encode()).hexdigest()[:16]
    out_dir = os.path.join(FRAME_CACHE, safe)
    os.makedirs(out_dir, exist_ok=True)
    existing = sorted(
        os.path.join(out_dir, f) for f in os.listdir(out_dir) if f.endswith(".jpg"))
    if len(existing) >= n:
        return existing[:n]
    for f in existing:
        try:
            os.remove(f)
        except OSError:
            pass
    raw = _download_segment_bytes(source)
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", tmp_path],
            capture_output=True, text=True, timeout=30, check=False)
        try:
            duration = float(probe.stdout.strip() or "5")
        except ValueError:
            duration = 5.0
        duration = max(duration, 0.5)
        paths = []
        for i in range(n):
            t = duration * (i + 0.5) / n
            out = os.path.join(out_dir, f"f{i:02d}.jpg")
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.3f}",
                 "-i", tmp_path, "-frames:v", "1", "-q:v", "4", out],
                check=True, timeout=60)
            if os.path.exists(out):
                paths.append(out)
        return paths
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def segment_playlist():
    """Ordered clips for the hero player (clip_id, times, path)."""
    from urllib.parse import quote
    store = STATE["store"]
    if not store:
        return []
    rows = store.db.execute(
        "SELECT clip_id, path, t_start, t_end, description FROM clips ORDER BY t_start, t_end"
    ).fetchall()
    return [{
        "clip_id": r["clip_id"],
        "path": r["path"],
        "t_start": r["t_start"],
        "t_end": r["t_end"],
        "caption": (r["description"] or "")[:240],
        "stream_url": stream_url(r["path"], clip_id=r["clip_id"]),
        "frames_url": f"api/frames?clip_id={quote(r['clip_id'], safe='')}",
    } for r in rows]


def _seg_label(clip_id):
    """Short segment tag like 'seg 002' from filename."""
    if not clip_id:
        return "seg ?"
    m = __import__("re").search(r"segment_(\d+)", clip_id)
    if m:
        return f"seg {m.group(1)}"
    return clip_id[-24:]


def _ingest_one(store, seg, src):
    """Ingest one segment; return (results, events)."""
    if not seg.get("description"):
        seg = dict(seg)
        seg["description"] = seg.get("caption") or ""
    before_audit = store.db.execute("SELECT MAX(id) m FROM audit").fetchone()["m"] or 0
    results = ingest_clip(store, seg, src, model_name="cosmos_reason_vss")
    events = []
    claims_out = []
    for r in results:
        row = store.db.execute(
            "SELECT entity,attribute,value,location,status,rule_fired FROM claims WHERE id=?",
            (r["claim_id"],)).fetchone()
        if not row:
            continue
        claim = {
            "claim_id": r["claim_id"],
            "entity": row["entity"],
            "attribute": row["attribute"],
            "value": row["value"],
            "location": row["location"],
            "status": row["status"],
            "rule_id": r["rule_id"],
            "superseded_ids": r.get("superseded_ids") or [],
        }
        claims_out.append(claim)
        if r["rule_id"] == "SUPERSEDE_NEWER_CONTRADICTS":
            old_id = (r.get("superseded_ids") or [None])[0]
            old_val = None
            if old_id:
                o = store.db.execute("SELECT value FROM claims WHERE id=?", (old_id,)).fetchone()
                old_val = o["value"] if o else None
            events.append({
                "type": "supersede",
                "entity": row["entity"],
                "attribute": row["attribute"],
                "location": row["location"],
                "old": old_val,
                "new": row["value"],
                "camera": CAMERA,
                "seg": _seg_label(seg["clip_id"]),
                "clip_id": seg["clip_id"],
                "t_end": float(seg["t_end"]),
                "toast": (f"SUPERSEDED: {old_val} → {row['value']} "
                          f"({CAMERA.split('_')[-1]}, {_seg_label(seg['clip_id'])})"),
            })
    events.insert(0, {
        "type": "segment",
        "clip_id": seg["clip_id"],
        "t_start": float(seg["t_start"]),
        "t_end": float(seg["t_end"]),
        "caption": (seg.get("caption") or seg.get("description") or "")[:600],
        "claims": claims_out,
        "audit_from": before_audit,
    })
    return results, events


def build_store():
    backend, token = login()
    segments, backend, token = fetch_warehouse_segments(
        backend, token, camera_id=CAMERA, location=LOCATION)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    store = Store(DB_PATH)
    src = VssSource(camera_id=CAMERA)
    event_log = []
    known = set()
    for seg in segments:
        seg = dict(seg)
        seg["description"] = seg.get("caption") or ""
        _, evs = _ingest_one(store, seg, src)
        event_log.extend(evs)
        known.add(seg["clip_id"])
    t_min = store.db.execute("SELECT MIN(t_start) m FROM clips").fetchone()["m"] or 0.0
    t_max = store.db.execute("SELECT MAX(t_end) m FROM clips").fetchone()["m"] or 1.0
    # Default scrubber: first forklift moving → parked supersede (demo-visible flip).
    default_as_of = float(t_max)
    row = store.db.execute("""
        SELECT n.observed_at AS t
        FROM audit a
        JOIN claims n ON n.id = a.claim_id
        JOIN claims o ON o.id = a.other_claim_id
        WHERE a.rule_id = 'SUPERSEDE_NEWER_CONTRADICTS'
          AND n.entity = 'forklift' AND n.attribute = 'state'
          AND lower(o.value) = 'moving' AND lower(n.value) = 'parked'
        ORDER BY n.observed_at ASC LIMIT 1
    """).fetchone()
    if row and row["t"] is not None:
        default_as_of = float(row["t"])
    with STATE["lock"]:
        STATE.update(
            store=store, token=token, backend=backend.rstrip("/"),
            t_min=float(t_min), t_max=float(t_max),
            segment_count=len(segments), ready=True, error=None,
            known_clips=known, event_log=event_log,
            default_as_of=default_as_of,
        )


def poll_new_segments():
    """Background: pull explore; ingest any clip_id we have not seen (re-ingest landing)."""
    src = VssSource(camera_id=CAMERA)
    while True:
        time.sleep(POLL_INTERVAL)
        if not STATE["ready"]:
            continue
        try:
            segs, backend, token = fetch_warehouse_segments(
                STATE["backend"], STATE["token"],
                camera_id=CAMERA, location=LOCATION)
            with STATE["lock"]:
                STATE["backend"] = backend.rstrip("/")
                STATE["token"] = token
                store = STATE["store"]
                known = STATE["known_clips"]
            new = [s for s in segs if s["clip_id"] not in known]
            if not new:
                _publish({"type": "heartbeat", "t": time.time()})
                continue
            for seg in new:
                seg = dict(seg)
                seg["description"] = seg.get("caption") or ""
                with STATE["lock"]:
                    _, evs = _ingest_one(store, seg, src)
                    STATE["known_clips"].add(seg["clip_id"])
                    STATE["event_log"].extend(evs)
                    STATE["segment_count"] = len(STATE["known_clips"])
                    t_max = store.db.execute("SELECT MAX(t_end) m FROM clips").fetchone()["m"]
                    t_min = store.db.execute("SELECT MIN(t_start) m FROM clips").fetchone()["m"]
                    STATE["t_max"] = float(t_max or STATE["t_max"])
                    STATE["t_min"] = float(t_min or STATE["t_min"])
                for ev in evs:
                    ev = dict(ev)
                    ev["live_new"] = True
                    _publish(ev)
        except Exception as e:
            _publish({"type": "poll_error", "error": str(e)})


def tile_tone(attribute, value, stale):
    if stale or value is None:
        return "stale"
    v = str(value).lower()
    if attribute == "worker_present":
        return "amber" if v == "yes" else "green"
    if attribute == "state":
        return "red" if v == "moving" else "green"
    if attribute == "blocked":
        return "red" if v.startswith("yes") else "green"
    return "amber"


def board(as_of):
    store = STATE["store"]
    as_of = parse_t(as_of)
    cov = store.coverage_end(as_of)
    tiles = []
    # Forklift first — hero tile
    r = answer(store, ("forklift", "state", CAMERA), as_of, max_age=3600)
    last = None
    if r["claim_id"]:
        ev = store.db.execute(
            "SELECT MAX(observed_at) m FROM evidence WHERE claim_id=?",
            (r["claim_id"],)).fetchone()
        last = ev["m"] if ev else r["t_end"]
    age = (as_of - last) if last is not None else None
    stale = bool(r.get("stale")) or (cov is not None and (as_of - cov) > STALE_BANNER_GAP)
    tiles.append({
        "id": "forklift_state",
        "label": "FORKLIFT",
        "sub": CAMERA.replace("sdg_warehouse_", ""),
        "entity": "forklift",
        "attribute": "state",
        "value": r["answer"],
        "display": (
            "MOVING" if r["answer"] == "moving" else
            "PARKED" if r["answer"] == "parked" else
            (str(r["answer"]).upper() if r["answer"] else "—")
        ),
        "tone": tile_tone("state", r["answer"], stale or r["answer"] is None),
        "status": (r["status"] or "no_evidence").upper(),
        "last_confirmed": last,
        "last_confirmed_fmt": fmt_t(last) if last is not None else None,
        "age_sec": age,
        "stale": stale,
        "clip_id": r["clip_id"],
        "stale_banner": (
            f"NO FOOTAGE SINCE {fmt_t(cov)} — may be stale"
            if cov is not None and (as_of - cov) > STALE_BANNER_GAP else None
        ),
    })
    for zone in ZONES:
        r = answer(store, (zone, "worker_present", CAMERA), as_of, max_age=3600)
        last = None
        if r["claim_id"]:
            ev = store.db.execute(
                "SELECT MAX(observed_at) m FROM evidence WHERE claim_id=?",
                (r["claim_id"],)).fetchone()
            last = ev["m"] if ev else r["t_end"]
        age = (as_of - last) if last is not None else None
        zone_stale = bool(r.get("stale"))
        tiles.append({
            "id": f"worker_{zone}",
            "label": "WORKER PRESENT",
            "sub": zone.replace("_", " "),
            "entity": zone,
            "attribute": "worker_present",
            "value": r["answer"],
            "display": (
                "YES" if r["answer"] == "yes" else
                "NO" if r["answer"] == "no" else
                (str(r["answer"]).upper() if r["answer"] else "—")
            ),
            "tone": tile_tone("worker_present", r["answer"], zone_stale or r["answer"] is None),
            "status": (r["status"] or "no_evidence").upper(),
            "last_confirmed": last,
            "last_confirmed_fmt": fmt_t(last) if last is not None else None,
            "age_sec": age,
            "stale": zone_stale,
            "clip_id": r["clip_id"],
            "stale_banner": None,
        })
        # blocked tiles only when we have a claim (re-ingest path)
        rb = answer(store, (zone, "blocked", CAMERA), as_of, max_age=3600)
        if rb["answer"] is not None:
            tiles.append({
                "id": f"blocked_{zone}",
                "label": "AISLE BLOCKED",
                "sub": zone.replace("_", " "),
                "entity": zone,
                "attribute": "blocked",
                "value": rb["answer"],
                "display": ("BLOCKED" if str(rb["answer"]).startswith("yes") else "CLEAR"),
                "tone": tile_tone("blocked", rb["answer"], bool(rb.get("stale"))),
                "status": (rb["status"] or "no_evidence").upper(),
                "last_confirmed": rb["t_end"],
                "last_confirmed_fmt": fmt_t(rb["t_end"]) if rb["t_end"] is not None else None,
                "age_sec": (as_of - rb["t_end"]) if rb["t_end"] is not None else None,
                "stale": bool(rb.get("stale")),
                "clip_id": rb["clip_id"],
                "stale_banner": None,
            })
    return {
        "as_of": as_of,
        "as_of_fmt": fmt_t(as_of),
        "coverage_end": cov,
        "coverage_end_fmt": fmt_t(cov) if cov is not None else None,
        "camera_id": CAMERA,
        "tiles": tiles,
    }


def timeline(as_of=None):
    store = STATE["store"]
    as_of = parse_t(as_of) if as_of is not None else STATE["t_max"]
    rows = store.db.execute("""
        SELECT id, entity, attribute, value, location, clip_id, t_start, t_end,
               observed_at, status, superseded_by, rule_fired
        FROM claims WHERE observed_at <= ? AND location = ?
        ORDER BY observed_at, id
    """, (as_of, CAMERA)).fetchall()
    supersedes = []
    for a in store.db.execute("""
        SELECT a.id, a.claim_id, a.other_claim_id, a.detail,
               n.entity, n.attribute, n.value AS new_v, n.observed_at,
               o.value AS old_v, n.clip_id
        FROM audit a
        JOIN claims n ON n.id = a.claim_id
        LEFT JOIN claims o ON o.id = a.other_claim_id
        WHERE a.rule_id = 'SUPERSEDE_NEWER_CONTRADICTS' AND n.observed_at <= ?
        ORDER BY n.observed_at
    """, (as_of,)):
        supersedes.append({
            "t": a["observed_at"],
            "entity": a["entity"],
            "attribute": a["attribute"],
            "old": a["old_v"],
            "new": a["new_v"],
            "clip_id": a["clip_id"],
            "seg": _seg_label(a["clip_id"]),
        })
    return {
        "t_min": STATE["t_min"],
        "t_max": STATE["t_max"],
        "as_of": as_of,
        "claims": [dict(r) for r in rows],
        "supersedes": supersedes,
    }


def ask(entity, attribute, as_of):
    store = STATE["store"]
    if attribute == "state":
        entity = "forklift"
    key = (entity, attribute, CAMERA)
    r = answer(store, key, as_of, max_age=3600)
    history = []
    for h in r.get("history") or []:
        row = store.db.execute(
            "SELECT rule_fired, status, value, clip_id, t_start, t_end, observed_at FROM claims WHERE id=?",
            (h["claim_id"],)).fetchone()
        history.append({
            "claim_id": h["claim_id"],
            "value": h["value"],
            "clip_id": h["clip_id"],
            "t_start": h["t_start"],
            "t_end": h["t_end"],
            "status": "SUPERSEDED",
            "rule_id": row["rule_fired"] if row else "SUPERSEDE_NEWER_CONTRADICTS",
        })
    # Full audit trail for this key
    audit_trail = []
    for a in store.db.execute("""
        SELECT a.rule_id, a.claim_id, a.other_claim_id, a.detail, c.value, c.observed_at, c.clip_id
        FROM audit a JOIN claims c ON c.id = a.claim_id
        WHERE c.entity=? AND c.attribute=? AND c.location=? AND c.observed_at<=?
        ORDER BY a.id
    """, (entity, attribute, CAMERA, parse_t(as_of))):
        audit_trail.append(dict(a))

    rule = None
    path = None
    caption = None
    if r["claim_id"]:
        row = store.db.execute(
            "SELECT rule_fired FROM claims WHERE id=?", (r["claim_id"],)).fetchone()
        rule = row["rule_fired"] if row else None
        ev = store.db.execute(
            "SELECT e.clip_id, cl.path, cl.description FROM evidence e "
            "LEFT JOIN clips cl ON cl.clip_id = e.clip_id "
            "WHERE e.claim_id=? AND e.observed_at<=? "
            "ORDER BY e.observed_at DESC LIMIT 1",
            (r["claim_id"], parse_t(as_of))).fetchone()
        if ev:
            path = ev["path"]
            caption = ev["description"]
        if not path and r["clip_id"]:
            cl = store.db.execute(
                "SELECT path, description FROM clips WHERE clip_id=?",
                (r["clip_id"],)).fetchone()
            if cl:
                path = cl["path"]
                caption = caption or cl["description"]
    # Claim lifecycle status (ACTIVE/SUPERSEDED/NO_EVIDENCE) separate from stale flag.
    claim_status = "NO_EVIDENCE"
    if r["claim_id"]:
        claim_status = "SUPERSEDED" if store.is_superseded_at(r["claim_id"], parse_t(as_of)) else "ACTIVE"
    return {
        "answer": r["answer"],
        "status": claim_status,
        "stale": bool(r.get("stale")),
        "stale_reason": r["stale_reason"],
        "data_gap_note": r["data_gap_note"],
        "claim_id": r["claim_id"],
        "rule_id": rule,
        "camera_id": CAMERA,
        "clip_id": r["clip_id"],
        "segment_label": _seg_label(r["clip_id"] or ""),
        "source_path": path,
        "t_start": r["t_start"],
        "t_end": r["t_end"],
        "t_start_fmt": fmt_t(r["t_start"]) if r["t_start"] is not None else None,
        "t_end_fmt": fmt_t(r["t_end"]) if r["t_end"] is not None else None,
        "stream_url": stream_url(path, clip_id=r["clip_id"]) if (path or r["clip_id"]) else None,
        "caption": caption,
        "history": history,
        "audit": audit_trail,
        "as_of": parse_t(as_of),
        "as_of_fmt": fmt_t(parse_t(as_of)),
        "entity": entity,
        "attribute": attribute,
    }


def demo_as_of():
    """Prefer a forklift moving→parked supersede moment for the default board view."""
    store = STATE["store"]
    if not store:
        return STATE["t_max"]
    row = store.db.execute("""
        SELECT n.observed_at AS t, o.value AS old_v, n.value AS new_v
        FROM audit a
        JOIN claims n ON n.id = a.claim_id
        JOIN claims o ON o.id = a.other_claim_id
        WHERE a.rule_id = 'SUPERSEDE_NEWER_CONTRADICTS'
          AND n.entity = 'forklift' AND n.attribute = 'state'
          AND o.value = 'moving' AND n.value = 'parked'
        ORDER BY n.observed_at
        LIMIT 1
    """).fetchone()
    if row:
        return float(row["t"])
    row = store.db.execute("""
        SELECT n.observed_at AS t FROM audit a
        JOIN claims n ON n.id = a.claim_id
        WHERE a.rule_id = 'SUPERSEDE_NEWER_CONTRADICTS'
        ORDER BY n.observed_at LIMIT 1
    """).fetchone()
    return float(row["t"]) if row else STATE["t_max"]


def parse_nl_question(q):
    """Map a natural question to entity/attribute. Fallback center_aisle/worker_present."""
    text = (q or "").lower()
    attr = "worker_present"
    entity = "center_aisle"
    if "forklift" in text or "moving" in text or "parked" in text:
        attr = "state"
        entity = "forklift"
    elif "block" in text or "clear" in text or "obstruct" in text:
        attr = "blocked"
    if "left" in text:
        entity = "left_aisle" if attr != "state" else entity
    elif "right" in text:
        entity = "right_aisle" if attr != "state" else entity
    elif "loading" in text:
        entity = "loading_area" if attr != "state" else entity
    elif "wall" in text:
        entity = "wall_area" if attr != "state" else entity
    elif "center" in text or "middle" in text:
        entity = "center_aisle" if attr != "state" else entity
    return entity, attr


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>RECEIPTS — every answer has a clip</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Syne:wght@600;700;800&display=swap" rel="stylesheet"/>
<style>
:root{
  --bg:#0b0e0c; --panel:#121714; --line:#243028; --fg:#e6efe8; --muted:#7f9184;
  --green:#3dcf7a; --amber:#e0a84a; --red:#e4574a; --stale:#5a635c;
  --accent:#8fd4a8; --toast:#1c241e;
}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;color:var(--fg);
  font-family:"IBM Plex Mono",ui-monospace,monospace;
  background:
    radial-gradient(900px 420px at 80% -10%, rgba(61,207,122,.08), transparent),
    radial-gradient(700px 380px at 0% 100%, rgba(224,168,74,.06), transparent),
    linear-gradient(180deg,#070908,#0b0e0c 50%,#0a0d0b);
}
header{padding:.85rem 1.25rem .55rem;display:flex;flex-wrap:wrap;align-items:flex-end;gap:1rem;justify-content:space-between;
  border-bottom:1px solid var(--line)}
header h1{font-family:Syne,sans-serif;font-size:clamp(1.45rem,3.5vw,2.1rem);letter-spacing:.02em;margin:0;line-height:1}
header h1 span{color:var(--accent)}
header .tagline{color:var(--muted);font-size:.8rem;margin:.2rem 0 0}
.live-ctl{display:flex;align-items:center;gap:.75rem;background:var(--panel);border:1px solid var(--line);padding:.5rem .8rem}
.live-ctl label{display:flex;align-items:center;gap:.5rem;cursor:pointer;user-select:none;font-size:.78rem;letter-spacing:.06em}
.live-ctl input{accent-color:var(--green);width:1.1rem;height:1.1rem}
.live-hint{color:var(--muted);font-size:.7rem;max-width:15rem;line-height:1.35}
.live-dot{width:.55rem;height:.55rem;border-radius:50%;background:var(--stale);display:inline-block}
.live-dot.on{background:var(--green);box-shadow:0 0 10px var(--green);animation:pulse 1.2s infinite}
@keyframes pulse{50%{opacity:.45}}
main{padding:.75rem 1.25rem 5rem;max-width:1280px;margin:0 auto}
.stage{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(260px,.9fr);gap:.85rem;align-items:start}
@media (max-width:900px){.stage{grid-template-columns:1fr}}
.player-wrap{background:#000;border:1px solid var(--line);position:relative;min-height:280px}
#heroVideo{width:100%;display:block;max-height:min(62vh,560px);background:#000;aspect-ratio:16/9;object-fit:contain}
#frameStrip{display:none;grid-template-columns:repeat(auto-fit,minmax(90px,1fr));gap:4px;padding:4px;background:#0a0d0b}
#frameStrip.show{display:grid}
#frameStrip img{width:100%;height:100px;object-fit:cover;border:1px solid var(--line)}
.player-meta{display:flex;justify-content:space-between;gap:.75rem;flex-wrap:wrap;padding:.45rem .55rem;background:var(--panel);border:1px solid var(--line);border-top:0;font-size:.72rem;color:var(--muted)}
.player-meta strong{color:var(--fg)}
.range-tag{color:var(--amber)}
.side-tiles{display:grid;grid-template-columns:1fr 1fr;gap:.55rem}
.side-tiles .tile{min-height:96px;padding:.75rem .7rem}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:.65rem;margin-top:.85rem}
.tile{position:relative;background:var(--panel);border:1px solid var(--line);padding:.85rem .75rem .7rem;
  min-height:110px;overflow:hidden;transition:border-color .25s, background .25s, transform .35s}
.tile.flash{animation:claimIn .55s ease}
@keyframes claimIn{from{transform:translateY(8px);opacity:.15}to{transform:none;opacity:1}}
.tile .lbl{font-size:.62rem;letter-spacing:.12em;color:var(--muted)}
.tile .sub{font-size:.68rem;color:var(--muted);margin-top:.1rem;text-transform:uppercase}
.tile .val{font-family:Syne,sans-serif;font-size:1.35rem;font-weight:700;margin:.4rem 0 .25rem;letter-spacing:.02em}
.tile .val.strike{text-decoration:line-through;color:var(--red);animation:strikeFlash .7s ease}
@keyframes strikeFlash{0%,100%{opacity:1}40%{opacity:.25;color:#fff}}
.tile .age{font-size:.65rem;color:var(--muted)}
.tile.green{border-color:rgba(61,207,122,.45)}
.tile.green .val{color:var(--green)}
.tile.amber{border-color:rgba(224,168,74,.5)}
.tile.amber .val{color:var(--amber)}
.tile.red{border-color:rgba(228,87,74,.55)}
.tile.red .val{color:var(--red)}
.tile.stale{opacity:.55;filter:grayscale(.7);border-color:#333}
.tile.stale .val{color:var(--stale)}
.clock{color:var(--muted);font-size:.75rem;margin:.35rem 0 .65rem}
.banner{margin:.5rem 0;padding:.5rem .7rem;background:#2a1e1c;border:1px solid #5a3530;color:#f0b4ae;font-size:.75rem}
section{margin-top:1.5rem}
section h2{font-family:Syne,sans-serif;font-size:.95rem;letter-spacing:.08em;margin:0 0 .65rem;color:var(--accent)}
.ask-row{display:flex;gap:.5rem;flex-wrap:wrap}
.ask-row input[type=text]{flex:1;min-width:220px;background:#0e1210;border:1px solid var(--line);color:var(--fg);
  padding:.65rem .75rem;font:inherit;font-size:.88rem}
.ask-row button{background:var(--accent);color:#0b120e;border:0;padding:.65rem 1rem;font:inherit;font-weight:600;cursor:pointer}
.presets{display:flex;flex-wrap:wrap;gap:.35rem;margin:.5rem 0 0}
.presets button{background:transparent;border:1px solid var(--line);color:var(--muted);padding:.3rem .5rem;font:inherit;font-size:.68rem;cursor:pointer}
.presets button:hover{border-color:var(--accent);color:var(--fg)}
#askOut{margin-top:.85rem;background:var(--panel);border:1px solid var(--line);padding:.9rem;display:none}
.badge{display:inline-block;padding:.12rem .45rem;font-size:.66rem;letter-spacing:.06em;font-weight:700}
.badge.ACTIVE{background:#163222;color:var(--green)}
.badge.STALE{background:#3a2422;color:var(--red)}
.badge.SUPERSEDED{background:#3a2f1a;color:var(--amber)}
.badge.NO_EVIDENCE{background:#222;color:var(--muted)}
.quote{margin:.65rem 0;padding:.55rem .7rem;border-left:3px solid var(--accent);color:#c5d4c9;font-size:.8rem;line-height:1.4}
details{margin-top:.65rem;font-size:.75rem;color:var(--muted)}
details table{width:100%;border-collapse:collapse;margin-top:.35rem}
details th,details td{text-align:left;padding:.3rem;border-bottom:1px solid var(--line);vertical-align:top}
.scrub input[type=range]{width:100%;accent-color:var(--accent)}
.timeline{position:relative;height:68px;background:#0e1210;border:1px solid var(--line);margin-top:.45rem;overflow:hidden}
.tl-track{position:absolute;left:0;right:0;top:28px;height:4px;background:#2a332c}
.tl-mark{position:absolute;top:18px;width:3px;height:24px;background:var(--amber);transform:translateX(-1px)}
.tl-mark.sup{background:var(--red);width:4px;height:32px;top:14px}
.tl-mark .tip{display:none;position:absolute;bottom:110%;left:50%;transform:translateX(-50%);background:#1a211c;border:1px solid var(--line);
  padding:.25rem .4rem;white-space:nowrap;font-size:.65rem;color:var(--fg);z-index:2}
.tl-mark:hover .tip{display:block}
.tl-cursor{position:absolute;top:0;bottom:0;width:2px;background:var(--accent);box-shadow:0 0 8px var(--accent)}
.tl-cite{position:absolute;top:8px;height:52px;background:rgba(143,212,168,.18);border:1px solid var(--accent);pointer-events:none}
#toasts{position:fixed;right:1rem;bottom:4.5rem;display:flex;flex-direction:column;gap:.45rem;z-index:20;max-width:min(380px,92vw)}
.toast{background:var(--toast);border:1px solid var(--red);color:#f3d0cb;padding:.6rem .75rem;font-size:.75rem;animation:toastIn .35s ease}
@keyframes toastIn{from{transform:translateX(40px);opacity:0}to{transform:none;opacity:1}}
footer{position:fixed;left:0;right:0;bottom:0;padding:.5rem 1rem;background:rgba(8,10,9,.92);border-top:1px solid var(--line);
  display:flex;flex-wrap:wrap;gap:.35rem;align-items:center;font-size:.66rem;color:var(--muted);backdrop-filter:blur(6px)}
footer .chip{border:1px solid var(--line);padding:.18rem .4rem;color:#b7c7bb;letter-spacing:.04em}
.meta{color:var(--muted);font-size:.75rem}
.err{color:var(--red)}
</style>
</head>
<body>
<header>
  <div>
    <h1>RECEIPTS <span>— every answer has a clip</span></h1>
    <p class="tagline">Warehouse ops · cam-2 · Pack C · footage first, then the claim</p>
  </div>
  <div class="live-ctl">
    <label><span class="live-dot" id="liveDot"></span>
      <input type="checkbox" id="liveToggle"/> LIVE
    </label>
    <div class="live-hint">Replay of indexed footage · player advances per segment · ~2s cadence</div>
  </div>
</header>
<main>
  <div class="clock">as_of <strong id="asofLabel">—</strong> · coverage <span id="covLabel">—</span> · <span id="segLabel">…</span></div>
  <div id="staleBanner" class="banner" hidden></div>

  <div class="stage">
    <div>
      <div class="player-wrap">
        <video id="heroVideo" controls playsinline autoplay muted></video>
        <div id="frameStrip" aria-label="segment frames"></div>
      </div>
      <div class="player-meta">
        <div>Playing <strong id="playClip">—</strong></div>
        <div id="playRange" class="range-tag"></div>
        <div id="playStatus">loading footage…</div>
      </div>
    </div>
    <div class="side-tiles" id="sideTiles"></div>
  </div>
  <div class="tiles" id="tiles"></div>

  <section>
    <h2>ASK</h2>
    <div class="ask-row">
      <input id="q" type="text" placeholder="Is the forklift on cam-2 moving?" value="Is the forklift on cam-2 moving?"/>
      <button id="askBtn" type="button">Answer</button>
    </div>
    <div class="presets">
      <button type="button" data-q="Is the forklift on cam-2 moving?">forklift moving?</button>
      <button type="button" data-q="Is a worker present in the center aisle?">worker center?</button>
      <button type="button" data-q="Is a worker present near the wall?">worker wall?</button>
      <button type="button" data-q="Is the left aisle blocked?">aisle blocked?</button>
    </div>
    <div id="askOut"></div>
  </section>

  <section>
    <h2>TIMELINE</h2>
    <div class="scrub">
      <label class="meta">Time scrubber (as_of)
        <input id="scrub" type="range" min="0" max="1" step="1" value="0"/>
      </label>
    </div>
    <div class="timeline" id="timeline"></div>
    <p class="meta" id="tlMeta"></p>
  </section>
</main>
<div id="toasts"></div>
<footer>
  <span>Sponsor tools used</span>
  <span class="chip">VAST DataEngine</span>
  <span class="chip">NVIDIA Cosmos Reason</span>
  <span class="chip">CoreWeave</span>
  <span class="chip">Cursor</span>
</footer>
<script>
let meta={t_min:0,t_max:1,ready:false,segments:[]};
let asOf=0;
let liveOn=false;
let es=null;
let prevTiles={};
let currentClipId=null;
let citeRange=null;
const $ = id => document.getElementById(id);

function fmt(s){
  s=Math.round(Number(s)||0);
  const h=String(Math.floor(s/3600)).padStart(2,'0');
  const m=String(Math.floor((s%3600)/60)).padStart(2,'0');
  const sec=String(s%60).padStart(2,'0');
  return h+':'+m+':'+sec;
}
function ageText(sec){
  if(sec==null||!isFinite(sec)) return 'unconfirmed';
  const s=Math.max(0,Math.round(sec));
  if(s<60) return 'last confirmed '+s+'s ago';
  return 'last confirmed '+Math.floor(s/60)+'m '+(s%60)+'s ago';
}
function toast(msg){
  const el=document.createElement('div');
  el.className='toast';
  el.textContent=msg;
  $('toasts').appendChild(el);
  setTimeout(()=>el.remove(),5200);
}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));}
function setScrub(v){
  const sl=$('scrub');
  sl.value=Math.round(v);
  asOf=Number(sl.value);
  $('asofLabel').textContent=fmt(asOf);
}
function shortClip(id){
  if(!id) return '—';
  const m=String(id).match(/(ceiling_\d+|eye_\d+).*?(segment_\d+)/);
  return m ? (m[1]+' '+m[2].replace('segment_','seg ')) : id.slice(-36);
}

async function showFrames(clipId){
  const strip=$('frameStrip');
  const vid=$('heroVideo');
  try{
    const r=await fetch('api/frames?clip_id='+encodeURIComponent(clipId)+'&n=5');
    const d=await r.json();
    if(!d.frames||!d.frames.length) throw new Error(d.error||'no frames');
    strip.innerHTML=d.frames.map(u=>'<img src="'+u+'" alt="frame"/>').join('');
    strip.classList.add('show');
    vid.style.display='none';
    $('playStatus').textContent='frame strip (video unavailable)';
  }catch(e){
    strip.classList.remove('show');
    $('playStatus').textContent='no video / frames: '+(e.message||e);
  }
}

function playClip(clip, {autoplay=true, highlight=null}={}){
  if(!clip||!clip.clip_id) return;
  const vid=$('heroVideo');
  const strip=$('frameStrip');
  strip.classList.remove('show');
  strip.innerHTML='';
  vid.style.display='block';
  currentClipId=clip.clip_id;
  citeRange=highlight;
  $('playClip').textContent=shortClip(clip.clip_id);
  $('playRange').textContent=fmt(clip.t_start)+'–'+fmt(clip.t_end)
    +(highlight?(' · cited '+fmt(highlight[0])+'–'+fmt(highlight[1])):'');
  $('playStatus').textContent='loading segment…';
  const url=clip.stream_url||('api/clip?clip_id='+encodeURIComponent(clip.clip_id));
  if(vid.dataset.src!==url){
    vid.dataset.src=url;
    vid.src=url;
  }
  const onErr=()=>{ showFrames(clip.clip_id); };
  vid.onerror=onErr;
  vid.onloadeddata=()=>{
    $('playStatus').textContent=autoplay?'playing':'ready';
    if(autoplay){ vid.play().catch(()=>{}); }
  };
  // If the proxy returns JSON error, video errors quickly
  setTimeout(()=>{
    if(vid.readyState===0 && currentClipId===clip.clip_id){
      // still nothing — try frames
      fetch(url,{method:'GET'}).then(r=>{
        if(!r.ok) showFrames(clip.clip_id);
      }).catch(()=>showFrames(clip.clip_id));
    }
  }, 2500);
}

function clipAt(t){
  const segs=meta.segments||[];
  let best=null;
  for(const s of segs){
    if(s.t_start<=t && t<=s.t_end+0.05) return s;
    if(s.t_end<=t) best=s;
  }
  return best||segs[0]||null;
}

function renderTiles(tiles, rootId, limit){
  const root=$(rootId);
  const list=limit?tiles.slice(0,limit):tiles;
  const html=[];
  for(const t of list){
    const prev=prevTiles[t.id];
    let valHtml=esc(t.display||'—');
    let flash='';
    if(prev && prev.value!=null && t.value!=null && prev.value!==t.value){
      valHtml='<span class="strike">'+esc(String(prev.display))+'</span> <span>'+esc(t.display)+'</span>';
      flash=' flash';
    } else if(prev && prev.value==null && t.value!=null){
      flash=' flash';
    }
    html.push('<div class="tile '+t.tone+flash+'" data-id="'+t.id+'">'
      +'<div class="lbl">'+esc(t.label)+'</div>'
      +'<div class="sub">'+esc(t.sub)+'</div>'
      +'<div class="val">'+valHtml+'</div>'
      +'<div class="age">'+esc(ageText(t.age_sec))+'</div></div>');
  }
  root.innerHTML=html.join('');
}

async function refreshBoard(){
  if(!meta.ready) return;
  const r=await fetch('api/board?as_of='+encodeURIComponent(asOf));
  const d=await r.json();
  $('covLabel').textContent=d.coverage_end_fmt||'—';
  let banner=null;
  for(const t of d.tiles){ if(t.stale_banner) banner=t.stale_banner; }
  // Hero side: forklift + first few worker tiles
  renderTiles(d.tiles, 'sideTiles', 4);
  renderTiles(d.tiles.slice(4), 'tiles', 99);
  const map={}; d.tiles.forEach(t=>map[t.id]=t); prevTiles=map;
  const b=$('staleBanner');
  if(banner){b.hidden=false;b.textContent=banner;} else b.hidden=true;
  // Keep player on segment for current as_of unless live/ask owns it
  if(!liveOn){
    const c=clipAt(asOf);
    if(c && c.clip_id!==currentClipId) playClip(c,{autoplay:true});
  }
}

async function refreshTimeline(){
  if(!meta.ready) return;
  const r=await fetch('api/timeline?as_of='+encodeURIComponent(meta.t_max));
  const d=await r.json();
  const el=$('timeline');
  const span=Math.max(1,d.t_max-d.t_min);
  const marks=['<div class="tl-track"></div>'];
  if(citeRange){
    const a=((citeRange[0]-d.t_min)/span)*100;
    const w=((citeRange[1]-citeRange[0])/span)*100;
    marks.push('<div class="tl-cite" style="left:'+a+'%;width:'+Math.max(w,0.8)+'%"></div>');
  }
  for(const c of d.claims){
    const pct=((c.observed_at-d.t_min)/span)*100;
    marks.push('<div class="tl-mark" style="left:'+pct+'%"><span class="tip">'+esc(c.entity)+'/'+esc(c.attribute)+'='+esc(c.value)+' @ '+fmt(c.observed_at)+'</span></div>');
  }
  for(const s of d.supersedes){
    const pct=((s.t-d.t_min)/span)*100;
    marks.push('<div class="tl-mark sup" style="left:'+pct+'%"><span class="tip">SUPERSEDE '+esc(s.old)+'→'+esc(s.new)+' '+esc(s.seg)+'</span></div>');
  }
  const cur=((asOf-d.t_min)/span)*100;
  marks.push('<div class="tl-cursor" style="left:'+cur+'%"></div>');
  el.innerHTML=marks.join('');
  $('tlMeta').textContent=d.claims.length+' claims · '+d.supersedes.length+' supersedes';
}

let didAutoAsk=false;
async function init(){
  const r=await fetch('api/meta');
  meta=await r.json();
  const sl=$('scrub');
  sl.min=Math.floor(meta.t_min);
  sl.max=Math.ceil(meta.t_max);
  if(!liveOn){
    const demo=meta.demo_as_of!=null ? meta.demo_as_of : meta.t_max;
    setScrub(demo);
  } else setScrub(Math.max(sl.min, asOf||sl.min));
  $('segLabel').textContent=meta.ready
    ? (meta.segment_count+' segments · '+fmt(meta.t_min)+' → '+fmt(meta.t_max))
    : ('Not ready: '+(meta.error||'building store…'));
  if(meta.error) $('segLabel').classList.add('err');
  if(meta.ready){
    const boot=meta.demo_clip || clipAt(asOf);
    if(boot) playClip(boot,{autoplay:true});
    await refreshBoard();
    await refreshTimeline();
    if(!didAutoAsk && !liveOn){
      didAutoAsk=true;
      $('q').value='Is the forklift on cam-2 moving?';
      await doAsk();
    }
  }
}

$('scrub').addEventListener('input', async (e)=>{
  setScrub(e.target.value);
  await refreshBoard();
  await refreshTimeline();
});

function startLive(){
  if(es) es.close();
  liveOn=true;
  $('liveDot').classList.add('on');
  setScrub(meta.t_min||0);
  prevTiles={};
  es=new EventSource('api/live');
  es.onmessage=(ev)=>{
    let d; try{d=JSON.parse(ev.data);}catch{return;}
    if(d.type==='heartbeat'||d.type==='poll_error'||d.type==='hello') return;
    if(d.type==='replay_done'){ toast('Replay complete'); return; }
    if(d.t_end!=null) setScrub(d.t_end);
    if(d.type==='segment' && d.clip_id){
      playClip({clip_id:d.clip_id, t_start:d.t_start, t_end:d.t_end,
        stream_url:'api/clip?clip_id='+encodeURIComponent(d.clip_id)}, {autoplay:true});
    }
    if(d.type==='supersede'){
      toast(d.toast||('SUPERSEDED: '+d.old+' → '+d.new));
    }
    if(d.live_new){ toast('NEW SEGMENT from VSS'); init(); }
    refreshBoard();
    refreshTimeline();
  };
}
function stopLive(){
  liveOn=false;
  $('liveDot').classList.remove('on');
  if(es){es.close();es=null;}
}
$('liveToggle').addEventListener('change',(e)=>{
  if(e.target.checked) startLive();
  else { stopLive(); setScrub(meta.demo_as_of||meta.t_max); refreshBoard(); refreshTimeline(); }
});

async function doAsk(){
  const q=$('q').value;
  const r=await fetch('api/ask?q='+encodeURIComponent(q)+'&as_of='+encodeURIComponent(asOf));
  const d=await r.json();
  const el=$('askOut');
  el.style.display='block';
  let audit='';
  if(d.audit&&d.audit.length){
    audit='<details><summary>Audit trail ('+d.audit.length+' rules)</summary><table><tr><th>Rule</th><th>Value</th><th>Clip</th><th>t</th></tr>'
      +d.audit.map(a=>'<tr><td><code>'+esc(a.rule_id)+'</code></td><td>'+esc(a.value)+'</td><td style="word-break:break-all">'+esc(a.clip_id||'')+'</td><td>'+fmt(a.observed_at)+'</td></tr>').join('')
      +'</table></details>';
  }
  const ans=d.answer==null?'(no claim)':d.answer;
  const staleBadge=d.stale?' <span class="badge STALE">STALE</span>':'';
  const quote=d.caption ? '<div class="quote">“'+esc(d.caption)+'”</div>' : '';
  el.innerHTML=
    '<p><span class="badge '+(d.status||'')+'">'+(d.status||'')+'</span>'+staleBadge
    +(d.rule_id?' · created by <code>'+esc(d.rule_id)+'</code>':'')+'</p>'
    +'<div style="font-family:Syne,sans-serif;font-size:1.45rem;margin:.35rem 0">'+esc(String(ans))+'</div>'
    +'<p class="meta"><code>'+esc(d.entity)+'</code> / <code>'+esc(d.attribute)+'</code> @ <code>'+esc(d.camera_id)
    +'</code> · as_of '+esc(d.as_of_fmt)+'</p>'
    +(d.clip_id?'<p class="meta">Cited '+esc(d.segment_label||'')+' · <code>'+esc(d.clip_id)+'</code> · '+esc(d.t_start_fmt)+'–'+esc(d.t_end_fmt)+'</p>':'')
    +quote
    +(d.stale&&d.stale_reason?'<p class="err">'+esc(d.stale_reason)+'</p>':'')
    +(d.data_gap_note?'<p class="meta">'+esc(d.data_gap_note)+'</p>':'')
    +audit;
  if(d.clip_id){
    playClip({
      clip_id:d.clip_id,
      t_start:d.t_start,
      t_end:d.t_end,
      stream_url:d.stream_url
    }, {autoplay:true, highlight:[d.t_start,d.t_end]});
    refreshTimeline();
  }
}
$('askBtn').addEventListener('click', doAsk);
document.querySelectorAll('.presets button').forEach(b=>{
  b.addEventListener('click',()=>{ $('q').value=b.dataset.q; doAsk(); });
});

setInterval(()=>{ if(meta.ready && !document.hidden) refreshBoard(); }, 2500);
init();
setInterval(()=>{ if(!meta.ready) init(); }, 2500);
</script>
</body>
</html>
"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, content_type="text/html; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _sse(self):
        """Replay indexed footage at LIVE_INTERVAL; also forward poller events."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = _subscribe()
        try:
            # Snapshot replay log
            with STATE["lock"]:
                log = list(STATE["event_log"])
            hello = {"type": "hello", "label": "replay of indexed footage",
                     "interval_sec": LIVE_INTERVAL, "events": len(log)}
            self.wfile.write(f"data: {json.dumps(hello)}\n\n".encode())
            self.wfile.flush()
            for ev in log:
                if not STATE["ready"]:
                    break
                self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                self.wfile.flush()
                # Drain any live-new events without blocking the paced replay long
                try:
                    while True:
                        extra = q.get_nowait()
                        if extra.get("live_new") or extra.get("type") == "supersede":
                            self.wfile.write(f"data: {json.dumps(extra)}\n\n".encode())
                            self.wfile.flush()
                except queue.Empty:
                    pass
                # Pace ~1 segment / LIVE_INTERVAL; supersede toasts ride along free
                if ev.get("type") == "segment":
                    time.sleep(LIVE_INTERVAL)
            self.wfile.write(f"data: {json.dumps({'type': 'replay_done'})}\n\n".encode())
            self.wfile.flush()
            # Keep connection for poller-driven new segments
            while True:
                try:
                    ev = q.get(timeout=25)
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(f"data: {json.dumps({'type': 'heartbeat', 't': time.time()})}\n\n".encode())
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            _unsubscribe(q)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/health":
            return self._send(200, json.dumps({
                "ok": True, "ready": STATE["ready"], "error": STATE["error"],
                "segments": STATE["segment_count"],
            }), "application/json")
        if path == "/api/meta":
            demo_t = demo_as_of() if STATE["ready"] else STATE["t_max"]
            segs = segment_playlist() if STATE["ready"] else []
            demo_clip = None
            for s in segs:
                if s["t_start"] <= demo_t <= s["t_end"] + 0.01:
                    demo_clip = s
                    break
            if demo_clip is None and segs:
                demo_clip = min(segs, key=lambda s: abs(s["t_end"] - demo_t))
            return self._send(200, json.dumps({
                "ready": STATE["ready"], "error": STATE["error"],
                "t_min": STATE["t_min"], "t_max": STATE["t_max"],
                "segment_count": STATE["segment_count"],
                "camera_id": CAMERA, "zones": list(ZONES),
                "live_interval_sec": LIVE_INTERVAL,
                "live_label": "replay of indexed footage",
                "demo_as_of": demo_t,
                "demo_as_of_fmt": fmt_t(demo_t) if STATE["ready"] else None,
                "demo_clip": demo_clip,
                "segments": segs,
            }), "application/json")
        if path == "/api/segments":
            if not STATE["ready"]:
                return self._send(503, json.dumps({"error": "not ready"}), "application/json")
            return self._send(200, json.dumps({"segments": segment_playlist()}),
                              "application/json")
        if path == "/api/board":
            if not STATE["ready"]:
                return self._send(503, json.dumps({"error": STATE["error"] or "not ready"}),
                                  "application/json")
            q = parse_qs(parsed.query)
            as_of = (q.get("as_of") or [str(STATE["t_max"])])[0]
            return self._send(200, json.dumps(board(as_of)), "application/json")
        if path == "/api/timeline":
            if not STATE["ready"]:
                return self._send(503, json.dumps({"error": "not ready"}), "application/json")
            q = parse_qs(parsed.query)
            as_of = (q.get("as_of") or [str(STATE["t_max"])])[0]
            return self._send(200, json.dumps(timeline(as_of)), "application/json")
        if path == "/api/answer":
            if not STATE["ready"]:
                return self._send(503, json.dumps({"error": STATE["error"] or "not ready"}),
                                  "application/json")
            q = parse_qs(parsed.query)
            entity = (q.get("entity") or ["center_aisle"])[0]
            attribute = (q.get("attribute") or ["worker_present"])[0]
            if attribute not in ("worker_present", "state", "blocked"):
                attribute = "worker_present"
            if attribute != "state" and entity not in ZONES:
                entity = "center_aisle"
            as_of = (q.get("as_of") or [str(STATE["t_max"])])[0]
            return self._send(200, json.dumps(ask(entity, attribute, as_of)), "application/json")
        if path == "/api/ask":
            if not STATE["ready"]:
                return self._send(503, json.dumps({"error": STATE["error"] or "not ready"}),
                                  "application/json")
            q = parse_qs(parsed.query)
            entity, attribute = parse_nl_question((q.get("q") or [""])[0])
            # allow explicit overrides
            if q.get("entity"):
                entity = q["entity"][0]
            if q.get("attribute"):
                attribute = q["attribute"][0]
            as_of = (q.get("as_of") or [str(STATE["t_max"])])[0]
            return self._send(200, json.dumps(ask(entity, attribute, as_of)), "application/json")
        if path == "/api/live":
            if not STATE["ready"]:
                return self._send(503, json.dumps({"error": "not ready"}), "application/json")
            return self._sse()
        if path == "/api/clip":
            if not STATE["ready"]:
                return self._send(503, json.dumps({"error": "not ready"}), "application/json")
            q = parse_qs(parsed.query)
            clip_id = (q.get("clip_id") or [None])[0]
            source = _resolve_source(clip_id, (q.get("source") or [None])[0])
            if not source:
                return self._send(404, json.dumps({"error": "clip not found"}), "application/json")
            req = _vss_stream_request(source)
            if not req:
                return self._send(502, json.dumps({"error": "vss unavailable"}), "application/json")
            try:
                import urllib.error
                import urllib.request
                with urllib.request.urlopen(req, timeout=120) as upstream:
                    ctype = upstream.headers.get("Content-Type") or "video/mp4"
                    if "octet-stream" in ctype or not ctype.startswith("video/"):
                        ctype = "video/mp4"
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    clen = upstream.headers.get("Content-Length")
                    if clen:
                        self.send_header("Content-Length", clen)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Cache-Control", "private, max-age=120")
                    self.end_headers()
                    while True:
                        chunk = upstream.read(64 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                return
            except urllib.error.HTTPError as e:
                body = e.read() if hasattr(e, "read") else b""
                return self._send(e.code, json.dumps({
                    "error": "stream failed",
                    "source": source,
                    "detail": body[:200].decode("utf-8", "replace"),
                    "frames_url": (f"api/frames?clip_id={clip_id}" if clip_id else None),
                }), "application/json")
            except Exception as e:
                return self._send(502, json.dumps({
                    "error": "stream proxy failed",
                    "source": source,
                    "detail": str(e),
                    "frames_url": (f"api/frames?clip_id={clip_id}" if clip_id else None),
                }), "application/json")
        if path == "/api/frames":
            if not STATE["ready"]:
                return self._send(503, json.dumps({"error": "not ready"}), "application/json")
            q = parse_qs(parsed.query)
            clip_id = (q.get("clip_id") or [None])[0]
            source = _resolve_source(clip_id, (q.get("source") or [None])[0])
            if not source:
                return self._send(404, json.dumps({"error": "clip not found"}), "application/json")
            try:
                n = int((q.get("n") or ["5"])[0])
                n = max(3, min(n, 6))
                paths = extract_frames(clip_id or source, source, n=n)
                # Serve as multipart-ish JSON with data URLs is heavy; list indexed URLs.
                from urllib.parse import quote as _quote
                urls = [
                    f"api/frame_file?clip_id={_quote(clip_id or '', safe='')}&i={i}"
                    for i in range(len(paths))
                ]
                # Stash paths for frame_file
                with STATE["lock"]:
                    STATE.setdefault("frame_paths", {})[clip_id or source] = paths
                return self._send(200, json.dumps({
                    "clip_id": clip_id, "source": source, "frames": urls, "count": len(urls),
                }), "application/json")
            except Exception as e:
                return self._send(502, json.dumps({
                    "error": "frame extract failed", "detail": str(e), "source": source,
                }), "application/json")
        if path == "/api/frame_file":
            q = parse_qs(parsed.query)
            clip_id = (q.get("clip_id") or [""])[0]
            try:
                i = int((q.get("i") or ["0"])[0])
            except ValueError:
                i = 0
            with STATE["lock"]:
                paths = (STATE.get("frame_paths") or {}).get(clip_id) or []
            if not paths or i < 0 or i >= len(paths):
                # Try extract on the fly
                source = _resolve_source(clip_id, None)
                if not source:
                    return self._send(404, b"missing", "text/plain")
                try:
                    paths = extract_frames(clip_id, source, n=5)
                    with STATE["lock"]:
                        STATE.setdefault("frame_paths", {})[clip_id] = paths
                except Exception as e:
                    return self._send(502, str(e).encode(), "text/plain")
            if i >= len(paths):
                return self._send(404, b"no frame", "text/plain")
            data = open(paths[i], "rb").read()
            return self._send(200, data, "image/jpeg")
        if path in ("/", "/index.html", "/app"):
            return self._send(200, PAGE)
        return self._send(404, "not found")


def main():
    def worker():
        try:
            build_store()
            print(f"store ready: {STATE['segment_count']} segments "
                  f"{fmt_t(STATE['t_min'])}-{fmt_t(STATE['t_max'])} "
                  f"events={len(STATE['event_log'])}", flush=True)
        except Exception as e:
            STATE["error"] = str(e)
            STATE["ready"] = False
            print(f"store build failed: {e}", flush=True)

    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=poll_new_segments, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"listening on 0.0.0.0:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
