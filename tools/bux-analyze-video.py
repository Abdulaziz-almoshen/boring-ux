#!/usr/bin/env python3
"""
bux-analyze-video — offline AI post-processing of a Boring UX session's face.webm.

Implements docs/GAZE-FROM-VIDEO-DESIGN.md: MediaPipe (face, blink, head pose, blendshapes) +
L2CS-Net gaze (heads bound by name, flip self-test), calibration-free angle→screen mapping
with click-based bias correction, 10 Hz region states, per-second fusion with mouse/clicks/
transcript, UX moments, quality grades and click-consistency gating.

Usage:
  source ~/Desktop/gaze-ai/.venv/bin/activate
  python3 tools/bux-analyze-video.py <session-folder> [--fps 10] [--limit-s N] [--whisper-model ggml.bin]

Outputs → <session>/analysis/: gaze-ai.csv (1 Hz fused), frames.csv, expressions.csv,
moments.json, quality.json, transcript.srt (if a whisper model is available), debug/*.jpg
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bux_gaze.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
