"""agentqa wrapper: parses evidence, caches, never fakes an answer on failure."""
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agentqa  # noqa: E402

PAYLOAD = {
    "answer": "Yes, a forklift is moving toward the loading area.",
    "evidence": [{"source": "s3://b/20261001_074655_x.ceiling_02.rgb_chunk_0000_segment_001_of_002.mp4",
                  "segment_start_sec": 4.0, "segment_end_sec": 9.0}],
    "llm_synthesis": {"model": "test-model"},
}


class AgentQaTest(unittest.TestCase):
    def setUp(self):
        agentqa._CACHE.clear()

    def test_parses_answer_and_evidence(self):
        with mock.patch.object(agentqa, "login", return_value=("http://x", "t")), \
             mock.patch.object(agentqa, "_post_json", return_value=PAYLOAD) as post:
            out = agentqa.agentqa_answer("Is the forklift moving?")
            agentqa.agentqa_answer("Is the forklift moving?")
        self.assertTrue(out["ok"])
        self.assertIn("moving", out["answer"])
        self.assertEqual(out["evidence_start_s"], 4.0)
        self.assertFalse(out["time_restricted"])
        self.assertEqual(out["model_id"], "test-model")
        self.assertEqual(post.call_count, 1)  # cached

    def test_http_error_reports_no_answer(self):
        err = urllib.error.HTTPError("http://x", 500, "boom", None, None)
        with mock.patch.object(agentqa, "login", return_value=("http://x", "t")), \
             mock.patch.object(agentqa, "_post_json", side_effect=err):
            out = agentqa.agentqa_answer("q")
        self.assertFalse(out["ok"])
        self.assertNotIn("answer", out)


if __name__ == "__main__":
    unittest.main()
