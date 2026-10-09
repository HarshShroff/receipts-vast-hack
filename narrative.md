# RECEIPTS demo narrative (about 2:15)

Driver: Zain (screen). Voice: Harsh. Eval questions in Q&A: Yuri.
Load the app and wait ~5 s for the video before clicking **Run demo** (check "DEMO 1/4" appears). Step with **Next ->** only, after each beat finishes. Don't free-type questions, and don't open Browse, .env or the VM on camera.

## 1. Hook (20s)
- 2.5 million nonfatal workplace injuries and illnesses in U.S. private industry in 2024 (BLS).
- Warehouses, hospitals, factories and construction sites are full of cameras.
- Video AI can now search that footage and answer with a clip.
- But it answers from whatever clip it finds, even one filmed before things changed. "The aisle is clear" from ten minutes ago is how someone gets hurt.

## 2. What RECEIPTS is (10s)
- A video agent with memory, that knows when its answers are out of date.
- Every answer comes with a receipt: the exact clip and the time.
- When new footage contradicts it, the answer changes. When the footage stops, it says so.

## 3. Walkthrough (60s): Run demo
You're a warehouse safety supervisor watching camera 2. The boxes on the video are YOLO detections from the event pipeline.

**Beat 1: what's happening now?**
- "Is the forklift on camera 2 moving?" The agent answers moving, and shows the exact clip it saw.
- "Every answer has a receipt: the clip and the time."

**Beat 2: what changed?** (Next)
- New footage arrives: the forklift has parked. The old answer is crossed out, not deleted, and both clips sit side by side.
- "It remembers what it saw and updates when the world changes."

**Beat 3: what would a typical agent say?** (Next)
- Same footage, a retrieval-only agent still says "moving".
- "It found a matching clip but never checked when it was filmed."

**Beat 4: what if the camera stops?** (Next, then pause)
- Three minutes after the last footage, ask again.
- RECEIPTS: last seen parked at 07:57, no footage since, flagged STALE.
- "There's a difference between knowing it was safe and knowing it is safe."

## 4. Proof (20s)
- Scroll to the Eval chart. "We filmed this hall today and hand-labeled 6 questions about where someone was."
- "After the camera stopped, four baselines answered with confidence: latest clip, retrieve-everything, and a short memory window. Only RECEIPTS flagged it."
- If the VSS agent card shows an answer: "We build on the event's own VSS agent and add time. Asked the same question, it answers with no timestamp and no stale flag."
- Say "6 questions". Never a percentage.

## 5. How we built it (10s)
- NVIDIA Cosmos Reason describes every video segment; YOLO11 boxes the people in it.
- VAST DataEngine ingests and indexes the footage; models run on CoreWeave GPUs.
- RECEIPTS turns each description into a claim (what, where, when, which clip) and audits every change.
- Weights & Biases tracks the eval. Built with Cursor.

## 6. Beyond warehouses (10s)
- Hospitals: is the emergency corridor blocked right now?
- Construction: were workers exposed to the hazard, and when?
- Retail: how long was the spill unattended?

## 7. Close (5s)
- "Ask what happened. See the evidence. Know if it's still true."

---
Q&A notes
- Warehouse footage is NVIDIA synthetic data; the venue clip is real, filmed today.
- YOLO11 labels people only (no forklift class), so boxes appear on workers.
- The retrieval-only agent is our own simulated baseline, not a named product.
- The VSS agent card uses the event's agent-qa skill (search-and-answer endpoint when /agent/ask errors).
- Eval: same claims for every system, so it tests time handling, not caption quality. n=6, labels by eye at 2 fps.
