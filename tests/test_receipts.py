import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from answer import answer  # noqa: E402
from baselines import LastN, NoMemory, RagAll  # noqa: E402
from claims import Store, fmt_t, parse_t  # noqa: E402
from eval.score import iou, score_system, load_questions, Receipts  # noqa: E402
from fixtures import demo_scenario as demo  # noqa: E402
from ingest_adapter import parse_claims_json, tracks_to_claims  # noqa: E402
from supersede import ingest_claim, sweep_stale  # noqa: E402

KEY = ("exit", "state", "north_exit")


def fresh():
    s = Store()
    s.add_clip("A", "10:10", "10:20"); s.add_clip("B", "10:40", "10:50")
    return s


class Supersession(unittest.TestCase):
    def test_newer_contradiction_retires_old_and_keeps_it(self):
        s = fresh()
        a = ingest_claim(s, "exit", "state", "blocked", "A", "10:12", "10:15", "north_exit")
        b = ingest_claim(s, "exit", "state", "clear", "B", "10:42", "10:45", "north_exit")
        self.assertEqual(b["rule_id"], "SUPERSEDE_NEWER_CONTRADICTS")
        old = s.db.execute("SELECT * FROM claims WHERE id=?", (a["claim_id"],)).fetchone()
        self.assertEqual((old["status"], old["superseded_by"]), ("superseded", b["claim_id"]))
        self.assertEqual(old["rule_fired"], "SUPERSEDE_NEWER_CONTRADICTS")
        self.assertEqual(answer(s, KEY, "10:50")["answer"], "clear")
        self.assertEqual(s.db.execute("SELECT COUNT(*) n FROM claims WHERE status='active'").fetchone()["n"], 1)

    def test_corroboration_keeps_one_active_and_bumps_confidence(self):
        s = fresh()
        a = ingest_claim(s, "door", "state", "closed", "A", "10:13", "10:14", "dock", confidence=0.8)
        b = ingest_claim(s, "door", "state", "Closed ", "B", "10:43", "10:44", "dock", confidence=0.8)
        self.assertEqual(b["rule_id"], "CORROBORATE_SAME_VALUE")
        self.assertEqual(b["claim_id"], a["claim_id"])
        row = s.db.execute("SELECT * FROM claims").fetchall()
        self.assertEqual(len(row), 1)
        self.assertAlmostEqual(row[0]["confidence"], 0.96)
        self.assertEqual(s.db.execute("SELECT COUNT(*) n FROM evidence").fetchone()["n"], 2)
        r = answer(s, ("door", "state", "dock"), "10:50")
        self.assertEqual((r["clip_id"], fmt_t(r["t_end"])), ("B", "10:44"))  # cites latest confirmation

    def test_late_arrival_does_not_override_newer(self):
        s = fresh()
        ingest_claim(s, "exit", "state", "clear", "B", "10:42", "10:45", "north_exit")
        late = ingest_claim(s, "exit", "state", "blocked", "A", "10:12", "10:15", "north_exit")
        self.assertEqual(late["rule_id"], "OLDER_OR_TIED_CONTRADICTION")
        self.assertEqual(answer(s, KEY, "10:50")["answer"], "clear")

    def test_every_decision_is_audited(self):
        s = fresh()
        ingest_claim(s, "exit", "state", "blocked", "A", "10:12", "10:15", "north_exit")
        ingest_claim(s, "exit", "state", "blocked", "B", "10:42", "10:45", "north_exit")
        ingest_claim(s, "exit", "state", "clear", "B", "10:46", "10:48", "north_exit")
        self.assertEqual([a["rule_id"] for a in s.audit()],
                         ["FIRST_CLAIM", "CORROBORATE_SAME_VALUE", "SUPERSEDE_NEWER_CONTRADICTS"])

    def test_time_travel_as_of_before_supersession(self):
        s = fresh()
        ingest_claim(s, "exit", "state", "blocked", "A", "10:12", "10:15", "north_exit")
        ingest_claim(s, "exit", "state", "clear", "B", "10:42", "10:45", "north_exit")
        r = answer(s, KEY, "10:20")
        self.assertEqual((r["answer"], r["clip_id"], r["history"]), ("blocked", "A", []))

    def test_sweep_marks_stale_and_corroboration_revives(self):
        s = fresh()
        ingest_claim(s, "door", "state", "closed", "A", "10:13", "10:14", "dock")
        self.assertEqual(len(sweep_stale(s, parse_t("10:50"), 900)), 1)
        self.assertEqual(s.db.execute("SELECT status FROM claims").fetchone()["status"], "stale")
        ingest_claim(s, "door", "state", "closed", "B", "10:43", "10:44", "dock")
        self.assertEqual(s.db.execute("SELECT status FROM claims").fetchone()["status"], "active")


class Flags(unittest.TestCase):
    def setUp(self):
        self.s = fresh()
        ingest_claim(self.s, "exit", "state", "blocked", "A", "10:12", "10:15", "north_exit")

    def test_fresh_answer_not_stale(self):
        r = answer(self.s, KEY, "10:20")
        self.assertFalse(r["stale"]); self.assertIsNone(r["data_gap_note"])

    def test_age_based_stale(self):
        r = answer(self.s, KEY, "10:50", max_age=900)
        self.assertTrue(r["stale"]); self.assertEqual(r["status"], "stale")
        self.assertIn("answer may be stale", r["data_gap_note"])

    def test_max_age_is_configurable(self):
        ingest_claim(self.s, "exit", "state", "blocked", "B", "10:44", "10:45", "north_exit")  # keep coverage
        self.assertTrue(answer(self.s, KEY, "10:50", max_age=200)["stale"])
        self.assertFalse(answer(self.s, KEY, "10:50", max_age=3600)["stale"])

    def test_coverage_gap_note(self):
        r = answer(self.s, KEY, "10:30", max_age=10 ** 6)
        self.assertTrue(r["stale"])
        self.assertEqual(r["data_gap_note"], "no footage after 10:20, answer may be stale")

    def test_no_claim_means_no_answer_and_no_clip(self):
        r = answer(self.s, ("exit", "state", "east_exit"), "10:20")
        self.assertIsNone(r["answer"]); self.assertIsNone(r["clip_id"]); self.assertEqual(r["status"], "no_evidence")
        self.assertEqual(answer(Store(), KEY, "10:05")["data_gap_note"], "no footage yet")

    def test_data_gaps_view(self):
        rows = self.s.db.execute("SELECT * FROM data_gaps").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["unobserved_seconds"], parse_t("10:50") - parse_t("10:15"))  # footage runs to 10:50


class TimeParsing(unittest.TestCase):
    def test_bare_numeric_string_is_seconds_not_hours(self):
        # Slider/query as_of values are seconds; "28667" must not become 28667*3600.
        self.assertEqual(parse_t("28667"), 28667.0)
        self.assertEqual(parse_t(28667), 28667.0)
        self.assertEqual(parse_t("07:57:47"), 28667.0)

    def test_as_of_at_footage_end_not_stale(self):
        s = Store()
        s.add_clip("A", 100.0, 200.0)
        ingest_claim(s, "forklift", "state", "parked", "A", 150.0, 200.0, "cam")
        end = 200.0
        r = answer(s, ("forklift", "state", "cam"), end, max_age=3600)
        self.assertFalse(r["stale"])
        self.assertEqual(r["status"], "active")
        # Same when as_of arrives as a bare seconds string (HTTP query).
        r2 = answer(s, ("forklift", "state", "cam"), "200", max_age=3600)
        self.assertFalse(r2["stale"])
        self.assertEqual(r2["answer"], "parked")


class Scoring(unittest.TestCase):
    def test_iou(self):
        self.assertAlmostEqual(iou((0, 10), (5, 15)), 5 / 15)
        self.assertEqual(iou((0, 10), (0, 10)), 1.0)
        self.assertEqual(iou((0, 1), (2, 3)), 0.0)
        self.assertEqual(iou(None, (0, 1)), 0.0)

    def test_threshold_is_half(self):
        self.assertGreaterEqual(iou((0, 10), (0, 5)), 0.5)
        self.assertLess(iou((0, 10), (0, 4.9)), 0.5)

    def test_questions_load_and_receipts_beats_rag_on_stale(self):
        qs = load_questions(os.path.join(os.path.dirname(__file__), "..", "eval", "questions.json"))
        self.assertEqual(len(qs), 25)
        r = score_system(Receipts(), qs)
        rag = score_system(RagAll(tiebreak="oldest"), qs)
        self.assertEqual(r["stale_used"], 0)
        self.assertGreater(rag["stale_used"], 0)


class Baselines(unittest.TestCase):
    def setUp(self):
        self.s = Store()
        demo.load(self.s)
        self.q = "Is the north exit blocked?"

    def test_rag_all_ignores_supersession_and_cites_old_clip(self):
        r = RagAll().answer(self.s, self.q, KEY, "10:50")
        self.assertEqual((r["answer"], r["clip_id"]), ("blocked", "clip_A"))
        self.assertTrue(self.s.is_superseded_at(r["claim_id"], parse_t("10:50")))

    def test_no_memory_sees_only_latest_clip(self):
        self.assertEqual(NoMemory().answer(self.s, self.q, KEY, "11:10")["answer"], None)
        self.assertEqual(NoMemory().answer(self.s, self.q, KEY, "10:50")["answer"], "clear")

    def test_window_drops_old_clips(self):
        r = LastN(1).answer(self.s, self.q, KEY, "11:10")
        self.assertIsNone(r["answer"])
        self.assertEqual(LastN(2).answer(self.s, self.q, KEY, "11:10")["clip_id"], "clip_B")


class Adapter(unittest.TestCase):
    def test_parse_fenced_json(self):
        out = parse_claims_json('here:\n```json\n[{"entity":"a","attribute":"b","value":"c","t_start":1,"t_end":2}]\n```')
        self.assertEqual(out[0]["entity"], "a")
        with self.assertRaises(ValueError):
            parse_claims_json('[{"entity":"a"}]')

    def test_tracks_to_claims(self):
        c = tracks_to_claims([{"label": "pallet", "track_id": 7, "zone": "north_exit", "t_start": 1, "t_end": 2}])
        self.assertEqual((c[0]["entity"], c[0]["value"]), ("pallet_7", "north_exit"))


if __name__ == "__main__":
    unittest.main()
