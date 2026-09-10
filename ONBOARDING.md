# Set up Boring UX with Claude Code — one message

Most people set this up by pasting **one prompt into Claude Code**. Claude installs everything (models, pipeline, the
local processing service) and walks you through the two clicks it can't do for you. After that you never run a command:
**record with the extension → click Stop → the report opens by itself.**

## The prompt (copy everything in the box)

```text
Set up Boring UX (open-source webcam eye-tracking usability testing) on this Mac so I never have to run commands afterwards.

1. Clone https://github.com/Abdulaziz-almoshen/boring-ux into ~/Desktop/boring-ux (or update it if it exists) and cd into it.
2. Install system tools with Homebrew: uv, ffmpeg, whisper-cpp (install Homebrew first if missing).
3. Run: bash tools/setup-analysis.sh --all
   This creates the Python 3.12 env in ~/.boring-ux (it must NOT be under Desktop/Documents/Downloads — macOS blocks
   background services from those folders), downloads the gaze/face models and the speech model (~1.7 GB total), and
   installs the local processing service so it starts at login. Confirm the smoke test passes and that
   http://127.0.0.1:7331/health returns ok.
4. The report findings are written by LOCAL open-source models via Ollama, never by a cloud model: a text model writes the
   findings (Gemma 3 12B, or Qwen3 14B / Qwen 2.5 if one is already installed) and a vision model reads the screen recording
   (Qwen3-VL 8B, ~6 GB) so the report can name the button someone clicked instead of a screen region.
   setup-analysis.sh --all installs Ollama and pulls each only if none is present. Confirm http://127.0.0.1:7331/health shows
   "llm": {"backend": "ollama", "available": true}. Do not configure Claude or any API key for report writing.
5. Then guide me, step by step, through the two things you cannot click for me:
   a) Load the extension: open chrome://extensions, turn on Developer mode, click "Load unpacked", choose
      ~/Desktop/boring-ux/extension, then pin "Boring UX" in the toolbar.
   b) Do a 30-second test: open any website, click the Boring UX icon, Enable camera (allow the camera prompt),
      Calibrate, Start, press "Sign self-test", talk for a few seconds, click "Stop & save".
   After Stop, the panel should switch to a processing view and the report should open automatically when done.
6. If anything fails, read tools/ (and its comments) to diagnose, fix it, and re-verify. Finish by telling me,
   in plain language, how to run a real usability session and where reports are saved.
Note: the service runs a staged copy of the repo's tools from ~/.boring-ux/app (macOS blocks background services from
reading ~/Desktop). Whenever the repo is updated (git pull), re-run: bash tools/install-daemon.sh — and click the reload icon on the Boring UX card in chrome://extensions.
```

## What gets installed (all local, nothing leaves your Mac)
| Piece | Where | Purpose |
|---|---|---|
| Python env + models | `~/.boring-ux/` | gaze (L2CS-Net), face/expressions (MediaPipe), speech (whisper large-v3-turbo) |
| Processing service | `~/Library/LaunchAgents/com.boringux.daemon.plist` → `tools/bux-daemon.py` on `127.0.0.1:7331` | turns an uploaded recording into a report; resumable; the extension shows its progress. Sessions it processes live in `~/.boring-ux/sessions/` |
| Extension | `extension/` (unpacked) | records; on Stop saves the session to `Downloads/boring-ux/…`, uploads a copy to the service, shows processing, then saves and opens the report |

## After setup — the whole workflow for a product person
1. Open the site to test → click **Boring UX** → **Enable camera → Calibrate → Start** → (press **Sign self-test** once) → do the task **talking aloud** → **Stop & save**.
   While recording, the panel shows a **mic level bar** and **live captions** (local whisper) so you can see the microphone is working. **Stay on the recording tab**: Chrome freezes the camera while the tab is hidden, and those seconds are reported as *tab switched*, not as attention.
2. The panel turns into **Processing…** with stages, progress and time remaining. You can close the tab; it continues, and any tab shows the same job. Pause/resume if you need the Mac.
3. When done, `report.pdf` opens and is saved next to the recording in `~/Downloads/boring-ux/<site>-<time>/`. (The service keeps its working copy and the full `analysis/` folder in `~/.boring-ux/sessions/<site>-<time>/`.)

Timing on an Apple-Silicon Mac (24 GB), measured on 2026-09-10: a 69-second session was analysed in 23 s and written by the local model (Qwen3 14B) in about 3 minutes; a 14-minute session took 3 minutes of analysis and just under 10 minutes of writing. Rule of thumb: **about 4 minutes plus half the recording length.** Keep the recording tab in front and close heavy apps: the writer model needs ~12 GB of memory.