import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from baselines_vss import LlmCaptionBaseline, VssAgentBaseline, parse_llm_json  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_parses_fenced_json(self):
        answer, cited = parse_llm_json('```json\n{"answer": "left of the center pillar", "cited_time_s": 0.5}\n```')
        self.assertEqual(answer, "left of the center pillar")
        self.assertEqual(cited, 0.5)

    def test_null_answer(self):
        answer, cited = parse_llm_json('{"answer": null, "cited_time_s": null}')
        self.assertIsNone(answer)
        self.assertIsNone(cited)


class VssAgentTests(unittest.TestCase):
    def test_records_that_the_api_is_not_time_restricted(self):
        def ask(question, as_of):
            self.assertIn("hoodie", question)
            self.assertGreater(as_of, 0)
            return {
                "answer": "near a forklift",
                "t_start": 5.0,
                "t_end": 10.0,
                "cited_time_s": 7.5,
                "clip_id": "seg.mp4",
                "time_restricted": False,
            }

        r = VssAgentBaseline(type("C", (), {"ask": staticmethod(ask)})()).answer(
            None, "Where in the frame is the man in the gray hoodie?", ["gray_hoodie", "position", "center_pillar"], "12:03:03")
        self.assertEqual(r["answer"], "near a forklift")
        self.assertEqual(r["cited_time_s"], 7.5)
        self.assertFalse(r["time_restricted"])
        self.assertIn("not time-restricted", r["note"])

    def test_no_evidence_still_flags_the_missing_cutoff(self):
        client = type("C", (), {"ask": staticmethod(lambda q, a: {"answer": None})})()
        r = VssAgentBaseline(client).answer(None, "Where?", ["e", "a", "l"], 1)
        self.assertIsNone(r["answer"])
        self.assertFalse(r["time_restricted"])


class LlmCaptionTests(unittest.TestCase):
    CAPTIONS = [
        {"text": "man stands left of the pillar", "t_start": 0.0, "t_end": 1.0},
        {"text": "man stands right of the pillar", "t_start": 2.5, "t_end": 3.2},
    ]

    def test_drops_captions_after_as_of_and_records_model(self):
        seen = {}

        def llm(prompt):
            seen["prompt"] = prompt
            return '{"answer": "left of the center pillar", "cited_time_s": 0.5}'

        r = LlmCaptionBaseline(self.CAPTIONS, llm, model_id="fake-model").answer(
            None, "Where in the frame is the man in the gray hoodie?", None, 1.0)
        self.assertEqual(r["answer"], "left of the center pillar")
        self.assertEqual(r["cited_time_s"], 0.5)
        self.assertEqual(r["model_id"], "fake-model")
        self.assertTrue(r["time_restricted"])
        self.assertIn("left of the pillar", seen["prompt"])
        self.assertNotIn("right of the pillar", seen["prompt"])

    def test_keeps_a_caption_that_has_started_even_if_it_ends_after_as_of(self):
        seen = {}

        def llm(prompt):
            seen["prompt"] = prompt
            return '{"answer": "right of the center pillar", "cited_time_s": 2.5}'

        r = LlmCaptionBaseline(self.CAPTIONS, llm, model_id="fake-model").answer(None, "Where?", None, 3.0)
        self.assertEqual(r["answer"], "right of the center pillar")
        self.assertIn("right of the pillar", seen["prompt"])

    def test_empty_caption_window_does_not_call_the_model(self):
        def llm(prompt):
            raise AssertionError("llm should not be called")

        r = LlmCaptionBaseline(self.CAPTIONS, llm, model_id="fake-model").answer(None, "Where?", None, -1)
        self.assertIsNone(r["answer"])
        self.assertIn("no captions", r["note"])

    def test_bad_json_is_an_empty_answer(self):
        r = LlmCaptionBaseline(self.CAPTIONS, lambda p: "I think he is on the left.", model_id="m").answer(
            None, "Where?", None, 3.2)
        self.assertIsNone(r["answer"])
        self.assertIn("unparsed", r["note"])


if __name__ == "__main__":
    unittest.main()
