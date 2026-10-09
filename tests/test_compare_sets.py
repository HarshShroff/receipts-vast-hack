"""eval/compare.py on multi-clip question sets (labels timeline, per-key outdated check). No network."""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from eval import compare as cmp  # noqa: E402
from viewer import serve as sv  # noqa: E402


def load(name):
    with open(os.path.join(ROOT, "eval", f"questions_{name}.json")) as f:
        return json.load(f)


class LabelStore(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def build(self, name):
        q = load(name)
        store, _ = cmp.build_store_from_labels(os.path.join(self.d, name + ".db"), q["clip_clock"], q["labels"])
        return q, store

    def test_bridge_clips_on_camera_clock(self):
        q, store = self.build("bridge")
        rows = store.db.execute("SELECT clip_id, t_start, t_end FROM clips ORDER BY t_start").fetchall()
        self.assertEqual([r["clip_id"] for r in rows], list(q["clip_clock"]))
        self.assertEqual(rows[0]["t_start"], 9 * 3600 + 6 * 60 + 5)
        self.assertAlmostEqual(rows[1]["t_start"] - rows[0]["t_end"], 16 - 9.009, places=2)  # real 7 s gap

    def test_receipts_and_gap_scoring(self):
        for name in ("bridge", "garage"):
            q, store = self.build(name)
            summary, rows = cmp.score_one(cmp.Receipts(), store, q["questions"])
            self.assertEqual(summary["exact"], summary["n"], name)
            self.assertEqual(summary["stale_claim"], 0, name)  # per-key check: no false "outdated"
            self.assertEqual(summary["after_footage_correct_stale"], summary["after_footage_n"], name)
            self.assertGreaterEqual(summary["after_footage_n"], 2, name)

    def test_outdated_check_is_per_key(self):
        q, store = self.build("bridge")
        as_of = 9 * 3600 + 7 * 60 + 28
        result = {"answer": "clear", "claim_id": None}
        self.assertFalse(cmp.answer_is_stale(store, result, as_of, ["underpass", "state", "bridge"]))
        old = {"answer": "under the bridge", "claim_id": None}
        self.assertTrue(cmp.answer_is_stale(store, old, as_of, ["truck", "position", "bridge"]))


class Sets(unittest.TestCase):
    def test_question_files_are_consistent(self):
        for name in ("bridge", "garage"):
            q = load(name)
            self.assertEqual(q["label_status"], "draft")
            clips = set(q["clip_clock"])
            for lab in q["labels"]:
                self.assertIn(lab["clip_id"], clips)
            for item in q["questions"]:
                if item["category"] in cmp.GAP_CATEGORIES:
                    self.assertTrue(item["expected_stale"], item["id"])
                if item["expected_interval"]:
                    self.assertIn(item["expected_clip_id"], clips, item["id"])
                    match = [l for l in q["labels"] if l["key"] == item["question_key"]
                             and l["value"] == item["expected_answer"] and l["clip_id"] == item["expected_clip_id"]]
                    self.assertTrue(match, f"{item['id']} has no matching label")

    def test_compare_api_lists_sets(self):
        names = [s["set"] for s in sv.compare_sets()]
        self.assertEqual(names[0], "real")
        self.assertIn("bridge", names)
        self.assertIn("garage", names)
        d = sv.api_compare("garage")
        self.assertEqual([c["clip_id"] for c in d["clips"]], ["garage_01_cluttered", "garage_02_cleared"])
        self.assertEqual(d["label_status"], "draft")
        self.assertIsNone(sv.api_compare("../etc"))


if __name__ == "__main__":
    unittest.main()
