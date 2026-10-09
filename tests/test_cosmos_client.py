import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cosmos_client as cc  # noqa: E402


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def ok(text):
    return FakeResp(json.dumps({"choices": [{"message": {"content": text}}]}).encode())


def err429():
    return urllib.error.HTTPError("u", 429, "Too Many", {"Retry-After": "1"}, io.BytesIO(b"slow"))


class CosmosClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        self.tmp.write(b"fake")
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _fake_transcode(self, src, out, **kw):
        with open(out, "wb") as f:
            f.write(b"sampled")

    def test_missing_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "NVIDIA_API_KEY"):
                cc.call_model("q", self.tmp.name)

    def test_success_payload_and_key_not_in_body(self):
        seen = {}

        def fake_open(req, timeout=None):
            seen["req"] = req
            return ok("answer")
        with mock.patch.dict(os.environ, {"NVIDIA_API_KEY": "nvapi-SECRET"}), \
                mock.patch.object(cc, "transcode", self._fake_transcode), \
                mock.patch("urllib.request.urlopen", fake_open):
            self.assertEqual(cc.call_model("what?", self.tmp.name), "answer")
        req = seen["req"]
        self.assertTrue(req.full_url.endswith("/chat/completions"))
        self.assertEqual(req.get_header("Authorization"), "Bearer nvapi-SECRET")
        body = json.loads(req.data)
        self.assertNotIn("SECRET", req.data.decode())
        part = body["messages"][1]["content"][0]
        self.assertEqual(part["type"], "video_url")
        self.assertTrue(part["video_url"]["url"].startswith("data:video/mp4;base64,"))
        self.assertEqual(body["messages"][1]["content"][1]["text"], "what?")

    def test_retry_on_429_then_success(self):
        calls = [err429(), err429(), ok("fine")]
        slept = []

        def fake_open(req, timeout=None):
            r = calls.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch("urllib.request.urlopen", fake_open):
            data = cc._post({"x": 1}, "k", sleep=slept.append)
        self.assertEqual(data["choices"][0]["message"]["content"], "fine")
        self.assertEqual(slept, [1.0, 1.0])

    def test_429_exhausted_raises(self):
        def fake_open(req, timeout=None):
            raise err429()
        with mock.patch("urllib.request.urlopen", fake_open):
            with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
                cc._post({}, "k", sleep=lambda s: None)

    def test_non_429_error_not_retried(self):
        n = []

        def fake_open(req, timeout=None):
            n.append(1)
            raise urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b"fps too high"))
        with mock.patch("urllib.request.urlopen", fake_open):
            with self.assertRaisesRegex(RuntimeError, "HTTP 400: fps too high"):
                cc._post({}, "k", sleep=lambda s: None)
        self.assertEqual(len(n), 1)

    def test_missing_file(self):
        with mock.patch.dict(os.environ, {"NVIDIA_API_KEY": "k"}):
            with self.assertRaises(FileNotFoundError):
                cc.call_model("q", "/nonexistent.mp4")


if __name__ == "__main__":
    unittest.main()
