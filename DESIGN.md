# Receipts (model-independent core)

A video agent that never answers without a clip. Footage becomes **claims** tied to a source clip and
timestamps. When later footage contradicts an earlier claim for the same (entity, attribute, location), the old
claim is marked **SUPERSEDED** (kept, never deleted). Answers return the latest verified claim, its clip and time,
and flag **STALE** answers (old evidence, or footage that ends before the question time).

Everything here is deterministic Python 3 + sqlite3. No model calls, no network, no accounts. Cosmos Reason and
YOLO plug in on hackathon day through `ingest_adapter.py`. All fixtures and eval labels are **SYNTHETIC**.

## Run

    python3 fixtures/demo_scenario.py        # ask after clip A, ingest B, ask again, SUPERSEDED + new clip, stale after C
    python3 eval/score.py [questions.json]   # 25-question table, Receipts vs 4 baseline configs (--json for raw counts)
    python3 -m unittest discover -s tests -v # 20 tests

## Files

| file | what |
|---|---|
| `claims.py` | SQLite store: `clips`, `claims`, `evidence`, `audit` tables, `data_gaps` view |
| `supersede.py` | rule engine: `ingest_claim()`, `sweep_stale()` |
| `answer.py` | `answer(store, key, as_of, max_age=900)` |
| `baselines.py` | B1 no-memory, B2 RAG-all, B3 last-N-clips, same interface |
| `ingest_adapter.py` | `ClaimSource` ABC, `CannedSource` stub, Cosmos prompt, JSON parser, YOLO track hook |
| `eval/` | `eval_schema.json`, `questions.json` (25 synthetic), `score.py` |
| `fixtures/demo_scenario.py` | synthetic clips A/B/C and claims, demo narrative |

## Schema

`claims(id, entity, attribute, value, location, clip_id, t_start, t_end, observed_at, confidence,
status[active|superseded|stale], superseded_by, rule_fired, source)`. Supersession key = (entity, attribute, location).
`evidence(claim_id, clip_id, t_start, t_end, observed_at)` holds every sighting, including corroborations, so an
answer cites the most recent clip that confirmed the claim. `audit(rule_id, claim_id, other_claim_id, detail)` logs
every decision. Times are float seconds on one shared timeline.

## Rule IDs

| rule | effect |
|---|---|
| `FIRST_CLAIM` | no earlier claim for the key: stored active |
| `SUPERSEDE_NEWER_CONTRADICTS` | later `observed_at`, different value: old claim -> superseded, `superseded_by` set |
| `CORROBORATE_SAME_VALUE` | same value again: still one active claim, confidence noisy-OR (cap 0.99), new clip linked as evidence |
| `OLDER_OR_TIED_CONTRADICTION` | different value but not newer (late arrival, tie): stored as superseded |
| `STALE_MAX_AGE` | `sweep_stale()` only: active claim not re-observed within `max_age` |

## Answers

`answer()` returns `{answer, claim_id, clip_id, t_start, t_end, status, stale, stale_reason, data_gap_note, history}`.
It only sees footage observed at or before `as_of`, and is time-travel correct (a claim superseded after `as_of`
still answers as active). STALE when the latest confirmation is older than `max_age` (default 15 min) or footage
coverage ends before `as_of` ("no footage after 10:42, answer may be stale"). No claim -> `answer=None`, no clip.

## Eval (counts, not significance)

Metrics: exact match; Acc@GQA (exact AND cited clip matches AND interval IoU >= 0.5, NExT-GQA style);
citation precision; **stale-claim rate** (answers built on a claim already superseded at `as_of`, our own metric).
Current synthetic result (25 questions): Receipts 25/25 exact, 0/23 stale-claim; RAG-all with oldest-wins ties
15/25 exact, 9/24 stale-claim. Read that with care: labels were written against the same scenario the rule engine
runs on, so Receipts scoring perfectly shows the plumbing works, not that it generalises. The informative part is
the baselines' failure modes. B2 tie-breaking is arbitrary, so both variants are reported. B1 answers nothing at
11:10 because the latest clip never shows the exit (honest no-memory behaviour; it does not answer with the old state).
Real numbers need your own footage and labels.

## Deliberately NOT done

No model calls (Cosmos, any VLM), no YOLO, no UI, no VAST DataEngine integration, no embeddings (baseline
retrieval is keyword overlap), no entity resolution beyond exact-name match, no W&B logging.

## Hackathon-day checklist

1. Film or pick 3-4 clips with a visible state change; hand-label `eval/questions.json` (same schema, real clip IDs/intervals).
2. Wrap the Cosmos Reason endpoint as `call_model(prompt, video_path) -> str`; use `CosmosSource` (prompt in `COSMOS_PROMPT`). Pin entity/zone names.
3. Optional: YOLO + ByteTrack -> zone polygons -> `tracks_to_claims()`.
4. Loop clips: `ingest_clip(store, clip, source)` with a file-backed `Store("receipts.db")`; clip `t_start` = absolute stream time.
5. Replace `build_store` in `eval/score.py` with a store built from your ingestion (cut at `as_of`), run `score.py`.
6. Swap B2/B3 keyword scoring for the same embedder and VLM prompt the agent uses, so the comparison is fair.
7. Tune `max_age` per scene; log eval runs to W&B if wanted (sponsor tie-in).
8. Rehearse `fixtures/demo_scenario.py` flow on the real clips.
