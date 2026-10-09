# Local clip viewer

Plays `clips/*.mp4` with its metadata drawn over the picture as time passes: detector boxes,
named zones, the active VLM segment, and each claim's status in `receipts.db` (ACTIVE /
SUPERSEDED, the rule that created it, what superseded it). An "ask as of" panel runs the same
`answer()` the agent uses and jumps the player to the cited clip and time range.

Server and ingest are standard library only; the detector is the one optional heavy step.

## Run

    python3 viewer/ingest_clips.py --init          # skeleton sidecars for clips lacking one (needs ffprobe)
    python3 viewer/ingest_clips.py [--gap 30]      # clips -> viewer/receipts.db; prints rule results
    python3 viewer/serve.py                        # http://localhost:8765  (PORT=... to change)
    python3 -m unittest tests.test_viewer -v

YOLO tracks (needs ultralytics + torch; use an env that has them, not the project env):

    ~/.pyenv/versions/3.11.9/envs/hack51/bin/python viewer/detect.py clips/*.mp4
    python3 viewer/ingest_clips.py                 # re-ingest so zone claims land in the db

The server reopens `receipts.db` and re-reads sidecars when they change, so re-ingesting while
it runs is fine.

## Sidecar: `clips/<name>.meta.json`

One file per clip, committed next to the mp4. Times are float seconds relative to the clip
start; boxes are `[t, x, y, w, h, conf]` with top-left + size normalized to 0..1.

| key | written by | meaning |
|---|---|---|
| `video` | `--init` (ffprobe) | fps, width, height, duration; clips differ, so these matter |
| `timeline` | `ingest_clips.py` | the clip's `t_start`/`t_end` on the shared timeline (file order) |
| `scene`, `description` | hand | `scene` is the default claim `location` |
| `zones` | hand | name -> polygon of normalized `[x, y]` points |
| `segments[]` | hand / converter | interval + `action` text + `claims[]` triples (`location` defaults to `scene`) |
| `tracks[]` | `detect.py` | `track_id`, `label`, `boxes[]` |

Claims reach the rule engine two ways, both through the existing `ingest_adapter.ingest_clip`:
`segments[].claims[]` directly (`source=sidecar_segments`), and tracks turned into zone-occupancy
intervals by `sidecar.track_zone_intervals` -> `ingest_adapter.tracks_to_claims`
(`source=yolo_tracks`, entity `label_trackid`, attribute `zone`). Runs shorter than 0.2 s are
treated as detector flicker and dropped.

`sidecar.gemini_to_segments` converts the Gemini prototype rows (`clip_name`, `MM:SS`
timestamps, prose box estimate, `action`); the prose lands in `segments[].notes`. Do not pass
`MM:SS` strings through `claims.parse_t`: it reads them as `HH:MM`.

## Authoring zones

Shift-click on the picture prints a normalized `[x, y]` to the browser console; paste the points
into `zones` and re-run `ingest_clips.py`. Zones are applied at ingest/serve time, so editing
them never requires re-running the detector.

## Notes

- Seeking needs HTTP Range responses; `serve.py` implements `bytes=a-b`, `a-`, `-n` and HEAD.
- `as_of` in `api/answer` is seconds on the shared timeline, the same unit as the slider.
  Claim status (ACTIVE / SUPERSEDED / NO EVIDENCE) and the stale flag (FRESH / STALE) are shown
  separately, and the rule shown is the one that created the cited claim (from `audit`), since
  `claims.rule_fired` is rewritten when a claim is superseded.
- Default `max_age` is 20 s because the whole local timeline is about 85 s; the three scenes are
  serialized, so a bridge key legitimately goes stale while the garage clips play.
- Track ids restart per clip, so `truck_1` in two clips may be different objects.
