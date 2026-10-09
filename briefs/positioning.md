# Positioning pass (after C13 Run demo; small, before 3:00 freeze)

Context from scanning all team apps at 1:58 PM: 24 of 50 deployed. Several teams already cite clips for every answer (Witness, CrossWise, The Street Talks Back, Close-Call Scout), and AisleGuard (team 2) runs on the SAME camera (sdg_warehouse_cam-2). We are the ONLY app with superseded/stale answers. So the page must sell TIME, not citations.

1. Headline (replace current): "RECEIPTS - video answers that know when they're out of date". Subhead: "Every answer cites its clip. When newer footage contradicts it, the answer changes. When the footage stops, it says so." "every answer has a clip" moves to the subhead only.
2. Above the fold, a one-line status strip that always shows time: "Answer as of 07:47:00 - evidence 5s old - footage ends 07:57:47". Stale turns it red.
3. EVAL panel (small, below the timeline): show eval/out/results_real.png (copy it to webapp/static or inline as base64) with the caption: "Own footage filmed at the venue today, 6 hand-labeled questions. Same claims for every system, so this isolates time handling. After the camera stopped, the 4 baselines answered with confidence; Receipts flagged stale." Link to eval/questions_real.json in the repo.
4. Footer sponsor chips: keep only what actually ran in this app. If W&B is not wired by freeze time, do not show it.
Do not add new features beyond these. Commit + push + redeploy.

UPDATE 2:10 PM: W&B is real now. The eval is logged at https://wandb.ai/harshrofff-na/receipts-vast-hack/runs/kvzklxkd (results table + chart). Keep the footer chip as "W&B (eval tracking)" linking to that run, and in the README "Sponsor tools" list it as: Weights & Biases, eval results table and chart logged per run (eval/compare.py). Do not claim W&B inference or Weave tracing; neither ran.
