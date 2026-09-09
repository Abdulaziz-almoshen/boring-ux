#!/usr/bin/env python3
"""
bux-analyze-video — offline AI post-processing of a Boring UX session's face.webm.

STAGES 1–2 (implemented, verified on real sessions):
  1. Decode face.webm on the TRUE clock: each frame's container pts_time is its
     timestamp on the shared session clock (t=0 = recording start). Never use
     frame_index / fps — MediaRecorder webm timing is non-uniform (multi-second gaps seen).
  2. Per sampled frame, run two models:
       - MediaPipe FaceLandmarker: face present, landmarks (iris 468/473), head pose
         (4x4 matrix), eye openness / blink, 52 expression blendshapes.
       - L2CS-Net (Gaze360 ResNet50) on a landmark-derived face crop: gaze yaw/pitch.
         Runs on Apple GPU (MPS): the library's bundled RetinaFace assumes CUDA, so we
         disable it and patch the two attributes its __init__ then skips.
     Writes gaze-ai.csv and expressions.csv into the session folder.

STAGES 3–6 (added after the gaze-from-video design is finalized):
  3. angles -> screen 3x3 regions (mirroring resolution, neutral offset, distance estimate)
  4. per-second fusion with mouse/events/transcript   5. UX moments   6. quality + self-test

Usage:
  source ~/Desktop/gaze-ai/.venv/bin/activate
  python3 tools/bux-analyze-video.py <session-folder> [--fps 10] [--limit-s N] [--cpu]

Env: torch>=2.14, l2cs (github.com/edavalosanaya/L2CS-Net), mediapipe==0.10.14,
     numpy 2.x, scipy>=1.18, opencv-python<5. Models expected in $BUX_MODELS
     (default ~/Desktop/gaze-ai/models): L2CSNet_gaze360.pkl, face_landmarker.task
"""
import argparse
import csv
import math
import os
import subprocess
import sys
import time

import cv2
import numpy as np

MODELS = os.environ.get("BUX_MODELS", os.path.expanduser("~/Desktop/gaze-ai/models"))
L2CS_WEIGHTS = os.path.join(MODELS, "L2CSNet_gaze360.pkl")
MP_MODEL = os.path.join(MODELS, "face_landmarker.task")

# MediaPipe landmark indices
L_OUT, L_IN, L_UP, L_DN = 33, 133, 159, 145
R_OUT, R_IN, R_UP, R_DN = 263, 362, 386, 374
BLEND_PAIRS = [
    ("brow_down", ("browDownLeft", "browDownRight")),
    ("brow_inner_up", ("browInnerUp", "browInnerUp")),
    ("eye_squint", ("eyeSquintLeft", "eyeSquintRight")),
    ("smile", ("mouthSmileLeft", "mouthSmileRight")),
    ("lip_press", ("mouthPressLeft", "mouthPressRight")),
    ("jaw_open", ("jawOpen", "jawOpen")),
    ("blink", ("eyeBlinkLeft", "eyeBlinkRight")),
]


def frame_timestamps(video: str):
    """True per-frame pts_time (seconds) from the container — the shared clock."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "frame=pts_time", "-of", "csv=p=0", video],
        capture_output=True, text=True, check=True).stdout.split()
    return [float(x) for x in out]


def load_models(use_cpu: bool):
    import torch
    import torch.nn as nn
    from l2cs import Pipeline
    import mediapipe as mp
    from mediapipe.tasks import python as mpp
    from mediapipe.tasks.python import vision

    dev = torch.device("cpu" if use_cpu or not torch.backends.mps.is_available() else "mps")
    l2 = Pipeline(weights=L2CS_WEIGHTS, arch="ResNet50", device=dev, include_detector=False)
    l2.softmax = nn.Softmax(dim=1)                                   # skipped by __init__ without detector
    l2.idx_tensor = torch.arange(90, dtype=torch.float32).to(dev)   # (library quirk)
    lm = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
        base_options=mpp.BaseOptions(model_asset_path=MP_MODEL),
        running_mode=vision.RunningMode.IMAGE, num_faces=1,
        output_face_blendshapes=True, output_facial_transformation_matrixes=True))
    return l2, lm, mp, dev


def face_crop_224(frame_bgr, lms):
    h, w = frame_bgr.shape[:2]
    xs = [p.x * w for p in lms]; ys = [p.y * h for p in lms]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    s = max(x1 - x0, y1 - y0) * 1.3; cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    X0, Y0 = int(max(0, cx - s / 2)), int(max(0, cy - s / 2))
    X1, Y1 = int(min(w, cx + s / 2)), int(min(h, cy + s / 2))
    crop = cv2.cvtColor(frame_bgr[Y0:Y1, X0:X1], cv2.COLOR_BGR2RGB)
    return cv2.resize(crop, (224, 224)), (x1 - x0)


def extract(session: str, fps: float, limit_s: float, use_cpu: bool):
    video = os.path.join(session, "face.webm")
    if not os.path.exists(video):
        sys.exit(f"no face.webm in {session}")
    pts = frame_timestamps(video)
    l2, lm, mp, dev = load_models(use_cpu)
    step_s = 1.0 / fps
    cap = cv2.VideoCapture(video)
    gaze_rows, expr_rows = [], []
    i, next_t, t0 = -1, 0.0, time.time()
    while True:
        ok, fr = cap.read()
        if not ok or i + 1 >= len(pts):
            break
        i += 1
        t = pts[i]
        if limit_s and t > limit_s:
            break
        if t + 1e-6 < next_t:
            continue
        next_t = t + step_s
        t_ms = round(t * 1000)
        h, w = fr.shape[:2]
        rgb = np.ascontiguousarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))
        res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
        if not res.face_landmarks:
            gaze_rows.append([t_ms, 0] + [""] * 6)
            expr_rows.append([t_ms, 0] + [""] * len(BLEND_PAIRS))
            continue
        L = res.face_landmarks[0]
        crop, face_w = face_crop_224(fr, L)
        p, y = l2.predict_gaze(np.stack([crop]))                    # radians, camera frame
        yaw, pitch = math.degrees(float(y[0])), math.degrees(float(p[0]))
        M = np.asarray(res.facial_transformation_matrixes[0]).reshape(4, 4)
        head_yaw = math.degrees(math.atan2(M[0, 2], M[2, 2]))
        head_pitch = math.degrees(math.atan2(-M[1, 2], math.hypot(M[0, 2], M[2, 2])))
        eye_w = abs(L[R_OUT].x - L[L_OUT].x) * w
        openness = ((abs(L[L_DN].y - L[L_UP].y) + abs(L[R_DN].y - L[R_UP].y)) / 2 * h) / max(eye_w, 1e-6)
        bs = {b.category_name: b.score for b in res.face_blendshapes[0]}
        gaze_rows.append([t_ms, 1, f"{yaw:.2f}", f"{pitch:.2f}", f"{head_yaw:.2f}", f"{head_pitch:.2f}",
                          f"{openness:.3f}", int(face_w)])
        expr_rows.append([t_ms, 1] + [f"{(bs[a] + bs[b]) / 2:.3f}" for _, (a, b) in BLEND_PAIRS])

    with open(os.path.join(session, "gaze-ai.csv"), "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["t_ms", "face", "gaze_yaw_deg_raw", "gaze_pitch_deg_raw",
                     "head_yaw_deg", "head_pitch_deg", "eye_openness", "face_width_px"])
        wr.writerows(gaze_rows)
    with open(os.path.join(session, "expressions.csv"), "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["t_ms", "face"] + [k for k, _ in BLEND_PAIRS])
        wr.writerows(expr_rows)
    n = len(gaze_rows); faces = sum(1 for r in gaze_rows if r[1] == 1)
    print(f"[bux-analyze-video] {n} frames @ {fps} fps, face in {faces} ({100 * faces / max(n, 1):.0f}%), "
          f"device={dev}, {time.time() - t0:.0f}s -> gaze-ai.csv, expressions.csv")


def main():
    ap = argparse.ArgumentParser(description="Offline AI analysis of a Boring UX session's face.webm")
    ap.add_argument("session")
    ap.add_argument("--fps", type=float, default=10.0, help="model sampling rate (default 10)")
    ap.add_argument("--limit-s", type=float, default=0, help="only process the first N seconds (0 = all)")
    ap.add_argument("--cpu", action="store_true", help="force CPU (default: Apple GPU/MPS if available)")
    a = ap.parse_args()
    extract(os.path.expanduser(a.session), a.fps, a.limit_s, a.cpu)


if __name__ == "__main__":
    main()
