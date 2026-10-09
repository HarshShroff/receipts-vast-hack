#!/usr/bin/env python3
"""Minimal Receipts warehouse safety UI — stdlib only. Routes at / (Ingress strips /app)."""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ConfigMap mounts this directory as /code; receipts modules sit beside main.py.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from answer import answer  # noqa: E402
from claims import Store, fmt_t, parse_t  # noqa: E402
from ingest_adapter import ingest_clip  # noqa: E402
from vss_source import VssSource, fetch_warehouse_segments, login  # noqa: E402

PORT = int(os.environ.get("PORT", "8080"))
CAMERA = os.environ.get("CAMERA_ID", "sdg_warehouse_cam-2")
LOCATION = os.environ.get("VSS_LOCATION", "warehouse3")
DB_PATH = os.environ.get("RECEIPTS_DB", "/tmp/receipts.db")
ZONES = ("left_aisle", "center_aisle", "right_aisle", "loading_area", "wall_area")

STATE = {
    "store": None,
    "token": None,
    "backend": None,
    "t_min": 0.0,
    "t_max": 1.0,
    "error": None,
    "segment_count": 0,
    "ready": False,
}


def build_store():
    backend, token = login()
    segments, backend, token = fetch_warehouse_segments(
        backend, token, camera_id=CAMERA, location=LOCATION)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    store = Store(DB_PATH)
    src = VssSource(camera_id=CAMERA)
    for seg in segments:
        ingest_clip(store, seg, src, model_name="cosmos_reason_vss")
    t_min = store.db.execute("SELECT MIN(t_start) m FROM clips").fetchone()["m"] or 0.0
    t_max = store.db.execute("SELECT MAX(t_end) m FROM clips").fetchone()["m"] or 1.0
    STATE.update(store=store, token=token, backend=backend.rstrip("/"),
                 t_min=float(t_min), t_max=float(t_max),
                 segment_count=len(segments), ready=True, error=None)


def public_backend():
    """Browser-facing VSS base (Ingress host). Server-side may use in-cluster DNS."""
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


def ask(entity, attribute, as_of):
    store = STATE["store"]
    # forklift/state is camera-scoped; zone attributes use the selected aisle
    if attribute == "state":
        entity = "forklift"
    key = (entity, attribute, CAMERA)
    r = answer(store, key, as_of, max_age=3600)
    # Attach rule ids from audit for this key's claims
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
    rule = None
    path = None
    if r["claim_id"]:
        row = store.db.execute(
            "SELECT rule_fired FROM claims WHERE id=?", (r["claim_id"],)).fetchone()
        rule = row["rule_fired"] if row else None
        ev = store.db.execute(
            "SELECT e.clip_id, cl.path FROM evidence e "
            "LEFT JOIN clips cl ON cl.clip_id = e.clip_id "
            "WHERE e.claim_id=? AND e.observed_at<=? "
            "ORDER BY e.observed_at DESC LIMIT 1",
            (r["claim_id"], parse_t(as_of))).fetchone()
        path = (ev["path"] if ev and ev["path"] else None) or (
            store.db.execute("SELECT path FROM clips WHERE clip_id=?",
                             (r["clip_id"],)).fetchone() or {"path": None})["path"]
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
        "history": history,
        "as_of": parse_t(as_of),
        "as_of_fmt": fmt_t(parse_t(as_of)),
        "entity": entity,
        "attribute": attribute,
    }


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Receipts — Warehouse aisle safety</title>
<style>
  :root { --bg:#1a1f1c; --fg:#e8efe9; --muted:#9aab9e; --accent:#c4a35a; --ok:#6fbf8a; --bad:#d97b6c; --line:#2e3831; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: "IBM Plex Sans", "Segoe UI", sans-serif; background:
    radial-gradient(1200px 600px at 10% -10%, #2a332c, transparent),
    linear-gradient(180deg, #141814, #1a1f1c 40%, #121613); color:var(--fg); min-height:100vh; }
  main { max-width: 760px; margin: 0 auto; padding: 2rem 1.25rem 3rem; }
  h1 { font-family: "IBM Plex Serif", Georgia, serif; font-weight: 600; font-size: 2rem; letter-spacing: -0.02em; margin: 0 0 0.35rem; }
  .sub { color: var(--muted); margin-bottom: 1.75rem; line-height: 1.45; }
  label { display:block; font-size: 0.8rem; color: var(--muted); margin: 1rem 0 0.35rem; text-transform: uppercase; letter-spacing: 0.06em; }
  select, input[type=range] { width: 100%; }
  select { background: #222924; color: var(--fg); border: 1px solid var(--line); padding: 0.65rem 0.75rem; font-size: 1rem; }
  button { margin-top: 1.25rem; background: var(--accent); color: #1a1510; border: 0; padding: 0.7rem 1.2rem; font-weight: 600; cursor: pointer; font-size: 1rem; }
  button:hover { filter: brightness(1.05); }
  .meta { color: var(--muted); font-size: 0.9rem; margin-top: 0.4rem; }
  #result { margin-top: 2rem; padding-top: 1.5rem; border-top: 1px solid var(--line); }
  .status { display:inline-block; padding: 0.15rem 0.5rem; font-size: 0.75rem; font-weight: 700; letter-spacing: 0.04em; }
  .ACTIVE { background: #244632; color: var(--ok); }
  .SUPERSEDED { background: #3a2a1a; color: var(--accent); }
  .STALE { background: #3a2422; color: var(--bad); }
  .NO_EVIDENCE { background: #2a2a2a; color: var(--muted); }
  .answer { font-size: 1.5rem; margin: 0.5rem 0 1rem; font-family: "IBM Plex Serif", Georgia, serif; }
  video { width: 100%; max-height: 360px; background: #000; margin: 0.75rem 0; }
  table { width: 100%; border-collapse: collapse; font-size: 0.9rem; margin-top: 1rem; }
  th, td { text-align: left; padding: 0.45rem 0.35rem; border-bottom: 1px solid var(--line); vertical-align: top; }
  th { color: var(--muted); font-weight: 500; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; }
  .err { color: var(--bad); }
</style>
</head>
<body>
<main>
  <h1>Receipts</h1>
  <p class="sub">Warehouse aisle safety — answers cite a clip, never from superseded footage.
    Camera <code>sdg_warehouse_cam-2</code> · Pack C</p>

  <label for="qtype">Question</label>
  <select id="qtype">
    <option value="worker_present">Is a worker present in this zone?</option>
    <option value="state">Is the forklift moving or parked?</option>
    <option value="blocked">Is the aisle blocked? (re-ingest captions)</option>
  </select>

  <label for="zone">Aisle / zone</label>
  <select id="zone">
    <option value="center_aisle">center aisle</option>
    <option value="left_aisle">left aisle</option>
    <option value="right_aisle">right aisle</option>
    <option value="loading_area">loading area</option>
    <option value="wall_area">wall area</option>
  </select>

  <label for="asof">Question time (as_of): <span id="asof_label">—</span></label>
  <input id="asof" type="range" min="0" max="1" step="1" value="0"/>
  <p class="meta" id="coverage">Loading index…</p>

  <button id="ask" type="button">Answer with cited clip</button>

  <div id="result" hidden></div>
</main>
<script>
let meta = {t_min:0, t_max:1, ready:false};
function fmt(s){
  s = Math.round(Number(s)||0);
  const h=String(Math.floor(s/3600)).padStart(2,'0');
  const m=String(Math.floor((s%3600)/60)).padStart(2,'0');
  const sec=String(s%60).padStart(2,'0');
  return h+':'+m+':'+sec;
}
async function init(){
  const r = await fetch('api/meta');
  meta = await r.json();
  const sl = document.getElementById('asof');
  sl.min = Math.floor(meta.t_min);
  sl.max = Math.ceil(meta.t_max);
  sl.value = sl.max;
  document.getElementById('asof_label').textContent = fmt(sl.value);
  document.getElementById('coverage').textContent =
    meta.ready
      ? (meta.segment_count + ' segments indexed · footage ' + fmt(meta.t_min) + ' → ' + fmt(meta.t_max))
      : ('Not ready: ' + (meta.error || 'building store…'));
  if (meta.error) document.getElementById('coverage').classList.add('err');
}
document.getElementById('asof').addEventListener('input', (e)=>{
  document.getElementById('asof_label').textContent = fmt(e.target.value);
});
document.getElementById('ask').addEventListener('click', async ()=>{
  const zone = document.getElementById('zone').value;
  const attr = document.getElementById('qtype').value;
  const as_of = document.getElementById('asof').value;
  const r = await fetch('api/answer?entity='+encodeURIComponent(zone)
    +'&attribute='+encodeURIComponent(attr)
    +'&as_of='+encodeURIComponent(as_of));
  const d = await r.json();
  const el = document.getElementById('result');
  el.hidden = false;
  let hist = '';
  if (d.history && d.history.length){
    hist = '<h3>Claim history</h3><table><tr><th>Status</th><th>Value</th><th>Rule</th><th>Clip</th><th>Time</th></tr>' +
      d.history.map(h => '<tr><td><span class="status SUPERSEDED">SUPERSEDED</span></td><td>'+
        (h.value||'')+'</td><td>'+(h.rule_id||'')+'</td><td style="word-break:break-all">'+
        (h.clip_id||'')+'</td><td>'+fmt(h.t_start)+'–'+fmt(h.t_end)+'</td></tr>').join('') +
      '</table>';
  }
  const ans = d.answer == null ? '(no claim)' : d.answer;
  const video = d.stream_url
    ? '<video controls preload="metadata" src="'+d.stream_url+'"></video>'
    : '<p class="meta">No playable stream for this citation.</p>';
  el.innerHTML =
    '<p><span class="status '+(d.status||'')+'">'+(d.status||'')+'</span>' +
    (d.rule_id ? ' · rule <code>'+d.rule_id+'</code>' : '') + '</p>' +
    '<div class="answer">'+ans+'</div>' +
    '<p class="meta"><code>'+d.entity+'</code> / <code>'+d.attribute+'</code> @ <code>'+d.camera_id+
    '</code> · as_of '+d.as_of_fmt+'</p>' +
    (d.clip_id ? '<p class="meta">Cited clip <code>'+d.clip_id+'</code> · '+
      d.t_start_fmt+'–'+d.t_end_fmt+'</p>' : '') +
    (d.stale_reason ? '<p class="err">'+d.stale_reason+'</p>' : '') +
    (d.data_gap_note ? '<p class="meta">'+d.data_gap_note+'</p>' : '') +
    video + hist;
});
init();
setInterval(()=>{ if(!meta.ready) init(); }, 3000);
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

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/health":
            # Process liveness — store may still be building from VSS.
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
            }), "application/json")
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
        if path in ("/", "/index.html", "/app"):
            return self._send(200, PAGE)
        return self._send(404, "not found")


def main():
    def worker():
        try:
            build_store()
            print(f"store ready: {STATE['segment_count']} segments "
                  f"{fmt_t(STATE['t_min'])}-{fmt_t(STATE['t_max'])}", flush=True)
        except Exception as e:
            STATE["error"] = str(e)
            STATE["ready"] = False
            print(f"store build failed: {e}", flush=True)

    threading.Thread(target=worker, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"listening on 0.0.0.0:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
