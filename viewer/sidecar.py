"""Per-clip metadata sidecar for the local viewer. Standard library only.

clips/<name>.mp4 pairs with clips/<name>.meta.json:
  {"version": 1, "clip_id": "<name>", "path": "clips/<name>.mp4",
   "scene": "bridge", "description": "...",
   "video": {"fps": 29.97, "width": 1280, "height": 720, "duration": 35.0},   # ffprobe
   "timeline": {"order": 1, "t_start": 9.0, "t_end": 44.0},                   # ingest_clips.py
   "zones": {"underpass": [[x, y], ...]},                                      # normalized polygons
   "segments": [{"id", "t_start", "t_end", "entity", "action", "source", "notes", "confidence",
                 "claims": [{"entity", "attribute", "value", "location"?, "t_start"?, "t_end"?}]}],
   "tracks": [{"track_id", "label", "source", "boxes": [[t, x, y, w, h, conf], ...]}],
   "detector": {...}}                                                           # detect.py

Every time in a sidecar is a float of seconds RELATIVE to the clip start. Strings are
rejected on purpose: claims.parse_t reads "00:01" as HH:MM (60 s) and a bare "20" as 20 hours,
so MM:SS strings from other tools must come through mmss_to_seconds() exactly once.
Boxes are top-left corner + size, normalized to 0..1 of the frame.
"""
import json
import os
import re
import subprocess

from ingest_adapter import ClaimSource, tracks_to_claims

SCHEMA_VERSION = 1
CREATING_RULES = ("FIRST_CLAIM", "SUPERSEDE_NEWER_CONTRADICTS", "OLDER_OR_TIED_CONTRADICTION")


def clip_id_for(clip_path):
    return os.path.splitext(os.path.basename(clip_path))[0]


def sidecar_path(clip_path):
    return os.path.splitext(clip_path)[0] + ".meta.json"


def mmss_to_seconds(s):
    """'0:01' / '01:30.5' / '00:01:02' / number -> float seconds. Not claims.parse_t."""
    if isinstance(s, bool):
        raise ValueError(f"not a time: {s!r}")
    if isinstance(s, (int, float)):
        return float(s)
    parts = str(s).strip().split(":")
    if not 1 <= len(parts) <= 3 or not all(p.strip() for p in parts):
        raise ValueError(f"not a M:SS time: {s!r}")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def ffprobe_info(path):
    """{fps, width, height, duration} from the first video stream; fps from the r_frame_rate fraction."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate:format=duration",
         "-of", "json", path], capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    st = d["streams"][0]
    num, _, den = st["r_frame_rate"].partition("/")
    fps = float(num) / float(den or 1)
    return {"fps": round(fps, 3), "width": int(st["width"]), "height": int(st["height"]),
            "duration": float(d["format"]["duration"])}


def skeleton(clip_path, scene=None, description="", video=None):
    cid = clip_id_for(clip_path)
    return {"version": SCHEMA_VERSION, "clip_id": cid, "path": clip_path.replace(os.sep, "/"),
            "scene": scene or cid, "description": description,
            "video": video or ffprobe_info(clip_path),
            "zones": {}, "segments": [], "tracks": []}


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _check_span(obj, where):
    if not (_num(obj.get("t_start")) and _num(obj.get("t_end"))):
        raise ValueError(f"{where}: t_start/t_end must be numbers (seconds), got "
                         f"{obj.get('t_start')!r}/{obj.get('t_end')!r}")
    if obj["t_start"] > obj["t_end"]:
        raise ValueError(f"{where}: t_start > t_end")


def validate(meta):
    """Raise ValueError on anything the viewer or ingest would misread."""
    if meta.get("version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported sidecar version {meta.get('version')!r}")
    for k in ("clip_id", "path"):
        if not isinstance(meta.get(k), str) or not meta[k]:
            raise ValueError(f"missing {k}")
    v = meta.get("video") or {}
    for k in ("fps", "width", "height", "duration"):
        if not _num(v.get(k)) or v[k] <= 0:
            raise ValueError(f"video.{k} must be a positive number")
    for name, poly in (meta.get("zones") or {}).items():
        if len(poly) < 3 or any(len(p) != 2 or not all(_num(c) and 0 <= c <= 1 for c in p) for p in poly):
            raise ValueError(f"zone {name!r}: need >=3 [x, y] points in 0..1")
    for i, seg in enumerate(meta.get("segments") or []):
        _check_span(seg, f"segments[{i}]")
        for j, c in enumerate(seg.get("claims") or []):
            for k in ("entity", "attribute", "value"):
                if not isinstance(c.get(k), str) or not c[k]:
                    raise ValueError(f"segments[{i}].claims[{j}]: missing {k}")
            if "t_start" in c or "t_end" in c:
                _check_span({"t_start": c.get("t_start", seg["t_start"]),
                             "t_end": c.get("t_end", seg["t_end"])}, f"segments[{i}].claims[{j}]")
    for i, tr in enumerate(meta.get("tracks") or []):
        if not isinstance(tr.get("track_id"), int) or not isinstance(tr.get("label"), str):
            raise ValueError(f"tracks[{i}]: need int track_id and str label")
        for j, b in enumerate(tr.get("boxes") or []):
            if len(b) != 6 or not all(_num(x) for x in b):
                raise ValueError(f"tracks[{i}].boxes[{j}]: expected [t, x, y, w, h, conf]")
            if not all(0 <= x <= 1 for x in b[1:5]):
                raise ValueError(f"tracks[{i}].boxes[{j}]: x/y/w/h must be normalized 0..1")


def load_sidecar(path):
    with open(path) as f:
        meta = json.load(f)
    validate(meta)
    return meta


_NUM_ARRAY = re.compile(r"\[\s+((?:-?[\d.eE+-]+,\s+)*-?[\d.eE+-]+)\s+\]")


def save_sidecar(path, meta):
    """Pretty JSON, but innermost numeric arrays (boxes, polygon points) stay on one line."""
    validate(meta)
    text = json.dumps(meta, indent=1, sort_keys=True)
    text = _NUM_ARRAY.sub(lambda m: "[" + re.sub(r",\s+", ", ", m.group(1)) + "]", text)
    with open(path, "w") as f:
        f.write(text + "\n")


def gemini_to_segments(items, entity="person", source="gemini"):
    """Gemini prototype rows ({clip_name, timestamp_start, timestamp_end, bounding_box_estimation,
    action}) -> segments. The prose box estimate is kept in `notes`; real boxes come from tracks."""
    out = []
    for i, it in enumerate(items):
        out.append({"id": it.get("clip_name") or f"g{i}",
                    "t_start": mmss_to_seconds(it["timestamp_start"]),
                    "t_end": mmss_to_seconds(it["timestamp_end"]),
                    "entity": it.get("entity", entity), "action": it.get("action", ""),
                    "source": source, "notes": it.get("bounding_box_estimation", ""),
                    "claims": list(it.get("claims", []))})
    return out


def point_in_polygon(x, y, poly):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def zone_of_box(box, zones):
    """Name of the first zone containing the box centre, else None."""
    cx, cy = box[1] + box[3] / 2, box[2] + box[4] / 2
    for name, poly in zones.items():
        if point_in_polygon(cx, cy, poly):
            return name
    return None


def track_zone_intervals(track, zones, min_dur=0.2):
    """Run-length encode a track's boxes by zone -> the tracks_to_claims input shape:
    [{label, track_id, zone, t_start, t_end, conf}]. Boxes outside every zone and runs shorter
    than min_dur (detector flicker) are dropped."""
    boxes = track.get("boxes") or []
    if not boxes:
        if track.get("zone") and _num(track.get("t_start")) and _num(track.get("t_end")):
            return [{"label": track["label"], "track_id": track["track_id"], "zone": track["zone"],
                     "t_start": track["t_start"], "t_end": track["t_end"],
                     "conf": track.get("conf", 0.7)}]
        return []
    runs = []
    for b in boxes:
        z = zone_of_box(b, zones)
        if runs and runs[-1]["zone"] == z:
            runs[-1]["t_end"] = b[0]
            runs[-1]["confs"].append(b[5])
        else:
            runs.append({"zone": z, "t_start": b[0], "t_end": b[0], "confs": [b[5]]})
    return [{"label": track["label"], "track_id": track["track_id"], "zone": r["zone"],
             "t_start": r["t_start"], "t_end": r["t_end"],
             "conf": round(sum(r["confs"]) / len(r["confs"]), 3)}
            for r in runs if r["zone"] and r["t_end"] - r["t_start"] >= min_dur]


def segment_claims(meta):
    """Flatten segments[].claims[] with defaults, ordered by when they end (= observed_at)."""
    scene = meta.get("scene") or meta["clip_id"]
    out = []
    for seg in meta.get("segments") or []:
        for c in seg.get("claims") or []:
            out.append({"entity": c["entity"], "attribute": c["attribute"], "value": c["value"],
                        "location": c.get("location", scene),
                        "t_start": c.get("t_start", seg["t_start"]),
                        "t_end": c.get("t_end", seg["t_end"]),
                        "confidence": c.get("confidence", seg.get("confidence", 0.8))})
    out.sort(key=lambda c: (c["t_end"], c["t_start"]))
    return out


def track_claims(meta):
    """Zone-occupancy claims from tracks, via the existing YOLO hook tracks_to_claims."""
    scene = meta.get("scene") or meta["clip_id"]
    zones = meta.get("zones") or {}
    intervals = [iv for tr in meta.get("tracks") or [] for iv in track_zone_intervals(tr, zones)]
    intervals.sort(key=lambda i: (i["t_end"], i["t_start"]))
    claims = tracks_to_claims(intervals)
    for c in claims:
        c["location"] = scene
    return claims


class SidecarSource(ClaimSource):
    """ClaimSource over loaded sidecars, keyed by clip_id (same shape as CannedSource).
    kind='segments' yields the hand/VLM claims, kind='tracks' the detector zone claims, so one
    clip is ingested twice with a distinct `source` label in the claims table."""
    relative = True

    def __init__(self, metas_by_clip_id, kind="segments", source=None):
        if kind not in ("segments", "tracks"):
            raise ValueError(kind)
        self.metas, self.kind = metas_by_clip_id, kind
        self.source = source or ("sidecar_segments" if kind == "segments" else "yolo_tracks")

    def extract(self, clip):
        meta = self.metas.get(clip["clip_id"])
        if meta is None:
            return []
        return segment_claims(meta) if self.kind == "segments" else track_claims(meta)
