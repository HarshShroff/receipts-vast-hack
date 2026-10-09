"""Tests for vss_source — fake VSS explore payload, no network."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claims import Store  # noqa: E402
from ingest_adapter import ingest_clip  # noqa: E402
from vss_source import (  # noqa: E402
    VssSource,
    caption_blocked_value,
    caption_forklift_state,
    caption_to_claims,
    caption_worker_present,
    normalize_detections,
    pin_zone,
    segments_from_explore,
    zone_named_in_caption,
)


FAKE_EXPLORE = {
    "chunks": [
        {
            "original_video": "s3://team-5-vss-chunks/team-5/20261001_074955_x.ceiling_01.rgb_chunk_0000.mp4",
            "filename": "20261001_074955_x.ceiling_01.rgb_chunk_0000.mp4",
            "camera_id": "sdg_warehouse_cam-2",
            "location": "warehouse3",
            "capture_type": "warehouse",
            "upload_timestamp": "2026-10-01T07:49:55.000000",
            "timeline": [
                {
                    "segment_number": 1,
                    "segment_start_sec": 0.0,
                    "segment_end_sec": 5.0,
                    "source": "s3://bucket/seg_a1.mp4",
                    "reasoning_content": (
                        "A blue forklift labeled ATLAS moves slowly forward in a warehouse aisle, "
                        "approaching a person. A person wearing a white uniform stands nearby."
                    ),
                },
                {
                    "segment_number": 2,
                    "segment_start_sec": 5.0,
                    "segment_end_sec": 10.0,
                    "source": "s3://bucket/seg_a2.mp4",
                    "reasoning_content": (
                        "The forklift remains stationary near the wall. "
                        "No visible workers remain in the warehouse."
                    ),
                },
            ],
        },
        {
            "original_video": "s3://team-5-vss-chunks/team-5/20261001_075321_x.eye_04.rgb_chunk_0000.mp4",
            "filename": "20261001_075321_x.eye_04.rgb_chunk_0000.mp4",
            "camera_id": "sdg_warehouse_cam-2",
            "location": "warehouse3",
            "timeline": [
                {
                    "segment_number": 1,
                    "segment_start_sec": 0.0,
                    "segment_end_sec": 5.0,
                    "source": "s3://bucket/seg_b1.mp4",
                    "reasoning_content": (
                        "Zone: wall area. Status: BLOCKED by forklift. The forklift is moving."
                    ),
                },
                {
                    "segment_number": 2,
                    "segment_start_sec": 5.0,
                    "segment_end_sec": 10.0,
                    "source": "s3://bucket/seg_b2.mp4",
                    "reasoning_content": (
                        "wall area is CLEAR. No forklift. The forklift has left the frame."
                    ),
                },
            ],
        },
    ]
}


class ParseHelpers(unittest.TestCase):
    def test_pin_zone_from_caption_vocab(self):
        self.assertEqual(pin_zone("visible in the left aisle"), "left_aisle")
        self.assertEqual(pin_zone("loading area near dock"), "loading_area")

    def test_explicit_blocked_clear(self):
        v, _ = caption_blocked_value("center aisle is BLOCKED by forklift")
        self.assertTrue(v.startswith("yes"))
        self.assertEqual(caption_blocked_value("center aisle is CLEAR")[0], "no")
        self.assertIsNone(caption_blocked_value(
            "The brand name is clearly displayed on the forklift")[0])

    def test_worker_present_explicit(self):
        self.assertEqual(caption_worker_present("no visible workers in the aisle")[0], "no")
        # "no other workers" means someone is already in frame.
        self.assertEqual(caption_worker_present("no other workers are visible")[0], "yes")
        self.assertEqual(caption_worker_present(
            "A person wearing a white shirt walks away")[0], "yes")
        self.assertIsNone(caption_worker_present("shelves stocked with boxes")[0])

    def test_worker_present_police_shirt_ceiling_04(self):
        """Exact warehouse caption: person in frame + 'no other workers' must be yes."""
        cap = (
            "A person wearing a white shirt with the word POLICE on the back and a white "
            "cap stands in a warehouse with dark blue tiled flooring. The individual is "
            "facing away from the camera, positioned near a blue forklift that is "
            "partially visible on the right. No other workers or people are visible in "
            "the frame."
        )
        self.assertEqual(caption_worker_present(cap)[0], "yes")

    def test_worker_present_scoped_to_camera_unless_aisle_named(self):
        cam = "sdg_warehouse_cam-2"
        # Filename angle must NOT invent an aisle for worker_present.
        claims = caption_to_claims(
            "A person wearing a white shirt walks across the warehouse floor.",
            camera_id=cam, source_uri="x.ceiling_02.rgb_chunk_0000.mp4",
            t_start=100.0, t_end=105.0,
        )
        workers = [c for c in claims if c["attribute"] == "worker_present"]
        self.assertEqual(len(workers), 1)
        self.assertEqual(workers[0]["entity"], cam)
        self.assertEqual(workers[0]["location"], cam)
        self.assertIsNone(zone_named_in_caption(
            "A person wearing a white shirt walks across the warehouse floor."))
        # Explicit aisle in caption → zone entity.
        named = caption_to_claims(
            "A worker walks down the left aisle.",
            camera_id=cam, source_uri="x.ceiling_02.rgb_chunk_0000.mp4",
            t_start=100.0, t_end=105.0,
        )
        w2 = [c for c in named if c["attribute"] == "worker_present"][0]
        self.assertEqual(w2["entity"], "left_aisle")
        self.assertEqual(w2["location"], cam)

    def test_forklift_state_explicit(self):
        self.assertEqual(caption_forklift_state(
            "The forklift remains stationary near the wall")[0], "parked")
        self.assertEqual(caption_forklift_state(
            "A blue forklift moves slowly forward")[0], "moving")
        # person moving near forklift, no forklift motion word → skip
        self.assertIsNone(caption_forklift_state(
            "A person walks away from a blue forklift")[0])


class ExploreFlatten(unittest.TestCase):
    def test_segments_sorted_absolute_times(self):
        segs = segments_from_explore(FAKE_EXPLORE)
        self.assertEqual(len(segs), 4)
        self.assertEqual(segs[0]["t_start"], 7 * 3600 + 49 * 60 + 55)


class IngestIntegration(unittest.TestCase):
    def test_stock_captions_supersede_forklift_and_worker(self):
        segs = segments_from_explore({"chunks": [FAKE_EXPLORE["chunks"][0]]})
        store = Store()
        src = VssSource()
        rules = []
        for seg in segs:
            rules.extend(r["rule_id"] for r in ingest_clip(store, seg, src))
        self.assertIn("SUPERSEDE_NEWER_CONTRADICTS", rules)
        fork = store.db.execute(
            "SELECT value FROM claims WHERE entity='forklift' AND attribute='state' "
            "AND status!='superseded'").fetchone()
        self.assertEqual(fork["value"], "parked")
        worker = store.db.execute(
            "SELECT entity, value FROM claims WHERE attribute='worker_present' "
            "AND status!='superseded'").fetchone()
        self.assertEqual(worker["value"], "no")
        # Camera-scoped (no aisle named in those stock captions).
        self.assertEqual(worker["entity"], "sdg_warehouse_cam-2")

    def test_blocked_path_still_supersedes(self):
        segs = segments_from_explore({"chunks": [FAKE_EXPLORE["chunks"][1]]})
        store = Store()
        src = VssSource()
        for seg in segs:
            ingest_clip(store, seg, src)
        active = store.db.execute(
            "SELECT value FROM claims WHERE entity='wall_area' AND attribute='blocked' "
            "AND status!='superseded'").fetchone()
        self.assertEqual(active["value"], "no")

    def test_explicit_blocked_shape(self):
        claims = caption_to_claims(
            "left aisle is blocked by a pallet",
            camera_id="sdg_warehouse_cam-2",
            source_uri="x.ceiling_02.mp4",
            t_start=100.0, t_end=105.0,
        )
        blocked = [c for c in claims if c["attribute"] == "blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["entity"], "left_aisle")


class NormalizeDetections(unittest.TestCase):
    def test_pixel_bbox_uses_frame_shape(self):
        out = normalize_detections({
            "fps": 10,
            "frames": [{
                "time_sec": 1.5,
                "shape": [100, 200],
                "detections": [
                    {"label": "forklift", "confidence": 0.8, "bbox": [20, 10, 60, 50]},
                    {"label": "bad", "confidence": 0.1, "bbox": [1, 2]},
                ],
            }],
        })
        self.assertEqual(out["frame_count"], 1)
        box = out["frames"][0]["boxes"][0]
        self.assertEqual(box["label"], "forklift")
        self.assertEqual(box["pixel"], False)
        self.assertAlmostEqual(box["x"], 0.1)
        self.assertAlmostEqual(box["y"], 0.1)
        self.assertAlmostEqual(box["w"], 0.2)
        self.assertAlmostEqual(box["h"], 0.4)
        self.assertEqual(len(out["frames"][0]["boxes"]), 1)

    def test_normalized_xyxy_stays_unit_interval(self):
        out = normalize_detections({
            "frames": [{"time_sec": 0, "detections": [
                {"label": "person", "confidence": 0.5, "bbox": [0.1, 0.2, 0.4, 0.6]},
            ]}],
        })
        box = out["frames"][0]["boxes"][0]
        self.assertEqual(box["pixel"], False)
        self.assertAlmostEqual(box["w"], 0.3)
        self.assertAlmostEqual(box["h"], 0.4)

    def test_pixels_without_shape_are_flagged(self):
        out = normalize_detections({
            "frames": [{"time_sec": 0.2, "detections": [
                {"label": "person", "confidence": 0.9, "bbox": [10, 20, 30, 80]},
            ]}],
        })
        box = out["frames"][0]["boxes"][0]
        self.assertTrue(box["pixel"])
        self.assertEqual((box["x"], box["y"], box["w"], box["h"]), (10, 20, 20, 60))


if __name__ == "__main__":
    unittest.main()
