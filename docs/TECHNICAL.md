# Boring UX — technical notes

Everything the README leaves out. For the eye-tracking analysis design itself (coordinate frames, mirroring, mapping,
detectors, validation) see [GAZE-FROM-VIDEO-DESIGN.md](GAZE-FROM-VIDEO-DESIGN.md).

## Architecture

```
Browser extension (Chrome/Edge, MV3)                Local processing service (Python, 127.0.0.1:7331)
 ├─ records face.webm, audio.webm, screen.webm       ├─ bux-analyze-video  face video → gaze / face / expressions / moments / quality
 ├─ logs mouse, clicks (+target rect), scroll,       ├─ bux-report         computed scaffold of the report (SVG figures, tables, appendix)
 │  pages, sign self-test events, viewport/screen    ├─ claude -p          writes findings / recommendations into the scaffold
 ├─ saves one folder: Downloads/boring-ux/<site>-<time>/   ├─ headless Chrome  report.pdf
 └─ on Stop: POST /jobs → panel shows the job        └─ opens the report; jobs persisted, pause/resume/retry, survive restarts
```

- The extension runs inside the real page (no iframe, so any site works) and captures clicks/mouse natively.
- The service is installed as a launch agent (`tools/install-daemon.sh`) and only listens on loopback. The extension talks to it
  through its background service worker, so the tested page's CSP cannot interfere.
- The live gaze dot in the panel uses WebGazer.js (region-level). The **report's gaze comes from the recorded video**, analysed offline.

## The offline analysis (`tools/bux-analyze-video.py`)

Implements the design document. Per session:

1. **Ingest** — validates/repairs `face.webm` (base64-text recordings → `tools/recover-webm.py`; junk before the EBML header → sliced;
   duplicate-heavy timestamps → synthesized from frame index), remuxes, and reads true per-frame timestamps (t = 0 = recording start).
2. **Models** — MediaPipe FaceLandmarker at 15 Hz (face, iris, blink via EAR, head pose from the forward vector, 52 expression
   blendshapes) and L2CS-Net at 10 Hz on landmark-derived face crops (gaze direction; heads bound by name; startup flip self-test).
3. **Screen mapping** — angles → display plane using camera-at-top geometry and a distance estimate from inter-ocular pixels;
   per-session bias from a self-centring prior and, when clicks exist, from click residuals; 10 Hz region state machine (3×3 grid,
   hysteresis, off-screen / keyboard / away states); per-second votes.
4. **Fusion** — 1 Hz `gaze-ai.csv` joining gaze, mouse, clicks, scroll, page, speech, expression z-scores and a quality grade.
5. **Moments** — SEARCHING, FOUND-THEN-ACTED, MISS-THEN-CORRECT, LOOK-AWAY, CONFUSION, FRUSTRATION with evidence.
6. **Quality** — sign self-tests, click-consistency vs permutation chance (gates how firmly the report may speak), per-second grades.

Outputs in `<session>/analysis/`: `gaze-ai.csv`, `expressions.csv`, `moments.json`, `quality.json`, `frames.csv`, `transcript.srt`,
`report-scaffold.html`, `report-data.json`, `debug/` (annotated frames).

**Speed (Apple Silicon, measured):** analysis ≈ 0.3× the recording length (10 min → ~3 min; CPU-only ≈ 10× slower); transcription
≈ 20× real time; report writing a few minutes. First run downloads ~1.7 GB of models.

## Manual use (without the service)

```bash
brew install uv ffmpeg whisper-cpp
bash tools/setup-analysis.sh --with-whisper          # env + models (add --with-daemon for the login service)
source ~/Desktop/gaze-ai/.venv/bin/activate
python3 tools/bux-analyze-video.py ~/Downloads/boring-ux/<site>-<time>
python3 tools/bux-report.py       ~/Downloads/boring-ux/<site>-<time> --product "Name"
```
Then run the `boring-ux-report` skill in Claude Code (`.claude/skills/boring-ux-report/SKILL.md`) to fill the findings and render the PDF.
The service does exactly these steps; `python3 tools/bux-daemon.py` runs it in the foreground.

## Session folder

| File | What it is |
|---|---|
| `face.webm` / `audio.webm` / `screen.webm` | webcam, microphone, optional screen (VP9/Opus) |
| `mouse.csv`, `events.csv` | cursor path; clicks (x, y, target text, rect, clickable), scroll, pages, self-test markers |
| `gaze.csv`, `session.json`, `SESSION-AI.md` | live WebGazer samples; viewport/screen/window geometry; the human-readable summary |
| `analysis/` | everything produced by the offline analysis (above) |
| `report.html`, `report.pdf` | the finished report |

Recordings made before Sept 9, 2026 whose `face.webm` won't play were saved as base64 text (a data-URL bug); `python3 tools/recover-webm.py <folder>` restores them.

## Models & licenses

Everything runs locally and is free to run. Licenses differ:

| Component | License | Commercial use |
|---|---|---|
| MediaPipe FaceLandmarker | Apache-2.0 | ✅ |
| whisper.cpp + Whisper `large-v3-turbo` | MIT (code and weights) | ✅ |
| L2CS-Net code | MIT | ✅ |
| **L2CS-Net weights** (trained on Gaze360) | Gaze360 Research License — **non-commercial** | ⚠️ research / internal evaluation only |
| WebGazer.js (live dot) | GPLv3 (LGPLv3 for companies under $1M valuation) | ✅ copyleft — the extension bundle inherits it |
| PyTorch, OpenCV, PyAV, ffmpeg (binary) | BSD / Apache-2.0 / BSD / LGPL-GPL | ✅ |
| Claude (writes the findings) | your Claude Code plan / API usage | the only paid step |

If the gaze-weights restriction matters, a MediaPipe-only gaze signal (iris + eye-look blendshapes → left/center/right, Apache-2.0) is the
commercial-clean alternative — coarser than L2CS. Sources: [Gaze360 license](https://github.com/erkil1452/gaze360/blob/master/LICENSE.md),
[L2CS-Net](https://github.com/ahmednull/l2cs-net), [WebGazer license](https://github.com/brownhci/WebGazer/blob/master/LICENSE.md).

## Honest limitations

- Webcam gaze error is ≈ 10° uncalibrated (≈ 8 cm at 55 cm) and ≈ 5–8° after click-based bias correction. Columns (L/C/R) are right
  roughly 7 times in 10, specific 3×3 cells about half the time; looking-away and keyboard glances are reliable. The report's wording is
  gated by each session's click-consistency score and quality grades.
- Expression cues are cue-level (brow lowering, squint, lip press, smile) — never validated emotion recognition.
- Eye tracking pauses when the recording tab is hidden; glasses glare, backlighting and large head turns lower quality (flagged per second).
- n = 1 per session unless several sessions are combined.

## The original localhost recorder

`index.html` + `analyze.html` are an earlier in-browser recorder that hosts a site in an iframe (needs `tracker.js` on the page, can't load
sites that block framing). It is kept for local dev sites; the extension is the product.
