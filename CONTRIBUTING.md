# Contributing to Boring UX

Thanks for helping make bad UX boring. 🙏 This is a tiny, dependency-free project — easy to hack on.

## Run it locally

```bash
git clone https://github.com/Abdulaziz-almoshen/boring-ux
cd boring-ux
python3 -m http.server 8000     # any static server
```
Open **http://localhost:8000** in Chrome (camera + screen capture need a secure context).
No build step, no `npm install` — it's vanilla HTML/JS.

## Project layout

| File | Role |
|---|---|
| `index.html` | the recorder (WebGazer + MediaRecorder streamed to disk, 2 gaze layers) |
| `tracker.js` | one-line snippet for the tested site — reports clicks/pages/field-times/frustration via `postMessage` |
| `analyze.html` | upload → replay a session → generate the quantitative report |
| `demo-form.html` | a sample instrumented site to test the whole loop |
| `ANALYZE-PROMPT.md` | the AI prompt that regenerates the full UX report |

## Coding style

- **Vanilla JS, no framework, no build.** Keep it hackable in a single file.
- Prefer small, readable functions; match the surrounding style.
- No new runtime dependencies without a very good reason (WebGazer from CDN is the only one).
- Everything stays **client-side** — no server, no telemetry, no data leaving the browser. That's the whole point.

## Ways to help

Check the [open issues](https://github.com/Abdulaziz-almoshen/boring-ux/issues) — look for `good first issue`. Big ones:
- Continuous **mouse-move** capture (replay a moving cursor)
- **In-browser transcription** (Whisper WASM) so the analyzer report includes the spoken analysis
- **AOI** (area-of-interest) tagging + time-to-first-fixation per element
- Multi-session aggregation / calibration accuracy

## Pull requests

1. Fork → branch → change → test in Chrome (record a short session, replay it, generate a report).
2. Keep PRs focused. Describe what you changed and how you verified it.
3. **Never commit recordings or participant data** — `.gitignore` already blocks `session-*/`, `*.webm`, `*.mp4`.

By contributing you agree your work is licensed under the repo's [MIT license](LICENSE).
