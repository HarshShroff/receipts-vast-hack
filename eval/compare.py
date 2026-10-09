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


def answer_is_stale(store, result, as_of):
    """True when the answer is a claim value that a newer observation had already replaced."""
    if result.get("claim_id") is not None and store.is_superseded_at(result["claim_id"], as_of):
        return True
    if result.get("answer") is None:
        return False
    obs = store.observations(as_of)
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
    # A cited instant counts if it falls inside the labeled window.
    # A cited range counts if its overlap with the window has IoU >= 0.5,
    # or if the range is a point inside the window.
    if point_iv[0] == point_iv[1]:
        return window[0] <= point_iv[0] <= window[1]
    return iou(window, point_iv) >= 0.5


def score_one(system, store, questions):
    rows = []
    n = len(questions)
    exact = cited_in = answered = stale_used = stale_flag_ok = after_n = after_ok = 0
    for q in questions:
        r = system.answer(store, q["question"], q["question_key"], q["as_of"])
        exp_iv = [parse_t(t) for t in q["expected_interval"]] if q["expected_interval"] else None
        got = cited_interval(r)
        after_footage = q.get("category") == "after-footage"
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
            if answer_is_stale(store, r, parse_t(q["as_of"])):
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


def bar_chart(summaries, path):
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
    ax.bar([i - w for i in x], exact, w, label="correct (after-footage: flagged stale)")
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
    ax.set_title(f"Own venue clip, hand-labeled positions (n={n}); claims = labels, so this tests time logic, not extraction", fontsize=9)
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
    args = p.parse_args(argv)

    questions = json.load(open(args.questions))["questions"]
    store, source = build_store_from_questions(args.db, questions)
    systems = [Receipts()] + list(ALL)
    notes = [
        f"store: {source}",
        "claims are the eye-checked position intervals, so Receipts is scored on those labels",
    ]

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
        summaries[name] = summary
        per_q[name] = rows

    os.makedirs(args.out, exist_ok=True)
    chart = os.path.join(args.out, "results_real.png")
    bar_chart(summaries, chart)
    payload = {"questions": os.path.basename(args.questions), "notes": notes, "summaries": summaries, "per_question": per_q}
    out_json = os.path.join(args.out, "results_real.json")
    with open(out_json, "w") as f:
        json.dump(payload, f, indent=2)
    print(json.dumps({"notes": notes, "summaries": summaries, "chart": chart}, indent=2))
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
