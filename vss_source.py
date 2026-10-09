"""VSS ClaimSource: pull warehouse segments and map Cosmos captions to claims.

Auth and base URL come from the environment (same vars the retrieval skills use).
Never hardcode credentials. Parsing is deterministic keyword mapping; when a
caption already uses the re-ingest vocabulary (BLOCKED/CLEAR + zone names),
that path wins so we can swap to re-ingested captions without code changes.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from ingest_adapter import ClaimSource

# Pinned vocabulary so the same real-world slot always gets the same key.
ZONES = (
    "left_aisle",
    "center_aisle",
    "right_aisle",
    "loading_area",
    "wall_area",
)

# Filename camera-angle → zone when the caption does not name one.
_ANGLE_ZONE = {
    "ceiling_00": "center_aisle",
    "ceiling_01": "center_aisle",
    "ceiling_02": "left_aisle",
    "ceiling_03": "right_aisle",
    "ceiling_04": "loading_area",
    "eye_00": "wall_area",
    "eye_01": "center_aisle",
    "eye_02": "left_aisle",
    "eye_03": "right_aisle",
    "eye_04": "wall_area",
}

# Explicit aisle/area names only (used for worker_present scoping).
_ZONE_NAMED_PATTERNS = (
    (r"\bleft\s+aisle\b", "left_aisle"),
    (r"\bright\s+aisle\b", "right_aisle"),
    (r"\bcenter\s+aisle\b|\bmiddle\s+aisle\b", "center_aisle"),
    (r"\bloading\s+(area|dock|bay)\b", "loading_area"),
    (r"\bwall\s+area\b", "wall_area"),
)
# Broader cues + bare "aisle" — only for blocked pin_zone fallbacks, not worker_present.
_ZONE_LOOSE_PATTERNS = _ZONE_NAMED_PATTERNS + (
    (r"\bnear (a |the )?wall\b|\bbrick wall\b", "wall_area"),
    (r"\bwarehouse aisle\b|\bin an? aisle\b|\baisle\b", "center_aisle"),
)


def _env(*names, default=None):
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


def login(backend=None, username=None, password=None):
    """POST /api/v1/auth/login → bearer token. Reads INGRESS_URL / USERNAME / PASSWORD."""
    backend = (backend or _env("INGRESS_URL", "VSS_URL") or "").rstrip("/")
    username = username or _env("USERNAME", "VSS_USERNAME")
    password = password or _env("PASSWORD", "VSS_PASSWORD")
    if not backend or not username or not password:
        raise RuntimeError("need INGRESS_URL/VSS_URL and USERNAME/PASSWORD (or VSS_*) in env")
    body = json.dumps({"username": username, "password": password}).encode()
    req = urllib.request.Request(
        f"{backend}/api/v1/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
    token = data.get("access_token")
    if not token:
        raise RuntimeError("login response missing access_token")
    return backend, token


def _get_json(backend, token, path, params=None):
    url = f"{backend}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def _post_json(backend, token, path, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{backend}{path}",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def filename_timestamp_seconds(name_or_uri):
    """Parse leading YYYYMMDD_HHMMSS from a chunk/segment filename → seconds since midnight UTC day."""
    base = str(name_or_uri).rstrip("/").split("/")[-1]
    m = re.match(r"(\d{8})_(\d{6})", base)
    if not m:
        return None
    hh, mm, ss = int(m.group(2)[0:2]), int(m.group(2)[2:4]), int(m.group(2)[4:6])
    return hh * 3600 + mm * 60 + ss


def angle_from_name(name_or_uri):
    base = str(name_or_uri).lower()
    m = re.search(r"(ceiling_\d+|eye_\d+)", base)
    return m.group(1) if m else None


def zone_named_in_caption(caption):
    """Return a ZONES member only when caption explicitly names left/center/right/loading/wall area."""
    low = (caption or "").lower()
    for pat, zone in _ZONE_NAMED_PATTERNS:
        if re.search(pat, low):
            return zone
    return None


def pin_zone(caption, name_or_uri=""):
    """Return a ZONES member. Prefer caption vocabulary; else filename angle; else center_aisle.

    Used for blocked/CLEAR aisle claims. Prefer zone_named_in_caption for worker_present.
    """
    low = (caption or "").lower()
    for pat, zone in _ZONE_LOOSE_PATTERNS:
        if re.search(pat, low):
            return zone
    angle = angle_from_name(name_or_uri)
    if angle and angle in _ANGLE_ZONE:
        return _ANGLE_ZONE[angle]
    return "center_aisle"


def _blocker(caption):
    """Optional 'by X' detail when the caption already made an explicit blocked claim."""
    low = (caption or "").lower()
    # Prefer "blocked/obstructed by <noun>"
    m = re.search(r"\b(?:blocked|obstructed|obstruction)\s+by\s+(\w+)", low)
    if m and m.group(1) in ("forklift", "pallet", "person", "people", "worker"):
        return "person" if m.group(1) in ("people", "worker") else m.group(1)
    if re.search(r"\bforklift\b", low):
        return "forklift"
    if re.search(r"\bpallet\b", low):
        return "pallet"
    if re.search(r"\bperson\b|\bpeople\b|\bworker\b", low):
        return "person"
    return "none"


# Explicit obstruction language only. Avoids "clearly", bare forklift presence, etc.
_BLOCKED_RE = re.compile(
    r"\b(?:blocked|obstructed|obstruction)\b"
    r"|\bblocking\b.{0,48}\b(?:aisle|zone|path|area|exit|passage)\b"
    r"|\b(?:aisle|zone|path|area|exit|passage)\b.{0,48}\bblocking\b",
    re.I,
)
_CLEAR_RE = re.compile(
    r"\bunobstructed\b"
    r"|\b(?:is|are|appears?|remains?|looks?)\s+clear\b"
    r"|\bclear\s+(?:of\b|aisle\b|zone\b|path\b|area\b)"
    r"|\bstatus:\s*clear\b",
    re.I,
)


def caption_blocked_value(caption):
    """Return (blocked_yes_no_detail, confidence) or (None, 0) if not explicit.

    Kept for re-ingest captions that say BLOCKED/CLEAR / blocked/obstructed.
    """
    text = caption or ""
    if not text.strip():
        return None, 0.0

    token_blocked = list(re.finditer(r"(?<![A-Za-z])BLOCKED(?![A-Za-z])", text))
    token_clear = list(re.finditer(r"(?<![A-Za-z])CLEAR(?![A-Za-z])", text))
    positions = ([(m.start(), "blocked") for m in _BLOCKED_RE.finditer(text)]
                 + [(m.start(), "clear") for m in _CLEAR_RE.finditer(text)]
                 + [(m.start(), "blocked") for m in token_blocked]
                 + [(m.start(), "clear") for m in token_clear])
    if not positions:
        return None, 0.0

    positions.sort()
    winner = positions[-1][1]
    if winner == "clear":
        return "no", 0.9 if len(positions) == 1 else 0.85
    by = _blocker(text)
    return (f"yes (by {by})" if by != "none" else "yes"), 0.9 if len(positions) == 1 else 0.85


# --- Existing-caption attributes (explicit phrases only) ---

_WORKER_NO_RE = re.compile(
    r"\bno\s+visible\s+workers\b"
    r"|\bno\s+workers\b"
    r"|\bno\s+(?:people|persons|personnel)\b"
    r"|\bdevoid\s+of\s+(?:other\s+)?workers\b"
    r"|\bwithout\s+(?:any\s+)?(?:visible\s+)?workers\b",
    re.I,
)
# "no other workers/people" implies at least one person is already in frame → yes.
_WORKER_NO_OTHER_YES_RE = re.compile(
    r"\bno\s+other\s+(?:workers?|people|persons|personnel)\b",
    re.I,
)
_WORKER_YES_RE = re.compile(
    r"\ba\s+worker\s+(?:walks?|walking|stands?|standing|approaches?|approaching|"
    r"is\s+positioned|positioned)\b"
    r"|\bworkers\s+(?:walk|walking|stand|standing)\b"
    r"|\ba\s+(?:person|individual|worker)\s+(?:walks?|walking|stands?|standing|"
    r"approaches?|approaching|wearing|is\s+seen|is\s+standing|is\s+walking|"
    r"is\s+positioned|positioned)\b"
    r"|\bthe\s+(?:person|individual|worker)\s+(?:walks?|walking|stands?|standing|"
    r"approaches?|approaching|turns?\s+and\s+(?:walks?|runs?)|runs?|running|"
    r"is\s+positioned|positioned|is\s+facing)\b"
    r"|\bperson\s+wearing\b",
    re.I,
)

# Forklift state: forklift as subject, short window, no person/individual in the span.
_FORK_PARKED_RE = re.compile(
    r"\bforklift\b(.{0,55}?)\b(?:is\s+|appears?\s+(?:to\s+be\s+)?|remains?\s+)?"
    r"(?:stationary|parked)\b"
    r"|\b(?:stationary|parked)\s+forklift\b",
    re.I | re.S,
)
_FORK_MOVING_RE = re.compile(
    r"\bforklift\b(.{0,55}?)\b(?:is\s+|then\s+|begins?\s+to\s+)?"
    r"(?:moving|moves|drove|drives|driving|traveling|travels|reverses?|rolling)\b"
    r"|\b(?:moving|driving)\s+forklift\b",
    re.I | re.S,
)
_PERSON_IN_SPAN = re.compile(r"\b(?:person|individual|worker|who|people)\b", re.I)


def caption_worker_present(caption):
    """Return ('yes'|'no', conf) or (None, 0) when the caption is explicit about workers."""
    text = caption or ""
    # Presence descriptions win over a later "no other workers…" negation.
    for m in _WORKER_YES_RE.finditer(text):
        prefix = text[max(0, m.start() - 24):m.start()].lower()
        if re.search(r"\bno\b", prefix):
            continue
        return "yes", 0.85
    if _WORKER_NO_OTHER_YES_RE.search(text):
        return "yes", 0.85
    positions = [(m.start(), "no") for m in _WORKER_NO_RE.finditer(text)]
    if not positions:
        return None, 0.0
    positions.sort()
    return positions[-1][1], 0.85


def caption_forklift_state(caption):
    """Return ('moving'|'parked', conf) or (None, 0) when forklift motion is explicit."""
    text = caption or ""
    if not re.search(r"\bforklift\b", text, re.I):
        return None, 0.0
    positions = []
    for label, cre in (("parked", _FORK_PARKED_RE), ("moving", _FORK_MOVING_RE)):
        for m in cre.finditer(text):
            # Group 1 is the between-span when present; reject person-as-subject bridges.
            mid = m.group(1) if m.lastindex else ""
            if mid and _PERSON_IN_SPAN.search(mid):
                continue
            positions.append((m.start(), label))
    if not positions:
        return None, 0.0
    positions.sort()
    return positions[-1][1], 0.85


def _claim(entity, attribute, value, location, t_start, t_end, observed_at, confidence):
    return {
        "entity": entity,
        "attribute": attribute,
        "value": value,
        "location": location,
        "t_start": float(t_start),
        "t_end": float(t_end),
        "observed_at": float(observed_at),
        "confidence": confidence,
    }


def caption_to_claims(caption, *, camera_id, source_uri, t_start, t_end, observed_at=None):
    """Map one caption → claim dicts (absolute times; relative=False).

    Emits, when the caption is explicit:
      - (zone, blocked, …) — re-ingest BLOCKED/CLEAR (zone from caption or angle pin)
      - (camera_id|zone, worker_present, yes/no) — camera-scoped unless caption names an aisle
      - (forklift, state, moving|parked) — stock captions
    Vague captions yield [].
    """
    obs = observed_at if observed_at is not None else t_end
    out = []

    blocked, bconf = caption_blocked_value(caption)
    if blocked is not None:
        zone = pin_zone(caption, source_uri)
        out.append(_claim(zone, "blocked", blocked, camera_id, t_start, t_end, obs, bconf))

    worker, wconf = caption_worker_present(caption)
    if worker is not None:
        # One worker in frame must not light every aisle — only name a zone when caption does.
        named = zone_named_in_caption(caption)
        worker_entity = named if named else camera_id
        out.append(_claim(worker_entity, "worker_present", worker, camera_id,
                          t_start, t_end, obs, wconf))

    fstate, fconf = caption_forklift_state(caption)
    if fstate is not None:
        out.append(_claim("forklift", "state", fstate, camera_id, t_start, t_end, obs, fconf))

    return out


def segments_from_explore(payload, *, camera_id="sdg_warehouse_cam-2", location="warehouse3"):
    """Flatten explore JSON into segment dicts sorted by absolute time."""
    out = []
    for chunk in payload.get("chunks") or []:
        if camera_id and chunk.get("camera_id") and chunk.get("camera_id") != camera_id:
            continue
        if location and chunk.get("location") and chunk.get("location") != location:
            continue
        ov = chunk.get("original_video") or ""
        base_t = filename_timestamp_seconds(ov) or filename_timestamp_seconds(chunk.get("filename") or "")
        if base_t is None:
            # fall back to upload_timestamp clock seconds-of-day
            ut = chunk.get("upload_timestamp") or ""
            try:
                dt = datetime.fromisoformat(ut.replace("Z", "+00:00"))
                base_t = dt.hour * 3600 + dt.minute * 60 + dt.second
            except ValueError:
                base_t = 0.0
        for seg in chunk.get("timeline") or []:
            src = seg.get("source") or chunk.get("preview_source") or ov
            s0 = float(seg.get("segment_start_sec") or 0.0)
            s1 = float(seg.get("segment_end_sec") or s0)
            abs0, abs1 = base_t + s0, base_t + s1
            out.append({
                "clip_id": src.split("/")[-1] if src else f"{ov}_{s0}_{s1}",
                "path": src,
                "original_video": ov,
                "camera_id": chunk.get("camera_id") or camera_id,
                "location_meta": chunk.get("location") or location,
                "caption": seg.get("reasoning_content") or "",
                "description": seg.get("reasoning_content") or "",
                "t_start": abs0,
                "t_end": abs1,
                "segment_start_sec": s0,
                "segment_end_sec": s1,
            })
    out.sort(key=lambda s: (s["t_start"], s["t_end"], s["clip_id"]))
    return out


def fetch_warehouse_segments(backend=None, token=None, *, camera_id="sdg_warehouse_cam-2",
                             location="warehouse3", limit=100):
    """Login if needed, GET /api/v1/videos/explore, return sorted segment dicts."""
    if not token:
        backend, token = login(backend)
    else:
        backend = (backend or _env("INGRESS_URL", "VSS_URL") or "").rstrip("/")
    payload = _get_json(backend, token, "/api/v1/videos/explore",
                        {"scope": "all", "limit": limit, "offset": 0, "location": location})
    return segments_from_explore(payload, camera_id=camera_id, location=location), backend, token


def normalize_detections(payload):
    """VSS /videos/detections sidecar → frames of normalized boxes.

    Each box is {label, conf, x, y, w, h} with x,y the top-left. Pixel bboxes
    (xyxy) are divided by frame shape [h, w]. Values already in 0..1 stay as
    xyxy normalized. If the sidecar has pixel coords but no shape, pixel=True
    and the browser divides by the video size.
    """
    if not isinstance(payload, dict):
        return {"frames": [], "frame_count": 0}
    video_shape = payload.get("video_shape") or []
    frames = []
    for fr in payload.get("frames") or []:
        shape = fr.get("shape") or video_shape
        height = width = None
        if isinstance(shape, (list, tuple)) and len(shape) >= 2:
            height, width = float(shape[0] or 0), float(shape[1] or 0)
        boxes = []
        for det in fr.get("detections") or []:
            bbox = det.get("bbox") or []
            if len(bbox) < 4:
                continue
            x1, y1, x2, y2 = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
            bw, bh = x2 - x1, y2 - y1
            if bw <= 0 or bh <= 0:
                continue
            pixel = False
            if width and height and max(abs(x2), abs(y2), abs(x1), abs(y1)) > 1.5:
                x, y, bw, bh = x1 / width, y1 / height, bw / width, bh / height
            elif max(abs(x2), abs(y2), abs(x1), abs(y1)) > 1.5:
                pixel = True
                x, y = x1, y1
            else:
                x, y = x1, y1
            boxes.append({
                "label": str(det.get("label") or "object"),
                "conf": float(det.get("confidence") or 0),
                "x": x, "y": y, "w": bw, "h": bh, "pixel": pixel,
            })
        frames.append({"t": float(fr.get("time_sec") or 0), "boxes": boxes})
    frames.sort(key=lambda item: item["t"])
    return {"fps": payload.get("fps"), "frames": frames, "frame_count": len(frames)}


class VssSource(ClaimSource):
    """ClaimSource over a clip dict that already carries a Cosmos caption.

    Use with clips produced by fetch_warehouse_segments / segments_from_explore.
    Times on claims are absolute (relative=False).
    """
    relative = False
    source = "vss_cosmos"

    def __init__(self, camera_id="sdg_warehouse_cam-2"):
        self.camera_id = camera_id

    def extract(self, clip):
        caption = clip.get("caption") or clip.get("description") or ""
        camera = clip.get("camera_id") or self.camera_id
        uri = clip.get("path") or clip.get("original_video") or clip.get("clip_id") or ""
        return caption_to_claims(
            caption,
            camera_id=camera,
            source_uri=uri,
            t_start=clip["t_start"],
            t_end=clip["t_end"],
        )
