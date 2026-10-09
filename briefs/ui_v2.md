# UI v2 brief (from an independent judge/designer review of the live app, 1:20 PM)
Do this after the credibility fixes are pushed. Only webapp/ (+ run_vss.py if needed). Hard stop 3:00 PM ET: commit, push, redeploy, freeze. Do items in order; skip what doesn't fit, never leave a half-done item visible.

## A. Must-fix before judges see it (S each)
1. Player status says "loading segment..." after the clip ended (0:05/0:05). Show "ended" / "playing" correctly.
2. Toast says "cam-2, seg 002" while the player shows ceiling_01 seg 002. One camera id everywhere (tiles, toast, player, answer).
3. Tiles: all tiles in one grid beside the video (2x3), no orphans wrapping under the video.
4. Colors mean something: amber/red = hazard (worker present near forklift, forklift moving, aisle blocked), green = safe, grey = unconfirmed/stale. Add a tiny legend.
5. Replace native video controls with a minimal custom bar that highlights the cited time range.
6. Subtitle: drop "Pack C" and "footage first, then the claim" jargon.
7. Answers must answer the question: "Is it moving?" -> "No - parked (as of 07:47:00)". Lead with yes/no.

## B. Make the core idea unmissable
8. ANSWER-CHANGED HERO (M), above the fold beside/under the video: two columns BEFORE (old claim struck through, its clip + time + play) -> AFTER (active claim, clip + time + play), one sentence: "Replaced because newer footage from the same camera contradicts it." Clicking either side plays that clip range.
9. NAIVE AGENT vs RECEIPTS (M), same question side by side: left grey "Typical video agent" = answer from the best-matching caption regardless of time (compute honestly: highest keyword/similarity caption over ALL segments, no time awareness), stated confidently with no timestamp; right = Receipts answer with time, citation, and "supersedes X". Label the naive side as our simulation of retrieval-only behaviour, not a named product.
10. LOUD SUPERSEDE (S): affected tile flashes, shows old value struck through next to new value; answer card re-renders with a highlight. Toast optional.
11. VISIBLE STALE (S): scrubber can go past end of coverage; red banner "STALE - last evidence Xm Ys old, footage ended hh:mm:ss". Replace "0s ago" with real evidence age relative to as_of.
12. ANSWER CARD COPY (S): cite as "ceiling_04 - seg 001 - 07:46:55-07:47:00 [play]", full filename on hover; audit trail as plain-English steps ("Newer clip contradicts older claim -> older claim superseded").

## C. Demo flow
13. RUN DEMO button + key "d" (M): deterministic script, ~60 s: ask "Is the forklift moving?" -> answer with clip -> replay to the contradicting segment -> supersede animates, answer flips, hero updates -> naive agent still shows the old answer -> scrub past end of footage -> STALE banner. Same result every run, no typing.

## D. If time remains
14. Timeline lanes (M): one row per entity/zone, minute ticks, grey dots = claims, red connector = supersede; click to seek.
15. Pipeline strip under header (S): "60 segments -> Cosmos Reason captions (VAST DataEngine) -> N claims -> M supersedes -> cited answers" with live counts.

Headline: "Receipts - video answers that know when they're out of date". Sub: "Cosmos Reason turns every camera segment into a claim. When newer footage contradicts an old claim, the answer changes and shows you both clips. When footage runs out, it says so."
Sponsor chips: only tools that actually ran (VAST DataEngine, NVIDIA Cosmos Reason, CoreWeave GPUs serving the models, Weights & Biases only if the eval logged to it, Cursor). Never fabricate data; if a hero example isn't in the real data, pick one that is.
