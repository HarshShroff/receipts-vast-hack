# Eval pass brief (runs in parallel with the demo pass; merge by 2:45 ET)

Only create NEW files: eval/questions_real.json, baselines_vss.py, eval/compare.py, tests/test_baselines_vss.py, eval/out/*. Never edit existing files (another agent is editing webapp/ and run_vss.py). Commit only your files; git pull --rebase before push.

No paid APIs: only the team VSS, the W&B serverless inference endpoint if WANDB_API_KEY is set, and local code. Never hardcode keys.

1. QUESTIONS. From receipts.db and the live captions, draft 8 questions about state that changes over time (forklift moving vs parked, worker present vs not), each with an as_of time and a proposed answer + segment id. At least 3 where the answer differs from earlier footage, and at least 1 asked after the footage ends (correct = stale). Mark every label "caption-derived, needs human check" in the file; a human will verify each against the clip. Write eval/questions_real.json in the eval/questions.json schema.
2. BASELINES (baselines_vss.py, same interface as baselines.py): (a) VSS agent-qa per question, time-restricted to as_of if the API allows (record if it can't); (b) LLM-over-captions: all captions up to as_of + question to W&B serverless inference, JSON {answer, cited_time_s}, record model id; skip (b) cleanly if no WANDB_API_KEY. Unit tests with fake clients, no network.
3. COMPARE (eval/compare.py --db receipts.db): Receipts vs agent-qa vs LLM-over-captions vs baselines.py baselines on questions_real.json. Metrics: exact answer, cited time inside labeled interval, stale-claim rate, correct "stale" on after-footage questions. Write eval/out/results_real.json and one bar chart PNG (matplotlib). Log to W&B project "receipts-vast-hack" if WANDB_API_KEY is set.

Report counts with n, no significance claims, report where baselines win. Never invent results.
