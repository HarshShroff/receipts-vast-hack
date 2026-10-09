"""Retrieval-only baseline ignores time; Receipts does not."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claims import Store  # noqa: E402
from naive import naive_answer, rank_clips  # noqa: E402

MOVING = ("A light blue forklift moves slowly forward through the warehouse, "
          "the forklift is driving toward the loading dock.")
PARKED = "The forklift remains stationary near the wall."


class NaiveTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.add_clip("seg_b_old", 100, 105, MOVING, path="s3://x/seg_b_old.mp4")
        self.store.add_clip("seg_a_new", 200, 205, PARKED, path="s3://x/seg_a_new.mp4")

    def test_ranks_by_keyword_not_time(self):
        ranked = rank_clips(self.store, "Is the forklift moving?")
        self.assertEqual(ranked[0][1]["clip_id"], "seg_b_old")

    def test_answers_from_best_match_even_if_older(self):
        out = naive_answer(self.store, "Is the forklift moving?", "forklift", "state",
                           camera_id="cam")
        self.assertEqual(out["answer"], "moving")
        self.assertEqual(out["clip_id"], "seg_b_old")

    def test_no_match_returns_none(self):
        out = naive_answer(self.store, "Is the forklift moving?", "pallet", "position")
        self.assertIsNone(out["answer"])


if __name__ == "__main__":
    unittest.main()
