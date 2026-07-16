# 🧠 Session → UX Report · AI Prompt

Paste everything below into **Claude** or **ChatGPT** (or run with **Claude Code** in this folder), and attach the session files listed. It will produce the full, evidence-based UX report — the same structure and design the tool ships with.

> **First, transcription.** The spoken analysis needs a transcript of `audio.webm`. If your model can't hear audio, transcribe it first (100% local, private):
> ```bash
> brew install whisper-cpp ffmpeg
> ffmpeg -i audio.webm -ar 16000 -ac 1 audio.wav
> whisper-cli -m ggml-large-v3-turbo.bin -f audio.wav -l auto -osrt -otxt -of transcript
> ```
> Then attach `transcript.srt` alongside the other files.

---

## PROMPT — copy from here ⬇

You are a **senior UX researcher + product manager**. I ran a moderated, think-aloud usability test with **webcam eye-tracking**. Analyze the attached session and produce a **single self-contained HTML report** (print-to-PDF ready).

### Inputs (attached)
- `gaze.csv` — per-sample gaze: `t_ms, screen_x, screen_y, page_x, page_y, h_region(L/C/R), cell(TL..BR)`. This is the eye-tracking (may be **uncalibrated** → treat absolute pixels as directional, not exact).
- `events.csv` — page changes, clicks (with `gazeRegion`), field fill-times, dead/rage clicks, scroll-thrash. **May be empty** if the tested site had no tracker snippet — say so honestly.
- `session.json` — duration, viewport, gaze distribution, frustration signals.
- `transcript.srt` — the think-aloud (any language; translate quotes to English + keep original).
- (optional) screen/face video frames.

### Method (do all of this)
1. **Fuse on one clock.** `gaze.csv`, `events.csv`, and `transcript.srt` share `t=0` = recording start. For **every spoken segment**, compute the gaze region/cell and on-screen % during it. Drop transcription silence-loops (repeated identical lines).
2. **Tie every finding to three signals:** *what they said* + *where the eyes were at that second* + *how long it took*.
3. **Classify each segment by intent:** taking an action / looking for data / confused-searching. Pool gaze per intent into a 3×3 attention grid → "where users look when they want to X".
4. **Compute:** L/C/R dwell, 3×3 heatmap, attention-over-time, on-screen % (engagement), scanning intensity (region-switches/min), fixations & long fixations (>1s), silent-gap **latency** (pauses ≥4s + what the eyes did during them), per-page time & per-field fill time (from events), frustration signals.
5. **Be honest:** flag uncalibrated gaze as directional; flag missing clicks/pages if `events.csv` is empty; n=1 unless more sessions.

### Report structure (produce exactly this)
1. **Title + product name.**
2. **Overall grade /10** with sub-scores (AI, visual design, data clarity, delight, task efficiency, actionability, discoverability) and a one-line verdict.
3. **Executive summary** (dark card): fix-first (P0), fix-next (P1), keep (delighters).
4. **Method & confidence** (sample size, honest caveats).
5. **Journeys** (per session): stage · time · emotion (emoji) · gaze/behaviour · opportunity.
6. **Attention analytics — graphed** (inline SVG): a per-session figure = gaze-region ribbon (orange=L, blue=C, green=R, grey=away) + engagement line + searching-intensity line + feature-phase bar.
7. **Feature-by-feature scorecard** (table): feature · time · gaze L/C/R bar · on-screen % · search/min · signal (Delight/Friction/Confusion/Request) · friction index (0–100) · recommendation.
8. **Product insights + feature attention×friction matrix + prioritised roadmap.**
9. **Detailed findings** P0→P1→Keep→P2, each: Arabic/original quote + English + 👁 eyes-at-that-moment + ⏱ timing + recommendation.
10. **Latency & timing** table (silent gaps, what the eyes did, meaning).
11. **RICE backlog** (Reach × Impact × Confidence ÷ Effort), ticket-ready.
12. **Conclusion — where the eyes go by intent:** three 3×3 heatmaps (action / data / confused) + a "where to place actions, data, filters" table.
13. **Appendix:** the complete fused timeline — every segment, verbatim, with gaze region/cell, on-screen %, and the latency gap before it. Nothing summarised.

### Design (match this — inline `<style>`, no external assets)
- Font: system sans-serif. Max-width ~960px, generous line-height.
- Colors: text `#1a2233`, muted `#6b7488`, borders `#e6eaf2`; P0 `#d33`, P1 `#e8871e`, P2/accent `#4f8cff`, delight/green `#0a9d54`.
- Original-language quotes in a green-bordered RTL-aware box (`direction:rtl` for Arabic) with the English translation beneath in italic muted text.
- 👁 "eyes" callout in a light-blue box. Tables with `#f0f3f9` headers. `@media print{}` for clean PDF.
- Region ribbon & heatmaps as inline SVG (no libraries).

Output the complete HTML in one file. Then I'll open it and **Print → Save as PDF**.

## ⬆ end of prompt
