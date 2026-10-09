"""Viewer: sidecar format, zones, SidecarSource ingest, timeline, Range serving. Stdlib only;
never imports viewer.detect (ultralytics)."""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from answer import answer  # noqa: E402
from claims import Store, parse_t  # noqa: E402
from ingest_adapter import ingest_clip  # noqa: E402
from viewer import ingest_clips as ic  # noqa: E402
from viewer import serve as sv  # noqa: E402
from viewer import sidecar as sc  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "viewer")
BRIDGE = ["bridge_01_road_clear", "bridge_02_truck_stuck", "bridge_03_road_clear_again"]


def make_clips_dir(names, mp4_bytes=b""):
    """Temp clips dir with (fake) mp4s and the fixture sidecars; no ffprobe needed."""
    d = tempfile.mkdtemp(prefix="viewer_clips_")
    for n in names:
        with open(os.path.join(d, n + ".mp4"), "wb") as f:
            f.write(mp4_bytes)
        shutil.copy(os.path.join(FIX, n + ".meta.json"), d)
    return d


def fixture(name):
    return sc.load_sidecar(os.path.join(FIX, name + ".meta.json"))


class SidecarFormat(unittest.TestCase):
    def test_mmss_not_parse_t(self):
        self.assertEqual(sc.mmss_to_seconds("0:01"), 1.0)
        self.assertEqual(sc.mmss_to_seconds("00:01"), 1.0)
        self.assertEqual(sc.mmss_to_seconds("01:30.5"), 90.5)
        self.assertEqual(sc.mmss_to_seconds("00:01:02"), 62.0)
        self.assertEqual(sc.mmss_to_seconds(3), 3.0)
        self.assertEqual(parse_t("00:01"), 60.0)    # the gotcha the converter exists for: HH:MM, not MM:SS
        self.assertEqual(parse_t("20"), 20.0)       # ...and a bare number is already seconds
        for bad in ("", "1:2:3:4", "a:b", True):
            with self.assertRaises(ValueError):
                sc.mmss_to_seconds(bad)

    def test_validate_rejects(self):
        base = fixture("person_moving")
        cases = [
            ("string time", lambda m: m["segments"][0].update(t_start="00:00")),
            ("reversed span", lambda m: m["segments"][0].update(t_start=5.0, t_end=1.0)),
            ("short box", lambda m: m["tracks"][0]["boxes"].append([1.0, 0.1, 0.1, 0.1, 0.1])),
            ("box out of range", lambda m: m["tracks"][0]["boxes"].append([1.0, 1.2, 0.1, 0.1, 0.1, 0.5])),
            ("claim without value", lambda m: m["segments"][0]["claims"][0].pop("value")),
            ("two-point zone", lambda m: m["zones"].update(bad=[[0, 0], [1, 1]])),
            ("bad version", lambda m: m.update(version=2)),
        ]
        for name, mutate in cases:
            m = json.loads(json.dumps(base))
            mutate(m)
            with self.assertRaises(ValueError, msg=name):
                sc.validate(m)
        sc.validate(base)

    def test_gemini_to_segments(self):
        rows = [{"clip_name": "yuri_thumbs_up", "timestamp_start": "00:00", "timestamp_end": "00:01",
                 "bounding_box_estimation": "Center-left of the frame.", "action": "Thumbs up."},
                {"timestamp_start": "00:01", "timestamp_end": "00:04", "action": "Hides."}]
        segs = sc.gemini_to_segments(rows, entity="person")
        self.assertEqual([s["id"] for s in segs], ["yuri_thumbs_up", "g1"])
        self.assertEqual((segs[0]["t_start"], segs[0]["t_end"]), (0.0, 1.0))
        self.assertEqual((segs[1]["t_start"], segs[1]["t_end"]), (1.0, 4.0))
        self.assertEqual(segs[0]["notes"], "Center-left of the frame.")
        self.assertEqual(segs[0]["entity"], "person")
        self.assertEqual(segs[1]["claims"], [])

    def test_skeleton_roundtrip(self):
        d = tempfile.mkdtemp()
        try:
            video = {"fps": 25.0, "width": 966, "height": 720, "duration": 12.0}
            m = sc.skeleton("clips/garage_01_cluttered.mp4", scene="garage", video=video)
            self.assertEqual(m["clip_id"], "garage_01_cluttered")
            p = os.path.join(d, "x.meta.json")
            m["zones"] = {"floor": [[0, 0.5], [1, 0.5], [1, 1], [0, 1]]}
            sc.save_sidecar(p, m)
            with open(p) as f:
                text = f.read()
            self.assertIn("[0, 0.5]", text)  # numeric arrays stay on one line
            self.assertEqual(sc.load_sidecar(p), m)
            self.assertEqual(sc.sidecar_path("clips/a.mp4"), "clips/a.meta.json")
        finally:
            shutil.rmtree(d)


class Zones(unittest.TestCase):
    SQ = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]

    def test_point_in_polygon(self):
        self.assertTrue(sc.point_in_polygon(0.5, 0.5, self.SQ))
        self.assertFalse(sc.point_in_polygon(0.1, 0.5, self.SQ))
        self.assertFalse(sc.point_in_polygon(0.5, 0.9, self.SQ))

    def test_zone_of_box_uses_centre(self):
        zones = {"sq": self.SQ}
        self.assertEqual(sc.zone_of_box([0, 0.4, 0.4, 0.2, 0.2, 1], zones), "sq")
        self.assertIsNone(sc.zone_of_box([0, 0.0, 0.0, 0.2, 0.2, 1], zones))  # centre (0.1, 0.1)

    def test_track_zone_intervals(self):
        m = fixture("person_moving")
        ivs = sc.track_zone_intervals(m["tracks"][0], m["zones"])
        self.assertEqual([(i["zone"], i["t_start"], i["t_end"]) for i in ivs],
                         [("left", 0.0, 1.0), ("left", 2.0, 2.5), ("right", 3.0, 4.0)])  # 1.5 s blip dropped
        self.assertEqual(ivs[0]["label"], "person")
        self.assertEqual(ivs[0]["track_id"], 1)
        self.assertAlmostEqual(ivs[0]["conf"], 0.9, places=3)
        self.assertEqual(sc.track_zone_intervals({"label": "x", "track_id": 2, "boxes": []}, {}), [])
        self.assertEqual(sc.track_zone_intervals(
            {"label": "x", "track_id": 2, "zone": "sq", "t_start": 1.0, "t_end": 2.0}, {})[0]["zone"], "sq")


class SidecarSourceIngest(unittest.TestCase):
    def test_tracks_kind(self):
        m = fixture("person_moving")
        store = Store(":memory:")
        clip = {"clip_id": "person_moving", "t_start": 100.0, "t_end": 108.63, "path": "clips/person_moving.mp4"}
        res = ingest_clip(store, clip, sc.SidecarSource({"person_moving": m}, "tracks"))
        self.assertEqual([r["rule_id"] for r in res],
                         ["FIRST_CLAIM", "CORROBORATE_SAME_VALUE", "SUPERSEDE_NEWER_CONTRADICTS"])
        rows = store.db.execute("SELECT entity, attribute, value, location, status, t_start, source "
                                "FROM claims ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in rows],
                         [("person_1", "zone", "left", "venue", "superseded", 100.0, "yolo_tracks"),
                          ("person_1", "zone", "right", "venue", "active", 103.0, "yolo_tracks")])

    def test_segments_kind(self):
        m = fixture("person_moving")
        store = Store(":memory:")
        clip = {"clip_id": "person_moving", "t_start": 0.0, "t_end": 8.63, "path": ""}
        res = ingest_clip(store, clip, sc.SidecarSource({"person_moving": m}, "segments"))
        self.assertEqual(len(res), 4)
        gesture = store.db.execute("SELECT location, source, t_end FROM claims WHERE attribute='gesture'").fetchone()
        self.assertEqual(tuple(gesture), ("venue", "sidecar_segments", 1.0))  # location defaulted to scene
        vals = [r["value"] for r in store.db.execute("SELECT value FROM claims WHERE attribute='position' ORDER BY id")]
        self.assertEqual(vals, ["left_of_pole", "behind_pole", "right_of_pole"])

    def test_missing_sidecar_yields_nothing(self):
        src = sc.SidecarSource({}, "segments")
        self.assertEqual(src.extract({"clip_id": "nope"}), [])
        with self.assertRaises(ValueError):
            sc.SidecarSource({}, "boxes")


class BridgeTimeline(unittest.TestCase):
    def setUp(self):
        self.dir = make_clips_dir(BRIDGE)
        self.store, self.results = ic.build_store(":memory:", self.dir, write_back=False)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_order_and_timeline(self):
        rows = self.store.db.execute("SELECT clip_id, t_start, t_end, path FROM clips ORDER BY t_start").fetchall()
        self.assertEqual([r["clip_id"] for r in rows], BRIDGE)
        self.assertAlmostEqual(rows[1]["t_start"], 9.009, places=4)
        self.assertAlmostEqual(rows[2]["t_start"], 9.009 + 35.001633, places=4)
        self.assertTrue(rows[0]["path"].endswith("bridge_01_road_clear.mp4"))
        counts = {}
        for r in self.results:
            counts[r["rule_id"]] = counts.get(r["rule_id"], 0) + 1
        self.assertEqual(counts, {"FIRST_CLAIM": 2, "SUPERSEDE_NEWER_CONTRADICTS": 3, "CORROBORATE_SAME_VALUE": 1})

    def test_gap_shifts_starts(self):
        store, _ = ic.build_store(":memory:", self.dir, gap=30.0, write_back=False)
        rows = store.db.execute("SELECT t_start FROM clips ORDER BY t_start").fetchall()
        self.assertAlmostEqual(rows[1]["t_start"], 39.009, places=4)
        self.assertAlmostEqual(rows[2]["t_start"], 39.009 + 35.001633 + 30, places=4)

    def test_answers_over_time(self):
        key = ("underpass", "state", "bridge")
        self.assertIsNone(answer(self.store, key, 5, max_age=20)["answer"])  # claim is known at its t_end (9.0)
        self.assertEqual(answer(self.store, key, 9.5, max_age=20)["answer"], "clear")
        r = answer(self.store, key, 20, max_age=20)
        self.assertEqual((r["answer"], r["clip_id"], r["stale"]), ("blocked", "bridge_02_truck_stuck", False))
        r = answer(self.store, key, 60, max_age=20)
        self.assertEqual((r["answer"], [h["value"] for h in r["history"]]), ("clear", ["clear", "blocked"]))

    def test_end_of_footage_is_not_stale(self):
        # bugs.md #1: slider at max == footage end must not be flagged stale when the claim was just confirmed.
        end = self.store.db.execute("SELECT MAX(t_end) m FROM clips").fetchone()["m"]
        r = answer(self.store, ("underpass", "state", "bridge"), end, max_age=20)
        self.assertEqual((r["answer"], r["stale"]), ("clear", False))
        r = answer(self.store, ("underpass", "state", "bridge"), end + 30, max_age=20)
        self.assertTrue(r["stale"])
        self.assertIn("footage ends", r["stale_reason"])

    def test_write_back_stores_timeline(self):
        ic.build_store(":memory:", self.dir, write_back=True)
        m = sc.load_sidecar(os.path.join(self.dir, "bridge_02_truck_stuck.meta.json"))
        self.assertEqual(m["timeline"]["order"], 1)
        self.assertAlmostEqual(m["timeline"]["t_start"], 9.009, places=4)

    def test_init_skips_existing(self):
        self.assertEqual(ic.init_sidecars(self.dir), [])  # all three already have sidecars


class RangeParsing(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual(sv.parse_range("bytes=0-99", 1000), (0, 99))
        self.assertEqual(sv.parse_range("bytes=900-", 1000), (900, 999))
        self.assertEqual(sv.parse_range("bytes=-100", 1000), (900, 999))
        self.assertEqual(sv.parse_range("bytes=-5000", 1000), (0, 999))
        self.assertEqual(sv.parse_range("bytes=0-5000", 1000), (0, 999))
        self.assertIsNone(sv.parse_range(None, 1000))
        self.assertIsNone(sv.parse_range("garbage", 1000))
        self.assertIsNone(sv.parse_range("bytes=0-99,200-299", 1000))
        for bad in ("bytes=1000-", "bytes=50-10", "bytes=-0"):
            with self.assertRaises(ValueError, msg=bad):
                sv.parse_range(bad, 1000)

    def test_seconds(self):
        self.assertEqual(sv.seconds("20"), 20.0)       # bare numbers are seconds, not hours
        self.assertEqual(sv.seconds(20.5), 20.5)
        self.assertEqual(sv.seconds("00:00:20"), 20.0)


class ServeHandlers(unittest.TestCase):
    """Real server on an ephemeral port, fake 10 KiB mp4s, fixture sidecars, a built db."""
    @classmethod
    def setUpClass(cls):
        cls.dir = make_clips_dir(BRIDGE + ["person_moving"], bytes(range(256)) * 40)
        cls.db = os.path.join(cls.dir, "receipts.db")
        ic.build_store(cls.db, cls.dir, write_back=True)
        cls.saved = (sv.CLIPS_DIR, sv.DB_PATH)
        sv.CLIPS_DIR, sv.DB_PATH = cls.dir, cls.db
        cls.httpd = sv.serve(port=0)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        sv.CLIPS_DIR, sv.DB_PATH = cls.saved
        shutil.rmtree(cls.dir)

    def get(self, path, headers=None, method="GET"):
        req = urllib.request.Request(self.base + path, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def test_range_request(self):
        code, h, body = self.get("/clips/bridge_01_road_clear.mp4", {"Range": "bytes=10-19"})
        self.assertEqual(code, 206)
        self.assertEqual(h["Content-Range"], "bytes 10-19/10240")
        self.assertEqual(body, bytes(range(10, 20)))
        code, h, body = self.get("/clips/bridge_01_road_clear.mp4")
        self.assertEqual((code, len(body), h["Accept-Ranges"]), (200, 10240, "bytes"))
        code, h, body = self.get("/clips/bridge_01_road_clear.mp4", method="HEAD")
        self.assertEqual((code, h["Content-Length"], body), (200, "10240", b""))
        code, h, _ = self.get("/clips/bridge_01_road_clear.mp4", {"Range": "bytes=99999-"})
        self.assertEqual((code, h["Content-Range"]), (416, "bytes */10240"))

    def test_browse_prefix_and_questions(self):
        code, _, body = self.get("/browse/api/clips")
        d = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(d["clips"][0]["clip_id"], "bridge_01_road_clear")
        code, _, body = self.get("/browse/clips/bridge_01_road_clear.mp4", {"Range": "bytes=0-3"})
        self.assertEqual((code, body), (206, bytes(range(4))))
        code, _, body = self.get("/browse/")
        self.assertEqual(code, 200)
        self.assertIn(b"clip viewer", body)
        code, _, body = self.get("/api/questions")
        q = json.loads(body)
        self.assertEqual(q["clip_clock"]["person_moving"], "12:03:00")
        self.assertIn("r01", [item["id"] for item in q["questions"]])
        self.assertEqual(q["questions"][0]["clip_origin"], "12:03:00")

    def test_path_guards(self):
        self.assertEqual(self.get("/clips/../serve.py")[0], 404)
        self.assertEqual(self.get("/clips/nope.mp4")[0], 404)
        self.assertEqual(self.get("/static/../serve.py")[0], 404)
        self.assertEqual(self.get("/static/app.js")[0], 200)
        self.assertEqual(self.get("/")[1]["Content-Type"], "text/html; charset=utf-8")

    def test_api_clips_and_meta(self):
        code, _, body = self.get("/api/clips")
        d = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual([c["clip_id"] for c in d["clips"]], BRIDGE + ["person_moving"])
        self.assertEqual(d["clips"][0]["url"], "clips/bridge_01_road_clear.mp4")
        self.assertAlmostEqual(d["t_max"], 9.009 + 35.001633 + 10.01 + 8.633333, places=3)
        _, _, body = self.get("/api/clips/bridge_01_road_clear/meta")
        db = json.loads(body)["segments"][0]["claims"][0]["db"]
        self.assertEqual((db["status"], db["created_rule"], db["rule_fired"], db["sighting"]),
                         ("SUPERSEDED", "FIRST_CLAIM", "SUPERSEDE_NEWER_CONTRADICTS", "first"))
        self.assertEqual(db["superseded_by_clip"], "bridge_02_truck_stuck")
        _, _, body = self.get("/api/clips/bridge_02_truck_stuck/meta")
        segs = json.loads(body)["segments"]
        self.assertEqual(segs[1]["claims"][0]["db"]["sighting"], "corroboration")
        _, _, body = self.get("/api/clips/person_moving/meta")
        tc = json.loads(body)["track_claims"]
        self.assertEqual([(t["zone"], t["db"]["status"]) for t in tc],
                         [("left", "SUPERSEDED"), ("left", "SUPERSEDED"), ("right", "ACTIVE")])
        self.assertEqual(self.get("/api/clips/nope/meta")[0], 404)

    def test_api_answer_units_and_status(self):
        # bugs.md #1: as_of=20 is 20 seconds (parse_t would read 20 hours); #2: status and stale are separate.
        _, _, body = self.get("/api/answer?entity=underpass&attribute=state&location=bridge&as_of=20")
        d = json.loads(body)
        self.assertEqual((d["answer"], d["clip_id"], d["claim_status"], d["stale"]),
                         ("blocked", "bridge_02_truck_stuck", "ACTIVE", False))
        self.assertEqual((d["created_rule"], d["current_status"]), ("SUPERSEDE_NEWER_CONTRADICTS", "SUPERSEDED"))
        self.assertAlmostEqual(d["rel_t_start"], 0.0)
        self.assertAlmostEqual(d["rel_t_end"], 6.0, places=4)
        _, _, body = self.get("/api/answer?entity=underpass&attribute=state&location=bridge&as_of=60&max_age=5")
        d = json.loads(body)
        self.assertEqual((d["answer"], d["claim_status"], d["stale"]), ("clear", "ACTIVE", True))
        self.assertEqual([(h["value"], h["created_rule"]) for h in d["history"]],
                         [("clear", "FIRST_CLAIM"), ("blocked", "SUPERSEDE_NEWER_CONTRADICTS")])
        _, _, body = self.get("/api/answer?entity=nobody&attribute=state&location=bridge&as_of=60")
        self.assertEqual(json.loads(body)["claim_status"], "NO_EVIDENCE")
        self.assertEqual(self.get("/api/answer?entity=x")[0], 400)
        _, _, body = self.get("/api/keys")
        self.assertIn({"entity": "underpass", "attribute": "state", "location": "bridge", "n": 3}, json.loads(body))


if __name__ == "__main__":
    unittest.main()
