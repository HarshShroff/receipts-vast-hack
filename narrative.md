# RECEIPTS demo narrative (about 2:15)

Driver: Zain (screen). Voice: Harsh. Q&A on the eval: Yuri.
Use **Run demo** and press **Space** for each beat. Do not free-type questions on camera. Do not open Browse, .env or the VM.

## 1. The Hook (20s)
- 2.5 million nonfatal workplace injuries and illnesses in U.S. private industry in 2024 (BLS).
- Cameras already record warehouses, hospitals, factories and construction sites.
- Video AI can now search that footage and answer with a clip.
- But it answers from whatever clip it finds, even one from before things changed. That is how a "the aisle is clear" from ten minutes ago gets someone hurt.

## 2. The Solution: RECEIPTS (10s)
- RECEIPTS: video answers that know when they're out of date.
- Every answer comes with its timestamp and video clip.
- When newer footage contradicts it, the answer changes. When the footage stops, it says so.

## 3. Live Walkthrough (60s), Run demo
Scenario: you're a warehouse safety supervisor watching camera 2.

**Beat 1, what's happening now?**
- "Is the forklift on camera 2 moving?"
- RECEIPTS: moving. Point at the cited clip and its timestamp.

**Beat 2, what changed?** (Space)
- Newer footage arrives. The old claim is superseded, not deleted.
- The answer flips to parked, with the old and the new clip side by side.

**Beat 3, what would a typical agent say?** (Space)
- A retrieval-only agent, same captions, still says "moving".
- "Same model, same captions. It just doesn't know what time it is."

**Beat 4, what if the camera stops?** (Space, then pause)
- Ask again three minutes after the last footage.
- RECEIPTS: last seen parked at 07:57, no footage since, flagged STALE.
- "There's a difference between knowing it was safe and knowing it is safe."

## 4. Proof (20s)
- Scroll to the Eval chart. "We filmed this hall today and hand-labeled 6 questions about where someone was."
- "After the camera stopped, four baselines answered with confidence: latest clip, retrieve-everything, and a short memory window. Only RECEIPTS flagged it."
- If the VSS agent card is showing: "We build on the event's own VSS agent and add time. Asked the same question, it answers with no timestamp and no stale flag."
- Say "6 questions". Never a percentage.

## 5. How we built it (10s)
- NVIDIA Cosmos Reason describes every video segment.
- VAST DataEngine ingests and indexes it; models run on CoreWeave GPUs.
- RECEIPTS turns each description into a claim (what, where, when, which clip) and audits every change.
- Weights & Biases tracks the eval. Built with Cursor.

## 6. Beyond Warehouses (10s)
- Hospitals: is the emergency corridor blocked right now?
- Construction: were workers exposed to the hazard, and when?
- Retail: how long was the spill unattended?

## 7. Closing (5s)
- "Ask what happened. See the evidence. Know if it's still true."

---
Notes for Q&A
- Warehouse footage is NVIDIA synthetic data; the venue clip is real, filmed today.
- The retrieval-only agent is our own simulated baseline, not a named product.
- The VSS agent card uses the event's agent-qa skill (search-and-answer endpoint when /agent/ask errors).
- Eval: same claims for every system, so it tests time handling, not caption quality. n=6.
