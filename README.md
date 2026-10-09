# Receipts

A video agent that never answers without a clip, and never answers from a clip that has since been overtaken.

## The problem

Video agents answer from whatever footage they retrieved, and they answer with confidence. If a pallet was blocking the north exit at 10:05 and was moved at 10:40, an agent that retrieves the 10:05 clip tells you the exit is blocked. Nothing in the answer says the evidence is old, that newer footage contradicts it, or that the cameras stopped recording at 10:42. In a safety or operations setting that stale answer is worse than no answer.

## How it works

1. **Footage becomes claims.** Each clip is passed to a vision-language model (NVIDIA Cosmos Reason) which returns structured claims: entity, attribute, value, location, and the second range where it was seen. Optional YOLO tracks can add zone-occupancy claims.
2. **Claims carry provenance.** Every claim is stored with its source clip, timestamps and confidence in SQLite.
3. **Supersession.** The key is (entity, attribute, location). A later clip with a different value marks the old claim `superseded`. Nothing is deleted. A repeat of the same value corroborates and raises confidence. Every decision is written to an audit table with a rule ID.
4. **Answers cite and flag.** `answer()` only sees footage up to the question time. It returns the latest claim, its clip and timestamps, and a STALE flag when the last confirmation is older than `max_age` or the footage ends before the question time ("no footage after 10:42, answer may be stale"). No claim means no answer and no invented clip.

The core is deterministic Python 3 and sqlite3, no model calls. The model only sits at the ingest edge (`ingest_adapter.CosmosSource`).

## Run it

    python3 fixtures/demo_scenario.py        # synthetic: ask, ingest newer clip, ask again, see SUPERSEDED + new clip
    python3 eval/score.py                    # 25-question table, Receipts vs 3 baselines
    python3 -m unittest discover -s tests -v

## Evaluation, with caveats

Metrics: exact match; Acc@GQA (exact answer AND correct clip AND interval IoU >= 0.5); citation precision; and a stale-claim rate (answers built on a claim already superseded at question time).

| set | questions | Receipts exact | Receipts stale-claim rate | best baseline exact | best baseline stale-claim rate |
|---|---|---|---|---|---|
| synthetic | 25 | 25/25 | 0/23 | 15/25 (RAG-all) | 9/24 |
| real clips | [__] | [__] | [__] | [__] | [__] |

Read the synthetic row carefully. The labels were written against the same scenario the rule engine runs on, so 25/25 shows the plumbing works, not that the approach generalises. The informative part is how the baselines fail: retrieval-style memory returns the old state when both states are in the index. Real numbers need real labeled clips, and the real-clip row is the only one that should be used to judge the idea. Counts only, small n, no significance claims.

## Limitations

Exact-name entity matching (no entity resolution). Keyword-overlap retrieval in the baselines. Timestamps come from the model and are only roughly accurate. The staleness window (`max_age`) is a per-scene setting. No UI.

## Stack

NVIDIA Cosmos Reason / VSS for clip understanding, [YOLO if used], Python 3 + SQLite, built with Cursor. [W&B / CoreWeave only if actually used.]

License: none yet (all rights reserved by default).


