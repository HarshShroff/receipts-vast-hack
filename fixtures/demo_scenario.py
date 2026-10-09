"""SYNTHETIC scenario. No real footage, no model output. Hand-written claims that
mimic what Cosmos/YOLO would emit, used for the demo, the tests and the eval.

Story: a pallet blocks the north exit in clip A; clip B shows it moved; clip C later shows the
forklift and loading door changing while the north exit is not re-observed.
Times are absolute HH:MM on one timeline.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from answer import answer  # noqa: E402
from baselines import ALL  # noqa: E402
from claims import Store, fmt_t, parse_t  # noqa: E402
from ingest_adapter import CannedSource, ingest_clip  # noqa: E402
from supersede import ingest_claim  # noqa: E402

LABEL = "SYNTHETIC"
CLIPS = [
    {"clip_id": "clip_A", "t_start": "10:10", "t_end": "10:20", "description": "SYNTHETIC: pallet at north exit"},
    {"clip_id": "clip_B", "t_start": "10:40", "t_end": "10:50", "description": "SYNTHETIC: pallet moved"},
    {"clip_id": "clip_C", "t_start": "11:00", "t_end": "11:10", "description": "SYNTHETIC: forklift and door"},
]


def _c(entity, attribute, value, location, t0, t1, conf=0.85):
    return dict(entity=entity, attribute=attribute, value=value, location=location,
                t_start=t0, t_end=t1, confidence=conf)


CLAIMS = {
    "clip_A": [
        _c("exit", "state", "blocked", "north_exit", "10:12", "10:15", 0.9),
        _c("pallet_7", "position", "at_north_exit", "warehouse", "10:12", "10:15"),
        _c("forklift_2", "position", "bay_2", "warehouse", "10:12", "10:14"),   # distractor
        _c("door_3", "state", "closed", "loading_dock", "10:13", "10:14"),      # distractor
        _c("exit", "state", "clear", "south_exit", "10:14", "10:16", 0.8),      # never revisited
    ],
    "clip_B": [
        _c("exit", "state", "clear", "north_exit", "10:42", "10:45", 0.9),
        _c("pallet_7", "position", "bay_4", "warehouse", "10:42", "10:45"),
        _c("forklift_2", "position", "bay_2", "warehouse", "10:43", "10:44"),   # corroborates A
        _c("door_3", "state", "closed", "loading_dock", "10:43", "10:44"),      # corroborates A
    ],
    "clip_C": [
        _c("forklift_2", "position", "bay_3", "warehouse", "11:05", "11:07"),
        _c("door_3", "state", "open", "loading_dock", "11:06", "11:08"),
    ],
}
NORTH = ("exit", "state", "north_exit")
SOURCE = CannedSource(CLAIMS, relative=False, source="SYNTHETIC")


def load(store, up_to=None):
    """Ingest clips in time order; up_to (time) skips clips starting after it."""
    for clip in CLIPS:
        if up_to is None or parse_t(clip["t_start"]) <= parse_t(up_to):
            ingest_clip(store, clip, SOURCE)


def build_store(as_of):
    """Store holding only footage observed by as_of (claims are cut at as_of)."""
    s, as_of = Store(), parse_t(as_of)
    for clip in CLIPS:
        if parse_t(clip["t_start"]) > as_of:
            continue
        s.add_clip(clip["clip_id"], clip["t_start"], clip["t_end"], clip["description"])
        for c in CLAIMS[clip["clip_id"]]:
            if parse_t(c["t_end"]) <= as_of:
                ingest_claim(s, c["entity"], c["attribute"], c["value"], clip["clip_id"], c["t_start"],
                             c["t_end"], c["location"], c["confidence"], LABEL)
    return s


def show(title, r):
    where = f"{r['clip_id']} {fmt_t(r['t_start'])}-{fmt_t(r['t_end'])}" if r["clip_id"] else "no clip"
    flag = f"  STALE ({r.get('stale_reason')})" if r.get("stale") else ""
    print(f"  {title:34s} -> {r['answer']!s:14s} [{where}]{flag}")


def ask_all(store, question, as_of):
    print(f"Q ({as_of}): {question}")
    show("Receipts", answer(store, NORTH, as_of))
    for b in ALL:
        show(b.name, b.answer(store, question, NORTH, as_of))


def main():
    print(f"=== Receipts demo, all data {LABEL} ===\n")
    store, q = Store(), "Is the north exit blocked?"
    ingest_clip(store, CLIPS[0], SOURCE)
    print("-- after clip A --"); ask_all(store, q, "10:20")
    ingest_clip(store, CLIPS[1], SOURCE)
    print("\n-- clip B ingested --"); ask_all(store, q, "10:50")
    r = answer(store, NORTH, "10:50")
    for h in r["history"]:
        print(f"  SUPERSEDED claim {h['claim_id']}: {h['value']!r} from {h['clip_id']} "
              f"{fmt_t(h['t_start'])}-{fmt_t(h['t_end'])}")
    print(f"  new answer: {r['answer']!r} from {r['clip_id']} {fmt_t(r['t_start'])}-{fmt_t(r['t_end'])}")
    ingest_clip(store, CLIPS[2], SOURCE)
    print("\n-- clip C ingested (north exit not re-observed) --"); ask_all(store, q, "11:10")
    print("  data note:", answer(store, NORTH, "11:10")["data_gap_note"])
    print("\n-- audit trail --")
    for a in store.audit():
        if a["rule_id"] != "FIRST_CLAIM":
            print(f"  {a['rule_id']:28s} claim {a['claim_id']} <- {a['other_claim_id']}  {a['detail']}")


if __name__ == "__main__":
    main()
