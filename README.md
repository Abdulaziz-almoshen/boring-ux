<h1 align="center">😴 Boring UX</h1>
<p align="center"><b>Open-source webcam eye-tracking usability lab.</b><br>
Record a session, watch exactly where people looked, and turn it into a UX report — no lab, no hardware, no SaaS, no data leaving the browser.</p>

<p align="center">
<img src="screenshots/live-recording.png" width="90%" alt="Live recording — gaze heat trail over the page, WebGazer face mesh, and the live panel"/>
<br><em>Live session — the precise-pixel <b>heat trail</b> follows the eyes across the page, the WebGazer <b>face mesh</b> tracks the participant, and the panel shows region + frustration signals in real time.</em>
</p>

<p align="center">
<img src="screenshots/replay-report.png" width="90%" alt="Session replay — gaze replay synced to audio and video"/>
<br><em>Replay any session — the <b>gaze replay</b> plays synced to audio + video, with live left/center/right, 3×3 cell, and pixel readouts at the playhead.</em>
</p>

---

## For product managers & designers

You shipped a new flow. Does it actually **work** — not "did QA pass," but does a real human's **eyes** land where you intended, or do they hunt, hesitate, and rage-click? Analytics tell you *what* people clicked. They never tell you **where attention went, what confused them, or why they gave up.** Boring UX does — in an afternoon, for free.

1. **Record** a real person using your new design, feature, or redesign — their **screen, voice (think-aloud), face, and exactly where their eyes go.**
2. **Watch it back** — see the gaze miss your new button, the moment confusion hits, the 90 seconds they stall on one form field.
3. **Get the report** — every finding tied to **what they said × where they looked × how long it took**, scored and turned into a **RICE-prioritized backlog** you drop straight into Jira/Linear.

The output isn't a dashboard — it's a **decision**: *"Users can't find 'New Leave' — 8-minute hunt, eyes drifting off-screen → move it top-right. P0."* Hand that to engineering on Monday.

> No \$30k eye-tracker. No SaaS seat. No recruiting agency. No data leaving your laptop. Just **proof of where attention goes** — the one thing you can't fake and can't measure any other way.

## Why "Boring UX"?

Great UX is **boring** — invisible, frictionless, nobody notices it. Bad UX is exciting: people hunt, hesitate, rage-click. **Boring UX finds the exciting parts so you can make them boring.** It watches a real user's **eyes, voice, screen, and clicks**, then shows you where attention went, where they got stuck, and what to fix.

Usability testing normally means a $30k eye-tracker or a per-seat SaaS. This does the 80% that matters with a **webcam and a browser** — and it's yours.

## What it does

- 🧩 **Runs on any website** — a Chrome/Edge extension that works inside the real page. No server, no code snippet, no iframe.
- 🎥 **Records** webcam face + mic audio (+ optional screen), all **streamed straight to disk** (can't corrupt, even on long sessions).
- 👁 **Two gaze layers** — coarse **left / center / right** regions, and a **precise-pixel heat trail** — from the webcam via [WebGazer](https://webgazer.cs.brown.edu/).
- ⏱ **Auto-captures** clicks, mouse path, per-page time, and **frustration signals** (dead clicks, rage clicks, scroll-thrash) — natively, no snippet needed.
- 🧠 **Saves one self-contained `SESSION-AI.md`** in every session folder — hand it to Claude/ChatGPT (or run Claude Code) to get a **full, evidence-based UX report** that fuses gaze + what they said + timing.

100% client-side. Your recordings never leave your machine.

## Quick start — the browser extension

Boring UX is a **Chrome/Edge extension**. It runs *inside the real page* (like Hotjar/FullStory), so it works on **any website on the web** — no server, no `tracker.js` snippet, no iframe — and it captures clicks + mouse natively.

### Install once (Chrome / Edge)
1. Download this repo — green **Code ▸ Download ZIP**, then unzip (or `git clone`).
2. Open **`chrome://extensions`** → turn on **Developer mode** (top-right).
3. Click **Load unpacked** → select the **`extension/`** folder inside this repo.
4. Boring UX appears in your toolbar (pin it via the puzzle-piece icon). ✅ You never repeat this.

### Record a session — the one journey
| # | Do this |
|---|---|
| 1 | Open the **website you want to test** — logged in, on the page where the task starts. |
| 2 | Click the **Boring UX** toolbar icon → a small panel appears in the page. |
| 3 | **Enable camera → Calibrate** (look at the 13 dots; it shows your accuracy). |
| 4 | Click **● Start** → the participant does the task **while talking out loud**. They never see their face or the gaze dot. |
| 5 | Click **■ Stop & save** → camera shuts off, the panel resets, and the session saves. |

**Where it saves:** one folder → **`Downloads/boring-ux/<site>-<date-time>/`** containing
`gaze.csv` · `mouse.csv` · `events.csv` · `session.json` · **`SESSION-AI.md`** · `face.webm` · `audio.webm` (+ `screen.webm` if you shared the screen).

> ⚠ Keep the tab in front while recording — eye tracking pauses on hidden tabs (it warns you and marks the gap). The **webcam preview is hidden by default** (still recorded to `face.webm`); a toggle shows a small corner preview for framing.

### Get the report
Open **`SESSION-AI.md`** from the session folder — it's **one self-contained file**: the session data **plus** the complete instructions to turn it into the graded UX report (transcription → gaze/speech fusion → dashboard graphs → feature scorecard → findings → RICE backlog → intent heatmaps, with an example and the design spec).

- **Paste into Claude / ChatGPT:** hand over `SESSION-AI.md` (+ the folder). It has everything, including the one local command to transcribe the audio.
- **Run the bundled skill:** open **Claude Code** in the session folder and say *"analyze this session"* — the `.claude/skills/boring-ux-report` skill transcribes, fuses, and writes `report.html` + `report.pdf`.

You get the complete report: overall grade, attention graphs, feature-by-feature scorecard, intent heatmaps ("where do users look when confused vs. acting"), findings, a RICE backlog, and a full fused transcript appendix.

### Locked-down sites (`Permissions-Policy: camera=()`)
A few sites disable the camera for everything in their page via a `Permissions-Policy` header — so even with camera permission granted, in-page tracking is blocked. For these, the extension applies a **local testing override**: on activation it strips that header (and `X-Frame-Options`) from the response **in your browser only**, for the domain you're testing, then reloads so the camera works.

> ⚠️ **Use responsibly.** This override changes **nothing** on the site's servers and affects **no other user** — it only alters the response your own browser enforces, for sites you own or are **authorized to test**. It's the same technique QA tools (e.g. Requestly) use for internal testing. It is **not** a way to attack a site, and it should stay **off for normal browsing** (it's scoped per-domain and is removed when you disable the extension). Don't use it on sites you don't have permission to test.

## What a session folder contains

| File | What it is |
|---|---|
| **`SESSION-AI.md`** | **the one file you need** — metadata + accuracy + unified gaze/mouse/event timeline **and** the full instructions to generate the report |
| `gaze.csv` | every gaze sample: `t_ms, x, y, region (L/C/R), cell (3×3)` |
| `mouse.csv` | continuous cursor path |
| `events.csv` | pages, clicks (+ gaze region), field fill-times, dead/rage/scroll signals |
| `session.json` | duration, viewport, gaze distribution, signals, `gazeAccuracyPx` |
| `face.webm` | webcam face + voice |
| `audio.webm` | audio only |
| `screen.webm` | screen recording + voice (only if you shared the screen) |

## Honest limitations

- **Webcam gaze is region-accurate, not pixel-perfect** (~50–150px). The recorder now fights this three ways: a required **13-point calibration**, a **validation pass that measures your real accuracy in px** (saved into `session.json` as `gazeAccuracyPx` so reports can weight the data), and **continuous recalibration from every in-page click** during the session. Still: great for regions and heatmaps, not for telling two adjacent buttons apart — hardware trackers exist for that.
- **Calibrate every session** — the panel warns you before starting uncalibrated; the measured accuracy score tells you when to redo it (aim for <140px).
- **Keep the recording tab in front** — Chrome pauses eye tracking on hidden tabs (the panel warns you and marks the gap).
- The report's *spoken* analysis needs a transcription step (Whisper, one local command — private; the exact command is inside `SESSION-AI.md`).

## How it works

The **extension** injects a content script into the real page, runs WebGazer on the webcam for the two gaze layers, records `MediaRecorder` tracks (face/audio/optional screen) streamed to disk, and logs gaze + clicks + mouse + page changes on one clock — natively, so no snippet is required. On stop it writes the whole session into one `Downloads/boring-ux/<site>-<time>/` folder via the `chrome.downloads` API, with a self-contained `SESSION-AI.md`. Everything is vanilla JS — no build step, WebGazer is bundled.

<details>
<summary><b>Optional: the localhost recorder</b> (only for a site you're building locally)</summary>

The repo also ships a standalone in-browser recorder (`index.html` + `analyze.html`) that hosts your site in an iframe. It's optional and **only** useful for a local dev site — it can't load sites that block framing, and it needs the `tracker.js` snippet on your page to capture clicks. For everything else, use the extension above.

```bash
python3 -m http.server 8000   # then open http://localhost:8000 in Chrome
```
</details>

## Contributing

Issues and PRs welcome — ideas: continuous mouse-move capture, in-browser transcription (Whisper WASM), AOI (area-of-interest) tagging, multi-session aggregation, a hosted demo.

## License

[MIT](LICENSE) — do what you like, no warranty.
