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


def stream_url(source):
    base = public_backend()
    if not source or not base or not STATE["token"]:
        return None
    from urllib.parse import quote
    return (f"{base}/api/v1/videos/stream?"
            f"source={quote(source, safe='')}&token={STATE['token']}")


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
    with STATE["lock"]:
        STATE.update(
            store=store, token=token, backend=backend.rstrip("/"),
            t_min=float(t_min), t_max=float(t_max),
            segment_count=len(segments), ready=True, error=None,
            known_clips=known, event_log=event_log,
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
    status = r["status"].upper() if r["status"] else "NO_EVIDENCE"
    if r.get("stale"):
        status = "STALE"
    return {
        "answer": r["answer"],
        "status": status,
        "stale": r["stale"],
        "stale_reason": r["stale_reason"],
        "data_gap_note": r["data_gap_note"],
        "claim_id": r["claim_id"],
        "rule_id": rule,
        "camera_id": CAMERA,
        "clip_id": r["clip_id"],
        "t_start": r["t_start"],
        "t_end": r["t_end"],
        "t_start_fmt": fmt_t(r["t_start"]) if r["t_start"] is not None else None,
        "t_end_fmt": fmt_t(r["t_end"]) if r["t_end"] is not None else None,
        "stream_url": stream_url(path) if path else None,
        "caption": caption,
        "history": history,
        "audit": audit_trail,
        "as_of": parse_t(as_of),
        "as_of_fmt": fmt_t(parse_t(as_of)),
        "entity": entity,
        "attribute": attribute,
    }


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
header{padding:1.1rem 1.5rem .6rem;display:flex;flex-wrap:wrap;align-items:flex-end;gap:1rem;justify-content:space-between;
  border-bottom:1px solid var(--line)}
header h1{font-family:Syne,sans-serif;font-size:clamp(1.6rem,4vw,2.4rem);letter-spacing:.02em;margin:0;line-height:1}
header h1 span{color:var(--accent)}
header .tagline{color:var(--muted);font-size:.85rem;margin:.25rem 0 0}
.live-ctl{display:flex;align-items:center;gap:.75rem;background:var(--panel);border:1px solid var(--line);padding:.55rem .85rem}
.live-ctl label{display:flex;align-items:center;gap:.5rem;cursor:pointer;user-select:none;font-size:.8rem;letter-spacing:.06em}
.live-ctl input{accent-color:var(--green);width:1.1rem;height:1.1rem}
.live-hint{color:var(--muted);font-size:.72rem;max-width:16rem;line-height:1.35}
.live-dot{width:.55rem;height:.55rem;border-radius:50%;background:var(--stale);display:inline-block}
.live-dot.on{background:var(--green);box-shadow:0 0 10px var(--green);animation:pulse 1.2s infinite}
@keyframes pulse{50%{opacity:.45}}
main{padding:1rem 1.5rem 5rem;max-width:1200px;margin:0 auto}
.clock{color:var(--muted);font-size:.78rem;margin:.4rem 0 1rem}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:.75rem}
.tile{position:relative;background:var(--panel);border:1px solid var(--line);padding:1rem .9rem .85rem;
  min-height:128px;overflow:hidden;transition:border-color .25s, background .25s, transform .35s}
.tile.flash{animation:claimIn .55s ease}
@keyframes claimIn{from{transform:translateY(10px);opacity:.2}to{transform:none;opacity:1}}
.tile .lbl{font-size:.65rem;letter-spacing:.12em;color:var(--muted)}
.tile .sub{font-size:.7rem;color:var(--muted);margin-top:.15rem;text-transform:uppercase}
.tile .val{font-family:Syne,sans-serif;font-size:1.55rem;font-weight:700;margin:.55rem 0 .35rem;letter-spacing:.02em}
.tile .val.strike{text-decoration:line-through;color:var(--red);animation:strikeFlash .7s ease}
@keyframes strikeFlash{0%,100%{opacity:1}40%{opacity:.25;color:#fff}}
.tile .age{font-size:.68rem;color:var(--muted)}
.tile.green{border-color:rgba(61,207,122,.45);box-shadow:inset 0 0 0 1px rgba(61,207,122,.08)}
.tile.green .val{color:var(--green)}
.tile.amber{border-color:rgba(224,168,74,.5);box-shadow:inset 0 0 0 1px rgba(224,168,74,.1)}
.tile.amber .val{color:var(--amber)}
.tile.red{border-color:rgba(228,87,74,.55);box-shadow:inset 0 0 0 1px rgba(228,87,74,.12)}
.tile.red .val{color:var(--red)}
.tile.stale{opacity:.55;filter:grayscale(.7);border-color:#333}
.tile.stale .val{color:var(--stale)}
.banner{margin:.75rem 0;padding:.55rem .75rem;background:#2a1e1c;border:1px solid #5a3530;color:#f0b4ae;font-size:.78rem;letter-spacing:.04em}
section{margin-top:1.75rem}
section h2{font-family:Syne,sans-serif;font-size:1rem;letter-spacing:.08em;margin:0 0 .75rem;color:var(--accent)}
.ask-row{display:flex;gap:.5rem;flex-wrap:wrap}
.ask-row input[type=text]{flex:1;min-width:220px;background:#0e1210;border:1px solid var(--line);color:var(--fg);
  padding:.7rem .8rem;font:inherit;font-size:.9rem}
.ask-row button,.scrub button{background:var(--accent);color:#0b120e;border:0;padding:.7rem 1rem;font:inherit;font-weight:600;cursor:pointer}
.ask-row button:hover{filter:brightness(1.08)}
.presets{display:flex;flex-wrap:wrap;gap:.4rem;margin:.55rem 0 0}
.presets button{background:transparent;border:1px solid var(--line);color:var(--muted);padding:.35rem .55rem;font:inherit;font-size:.7rem;cursor:pointer}
.presets button:hover{border-color:var(--accent);color:var(--fg)}
#askOut{margin-top:1rem;background:var(--panel);border:1px solid var(--line);padding:1rem;display:none}
.badge{display:inline-block;padding:.12rem .45rem;font-size:.68rem;letter-spacing:.06em;font-weight:700}
.badge.ACTIVE{background:#163222;color:var(--green)}
.badge.STALE{background:#3a2422;color:var(--red)}
.badge.SUPERSEDED{background:#3a2f1a;color:var(--amber)}
.badge.NO_EVIDENCE{background:#222;color:var(--muted)}
.quote{margin:.75rem 0;padding:.65rem .75rem;border-left:3px solid var(--accent);color:#c5d4c9;font-size:.82rem;line-height:1.45}
video{width:100%;max-height:320px;background:#000;margin:.6rem 0;border:1px solid var(--line)}
details{margin-top:.75rem;font-size:.78rem;color:var(--muted)}
details table{width:100%;border-collapse:collapse;margin-top:.4rem}
details th,details td{text-align:left;padding:.35rem .3rem;border-bottom:1px solid var(--line);vertical-align:top}
.scrub{margin-top:.5rem}
.scrub input[type=range]{width:100%;accent-color:var(--accent)}
.timeline{position:relative;height:72px;background:#0e1210;border:1px solid var(--line);margin-top:.5rem;overflow:hidden}
.tl-track{position:absolute;left:0;right:0;top:28px;height:4px;background:#2a332c}
.tl-mark{position:absolute;top:18px;width:3px;height:24px;background:var(--amber);transform:translateX(-1px)}
.tl-mark.sup{background:var(--red);width:4px;height:32px;top:14px}
.tl-mark .tip{display:none;position:absolute;bottom:110%;left:50%;transform:translateX(-50%);background:#1a211c;border:1px solid var(--line);
  padding:.25rem .4rem;white-space:nowrap;font-size:.65rem;color:var(--fg);z-index:2}
.tl-mark:hover .tip{display:block}
.tl-cursor{position:absolute;top:0;bottom:0;width:2px;background:var(--accent);box-shadow:0 0 8px var(--accent)}
#toasts{position:fixed;right:1rem;bottom:4.5rem;display:flex;flex-direction:column;gap:.45rem;z-index:20;max-width:min(380px,92vw)}
.toast{background:var(--toast);border:1px solid var(--red);color:#f3d0cb;padding:.65rem .8rem;font-size:.78rem;letter-spacing:.03em;
  animation:toastIn .35s ease}
@keyframes toastIn{from{transform:translateX(40px);opacity:0}to{transform:none;opacity:1}}
footer{position:fixed;left:0;right:0;bottom:0;padding:.55rem 1rem;background:rgba(8,10,9,.92);border-top:1px solid var(--line);
  display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;font-size:.68rem;color:var(--muted);backdrop-filter:blur(6px)}
footer .chip{border:1px solid var(--line);padding:.2rem .45rem;color:#b7c7bb;letter-spacing:.04em}
.meta{color:var(--muted);font-size:.78rem}
.err{color:var(--red)}
</style>
</head>
<body>
<header>
  <div>
    <h1>RECEIPTS <span>— every answer has a clip</span></h1>
    <p class="tagline">Warehouse ops board · cam-2 · Pack C · answers cite footage, never a guess</p>
  </div>
  <div class="live-ctl">
    <label><span class="live-dot" id="liveDot"></span>
      <input type="checkbox" id="liveToggle"/> LIVE
    </label>
    <div class="live-hint">Replay of indexed footage · ~1 segment / 2s. Poller watches VSS for new segments (re-ingest).</div>
  </div>
</header>
<main>
  <div class="clock">as_of <strong id="asofLabel">—</strong> · coverage <span id="covLabel">—</span> · <span id="segLabel">…</span></div>
  <div id="staleBanner" class="banner" hidden></div>
  <div class="tiles" id="tiles"></div>

  <section>
    <h2>ASK</h2>
    <div class="ask-row">
      <input id="q" type="text" placeholder='Is the forklift on cam-2 moving?' value="Is the forklift on cam-2 moving?"/>
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
      <label class="meta">Time scrubber (as_of) — travel the whole board
        <input id="scrub" type="range" min="0" max="1" step="1" value="0"/>
      </label>
    </div>
    <div class="timeline" id="timeline" title="Claim + supersede markers"></div>
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
let meta={t_min:0,t_max:1,ready:false};
let asOf=0;
let liveOn=false;
let es=null;
let prevTiles={};
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
  return 'last confirmed '+Math.floor(s/60)+'m '+ (s%60) +'s ago';
}
function toast(msg){
  const el=document.createElement('div');
  el.className='toast';
  el.textContent=msg;
  $('toasts').appendChild(el);
  setTimeout(()=>el.remove(),5200);
}
function setScrub(v){
  const sl=$('scrub');
  sl.value=Math.round(v);
  asOf=Number(sl.value);
  $('asofLabel').textContent=fmt(asOf);
}

async function refreshBoard(){
  if(!meta.ready) return;
  const r=await fetch('api/board?as_of='+encodeURIComponent(asOf));
  const d=await r.json();
  $('covLabel').textContent=d.coverage_end_fmt||'—';
  let banner=null;
  const root=$('tiles');
  const html=[];
  for(const t of d.tiles){
    const prev=prevTiles[t.id];
    let valHtml=t.display||'—';
    let flash='';
    if(prev && prev.value!=null && t.value!=null && prev.value!==t.value){
      valHtml='<span class="strike">'+esc(String(prev.display))+'</span> <span>'+esc(t.display)+'</span>';
      flash=' flash';
    } else if(prev && prev.value==null && t.value!=null){
      flash=' flash';
    }
    if(t.stale_banner) banner=t.stale_banner;
    html.push('<div class="tile '+t.tone+flash+'" data-id="'+t.id+'">'
      +'<div class="lbl">'+esc(t.label)+'</div>'
      +'<div class="sub">'+esc(t.sub)+'</div>'
      +'<div class="val">'+valHtml+'</div>'
      +'<div class="age">'+esc(ageText(t.age_sec))+'</div></div>');
  }
  root.innerHTML=html.join('');
  const map={};
  d.tiles.forEach(t=>map[t.id]=t);
  prevTiles=map;
  const b=$('staleBanner');
  if(banner){b.hidden=false;b.textContent=banner;} else {b.hidden=true;}
}

function esc(s){return String(s).replace(/[&<>"']/g,c=>({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));}

async function refreshTimeline(){
  if(!meta.ready) return;
  const r=await fetch('api/timeline?as_of='+encodeURIComponent(meta.t_max));
  const d=await r.json();
  const el=$('timeline');
  const span=Math.max(1,d.t_max-d.t_min);
  const marks=[];
  marks.push('<div class="tl-track"></div>');
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
  $('tlMeta').textContent=d.claims.length+' claims · '+d.supersedes.length+' supersedes on strip';
}

async function init(){
  const r=await fetch('api/meta');
  meta=await r.json();
  const sl=$('scrub');
  sl.min=Math.floor(meta.t_min);
  sl.max=Math.ceil(meta.t_max);
  if(!liveOn) setScrub(sl.max);
  else setScrub(Math.max(sl.min, asOf||sl.min));
  $('segLabel').textContent=meta.ready
    ? (meta.segment_count+' segments indexed · '+fmt(meta.t_min)+' → '+fmt(meta.t_max))
    : ('Not ready: '+(meta.error||'building store…'));
  if(meta.error) $('segLabel').classList.add('err');
  if(meta.ready){ await refreshBoard(); await refreshTimeline(); }
}

$('scrub').addEventListener('input', async (e)=>{
  if(liveOn){ /* scrubbing pauses visual only */ }
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
    if(d.type==='heartbeat'||d.type==='poll_error') return;
    if(d.type==='replay_done'){
      toast('Replay complete — board at end of indexed footage');
      return;
    }
    if(d.t_end!=null){
      setScrub(d.t_end);
    }
    if(d.type==='supersede'){
      toast(d.toast||('SUPERSEDED: '+d.old+' → '+d.new));
    }
    if(d.type==='segment' && d.live_new){
      toast('NEW SEGMENT from VSS · '+ (d.clip_id||'').slice(-40));
      init();
    }
    refreshBoard();
    refreshTimeline();
  };
  es.onerror=()=>{ /* browser retries */ };
}
function stopLive(){
  liveOn=false;
  $('liveDot').classList.remove('on');
  if(es){es.close();es=null;}
}
$('liveToggle').addEventListener('change',(e)=>{
  if(e.target.checked) startLive(); else { stopLive(); setScrub(meta.t_max); refreshBoard(); refreshTimeline(); }
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
  const video=d.stream_url
    ? '<video controls preload="metadata" src="'+d.stream_url+'"></video>'
    : '<p class="meta">No playable stream for this citation.</p>';
  const quote=d.caption ? '<div class="quote">“'+esc(d.caption)+'”</div>' : '';
  el.innerHTML=
    '<p><span class="badge '+(d.status||'')+'">'+(d.status||'')+'</span>'
    +(d.rule_id?' · <code>'+esc(d.rule_id)+'</code>':'')+'</p>'
    +'<div style="font-family:Syne,sans-serif;font-size:1.5rem;margin:.4rem 0">'+esc(String(ans))+'</div>'
    +'<p class="meta"><code>'+esc(d.entity)+'</code> / <code>'+esc(d.attribute)+'</code> @ <code>'+esc(d.camera_id)
    +'</code> · as_of '+esc(d.as_of_fmt)+'</p>'
    +(d.clip_id?'<p class="meta">Cited · <code>'+esc(d.clip_id)+'</code> · '+esc(d.t_start_fmt)+'–'+esc(d.t_end_fmt)+'</p>':'')
    +quote
    +(d.stale_reason?'<p class="err">'+esc(d.stale_reason)+'</p>':'')
    +(d.data_gap_note?'<p class="meta">'+esc(d.data_gap_note)+'</p>':'')
    +video+audit;
}
$('askBtn').addEventListener('click', doAsk);
document.querySelectorAll('.presets button').forEach(b=>{
  b.addEventListener('click',()=>{ $('q').value=b.dataset.q; doAsk(); });
});

// ticking ages
setInterval(()=>{ if(meta.ready && !document.hidden) refreshBoard(); }, 2000);
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
            return self._send(200, json.dumps({
                "ready": STATE["ready"], "error": STATE["error"],
                "t_min": STATE["t_min"], "t_max": STATE["t_max"],
                "segment_count": STATE["segment_count"],
                "camera_id": CAMERA, "zones": list(ZONES),
                "live_interval_sec": LIVE_INTERVAL,
                "live_label": "replay of indexed footage",
            }), "application/json")
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
