#!/usr/bin/env python3
"""Score Receipts and baselines on eval/questions_real.json.

Usage:
  python3 eval/compare.py --db receipts.db
  python3 eval/compare.py --db receipts.db --live

Without --live, VSS agent-qa and the W&B caption model are not called.
The db is rebuilt from the eye-checked intervals in the question file, under the
question key (gray_hoodie, position, center_pillar), so Receipts can supersede
when the position changes. Those claims are the labels, not Cosmos output.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from answer import answer as receipts_answer  # noqa: E402
from baselines import ALL  # noqa: E402
from baselines_vss import LlmCaptionBaseline, VssAgentBaseline, VssAgentClient, wandb_chat  # noqa: E402
from claims import Store, parse_t  # noqa: E402
from eval.score import iou, norm  # noqa: E402
from supersede import ingest_claim  # noqa: E402

CLIP_START = 12 * 3600 + 3 * 60  # 12:03:00
HERE = os.path.dirname(os.path.abspath(__file__))


class Receipts:
    name = "Receipts (supersession)"

    def answer(self, store, question, key, as_of):
        return receipts_answer(store, key, as_of)


CLIPS_DIR = os.path.join(os.path.dirname(HERE), "clips")
GAP_CATEGORIES = ("after-footage", "footage-gap")  # correct = flagged stale, whatever the value


def clip_duration(clip_id):
    """Seconds, from the viewer sidecar (clips/<id>.meta.json), else ffprobe."""
    meta = os.path.join(CLIPS_DIR, clip_id + ".meta.json")
    if os.path.exists(meta):
        with open(meta) as f:
            return float(json.load(f)["video"]["duration"])
    import subprocess
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          os.path.join(CLIPS_DIR, clip_id + ".mp4")], capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def scene_clips(clock):
    """clip_clock {clip_id: 'HH:MM:SS'} -> [(clip_id, path, t_start, t_end)] in time order."""
    out = []
    for cid, start in clock.items():
        t0 = parse_t(start)
        out.append((cid, os.path.join(CLIPS_DIR, cid + ".mp4"), t0, t0 + clip_duration(cid)))
    return sorted(out, key=lambda c: c[2])


def build_store_from_labels(db_path, clock, labels):
    """Every clip of the scene on its clock, and one claim per eye-checked label interval
    (observed_at = interval start, as for the venue set)."""
    if os.path.exists(db_path):
        os.remove(db_path)
    store = Store(db_path)
    for cid, path, t0, t1 in scene_clips(clock):
        store.add_clip(cid, t0, t1, "eye-checked labels", os.path.relpath(path, os.path.dirname(HERE)))
    for lab in sorted(labels, key=lambda l: parse_t(l["interval"][0])):
        entity, attribute, location = lab["key"]
        ingest_claim(store, entity, attribute, lab["value"], lab["clip_id"], lab["interval"][0], lab["interval"][1],
                     location, source="eye_check", observed_at=lab["interval"][0])
    return store, "eye-checked label intervals in " + os.path.basename(db_path)


def build_store_from_questions(db_path, questions):
    """One claim per labeled interval. observed_at is the interval start so a question during that interval can see it."""
    labeled = [q for q in questions if q.get("expected_answer") and q.get("expected_interval")]
    if not labeled:
        raise SystemExit("no labeled intervals to ingest")
    if os.path.exists(db_path):
        os.remove(db_path)
    store = Store(db_path)
    clip_id = labeled[0]["expected_clip_id"] or "person_moving"
    store.add_clip(clip_id, CLIP_START, CLIP_START + 8.633, "eye-checked position intervals", "clips/person_moving.mp4")
    for q in sorted(labeled, key=lambda q: parse_t(q["expected_interval"][0])):
        entity, attribute, location = q["question_key"]
        ingest_claim(
            store, entity, attribute, q["expected_answer"], clip_id,
            q["expected_interval"][0], q["expected_interval"][1], location,
            source="eye_check", observed_at=q["expected_interval"][0],
        )
    return store, "eye-checked intervals in " + os.path.basename(db_path)


def captions_from_questions(questions):
    """Timed sentences from the same intervals. A caption is available once its interval has started."""
    caps = []
    for q in questions:
        if not q.get("expected_answer") or not q.get("expected_interval"):
            continue
        t0, t1 = q["expected_interval"]
        caps.append({
            "text": f"The man in the gray hoodie is {q['expected_answer']}.",
            "t_start": t0,
            "t_end": t1,
        })
    return caps


def answer_is_stale(store, result, as_of, key=None):
    """True when the answer is a claim value that a newer observation of the same key had already replaced.
    key = (entity, attribute, location); without it every observation counts (single-key question sets)."""
    if result.get("claim_id") is not None and store.is_superseded_at(result["claim_id"], as_of):
        return True
    if result.get("answer") is None:
        return False
    obs = store.observations(as_of)
    if key is not None:
        k = tuple(norm(x) for x in key)
        obs = [o for o in obs if (norm(o["entity"]), norm(o["attribute"]), norm(o["location"])) == k]
    if not obs:
        return False
    latest = max(obs, key=lambda o: (o["observed_at"], o["claim_id"]))
    ans = norm(result["answer"])
    if norm(latest["value"]) == ans:
        return False
    return any(norm(o["value"]) == ans and o["observed_at"] < latest["observed_at"] for o in obs)


def cited_interval(result):
    if result.get("t_start") is not None and result.get("t_end") is not None:
        return (float(result["t_start"]), float(result["t_end"]))
    cited = result.get("cited_time_s")
    if cited is None:
        return None
    return (float(cited), float(cited))


def interval_contains(window, point_iv):
    if not window or not point_iv:
        return False
    # A cited instant or range counts if it lies inside the labeled window (a precise citation of
    # one moment of a long labeled state is right), or if it overlaps the window with IoU >= 0.5
    # (a citation a little wider than the label).
    eps = 1e-6
    if window[0] - eps <= point_iv[0] and point_iv[1] <= window[1] + eps:
        return True
    if point_iv[0] == point_iv[1]:
        return False
    return iou(window, point_iv) >= 0.5


def score_one(system, store, questions):
    rows = []
    n = len(questions)
    exact = cited_in = answered = stale_used = stale_flag_ok = after_n = after_ok = 0
    for q in questions:
        r = system.answer(store, q["question"], q["question_key"], q["as_of"])
        exp_iv = [parse_t(t) for t in q["expected_interval"]] if q["expected_interval"] else None
        got = cited_interval(r)
        after_footage = q.get("category") in GAP_CATEGORIES
        if after_footage:
            # Nothing was observed at as_of: the right behaviour is a stale flag, with or without the last known value.
            ok = bool(r.get("stale"))
        else:
            ok = norm(r.get("answer")) == norm(q["expected_answer"])
        inside = bool(exp_iv) and interval_contains(tuple(exp_iv), got)
        exact += ok
        if got is not None:
            cited_in += inside
        if r.get("answer") is not None:
            answered += 1
            if answer_is_stale(store, r, parse_t(q["as_of"]), q.get("question_key")):
                stale_used += 1
        if after_footage:
            after_n += 1
            after_ok += bool(r.get("stale"))
        stale_flag_ok += bool(r.get("stale")) == bool(q.get("expected_stale", False))
        rows.append({
            "id": q["id"],
            "expected": q["expected_answer"],
            "answer": r.get("answer"),
            "exact": ok,
            "cited_time_s": r.get("cited_time_s"),
            "cited_interval": list(got) if got else None,
            "cited_in_interval": inside,
            "time_restricted": r.get("time_restricted"),
            "model_id": r.get("model_id"),
            "note": r.get("note"),
            "stale": r.get("stale"),
        })
    summary = {
        "n": n,
        "exact": exact,
        "cited_in_interval": cited_in,
        "answered": answered,
        "stale_claim": stale_used,
        "stale_flag_ok": stale_flag_ok,
        "after_footage_n": after_n,
        "after_footage_correct_stale": after_ok,
    }
    return summary, rows


class _OneRepeat:
    """Scores a single repeat of a repeated system (for min-max across runs)."""

    def __init__(self, system, k):
        self.system, self.k, self.name = system, k, f"{system.name} run {k}"

    def answer(self, store, question, key, as_of):
        return self.system.answer(store, question, key, as_of, repeat=self.k)


def bar_chart(summaries, path, title="Own venue clip, hand-labeled positions"):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    names = list(summaries)
    ran = [summaries[n]["answered"] > 0 for n in names]
    exact = [summaries[n]["exact"] if r else 0 for n, r in zip(names, ran)]
    cited = [summaries[n]["cited_in_interval"] if r else 0 for n, r in zip(names, ran)]
    stale = [summaries[n]["stale_claim"] if r else 0 for n, r in zip(names, ran)]
    x = range(len(names))
    fig, ax = plt.subplots(figsize=(9, 5))
    w = 0.27
    ax.bar([i - w for i in x], exact, w, label="correct (after-footage / footage gap: flagged stale)")
    ax.bar(list(x), cited, w, label="cited time inside labeled interval")
    ax.bar([i + w for i in x], stale, w, label="answered from outdated state", color="#c0392b")
    n = next(iter(summaries.values()))["n"]
    for i, r in zip(x, ran):
        if not r:
            ax.text(i, 0.4, "did not run", ha="center", va="bottom", rotation=90, color="#777", fontsize=9)
    ax.set_xticks(list(x))
    ax.set_xticklabels(names, rotation=20, ha="right")
    ax.set_ylabel(f"count out of {n}")
    ax.set_ylim(0, max(n, 1))
    ax.set_title(f"{title} (n={n}); claims = labels, so this tests time logic, not extraction", fontsize=9)
    ax.legend(fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.32), ncol=3, frameon=False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def maybe_wandb(table, chart_path):
    if not os.environ.get("WANDB_API_KEY"):
        return "WANDB_API_KEY unset; chart not logged"
    try:
        import wandb
    except ImportError:
        return "wandb package not installed; chart not logged"
    entity = os.environ.get("WANDB_TEAM") or None
    run = wandb.init(project="receipts-vast-hack", entity=entity, job_type="eval", reinit=True)
    wandb.log({"results": wandb.Table(dataframe=_frame(table))})
    wandb.log({"chart": wandb.Image(chart_path)})
    url = run.url
    run.finish()
    return url


def _frame(table):
    import pandas as pd
    return pd.DataFrame(table)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="receipts.db")
    p.add_argument("--questions", default=os.path.join(HERE, "questions_real.json"))
    p.add_argument("--cosmos", default=os.path.join(HERE, "out", "person_moving_cosmos.txt"))
    p.add_argument("--out", default=os.path.join(HERE, "out"))
    p.add_argument("--live", action="store_true", help="call team VSS agent-qa and W&B inference")
    p.add_argument("--gemini", action="store_true",
                   help="add Gemini Flash watching the clip cut at as_of (cached in eval/out/gemini_cache)")
    p.add_argument("--gemini-model", default=None, help="default: newest stable gemini-*-flash")
    p.add_argument("--gemini-offline", action="store_true", help="use only cached Gemini answers")
    p.add_argument("--repeats", type=int, default=3, help="Gemini calls per question (majority reported)")
    args = p.parse_args(argv)

    with open(args.questions) as f:
        qdata = json.load(f)
    questions = qdata["questions"]
    clock = qdata.get("clip_clock") or {}
    labels = qdata.get("labels")
    set_name = os.path.splitext(os.path.basename(args.questions))[0].replace("questions_", "")
    if labels:
        store, source = build_store_from_labels(args.db, clock, labels)
        claims_note = "claims are the eye-checked label intervals, so Receipts is scored on those labels"
    else:
        store, source = build_store_from_questions(args.db, questions)
        claims_note = "claims are the eye-checked position intervals, so Receipts is scored on those labels"
    systems = [Receipts()] + list(ALL)
    notes = [f"store: {source}", claims_note]
    if qdata.get("label_status") == "draft":
        notes.append("LABELS ARE A DRAFT: not yet checked by a human; do not quote these numbers")

    if args.live:
        systems.append(VssAgentBaseline(VssAgentClient()))
        caps = captions_from_questions(questions)
        notes.append("LLM captions are those same eye-checked sentences, one per interval")
        def llm(prompt, holder={"model": None}):
            try:
                text, model = wandb_chat(prompt)
            except Exception as e:
                if f"W&B inference failed: {e}" not in notes:
                    notes.append(f"W&B inference failed: {e}")
                return '{"answer": null, "cited_time_s": null}'
            holder["model"] = model
            llm.model_id = model
            return text
        llm.model_id = os.environ.get("WANDB_MODEL", "")
        systems.append(LlmCaptionBaseline(caps, llm, model_id="wandb"))
    else:
        notes.append("VSS agent-qa and LLM-over-captions were not called (--live not set)")

    gemini = None
    if args.gemini:
        from baselines_gemini import GeminiFullContext, GeminiScene, answer_options, options_by_key
        from gemini_client import GeminiVideoQA
        qa = GeminiVideoQA(model=args.gemini_model, offline=args.gemini_offline)
        if args.gemini_offline and qa._model is None:
            raise SystemExit("--gemini-offline needs --gemini-model (no API call to resolve it)")
        clips = scene_clips(clock) if clock else []
        if len(clips) <= 1:  # single clip: the original venue setup, prompt unchanged so its cache stays valid
            clip = clips[0][1] if clips else os.path.join(CLIPS_DIR, "person_moving.mp4")
            start = clips[0][2] if clips else CLIP_START
            gemini = GeminiFullContext(clip, start, answer_options(questions), qa=qa, repeats=args.repeats)
            what, prompt_name = os.path.relpath(clip, os.path.dirname(HERE)) + " cut at as_of", "PROMPT_FULL"
        else:
            gemini = GeminiScene(clips, options_by_key(questions), qa=qa, repeats=args.repeats)
            what, prompt_name = (f"every clip of the scene that started before as_of ({len(clips)} clips), "
                                 "the last cut at as_of"), "PROMPT_SCENE"
        systems.append(gemini)
        notes.append(f"Gemini Flash ({qa.model_id}) watches {what} (720p, 2 fps sampling, temperature 0), answers from "
                     f"the label vocabulary or 'unknown', majority of {args.repeats} run(s); "
                     f"prompt in baselines_gemini.{prompt_name}")

    summaries = {}
    per_q = {}
    for system in systems:
        if isinstance(system, LlmCaptionBaseline) and getattr(system.llm, "model_id", None):
            pass
        summary, rows = score_one(system, store, questions)
        name = system.name
        if isinstance(system, LlmCaptionBaseline):
            mid = getattr(system.llm, "model_id", None) or system.model_id
            summary["model_id"] = mid
            name = f"{system.name} ({mid})" if mid else system.name
        if system is gemini:
            from baselines_gemini import MissingRepeat
            per_run = []
            for k in range(system.repeats):
                try:
                    per_run.append(score_one(_OneRepeat(system, k), store, questions)[0])
                except MissingRepeat:
                    pass  # incomplete run: left out of min-max, reported below
            summary.update(model_id=system.qa.model_id, repeats=system.repeats,
                           complete_runs=len(per_run),
                           exact_runs=[s["exact"] for s in per_run],
                           after_footage_runs=[s["after_footage_correct_stale"] for s in per_run])
            by_id = {q["id"]: q for q in questions}
            missing = []
            for row in rows:
                q = by_id[row["id"]]
                r = system.answer(store, q["question"], q["question_key"], q["as_of"])
                row.update(agreement=r["agreement"], repeats=r["repeats"], runs_missing=r["runs_missing"],
                           clip_id=r["clip_id"], t_start=r["t_start"], t_end=r["t_end"])
                if r["runs_missing"]:
                    missing.append(f"{row['id']} ({r['runs_missing']} missing)")
            if missing:
                summary["runs_missing"] = missing
                notes.append("Gemini runs not completed (free-tier quota), majority taken over the runs that exist: "
                             + ", ".join(missing))
        summaries[name] = summary
        per_q[name] = rows

    os.makedirs(args.out, exist_ok=True)
    chart = os.path.join(args.out, f"results_{set_name}.png")
    titles = {"real": "Own venue clip, hand-labeled positions"}
    draft = qdata.get("label_status") == "draft"
    bar_chart(summaries, chart, (titles.get(set_name, f"{set_name} clips, eye-checked labels")
                                 + (" [DRAFT LABELS]" if draft else "")))
    payload = {"questions": os.path.basename(args.questions), "label_status": qdata.get("label_status", "checked"),
               "notes": notes, "summaries": summaries, "per_question": per_q}
    out_json = os.path.join(args.out, f"results_{set_name}.json")
    with open(out_json, "w") as f:
        json.dump(payload, f, indent=2)
    print(json.dumps({"notes": notes, "summaries": summaries, "chart": chart}, indent=2))
    if draft:
        print("wandb: not logged (draft labels)")
        return 0
    try:
        logged = maybe_wandb(
            [{"system": n, **s} for n, s in summaries.items()],
            chart,
        )
    except Exception as e:
        logged = f"wandb log failed: {type(e).__name__}"
    print("wandb:", logged)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
