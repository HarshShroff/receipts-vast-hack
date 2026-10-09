# Wire in the naive baseline (B9) - already built and tested: naive.py, webapp/naive.py, tests/test_naive.py

Do NOT rewrite naive.py. Just wire it (about 10 lines):

1. webapp/main.py: `from naive import naive_answer`
2. New route GET /api/naive?q=<question>&entity=<e>&attribute=<a>:
   `return self._send(200, json.dumps(naive_answer(STATE["store"], q, entity, attribute, camera_id=CAMERA)), "application/json")`
   (503 if not ready, same as /api/answer). Add stream_url(result["path"]) as result["stream"] if you serve video that way.
3. Page: next to the Receipts answer card, a grey card titled "Typical video agent (retrieval-only, simulated)". Show its answer big, its caption quote, and NO timestamp/as_of (that is the point). If its answer differs from Receipts', show a red "OUTDATED: answered from older footage (hh:mm:ss) - Receipts uses hh:mm:ss" line under it, using the naive clip's t_start vs the Receipts claim time.
4. Run demo (C13) should land on a question where the two differ (forklift moving vs parked is the expected one). If on the real data they never differ, say so and show them agreeing; never fake it.
