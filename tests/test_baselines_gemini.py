"""Gemini baseline without network: fake VideoQA, fake cutter, cache behaviour, key loading."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gemini_client as gc  # noqa: E402
from baselines_gemini import (UNKNOWN, GeminiFullContext, answer_options, cut_until,  # noqa: E402
                              fmt_clock, response_schema)

START = 12 * 3600 + 3 * 60
OPTIONS = ["left of the center pillar", "right of the center pillar", "behind the center pillar", UNKNOWN]
CLIP = os.path.join(ROOT, "clips", "person_moving.mp4")


class FakeQA(gc.VideoQA):
    model_id = "fake-flash"

    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def ask(self, video_path, video_id, prompt, schema, repeat=0):
        self.calls.append({"path": video_path, "video_id": video_id, "prompt": prompt, "schema": schema, "repeat": repeat})
        return {"data": self.replies[repeat % len(self.replies)], "model_version": "fake-flash-001", "cached": False}


def fake_cut(src, seconds):
    return f"/cuts/{seconds:.1f}.mp4", min(seconds, 8.633)


def reply(answer, t0=1.0, t1=2.0, stale=False, reason="seen"):
    return {"answer": answer, "t_start": t0, "t_end": t1, "stale": stale, "reason": reason}


class Prompt(unittest.TestCase):
    def test_prompt_states_clock_rule_and_no_hints(self):
        qa = FakeQA([reply("right of the center pillar")])
        g = GeminiFullContext(CLIP, START, OPTIONS, qa=qa, repeats=1, cut=fake_cut)
        g.answer(None, "Where is he?", None, "12:03:15")
        p = qa.calls[0]["prompt"]
        self.assertIn("starts at 12:03:00", p)
        self.assertIn("covers 12:03:00 to 12:03:08.633", p)
        self.assertIn("asked at 12:03:15", p)
        self.assertIn("900 seconds", p)
        self.assertIn('"unknown"', p)
        for hint in ("after the footage", "trap", "camera stopped", "expected"):
            self.assertNotIn(hint, p.lower())
        self.assertEqual(qa.calls[0]["schema"]["properties"]["answer"]["enum"], OPTIONS)

    def test_same_template_for_every_question(self):
        qa = FakeQA([reply("left of the center pillar")])
        g = GeminiFullContext(CLIP, START, OPTIONS, qa=qa, repeats=1, cut=fake_cut)
        g.answer(None, "Where is he?", None, "12:03:00.5")
        g.answer(None, "Where is he?", None, "12:03:05.5")
        a, b = (c["prompt"] for c in qa.calls)
        strip = lambda s: [ln for ln in s.splitlines() if "covers" not in ln and "asked at" not in ln]  # noqa: E731
        self.assertEqual(strip(a), strip(b))

    def test_fmt_clock(self):
        self.assertEqual(fmt_clock(START), "12:03:00")
        self.assertEqual(fmt_clock(START + 0.5), "12:03:00.5")
        self.assertEqual(fmt_clock(START + 8.633333), "12:03:08.633")


class Results(unittest.TestCase):
    def test_clock_conversion_and_unknown(self):
        g = GeminiFullContext(CLIP, START, OPTIONS, qa=FakeQA([reply("right of the center pillar", 2.5, 3.0)]),
                              repeats=1, cut=fake_cut)
        r = g.answer(None, "q", None, "12:03:03")
        self.assertEqual((r["t_start"], r["t_end"]), (START + 2.5, START + 3.0))
        self.assertEqual((r["answer"], r["stale"], r["clip_id"], r["claim_id"]),
                         ("right of the center pillar", False, "person_moving", None))
        g = GeminiFullContext(CLIP, START, OPTIONS, qa=FakeQA([reply(UNKNOWN, None, None)]), repeats=1, cut=fake_cut)
        r = g.answer(None, "q", None, "12:03:03")
        self.assertIsNone(r["answer"])
        self.assertIsNone(r["t_start"])

    def test_cut_at_as_of(self):
        qa = FakeQA([reply("left of the center pillar")])
        g = GeminiFullContext(CLIP, START, OPTIONS, qa=qa, repeats=1, cut=fake_cut)
        g.answer(None, "q", None, "12:03:05.5")
        g.answer(None, "q", None, "12:03:15")
        self.assertEqual([c["path"] for c in qa.calls], ["/cuts/5.5.mp4", "/cuts/15.0.mp4"])
        self.assertTrue(qa.calls[0]["video_id"].endswith(":5.500:libx264 crf23 720p noaudio"))

    def test_majority_and_single_repeat(self):
        qa = FakeQA([reply("left of the center pillar"), reply("right of the center pillar", stale=True),
                     reply("left of the center pillar")])
        g = GeminiFullContext(CLIP, START, OPTIONS, qa=qa, repeats=3, cut=fake_cut)
        r = g.answer(None, "q", None, "12:03:01")
        self.assertEqual((r["answer"], r["stale"], r["agreement"]), ("left of the center pillar", False, "2/3"))
        self.assertEqual([x["answer"] for x in r["repeats"]],
                         ["left of the center pillar", "right of the center pillar", "left of the center pillar"])
        self.assertEqual([c["repeat"] for c in qa.calls], [0, 1, 2])
        self.assertTrue(g.answer(None, "q", None, "12:03:01", repeat=1)["stale"])

    def test_answer_options_from_labels(self):
        qs = [{"expected_answer": "a"}, {"expected_answer": "b"}, {"expected_answer": "a"}, {"expected_answer": None}]
        self.assertEqual(answer_options(qs), ["a", "b", UNKNOWN])
        self.assertEqual(response_schema(["a", UNKNOWN])["required"], ["answer", "t_start", "t_end", "stale", "reason"])


class Cutter(unittest.TestCase):
    def test_reencodes_and_caches(self):
        d = tempfile.mkdtemp()
        calls = []

        def run(cmd, check):
            calls.append(cmd)
            open(cmd[-1], "wb").close()
        try:
            with mock.patch("baselines_gemini.probe_duration", side_effect=[8.633, 5.5, 8.633, 5.5]):
                p, cov = cut_until("clips/person_moving.mp4", 5.5, d, run=run)
                p2, _ = cut_until("clips/person_moving.mp4", 5.5, d, run=run)
            self.assertEqual((os.path.basename(p), cov, p2), ("person_moving__to_5500ms.mp4", 5.5, p))
            self.assertEqual(len(calls), 1)  # second call hits the cut cache
            cmd = calls[0]
            self.assertEqual(cmd[cmd.index("-t") + 1], "5.500")
            self.assertIn("-an", cmd)
            self.assertIn("libx264", cmd)
        finally:
            shutil.rmtree(d)

    def test_beyond_end_is_whole_clip(self):
        d = tempfile.mkdtemp()
        try:
            run = lambda cmd, check: open(cmd[-1], "wb").close()  # noqa: E731
            with mock.patch("baselines_gemini.probe_duration", side_effect=[8.633, 8.6]):
                p, cov = cut_until("clips/person_moving.mp4", 15.0, d, run=run)
            self.assertEqual((os.path.basename(p), cov), ("person_moving__to_8633ms.mp4", 8.6))
        finally:
            shutil.rmtree(d)


class Client(unittest.TestCase):
    def test_key_never_from_google_env(self):
        env = {"GOOGLE_CLOUD_PROJECT": "p", "GOOGLE_API_KEY": "g", "GOOGLE_APPLICATION_CREDENTIALS": "/x"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(gc, "SECRETS", "/nonexistent"):
            with self.assertRaises(gc.GeminiError):
                gc.load_key()
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": " k1 "}, clear=True):
            self.assertEqual(gc.load_key(), "k1")
        d = tempfile.mkdtemp()
        try:
            f = os.path.join(d, "secrets.env")
            with open(f, "w") as fh:
                fh.write("OTHER=1\nGEMINI_API_KEY=k2\n")
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(gc, "SECRETS", f):
                self.assertEqual(gc.load_key(), "k2")
        finally:
            shutil.rmtree(d)

    def test_resolve_model_prefers_newest_stable_flash(self):
        models = [{"name": f"models/{n}", "supportedGenerationMethods": ["generateContent"]} for n in
                  ("gemini-2.5-flash", "gemini-3.8-flash", "gemini-4-flash-preview", "gemini-3.9-flash-lite",
                   "gemini-flash-latest", "gemini-3.8-pro")]
        with mock.patch.object(gc, "list_models", return_value=models), mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(gc.resolve_model(), "gemini-3.8-flash")
            self.assertEqual(gc.resolve_model("gemini-2.5-flash"), "gemini-2.5-flash")
            with self.assertRaises(gc.GeminiError):
                gc.resolve_model("gemini-9-flash")

    def test_cache_hit_avoids_call_and_offline_miss_raises(self):
        d = tempfile.mkdtemp()
        try:
            qa = gc.GeminiVideoQA(model="m", cache_dir=d)
            text = json.dumps(reply("left of the center pillar"))
            with mock.patch.object(gc, "generate", return_value=(text, "m-001", {})) as gen, \
                    mock.patch.object(gc, "video_part", return_value={}):
                a = qa.ask("v.mp4", "vid:1", "p", {"s": 1}, repeat=0)
                b = qa.ask("v.mp4", "vid:1", "p", {"s": 1}, repeat=0)
                c = qa.ask("v.mp4", "vid:1", "p", {"s": 1}, repeat=1)
            self.assertEqual(gen.call_count, 2)  # repeat 1 is its own cache slot
            self.assertEqual((a["cached"], b["cached"], c["cached"]), (False, True, False))
            self.assertEqual(b["data"]["answer"], "left of the center pillar")
            with open(os.path.join(d, os.listdir(d)[0])) as fh:
                entry = json.load(fh)
            self.assertNotIn("key", json.dumps(entry["request"]).lower().replace("model", ""))
            off = gc.GeminiVideoQA(model="m", cache_dir=d, offline=True)
            self.assertTrue(off.ask("v.mp4", "vid:1", "p", {"s": 1})["cached"])
            with self.assertRaises(gc.GeminiError):
                off.ask("v.mp4", "vid:2", "p", {"s": 1})
        finally:
            shutil.rmtree(d)


if __name__ == "__main__":
    unittest.main()
