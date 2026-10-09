# Wire the event's VSS agent (agent-qa) into the board - after qa_2_31, ~10 lines

Already built and tested: webapp/agentqa.py (+ agentqa.py, tests/test_agentqa.py). It calls POST /api/v1/agent/ask with the board's existing server-side VSS login, caches per question, and returns {ok, answer, evidence_source, evidence_start_s, evidence_end_s, evidence_abs_t, model_id, label} or {ok:false, error}. Do not rewrite it.

1. webapp/main.py: `from agentqa import agentqa_answer`; route GET /api/agentqa?q=... -> json.dumps(agentqa_answer(q)).
2. Board: a third card beside "Typical video agent", titled "Event VSS agent (agent-qa)". Show its answer text, its top evidence segment + time (fmt evidence_abs_t if present), and a grey line: "Searches the whole archive; no as_of cutoff, no stale flag." If ok is false, show "agent-qa unavailable: <error>" in grey; never invent an answer.
3. Fetch it when an answer is shown (Answer button and Run demo beat 3/4), async so it never blocks the board.
4. Curl-verify /app/api/agentqa?q=Is%20the%20forklift%20on%20cam-2%20moving%3F returns ok:true with a real answer before showing it in the demo. If it is slow (>10 s) or errors, keep the card but don't feature it in the video.
Commit, push, redeploy.
