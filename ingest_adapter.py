"""Where Cosmos-Reason output and YOLO tracks plug in on hackathon day.

Contract: a ClaimSource turns one clip into a list of claim dicts:
  {"entity": "pallet_7",        # stable name of the thing (YOLO track label/id)
   "attribute": "position",     # what is being claimed
   "value": "blocking exit",    # short, canonical value; same wording for same state
   "location": "north_exit",    # zone/camera area, '' if not applicable
   "t_start": 12.0, "t_end": 15.0,  # seconds, RELATIVE to the clip start (see `relative`)
   "confidence": 0.85}          # 0..1
The (entity, attribute, location) triple is the supersession key: pick canonical
names so the same real-world slot always gets the same triple.
"""
import json
import re
from abc import ABC, abstractmethod

from claims import parse_t
from supersede import ingest_claim

REQUIRED = ("entity", "attribute", "value", "t_start", "t_end")

COSMOS_PROMPT = """You are watching a surveillance clip of {scene}. The clip lasts {duration:.0f} seconds.
List every state you can see that could change later (for example: is an exit blocked, where a pallet
or forklift is, whether a door is open). Reply with ONLY a JSON array, no prose. Each item:
{{"entity": "<short snake_case name, e.g. pallet_7>",
  "attribute": "<state|position|count|...>",
  "value": "<short canonical value, e.g. blocked, clear, open, closed, bay_3>",
  "location": "<zone name from this list: {zones}>",
  "t_start": <seconds from clip start when you first see this state>,
  "t_end": <seconds from clip start when you last see it>,
  "confidence": <0.0-1.0>}}
Use the same entity/attribute/location names every time for the same thing. Only report what is
visible; do not guess. If nothing relevant is visible, reply [].
Known entities: {entities}"""


class ClaimSource(ABC):
    relative = True  # t_start/t_end are offsets from the clip start

    @abstractmethod
    def extract(self, clip):
        """clip: {"clip_id", "t_start", "t_end", "path"} -> list of claim dicts."""


class CannedSource(ClaimSource):
    """Stub: returns pre-written claims keyed by clip_id. Used by tests and the synthetic demo."""
    def __init__(self, claims_by_clip, relative=False, source="stub"):
        self.claims, self.relative, self.source = claims_by_clip, relative, source

    def extract(self, clip):
        return list(self.claims.get(clip["clip_id"], []))


class CosmosSource(ClaimSource):
    """Hackathon day: call_model(prompt, video_path) -> str is YOUR wrapper around the Cosmos
    Reason endpoint (build.nvidia.com / NIM). Not implemented here on purpose (no model calls)."""
    def __init__(self, call_model, scene, zones, entities=()):
        self.call_model, self.scene, self.zones, self.entities = call_model, scene, zones, entities

    def extract(self, clip):
        prompt = COSMOS_PROMPT.format(scene=self.scene, duration=clip["t_end"] - clip["t_start"],
                                      zones=", ".join(self.zones), entities=", ".join(self.entities) or "none")
        return parse_claims_json(self.call_model(prompt, clip["path"]))


def parse_claims_json(text):
    """Tolerates ```json fences and prose around the array. Raises ValueError on bad shape."""
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError("no JSON array in model output")
    items = json.loads(m.group(0))
    for it in items:
        missing = [k for k in REQUIRED if k not in it]
        if missing:
            raise ValueError(f"claim missing {missing}: {it}")
    return items


def tracks_to_claims(tracks, attribute="zone"):
    """YOLO/ByteTrack hook. tracks: [{"label": "pallet", "track_id": 7, "zone": "north_exit",
    "t_start": 12.0, "t_end": 15.0, "conf": 0.9}] (clip-relative seconds, zone from your polygon
    lookup) -> claims saying which zone each tracked object occupied."""
    return [{"entity": f"{t['label']}_{t['track_id']}", "attribute": attribute, "value": t["zone"],
             "location": "", "t_start": t["t_start"], "t_end": t["t_end"],
             "confidence": t.get("conf", 0.7)} for t in tracks]


def ingest_clip(store, clip, source, model_name=None):
    """Register the clip, extract claims, run them through the rule engine. Returns rule results."""
    store.add_clip(clip["clip_id"], clip["t_start"], clip["t_end"], clip.get("description", ""),
                   clip.get("path", ""))
    base = parse_t(clip["t_start"]) if source.relative else 0.0
    results = []
    for c in source.extract(clip):
        results.append(ingest_claim(
            store, c["entity"], c["attribute"], c["value"], clip["clip_id"],
            base + parse_t(c["t_start"]), base + parse_t(c["t_end"]), c.get("location", ""),
            c.get("confidence", 0.8), model_name or getattr(source, "source", type(source).__name__)))
    return results
