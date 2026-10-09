# Live clip brief (after the video-first fixes; skip if not indexed by 3:00 ET)

clips/person_moving.mp4 is OUR footage, recorded at the venue today (8.6 s, 1080p, has an audio track). Not internet footage, so it may go through the pipeline.

1. Make a copy with audio stripped (ffmpeg -an) for anything uploaded or shown; the room audio has other people's conversations.
2. Ingest that copy through the team pipeline (find the ingest/upload path the skills use; metadata camera_id=venue_cam-1, location=venue, scenario matching the warehouse safety prompt: worker present / walking, near the pillar). One clip only.
3. Poll until indexed. When its captions land, run run_vss.py so its claims appear; the app's poller should show them arriving live. Record how long ingest -> searchable took.
4. In the app, add "venue_cam-1 (recorded live today)" to the camera list so the demo can say: we filmed this an hour ago, ingested it through VAST + Cosmos, and the board picked it up.
5. If ingest is still pending at 3:00 ET, stop and leave the warehouse demo as is. Never fake its captions.
