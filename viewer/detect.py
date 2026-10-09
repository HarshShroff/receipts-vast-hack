#!/usr/bin/env python3
"""YOLO tracking -> `tracks` in each clip's sidecar. Needs ultralytics (torch); run it in an env
that has it, never in the stdlib-only project env:

    ~/.pyenv/versions/3.11.9/envs/hack51/bin/python viewer/detect.py clips/*.mp4

Writes boxes as [t, x, y, w, h, conf] (top-left + size, normalized 0..1) and replaces only the
`tracks` and `detector` keys, so hand-authored segments and zones survive a re-run. Zone
assignment is deliberately not done here: viewer/sidecar.py derives it at ingest/serve time, so
zones can be edited without re-running the detector. Nothing in serve/ingest/tests imports this.
"""
import argparse
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from viewer.sidecar import load_sidecar, save_sidecar, sidecar_path, skeleton  # noqa: E402

DEFAULT_CLASSES = "person,car,truck,bus,bicycle,motorcycle"


def run(path, model, args):
    import cv2
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.release()
    names = model.names
    want = {k for k, v in names.items() if v in set(args.classes.split(","))} if args.classes else None
    boxes, labels = defaultdict(list), defaultdict(Counter)
    frames = model.track(path, persist=True, stream=True, conf=args.conf, iou=args.iou, imgsz=args.imgsz,
                         tracker=args.tracker, classes=sorted(want) if want else None,
                         vid_stride=args.vid_stride, verbose=False)
    for i, r in enumerate(frames):
        if r.boxes is None or r.boxes.id is None:
            continue
        t = round(i * args.vid_stride / fps, 4)
        for xyxyn, tid, cls, conf in zip(r.boxes.xyxyn.tolist(), r.boxes.id.tolist(),
                                         r.boxes.cls.tolist(), r.boxes.conf.tolist()):
            x1, y1, x2, y2 = (min(max(v, 0.0), 1.0) for v in xyxyn)
            boxes[int(tid)].append([t, round(x1, 4), round(y1, 4), round(x2 - x1, 4), round(y2 - y1, 4),
                                    round(float(conf), 3)])
            labels[int(tid)][names[int(cls)]] += 1
    tracks = []
    for tid in sorted(boxes):
        if len(boxes[tid]) < args.min_boxes:
            continue
        tracks.append({"track_id": tid, "label": labels[tid].most_common(1)[0][0],
                       "source": os.path.basename(args.model), "boxes": sorted(boxes[tid])})
    return tracks


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("paths", nargs="+", help="clip files (clips/*.mp4)")
    p.add_argument("--model", default="yolo11n.pt")
    p.add_argument("--conf", type=float, default=0.25)
    p.add_argument("--iou", type=float, default=0.5)
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--tracker", default="bytetrack.yaml")
    p.add_argument("--classes", default=DEFAULT_CLASSES, help="comma list; '' = all COCO classes")
    p.add_argument("--min-boxes", type=int, default=5, help="drop tracks seen in fewer frames")
    p.add_argument("--vid-stride", type=int, default=1)
    args = p.parse_args(argv)

    from ultralytics import YOLO
    model = YOLO(args.model)
    for path in args.paths:
        sp = sidecar_path(path)
        meta = load_sidecar(sp) if os.path.exists(sp) else skeleton(os.path.relpath(path, ROOT))
        meta["tracks"] = run(path, model, args)
        meta["detector"] = {"model": os.path.basename(args.model), "conf": args.conf, "iou": args.iou,
                            "imgsz": args.imgsz, "tracker": args.tracker, "classes": args.classes,
                            "vid_stride": args.vid_stride,
                            "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        save_sidecar(sp, meta)
        summary = ", ".join(f"{t['label']}#{t['track_id']}({len(t['boxes'])})" for t in meta["tracks"]) or "no tracks"
        print(f"{os.path.relpath(sp, ROOT)}: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
