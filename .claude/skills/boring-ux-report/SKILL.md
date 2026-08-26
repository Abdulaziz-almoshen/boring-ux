---
name: boring-ux-report
description: Turn a Boring UX session folder (SESSION-AI.md + audio/gaze/mouse/events) into the full evidence-based UX report (HTML + PDF). Use when the user points at a session-* folder or asks to analyze a recorded usability session.
---

# Boring UX → full UX report

You are a senior UX researcher + product manager. The user has a `session-…/` folder recorded by Boring UX. Produce the complete, evidence-based UX report.

## Steps

1. **Locate the session folder** (the user names it, or find the newest `session-*/`). Read `SESSION-AI.md` first — it contains the metadata, data-quality notes, and the unified timeline (gaze + mouse + events on one clock). The full-resolution data is in `gaze.csv`, `mouse.csv`, `events.csv`, `session.json`.

2. **Transcribe the audio** (local, private) if no `transcript.srt` exists yet:
   ```bash
   ffmpeg -i audio.webm -ar 16000 -ac 1 /tmp/a.wav
   whisper-cli -m <ggml-large-v3-turbo.bin> -f /tmp/a.wav -l auto -osrt -of transcript
   ```
   (Install via `brew install whisper-cpp ffmpeg`; download the model from huggingface `ggerganov/whisper.cpp` if missing.) Collapse repeated identical lines (silence hallucinations). Translate quotes to English but keep the original.

3. **Fuse** every spoken segment with the gaze during it (same clock, t=0 = recording start): dominant region/cell, on-screen %, scanning intensity. Compute: L/C/R dwell, 3×3 heatmap, fixations (>1s = deep), silent-gap latency (pauses ≥4s + what the eyes did), per-page/per-field timing, frustration signals, gaze-vs-mouse lead/lag. Classify each segment's intent (action / seeking-data / confused) and pool gaze per intent.

4. **Write ONE self-contained HTML report** (inline CSS, print-ready) with exactly these sections: title+product · overall grade /10 with sub-scores + verdict · executive summary (P0/P1/Keep) · method & confidence (state `gazeAccuracyPx` from session.json; flag uncalibrated data as directional; flag missing events.csv) · journey map (stage·time·emotion·gaze·opportunity) · attention analytics as inline SVG (gaze ribbon orange=L blue=C green=R grey=away + engagement + searching lines) · feature scorecard (gaze L/C/R, on-screen %, search/min, signal, friction 0–100, recommendation) · findings P0→P1→Keep→P2 each with quote + eyes-at-that-moment + timing · latency table · RICE backlog (ticket-ready) · intent heatmaps conclusion ("where to place actions, data, filters") · appendix: every fused segment verbatim with gaze + latency gap.

   Design tokens: text `#1a2233`, muted `#6b7488`, borders `#e6eaf2`, P0 `#d33`, P1 `#e8871e`, accent `#4f8cff`, delight `#0a9d54`; original-language quotes in a green-left-border box (`direction:rtl` for Arabic), English italic beneath; table headers `#f0f3f9`.

5. **Save** `report.html` in the session folder and render a PDF:
   ```bash
   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu \
     --no-pdf-header-footer --print-to-pdf=report.pdf report.html
   ```

Be honest throughout: n=1 unless more sessions; webcam gaze is directional within the measured accuracy radius; never invent quotes or events not present in the data.
