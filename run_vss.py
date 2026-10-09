#!/usr/bin/env python3
"""Ingest warehouse VSS segments into receipts.db in time order; print rule results.

Uses existing Cosmos captions (keyword mapping). When re-ingest captions land with
BLOCKED/CLEAR + zone names, the same parser prefers that vocabulary automatically.
"""
from __future__ import annotations

import argparse
import os
import sys

# Allow `python3 run_vss.py` from the receipts directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from answer import answer  # noqa: E402
from claims import Store, fmt_t  # noqa: E402
from ingest_adapter import ingest_clip  # noqa: E402
from vss_source import VssSource, fetch_warehouse_segments, segments_from_explore  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(description="Ingest Pack C warehouse segments into receipts.db")
    p.add_argument("--db", default="receipts.db")
    p.add_argument("--camera-id", default="sdg_warehouse_cam-2")
    p.add_argument("--location", default="warehouse3")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--explore-json", default="",
                   help="offline: path to explore JSON (skip network)")
    p.add_argument("--demo-key", default="forklift|state|sdg_warehouse_cam-2",
                   help="question key to answer after ingest")
    args = p.parse_args(argv)

    if args.explore_json:
        import json
        with open(args.explore_json) as f:
            payload = json.load(f)
        segments = segments_from_explore(payload, camera_id=args.camera_id, location=args.location)
        print(f"loaded {len(segments)} segments from {args.explore_json}")
    else:
        segments, _, _ = fetch_warehouse_segments(
            camera_id=args.camera_id, location=args.location, limit=args.limit)
        print(f"fetched {len(segments)} warehouse segments from VSS")

    if os.path.exists(args.db):
        os.remove(args.db)
    store = Store(args.db)
    source = VssSource(camera_id=args.camera_id)

    counts = {}
    for seg in segments:
        results = ingest_clip(store, seg, source, model_name="cosmos_reason_vss")
        for r in results:
            counts[r["rule_id"]] = counts.get(r["rule_id"], 0) + 1
            print(f"{fmt_t(seg['t_start'])}-{fmt_t(seg['t_end'])}  {r['rule_id']:32}  "
                  f"claim={r['claim_id']}  superseded={r.get('superseded_ids') or []}")
            # show the claim values briefly
            row = store.db.execute("SELECT entity,attribute,value,location,status FROM claims WHERE id=?",
                                   (r["claim_id"],)).fetchone()
            if row:
                print(f"    -> {row['entity']}/{row['attribute']}@{row['location']} = {row['value']!r} [{row['status']}]")

    print("\n=== rule counts ===")
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")

    supersedes = [a for a in store.audit() if a["rule_id"] == "SUPERSEDE_NEWER_CONTRADICTS"]
    print(f"\n=== SUPERSEDE_NEWER_CONTRADICTS in audit: {len(supersedes)} ===")
    for a in supersedes[:10]:
        print(f"  audit#{a['id']} claim={a['claim_id']} other={a['other_claim_id']} detail={a['detail']}")
    if not supersedes:
        print("  (none — no value flip on the same entity/attribute/location key)")

    # Demo answer at end of coverage
    cov = store.db.execute("SELECT MAX(t_end) m FROM clips").fetchone()["m"]
    if cov is not None:
        key = args.demo_key
        r = answer(store, key, cov, max_age=3600)
        print(f"\n=== answer({key!r}, as_of={fmt_t(cov)}) ===")
        print(f"  answer={r['answer']!r} status={r['status']} stale={r['stale']}")
        print(f"  clip={r['clip_id']} t={r['t_start']}-{r['t_end']}")
        if r["history"]:
            print(f"  history={r['history']}")
        if r["answer"] is None:
            for k2 in (f"center_aisle|worker_present|{args.camera_id}",
                       f"wall_area|worker_present|{args.camera_id}",
                       f"forklift|state|{args.camera_id}"):
                r2 = answer(store, k2, cov, max_age=3600)
                if r2["answer"] is not None:
                    print(f"\n=== answer({k2!r}) ===")
                    print(f"  answer={r2['answer']!r} status={r2['status']} clip={r2['clip_id']}")
                    print(f"  history={r2['history']}")
                    break

    print(f"\ndb written: {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
