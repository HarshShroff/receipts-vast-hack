"""Gemini Flash as an answering baseline: it watches the footage itself instead of reading claims.

Variant (i), full context: for each question the clip is cut at as_of, so Gemini sees exactly the
footage Receipts could have seen, and gets the same facts Receipts uses (clip clock, what the
video covers, question time, and Receipts' stale rule in words). It picks one answer from the
question set's label vocabulary or "unknown", cites the moment, and sets a stale flag.

Same interface as baselines.py: answer(store, question, key, as_of) -> dict. The store is ignored.
Each question is asked `repeats` times (separately cached); the reported answer is the majority.
"""
import os
import subprocess
from collections import Counter

from claims import parse_t
from gemini_client import CacheMiss, GeminiVideoQA, QuotaExhausted, file_sha256

HERE = os.path.dirname(os.path.abspath(__file__))
CUT_DIR = os.path.join(HERE, "eval", "out", "gemini_cuts")
MAX_AGE = 900  # seconds; answer.DEFAULT_MAX_AGE, which eval/compare.py's Receipts uses
UNKNOWN = "unknown"
ENCODE = "libx264 crf23 720p noaudio"

PROMPT_FULL = """You are answering a question about security-camera footage.

Camera clock: the video starts at {clip_start}. The video you are given covers {cov_start} to {cov_end}.
The question is asked at {as_of}.

Question: {question}

Choose exactly one answer from this list: {options}. Answer "unknown" if the footage does not show it.
Cite the moment your answer is based on as t_start and t_end, in seconds from the start of the video.
Staleness rule: set stale to true if your answer relies on footage that ends before the question time,
or on evidence more than {max_age} seconds older than the question time. Otherwise set stale to false.
Give a one-sentence reason."""


class MissingRepeat(LookupError):
    """A repeat has no answer (quota or offline cache miss). Callers report it; nothing is filled in."""


def response_schema(options):
    return {"type": "OBJECT",
            "properties": {"answer": {"type": "STRING", "enum": list(options)},
                           "t_start": {"type": "NUMBER", "nullable": True},
                           "t_end": {"type": "NUMBER", "nullable": True},
                           "stale": {"type": "BOOLEAN"},
                           "reason": {"type": "STRING"}},
            "required": ["answer", "t_start", "t_end", "stale", "reason"],
            "propertyOrdering": ["answer", "t_start", "t_end", "stale", "reason"]}


def fmt_clock(s):
    """Seconds since midnight -> HH:MM:SS(.s)."""
    whole = int(s)
    frac = round(s - whole, 3)
    out = f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d}"
    return out + (f"{frac:.3f}".lstrip("0").rstrip("0").rstrip(".") if frac else "")


def probe_duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def cut_until(src, seconds, out_dir=CUT_DIR, run=subprocess.run):
    """First `seconds` of src, re-encoded (frame-accurate, unlike -c copy), 720p, no audio, cached.
    Returns (path, covered_seconds). seconds >= clip length -> the whole clip, encoded the same way,
    so every request sees the same resolution."""
    total = probe_duration(src)
    seconds = min(seconds, total)
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src))[0]
    dst = os.path.join(out_dir, f"{stem}__to_{int(round(seconds * 1000))}ms.mp4")
    if not os.path.exists(dst):
        run(["ffmpeg", "-v", "error", "-y", "-i", src, "-t", f"{seconds:.3f}", "-an",
             "-vf", "scale=-2:720", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-pix_fmt", "yuv420p", dst], check=True)
    return dst, min(seconds, probe_duration(dst))


def answer_options(questions):
    seen = []
    for q in questions:
        a = q.get("expected_answer")
        if a and a not in seen:
            seen.append(a)
    return seen + [UNKNOWN]


class GeminiFullContext:
    """(i) Full video up to as_of, one request per question and repeat."""

    def __init__(self, clip_path, clip_start, options, qa=None, repeats=3, max_age=MAX_AGE, cut=cut_until):
        self.clip_path, self.clip_start, self.options = clip_path, float(clip_start), list(options)
        self.qa = qa or GeminiVideoQA()
        self.repeats, self.max_age, self.cut = repeats, max_age, cut
        self._src_hash = None

    @property
    def name(self):
        return f"Gemini Flash, full video to as_of ({self.qa.model_id})"

    def prompt(self, question, as_of, covered):
        return PROMPT_FULL.format(clip_start=fmt_clock(self.clip_start), cov_start=fmt_clock(self.clip_start),
                                  cov_end=fmt_clock(self.clip_start + covered), as_of=fmt_clock(as_of),
                                  question=question, options=", ".join(f'"{o}"' for o in self.options),
                                  max_age=self.max_age)

    def ask_all(self, question, as_of):
        """All repeats for one question: [(data, model_version, cached)]."""
        as_of = parse_t(as_of)
        if self._src_hash is None:
            self._src_hash = file_sha256(self.clip_path)
        path, covered = self.cut(self.clip_path, max(as_of - self.clip_start, 0.0))
        video_id = f"{self._src_hash}:{covered:.3f}:{ENCODE}"
        prompt = self.prompt(question, as_of, covered)
        schema = response_schema(self.options)
        out = []
        for k in range(self.repeats):
            try:
                r = self.qa.ask(path, video_id, prompt, schema, repeat=k)
            except (CacheMiss, QuotaExhausted):
                out.append(None)  # reported as missing, never filled in
                continue
            out.append((r["data"], r["model_version"], r["cached"]))
        return out, covered

    def to_result(self, data, as_of, covered, model_version):
        ans = data.get("answer")
        t0, t1 = data.get("t_start"), data.get("t_end")
        cited = None
        if t0 is not None and t1 is not None:
            t0, t1 = sorted((max(0.0, float(t0)), max(0.0, float(t1))))
            cited = (self.clip_start + t0, self.clip_start + t1)
        return {"answer": None if ans in (None, UNKNOWN) else ans,
                "stale": bool(data.get("stale")),
                "t_start": cited[0] if cited else None, "t_end": cited[1] if cited else None,
                "cited_time_s": cited[1] if cited else None,
                "clip_id": os.path.splitext(os.path.basename(self.clip_path))[0] if cited else None,
                "claim_id": None, "time_restricted": True, "model_id": model_version,
                "note": data.get("reason"), "covered_until": self.clip_start + covered, "as_of": as_of}

    def answer(self, store, question, key, as_of, repeat=None):
        """Majority over repeats (or one repeat if `repeat` is given), in eval/compare.py's result shape."""
        runs, covered = self.ask_all(question, as_of)
        as_of = parse_t(as_of)
        results = [None if run is None else self.to_result(run[0], as_of, covered, run[1]) for run in runs]
        if repeat is not None:
            if results[repeat] is None:
                raise MissingRepeat(f"repeat {repeat} of {question!r} at {fmt_clock(as_of)} has no answer")
            return results[repeat]
        results = [r for r in results if r is not None]
        if not results:
            raise MissingRepeat(f"no answers for {question!r} at {fmt_clock(as_of)}")
        votes = Counter((r["answer"], r["stale"]) for r in results)
        (ans, stale), n = votes.most_common(1)[0]
        best = next(r for r in results if (r["answer"], r["stale"]) == (ans, stale))
        return dict(best, agreement=f"{n}/{len(results)}", runs_missing=self.repeats - len(results),
                    repeats=[{"answer": r["answer"], "stale": r["stale"], "t_start": r["t_start"],
                              "t_end": r["t_end"], "reason": r["note"]} for r in results])
