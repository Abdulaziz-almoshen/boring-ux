# Set up Boring UX with Claude Code — one message

Most people set this up by pasting **one prompt into Claude Code**. Claude installs everything (models, pipeline, the
local processing service) and walks you through the two clicks it can't do for you. After that you never run a command:
**record with the extension → click Stop → the report opens by itself.**

## The prompt (copy everything in the box)

```text
Set up Boring UX (open-source webcam eye-tracking usability testing) on this Mac so I never have to run commands afterwards.

1. Clone https://github.com/Abdulaziz-almoshen/boring-ux into ~/Desktop/boring-ux (or update it if it exists) and cd into it.
2. Install system tools with Homebrew: uv, ffmpeg, whisper-cpp (install Homebrew first if missing).
3. Run: bash tools/setup-analysis.sh --with-whisper --with-daemon
   This creates the Python 3.12 env in ~/Desktop/gaze-ai, downloads the gaze/face models and the speech model
   (~1.7 GB total), and installs the local processing service so it starts at login. Confirm the smoke test passes
   and that http://127.0.0.1:7331/health returns ok.
4. Verify the `claude` CLI is available (it writes the report findings automatically). If not, tell me.
5. Then guide me, step by step, through the two things you cannot click for me:
   a) Load the extension: open chrome://extensions, turn on Developer mode, click "Load unpacked", choose
      ~/Desktop/boring-ux/extension, then pin "Boring UX" in the toolbar.
   b) Do a 30-second test: open any website, click the Boring UX icon, Enable camera (allow the camera prompt),
      Calibrate, Start, press "Sign self-test", talk for a few seconds, click "Stop & save".
   After Stop, the panel should switch to a processing view and the report should open automatically when done.
6. If anything fails, read docs/ and tools/ in the repo to diagnose, fix it, and re-verify. Finish by telling me,
   in plain language, how to run a real usability session and where reports are saved.
```

## What gets installed (all local, nothing leaves your Mac)
| Piece | Where | Purpose |
|---|---|---|
| Python env + models | `~/Desktop/gaze-ai/` | gaze (L2CS-Net), face/expressions (MediaPipe), speech (whisper large-v3-turbo) |
| Processing service | `~/Library/LaunchAgents/com.boringux.daemon.plist` → `tools/bux-daemon.py` on `127.0.0.1:7331` | turns a finished recording into a report; resumable; the extension shows its progress |
| Extension | `extension/` (unpacked) | records; on Stop hands the session to the service and shows processing → opens the report |

## After setup — the whole workflow for a product person
1. Open the site to test → click **Boring UX** → **Enable camera → Calibrate → Start** → (press **Sign self-test** once) → do the task **talking aloud** → **Stop & save**.
2. The panel turns into **Processing…** with stages, progress and time remaining. You can close the tab; it continues, and any tab shows the same job. Pause/resume if you need the Mac.
3. When done, `report.pdf` opens. Everything is in `~/Downloads/boring-ux/<site>-<time>/` (`report.pdf`, `report.html`, `analysis/`).

Timing on an Apple-Silicon Mac: about **0.3× the recording length** for analysis (10 min → ~3 min) plus a few minutes for
the written findings.
