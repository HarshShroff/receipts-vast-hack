# Final polish (checked live at 2:25 PM). Hard stop 2:45, then freeze. Only webapp/.

1. RUN DEMO PACING (most important; a judge watches this in a recorded video). Right now it jumps to the end state in under 8 s. Make it step-through: each press of Space or Right-arrow (and a visible "Next ->" button) advances ONE beat, with a short caption bar describing the beat:
   1. "Q: Is the forklift on cam-2 moving? (as of 07:55:05)" -> answer MOVING, plays ceiling_03 seg 001.
   2. "Newer footage arrives (07:57:42)" -> tile flashes, BEFORE/AFTER hero animates moving -> parked, plays eye_04 seg 002.
   3. "A retrieval-only agent, same captions" -> highlight the naive card still saying moving + its OUTDATED line.
   4. "The camera stops. Ask again at 08:00:47" -> scrubber jumps past end, red NO FOOTAGE banner, tiles grey.
   Auto mode (no key presses) = 6 s per beat. Use the times/segments that actually exist; the ones above are what the live build shows now.
2. EVAL IMAGE IS STALE: webapp/results_real.png is the old n=5 chart with zero bars for systems that did not run. Replace it with eval/out/results_real.png (n=6, "did not run" handled, outdated-state bar). The caption already says 6.
3. Status strip on first load says "evidence -5s old" (negative) and "footage ends 07:47" (that is the scrubber coverage, not the end of footage, 07:57:47). Clamp age at 0 and show the true footage end.
4. On first load the tiles say "unconfirmed" while the answer card says parked at the same as_of. Tiles must show the claim valid at as_of.
Commit, push, redeploy, curl-verify. Then STOP; no more changes after 2:45.
