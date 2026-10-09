#!/usr/bin/env python3
"""Register clips/*.mp4 on one shared timeline and ingest their sidecar claims into receipts.db.

    python3 viewer/ingest_clips.py --init          # write skeleton sidecars for clips lacking one
    python3 viewer/ingest_clips.py [--gap 30]      # -> viewer/receipts.db, prints rule results

The shared timeline is file order (clips/SOURCES.md): each clip starts where the previous one
ended, plus --gap idle seconds (so "footage ends before as_of" staleness can be shown).
Only sidecars provide claims; the rule engine (supersede.py) is untouched.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from claims import Store, fmt_t  # noqa: E402
from ingest_adapter import ingest_clip  # noqa: E402
from viewer.sidecar import (SidecarSource, clip_id_for, ffprobe_info, load_sidecar,  # noqa: E402
                            save_sidecar, sidecar_path, skeleton)

DEFAULT_CLIPS = os.path.join(ROOT, "clips")
DEFAULT_DB = os.path.join(ROOT, "viewer", "receipts.db")

# Mirrors clips/SOURCES.md. Clips not listed here are appended in name order.
CLIP_ORDER = ["bridge_01_road_clear", "bridge_02_truck_stuck", "bridge_03_road_clear_again",
              "garage_01_cluttered", "garage_02_cleared", "person_moving"]
SCENES = {"bridge_": "bridge", "garage_": "garage", "person_": "venue"}


def scene_for(clip_id):
    for prefix, scene in SCENES.items():
        if clip_id.startswith(prefix):
            return scene
    return clip_id


def discover_clips(clips_dir=DEFAULT_CLIPS):
    """Absolute mp4 paths in timeline order."""
    names = [f for f in os.listdir(clips_dir)
             if f.lower().endswith(".mp4") and not f.lower().endswith("_noaudio.mp4")]

    def key(f):
        cid = clip_id_for(f)
        return (CLIP_ORDER.index(cid) if cid in CLIP_ORDER else len(CLIP_ORDER), f)
    return [os.path.join(clips_dir, f) for f in sorted(names, key=key)]


def init_sidecars(clips_dir=DEFAULT_CLIPS, overwrite=False):
    written = []
    for p in discover_clips(clips_dir):
        sp = sidecar_path(p)
        if os.path.exists(sp) and not overwrite:
            continue
        meta = skeleton(os.path.relpath(p, ROOT), scene=scene_for(clip_id_for(p)))
        save_sidecar(sp, meta)
        written.append(sp)
    return written


def load_all(clips_dir=DEFAULT_CLIPS):
    """[(clip_path, meta-or-None)] in timeline order."""
    out = []
    for p in discover_clips(clips_dir):
        sp = sidecar_path(p)
        out.append((p, load_sidecar(sp) if os.path.exists(sp) else None))
    return out


def assign_timeline(entries, gap=0.0):
    """Cumulative file order. Sets meta['timeline'] and returns clip dicts for ingest_clip."""
    t = 0.0
    clips = []
    for order, (path, meta) in enumerate(entries):
        dur = meta["video"]["duration"] if meta else ffprobe_info(path)["duration"]
        t_start, t_end = t, t + dur
        if meta is not None:
            meta["timeline"] = {"order": order, "t_start": t_start, "t_end": t_end}
        clips.append({"clip_id": clip_id_for(path), "t_start": t_start, "t_end": t_end,
                      "path": os.path.relpath(path, ROOT).replace(os.sep, "/"),
                      "description": (meta or {}).get("description", "")})
        t = t_end + gap
    return clips


def build_store(db_path=DEFAULT_DB, clips_dir=DEFAULT_CLIPS, gap=0.0, write_back=True):
    """Fresh store with every clip registered and sidecar claims run through the rules.
    Returns (store, [{clip_id, rule_id, claim_id, superseded_ids, source}])."""
    entries = load_all(clips_dir)
    clips = assign_timeline(entries, gap)
    metas = {clip_id_for(p): m for p, m in entries if m is not None}
    if db_path != ":memory:" and os.path.exists(db_path):
        os.remove(db_path)
    store = Store(db_path)
    sources = (SidecarSource(metas, "segments"), SidecarSource(metas, "tracks"))
    results = []
    for clip in clips:
        for src in sources:
            for r in ingest_clip(store, clip, src):
                results.append(dict(r, clip_id=clip["clip_id"], source=src.source))
    if write_back:
        for p, m in entries:
            if m is not None:
                save_sidecar(sidecar_path(p), m)
    return store, results


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--clips", default=DEFAULT_CLIPS)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--gap", type=float, default=0.0, help="idle seconds between clips")
    p.add_argument("--init", action="store_true", help="write skeleton sidecars, then exit")
    p.add_argument("--overwrite", action="store_true", help="with --init: replace existing sidecars")
    p.add_argument("--no-write-back", action="store_true", help="do not store timeline in sidecars")
    args = p.parse_args(argv)

    if args.init:
        for sp in init_sidecars(args.clips, args.overwrite):
            print(f"wrote {os.path.relpath(sp, ROOT)}")
        return 0

    store, results = build_store(args.db, args.clips, args.gap, not args.no_write_back)
    counts = {}
    for r in results:
        counts[r["rule_id"]] = counts.get(r["rule_id"], 0) + 1
        row = store.db.execute("SELECT entity,attribute,value,location,status,t_start,t_end FROM claims WHERE id=?",
                               (r["claim_id"],)).fetchone()
        print(f"{r['clip_id']:28} {r['rule_id']:28} claim={r['claim_id']:<3} "
              f"{row['entity']}/{row['attribute']}@{row['location']} = {row['value']!r} "
              f"[{row['status']}] {fmt_t(row['t_start'])}-{fmt_t(row['t_end'])}"
              + (f"  superseded={r['superseded_ids']}" if r.get("superseded_ids") else ""))
    print("\n=== rule counts ===")
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")
    n = store.db.execute("SELECT COUNT(*) n FROM clips").fetchone()["n"]
    end = store.db.execute("SELECT MAX(t_end) m FROM clips").fetchone()["m"] or 0
    print(f"\n{n} clips on timeline 0-{end:.2f}s -> {os.path.relpath(args.db, ROOT) if args.db != ':memory:' else args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
