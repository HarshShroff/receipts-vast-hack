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
    caption_to_claims,
    pin_zone,
    segments_from_explore,
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
                    # Vague stock caption — must NOT produce a blocked claim
                    "reasoning_content": (
                        "A blue forklift labeled ATLAS moves slowly forward in a warehouse aisle, "
                        "approaching a person. The scene is illuminated clearly."
                    ),
                },
                {
                    "segment_number": 2,
                    "segment_start_sec": 5.0,
                    "segment_end_sec": 10.0,
                    "source": "s3://bucket/seg_a2.mp4",
                    "reasoning_content": (
                        "The forklift remains stationary. The person walks away across an empty "
                        "warehouse floor with no other workers or activity."
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
        self.assertEqual(pin_zone("near a brick wall"), "wall_area")

    def test_pin_zone_from_filename_angle(self):
        self.assertEqual(pin_zone("generic text", "foo.ceiling_02.rgb_chunk_0000.mp4"), "left_aisle")
        self.assertEqual(pin_zone("generic text", "foo.eye_04.rgb.mp4"), "wall_area")

    def test_explicit_blocked_clear(self):
        v, c = caption_blocked_value("center aisle is BLOCKED by forklift")
        self.assertTrue(v.startswith("yes"))
        self.assertGreaterEqual(c, 0.85)
        v, c = caption_blocked_value("center aisle is CLEAR")
        self.assertEqual(v, "no")
        v, c = caption_blocked_value("the path is unobstructed")
        self.assertEqual(v, "no")
        v, c = caption_blocked_value("a pallet is blocking the aisle")
        self.assertTrue(v.startswith("yes"))

    def test_vague_captions_skipped(self):
        self.assertIsNone(caption_blocked_value("A forklift is stationary in the warehouse")[0])
        self.assertIsNone(caption_blocked_value(
            "The forklift reverses, moving away from the camera")[0])
        # "clearly" must not count as CLEAR
        self.assertIsNone(caption_blocked_value(
            "The brand name is clearly displayed on the forklift")[0])


class ExploreFlatten(unittest.TestCase):
    def test_segments_sorted_absolute_times(self):
        segs = segments_from_explore(FAKE_EXPLORE)
        self.assertEqual(len(segs), 4)
        times = [s["t_start"] for s in segs]
        self.assertEqual(times, sorted(times))
        self.assertEqual(segs[0]["t_start"], 7 * 3600 + 49 * 60 + 55)

    def test_ignores_other_cameras(self):
        payload = {"chunks": FAKE_EXPLORE["chunks"] + [{
            "original_video": "s3://x/20261001_080000_y.mp4",
            "camera_id": "other_cam",
            "location": "warehouse3",
            "timeline": [{"segment_start_sec": 0, "segment_end_sec": 5,
                          "source": "s3://x/s.mp4", "reasoning_content": "forklift"}],
        }]}
        segs = segments_from_explore(payload, camera_id="sdg_warehouse_cam-2")
        self.assertTrue(all(s["camera_id"] == "sdg_warehouse_cam-2" for s in segs))


class IngestIntegration(unittest.TestCase):
    def test_ingest_produces_supersede_on_structured_flip(self):
        segs = segments_from_explore(FAKE_EXPLORE)
        store = Store()
        src = VssSource()
        rules = []
        for seg in segs:
            rules.extend(r["rule_id"] for r in ingest_clip(store, seg, src))
        self.assertIn("FIRST_CLAIM", rules)
        self.assertIn("SUPERSEDE_NEWER_CONTRADICTS", rules)
        # Vague ceiling_01 captions produced nothing; only wall_area flip remains
        n_claims = store.db.execute("SELECT COUNT(*) n FROM claims").fetchone()["n"]
        self.assertEqual(n_claims, 2)
        active = store.db.execute(
            "SELECT value,status FROM claims WHERE entity='wall_area' AND attribute='blocked' "
            "AND location='sdg_warehouse_cam-2' AND status!='superseded'"
        ).fetchone()
        self.assertIsNotNone(active)
        self.assertEqual(active["value"], "no")

    def test_vague_caption_emits_no_claims(self):
        claims = caption_to_claims(
            "forklift parked in the center aisle",
            camera_id="sdg_warehouse_cam-2",
            source_uri="x.ceiling_01.mp4",
            t_start=100.0,
            t_end=105.0,
        )
        self.assertEqual(claims, [])

    def test_explicit_caption_shape(self):
        claims = caption_to_claims(
            "left aisle is blocked by a pallet",
            camera_id="sdg_warehouse_cam-2",
            source_uri="x.ceiling_02.mp4",
            t_start=100.0,
            t_end=105.0,
        )
        self.assertEqual(len(claims), 1)
        c = claims[0]
        self.assertEqual(c["attribute"], "blocked")
        self.assertEqual(c["location"], "sdg_warehouse_cam-2")
        self.assertEqual(c["entity"], "left_aisle")
        self.assertTrue(c["value"].startswith("yes"))


if __name__ == "__main__":
    unittest.main()
