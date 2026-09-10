---
name: boring-ux-report
description: Turn a Boring UX session folder (face.webm + audio + mouse/events) into the full evidence-based UX report (HTML + PDF) — the "Detailed Usability Findings & UX Recommendations" format. Use when the user points at a session folder (Downloads/boring-ux/<site>-<time>/ or session-*/) or asks to analyze a recorded usability session.
---

# Boring UX → full UX report

You are a senior UX researcher + product manager. The user has a Boring UX session folder. Produce the complete,
evidence-based report in the exact format of `~/Desktop/UX/UX-Recommendations-SehaMedical.pdf` (Parts A–E, P0/P1/Keep/P2,
latency table, action list, intent conclusion, fused-timeline appendix). Everything mechanical is generated for you;
you supply the judgment inside a fixed scaffold.

## Pipeline (run in order; each step is idempotent)

1. **Locate the session** (user names it, else newest folder). It must contain `face.webm` (mandatory). If `face.webm`
   starts with ASCII like `opus;base64,` run `python3 tools/recover-webm.py <folder>` first.

2. **Video analysis** — if `<session>/analysis/gaze-ai.csv` is missing or older than `face.webm`:
   ```bash
   source ~/.boring-ux/.venv/bin/activate          # (legacy installs: ~/Desktop/gaze-ai/.venv)
   python3 tools/bux-analyze-video.py <session> [--whisper-model ~/.boring-ux/models/ggml-large-v3-turbo.bin]
   ```
   Sessions processed by the local service live in `~/.boring-ux/sessions/<name>/` (the user's copy is in `~/Downloads/boring-ux/<name>/`).
   Writes `analysis/{gaze-ai.csv, expressions.csv, moments.json, quality.json, frames.csv, transcript.srt?, debug/}`.
   It aborts on a failed L2CS flip self-test — never work around that.

3. **Transcript** — if `analysis/transcript.srt` is missing and a whisper model exists, re-run step 2 with `--whisper-model`.
   If no model is available, proceed and state "no transcript" in Method & confidence (speech evidence unavailable).
   Never invent quotes.

4. **Scaffold** — `python3 tools/bux-report.py <session> --product "<Product name>"` →
   `analysis/report-scaffold.html` (design tokens, method & confidence, attention SVG, pre-filled scorecard/latency/moments/
   intent tables, complete appendix) and `analysis/report-data.json` (all computed stats + `placeholders`).

5. **Fill the placeholders** in `report-scaffold.html` and save as `<session>/report.html`. Read `report-data.json`,
   `gaze-ai.csv` (per-second: gaze cell/state/conf, mouse cell, clicks, speech, expression z-scores, quality grade),
   `moments.json`, and the transcript. Do NOT alter generated tables, the SVG, or appendix rows — only fill `{{…}}`
   and the empty judgment cells. Placeholders:
   - `GRADE_TABLE` — rows for: AI & intelligence · Visual design · Data clarity · Delight · Task efficiency · Actionability ·
     Discoverability, each `<tr><td>dim</td><td>x.x</td><td>evidence</td></tr>`.
   - `JOURNEY_SUMMARY`, `SIGNAL`/`FRICTION`/`RECOMMENDATION` per scorecard row (Signal ∈ Delight/Friction/Confusion/Request;
     Friction 0–100), `PRODUCT_INSIGHTS`, `ROADMAP_ROWS`, `INSTRUMENT_NEXT`.
   - `FINDINGS_P0`, `FINDINGS_P1`, `DELIGHTERS`, `FINDINGS_P2` — use the finding-block template embedded as an HTML comment:
     `.finding` > `h3` with `<span class="p0|p1|p2|keep">`, `Page/Task` line, `<span class="ar">` original quote (RTL box; use it
     for any language, English too), `<div class="en">[m:ss] translation</div>`, `<div class="eyes">👁 Eyes: … ⏱ …</div>`,
     `<div class="rec">Fix: …</div>`. Every finding cites the triple **said · eyes · time**; eyes come from `gaze-ai.csv`
     cells/states and `moments.json` dwell maps.
   - `LATENCY_MEANING` per latency row, `ACTION_LIST_ROWS` (# · Pri · Page · Action · Evidence), `INTENT_READS_AS`,
     `PLACEMENT_TABLE`, `PER_NEED_MAP`, `APPENDIX_ENGLISH` (translate each Arabic appendix row; leave English rows).

6. **Wording tier (mandatory)** — from `quality.json.click_consistency.tier`, echoed in the scaffold header:
   - `regions`: columns/halves firmly; 3×3 cells with "probably".
   - `likely`: columns with "likely"; no specific-cell claims.
   - `unvalidated` (also when `signs.orientation == INVERTED`): every gaze statement reads "estimated (unvalidated)";
     build findings on mouse, clicks, transcript, and LOOK-AWAY/keyboard events; grades capped at C for gaze-based claims.
   Expression cues are cue-level only ("brow lowering rose to z = 2.1"), never "the user felt X".

7. **PDF** — `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu --no-pdf-header-footer --print-to-pdf=<session>/report.pdf <session>/report.html`

8. **QA before returning**: no `{{` left in `report.html`; every finding has quote + eyes + time; Method & confidence shows
   usable-%, click-consistency, tier, flags; the footer disclaimer is present; n = 1 stated unless sessions were combined.

## Answering "was the user looking for X at <region>?"
Use the SEARCHING/FOUND-THEN-ACTED moments' `gaze_dwell` for the window before the relevant click, compare with the
session baseline dwell for that cell/column (report-data `stats.gaze_cells`), and report per the design's template
(likelihood ratio + column/cell + confidence), e.g. "During the 7 s before clicking *Submit*, 61 % of confident gaze was in
the right column (session baseline 38 %) — likely searching on the right; top-right unconfirmed." See docs/GAZE-FROM-VIDEO-DESIGN.md §6.
