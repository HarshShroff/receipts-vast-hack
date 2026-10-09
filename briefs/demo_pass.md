# Demo pass brief (demos 4:30 ET, feature freeze 3:30)

Only touch webapp/ and run_vss.py. Do not change supersede.py / answer.py logic. Goal: a judge understands it in 10 seconds and it looks like a serious ops product.

1. LIVE MODE. A "Live" toggle that streams segments in observed_at order via SSE (~1 segment / 2 s). Label it honestly: "replay of indexed footage". Also a poller that ingests any NEW segments that appear in VSS, so when our re-ingest lands it shows up live. Each new claim animates into the board.
2. LAYOUT. Dark control-room UI. Top: big status tiles per camera/zone (WORKER PRESENT / FORKLIFT MOVING / PARKED), green / amber / red, with a ticking "last confirmed Xs ago". On supersede: red strike-through flash of the old value, new value slides in, toast "SUPERSEDED: moving -> parked (cam-2, seg 002)". STALE: tile greys out with banner "NO FOOTAGE SINCE hh:mm:ss - may be stale".
3. ASK. Question box (e.g. "Is the forklift on cam-2 moving?") answering with the cited segment: playable clip or thumbnail, timestamps, caption quote, status badge, expandable audit trail with rule ids.
4. TIMELINE. Horizontal per-camera strip of claim values over time with supersede markers; a time scrubber (as_of) that time-travels the whole board.
5. Header "RECEIPTS - every answer has a clip". Footer chips for sponsor tools actually used: VAST DataEngine, NVIDIA Cosmos Reason, CoreWeave, Cursor (+ W&B only if the eval agent wires it).

Single page, vanilla JS or one CDN lib, no build step. Never show fabricated data. Redeploy, check /app/health, commit, push (git identity is set). Then summarize what a judge will see.
