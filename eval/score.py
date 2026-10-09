"""Scores Receipts and the baselines on eval/questions.json. Counts only, no significance claims.
Usage: python3 eval/score.py [questions.json] [--json]"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from answer import answer  # noqa: E402
from baselines import ALL  # noqa: E402
from claims import parse_t  # noqa: E402
from fixtures.demo_scenario import build_store  # noqa: E402

REQUIRED = ("id", "question", "question_key", "as_of", "expected_answer", "expected_clip_id", "expected_interval")


class Receipts:
    name = "Receipts (supersession)"

    def answer(self, store, question, key, as_of):
        return answer(store, key, as_of)


def iou(a, b):
    """Temporal IoU of two (start, end) intervals in seconds; 0 if either is missing."""
    if not a or not b:
        return 0.0
    inter = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    union = (a[1] - a[0]) + (b[1] - b[0]) - inter
    return inter / union if union > 0 else 0.0


def load_questions(path):
    with open(path) as f:
        qs = json.load(f)["questions"]
    for q in qs:
        missing = [k for k in REQUIRED if k not in q]
        if missing:
            raise ValueError(f"{q.get('id')}: missing {missing}")
    return qs


def norm(x):
    return None if x is None else str(x).strip().lower()


def score_system(system, questions, build=build_store):
    c = dict(n=len(questions), correct=0, acc_gqa=0, cited=0, cited_ok=0, answered=0, stale_used=0, flag_ok=0)
    for q in questions:
        store = build(q["as_of"])
        r = system.answer(store, q["question"], q["question_key"], q["as_of"])
        exp_iv = [parse_t(t) for t in q["expected_interval"]] if q["expected_interval"] else None
        got_iv = (r["t_start"], r["t_end"]) if r["clip_id"] else None
        ok = norm(r["answer"]) == norm(q["expected_answer"])
        hit = got_iv is not None and iou(got_iv, exp_iv) >= 0.5 and r["clip_id"] == q["expected_clip_id"]
        c["correct"] += ok
        c["acc_gqa"] += ok and (hit or q["expected_answer"] is None)
        if got_iv:
            c["cited"] += 1
            c["cited_ok"] += hit
        if r["answer"] is not None:
            c["answered"] += 1
            c["stale_used"] += store.is_superseded_at(r["claim_id"], parse_t(q["as_of"]))
        c["flag_ok"] += bool(r.get("stale")) == bool(q.get("expected_stale", False))
    return c


def table(rows):
    hdr = f"{'system':30s} {'exact':>7s} {'Acc@GQA':>8s} {'cite-prec':>10s} {'stale-claim':>12s} {'stale-flag':>11s}"
    lines = [hdr, "-" * len(hdr)]
    for name, c in rows:
        flag = f"{c['flag_ok']}/{c['n']}" if name.startswith("Receipts") else "n/a"
        lines.append(f"{name:30s} {c['correct']:>3d}/{c['n']:<3d} {c['acc_gqa']:>4d}/{c['n']:<3d} "
                     f"{c['cited_ok']:>5d}/{c['cited']:<4d} {c['stale_used']:>6d}/{c['answered']:<5d} {flag:>11s}")
    return "\n".join(lines)


def main(argv):
    path = next((a for a in argv if not a.startswith("--")),
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "questions.json"))
    qs = load_questions(path)
    rows = [(s.name, score_system(s, qs)) for s in [Receipts()] + ALL]
    if "--json" in argv:
        print(json.dumps(dict(rows), indent=1))
        return
    print(f"{len(qs)} questions from {os.path.basename(path)} (SYNTHETIC scenario)\n")
    print(table(rows))
    print("\nexact = answer string matches; Acc@GQA = exact AND cited clip/interval IoU>=0.5 with ground truth;"
          "\ncite-prec = citations with IoU>=0.5 / citations made; stale-claim = answers built on a claim already"
          "\nsuperseded at as_of / answers given; stale-flag = Receipts STALE flag agrees with expected_stale.")


if __name__ == "__main__":
    main(sys.argv[1:])
