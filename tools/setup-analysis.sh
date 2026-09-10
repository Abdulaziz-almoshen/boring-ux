#!/usr/bin/env bash
# One-command setup for the offline AI analysis (macOS, Apple Silicon or Intel).
#   bash tools/setup-analysis.sh                # env + gaze/face models (~1.1 GB incl. torch)
#   bash tools/setup-analysis.sh --with-whisper # also the speech model (+1.6 GB) for transcripts
# Everything goes to $BUX_AI_DIR (default ~/Desktop/gaze-ai). Re-runnable; skips what exists.
set -euo pipefail
# Lives in ~/.boring-ux (NOT Desktop/Documents/Downloads: macOS blocks background services from those folders).
DIR="${BUX_AI_DIR:-$HOME/.boring-ux}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WITH_WHISPER=0; WITH_DAEMON=0; WITH_LLM=0
LLM_MODEL="${BUX_LLM_MODEL:-gemma3:12b}"          # local report writer (≈8 GB); never a cloud model
for a in "$@"; do case "$a" in --with-whisper) WITH_WHISPER=1;; --with-daemon) WITH_DAEMON=1;; --with-llm) WITH_LLM=1;; --all) WITH_WHISPER=1; WITH_DAEMON=1; WITH_LLM=1;; esac; done
mkdir -p "$DIR/models"; cd "$DIR"
echo "▶ Boring UX analysis env → $DIR"

# Python 3.12 venv (mediapipe has no wheels for 3.13+). uv fetches 3.12 itself; otherwise python3.12 must exist.
if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null 2>&1; then uv venv --python 3.12 .venv
  elif command -v python3.12 >/dev/null 2>&1; then python3.12 -m venv .venv
  else echo "✗ need Python 3.12: 'brew install uv' (recommended) or 'brew install python@3.12'"; exit 1; fi
fi
# shellcheck disable=SC1091
source .venv/bin/activate
if command -v uv >/dev/null 2>&1; then PIP="uv pip install"; else PIP="pip install -q"; fi

echo "▶ Python packages (pinned set that works together)"
$PIP "torch>=2.4" "numpy>=2,<3" "scipy>=1.18" "opencv-python<5" "opencv-contrib-python<5" "mediapipe==0.10.14" "av>=12" \
     "git+https://github.com/edavalosanaya/L2CS-Net.git@main"      # both opencv packages pinned: mediapipe pulls opencv-contrib and 5.x would shadow 4.x

echo "▶ Models"
[ -s models/L2CSNet_gaze360.pkl ] || curl -L --progress-bar -o models/L2CSNet_gaze360.pkl \
   "https://huggingface.co/tianfxc/l2cs/resolve/main/L2CSNet_gaze360.pkl"                      # 96 MB, Gaze360 ResNet50 (official weights, HF mirror)
[ -s models/face_landmarker.task ] || curl -L --progress-bar -o models/face_landmarker.task \
   "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task"   # 3.8 MB
if [ "$WITH_WHISPER" = 1 ]; then
  [ -s models/ggml-large-v3-turbo.bin ] || curl -L -C - --progress-bar -o models/ggml-large-v3-turbo.bin \
     "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"      # 1.6 GB
  # Silero voice-activity model (0.9 MB): whisper only decodes speech regions, so silence never turns into hallucinated text
  [ -s models/ggml-silero-v5.1.2.bin ] || curl -L -C - -sS -o models/ggml-silero-v5.1.2.bin \
     "https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin"
fi

echo "▶ System tools"
for t in ffmpeg ffprobe; do command -v $t >/dev/null 2>&1 || echo "  ✗ $t missing → brew install ffmpeg"; done
command -v whisper-cli >/dev/null 2>&1 || echo "  ✗ whisper-cli missing (needed for transcripts) → brew install whisper-cpp"

echo "▶ Smoke test"
python - <<'EOF'
import torch, mediapipe, av, cv2, numpy
from l2cs import getArch
print(f"  torch {torch.__version__} (MPS={torch.backends.mps.is_available()}) · mediapipe {mediapipe.__version__} · opencv {cv2.__version__} · numpy {numpy.__version__} · av {av.__version__} · l2cs OK")
EOF
if [ "$WITH_LLM" = 1 ]; then
  echo "▶ Local report-writing model ($LLM_MODEL via Ollama — runs on this Mac, no cloud, no API key)"
  command -v ollama >/dev/null 2>&1 || brew install ollama
  if ! curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    (ollama serve >/dev/null 2>&1 &) ; for i in 1 2 3 4 5 6 7 8 9 10; do curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break; sleep 1; done
  fi
  if ollama list 2>/dev/null | grep -qE "^(gemma3|qwen3|qwen2\.5|llama3\.1)[:[:space:]]"; then
    echo "  a suitable local model is already installed — skipping the download"
  else
    ollama pull "$LLM_MODEL"                                                              # ≈8 GB, one time
  fi
fi
if [ "$WITH_DAEMON" = 1 ]; then
  echo "▶ Local processing service (launch agent, starts at login)"
  bash "$REPO/tools/install-daemon.sh"
  command -v claude >/dev/null 2>&1 || echo "  ⚠ 'claude' CLI not found — reports will contain the data scaffold without written findings until Claude Code is installed"
fi
echo "✓ Ready."
if [ "$WITH_DAEMON" = 1 ]; then
  echo "    Record with the extension → Stop & save → the report opens automatically (service on 127.0.0.1:7331)."
else
  echo "    Analyze a session: source $DIR/.venv/bin/activate && python3 tools/bux-analyze-video.py ~/Downloads/boring-ux/<site>-<time>"
fi
