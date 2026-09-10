"""bux-analyze-video: orchestrates docs/GAZE-FROM-VIDEO-DESIGN.md §8.3 and writes <session>/analysis/."""
import argparse
import glob
import json
import math
import os
import subprocess
import sys
import time

import numpy as np

from . import geometry as G
from . import fuse as F
from .models import FaceModel, GazeModel

VERSION = "0.3.0"


def log(msg):
    print(f"[bux] {msg}", flush=True)


def transcribe(session, out_dir, model_path, lang):
    srt = os.path.join(out_dir, "transcript.srt")
    if os.path.exists(srt):
        return srt, []
    audio = os.path.join(session, "audio.webm")
    if not os.path.exists(audio):
        return None, ["transcript_missing"]
    if not model_path:
        cands = glob.glob(os.path.expanduser("~/Desktop/gaze-ai/models/ggml-*.bin")) + glob.glob(os.path.expanduser("~/.cache/whisper*/ggml-*.bin"))
        cands = [c for c in cands if "tiny" not in c or len(cands) == 1]
        model_path = cands[0] if cands else None
    if not model_path or not os.path.exists(model_path):
        return None, ["transcript_missing"]
    wav = os.path.join(out_dir, "audio16k.wav")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", audio, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav], check=True)
    subprocess.run(["whisper-cli", "-m", model_path, "-f", wav, "-l", lang, "-osrt", "-of", os.path.join(out_dir, "transcript")], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return (srt if os.path.exists(srt) else None), ([] if os.path.exists(srt) else ["transcript_missing"])


def spearman(a, b):
    from scipy.stats import spearmanr
    r = spearmanr(a, b).correlation
    return None if r is None or np.isnan(r) else float(r)


def run(args):
    session = os.path.abspath(os.path.expanduser(args.session))
    out_dir = os.path.join(session, "analysis"); os.makedirs(out_dir, exist_ok=True)
    t_all = time.time(); flags = []
    S, mouse, events, clicks, rage, thrash, pages = F.load_session_files(session)
    if mouse is None: flags.append("mouse_missing")
    if events is None: flags.append("events_missing")
    vp0 = S.get("viewport", {}) or {}
    disp = G.resolve_display(int(S.get("screen", {}).get("width", vp0.get("winW", 0))), int(S.get("screen", {}).get("height", 0)), args.display)
    flags += disp["flags"]; vp = G.Viewport(S, disp); flags += vp.flags
    log(f"session {os.path.basename(session)} | display {disp['name']} {disp['W']}x{disp['H']}cm | viewport {vp.inner_w}x{vp.inner_h} | clicks {len(clicks)} | mouse rows {len(mouse or [])}")

    # 1–2 video + transcript
    video = F.prepare_video(session, out_dir)
    srt, tflags = transcribe(session, out_dir, args.whisper_model, args.lang); flags += tflags
    transcript = F.load_transcript(session, out_dir)
    log(f"transcript: {len(transcript) if transcript else 0} segments" + (" (missing — pass --whisper-model)" if not transcript else ""))

    # 3–4 models + extraction
    face = FaceModel(); gaze = GazeModel(device=args.device, fp16=not args.no_fp16)
    log(f"models ready (L2CS on {gaze.dev}, fp16={gaze.fp16})")
    rows, crops, gaps = F.extract_frames(video, face, gaze, mp_hz=args.mp_fps, gaze_hz=args.fps, limit_s=args.limit_s, log=log)
    if not rows:
        sys.exit("no frames decoded")
    img_w = next((r["img_w"] for r in rows if r.get("face")), 640); img_h = next((r["img_h"] for r in rows if r.get("face")), 480)
    f_px, fflags = G.focal_px(img_w, args.fpx); flags += fflags
    signs = dict(l2cs_channel_test=gaze.flip_selftest(crops[:30]))
    log(f"flip self-test: {signs['l2cs_channel_test']}")
    if signs["l2cs_channel_test"]["status"] == "FAIL":
        sys.exit("L2CS channel assignment FAILED the flip self-test — aborting (§2.6)")
    ear_thr = F.mark_blinks(rows)

    # 5 raw mapping, self-centring prior
    F.map_frames(rows, vp, f_px, img_w / 2, img_h / 2, b=(0, 0), assume_mirrored=args.assume_mirrored)
    for r in rows:
        r["conf"] = F.gaze_conf(r, img_w)
    valid = [r for r in rows if r.get("pt_cm") and r["conf"] >= 0.3]
    E_med = np.median([r["E_C"] for r in valid], axis=0) if valid else np.array([0, 0, 55.0])
    bh, bv, raw_h, raw_v = G.self_centering_bias([r["pt_cm"] for r in valid], E_med, vp.W, vp.H, vp.y_off)
    b0 = (bh, bv)
    calib = dict(self_centering_bias_deg=[round(math.degrees(bh), 2), round(math.degrees(bv), 2)],
                 self_centering_raw_deg=[round(math.degrees(raw_h), 2), round(math.degrees(raw_v), 2)],
                 self_centering_clipped=bool(abs(raw_h) > math.radians(5) or abs(raw_v) > math.radians(5)),
                 click=None, applied="self_centering")
    if calib["self_centering_clipped"]:
        flags.append("bias_clipped_needs_clicks")
    b = b0
    # 6 click residuals
    if clicks and not args.no_click_calib:
        F.map_frames(rows, vp, f_px, img_w / 2, img_h / 2, b=(0, 0), assume_mirrored=args.assume_mirrored)
        res, pairs = F.click_residuals(rows, clicks, vp, E_med)
        cb = G.click_bias(res); calib["click"] = {k: (round(math.degrees(v), 2) if k in ("b_h", "b_v") else v) for k, v in cb.items()}
        if cb["applied"]:
            b = (cb["b_h"], cb["b_v"]); calib["applied"] = "click_bias"
        if len(pairs) >= 10:
            rho = spearman([p[0] for p in pairs], [p[1] for p in pairs])
            signs["orientation"] = dict(status="INVERTED" if rho is not None and rho < -0.3 else "OK" if rho is not None and rho > 0.3 else "UNTESTED", rho=None if rho is None else round(rho, 3), n=len(pairs))
        else:
            signs["orientation"] = dict(status="UNTESTED", n=len(pairs))
        flags += cb["flags"]
    else:
        signs["orientation"] = dict(status="UNTESTED", n=0)
    # 7 corrected re-map + states
    F.map_frames(rows, vp, f_px, img_w / 2, img_h / 2, b=b, assume_mirrored=args.assume_mirrored)
    for r in rows:
        r["conf"] = F.gaze_conf(r, img_w)
    # cross-path consistency (§2.6 item 3)
    fr = [r for r in rows if r.get("h") is not None]
    if len(fr) >= 30:
        medR = np.median([r["hR"] for r in fr]); medL = np.median([r["hL"] for r in fr])
        iris_right = [-(((r["hR"] - medR) + (r["hL"] - medL)) / 2) for r in fr]
        hs = [r["h"] for r in fr]; vs = [r["v"] for r in fr]
        c1 = float(np.corrcoef(hs, iris_right)[0, 1]); c2 = float(np.corrcoef(hs, [-r["H_bl"] for r in fr])[0, 1]); c3 = float(np.corrcoef(vs, [r["V_bl"] for r in fr])[0, 1])
        signs["cross_path"] = dict(corr_h_iris_right=round(c1, 2), corr_h_blend=round(c2, 2), corr_v_blend=round(c3, 2), status="ok" if (c1 > 0.3 and c2 > 0.3) else "weak")
    if signs.get("orientation", {}).get("status") == "INVERTED":
        flags.append("orientation_inverted")

    # 8 fuse
    dur_s = rows[-1]["t_ms"] / 1000
    pitch_med = float(np.median([math.degrees(r["head_pitch"]) for r in rows if r.get("face")])) if any(r.get("face") for r in rows) else 0
    secs = F.per_second(rows, mouse, clicks, rage, thrash, pages, transcript, vp, dur_s, dict(pitch_med=pitch_med))
    expr = F.expressions(rows, secs)
    # 9 moments + click-consistency
    cc = F.click_consistency(rows, clicks, vp) if clicks else dict(n=0, tier="unvalidated", note="no clicks")
    if signs.get("orientation", {}).get("status") == "INVERTED":
        cc["tier"] = "unvalidated"
    moments = F.detect_moments(secs, clicks, vp)
    grades = dict((g, sum(1 for s in secs if s["quality_grade"] == g)) for g in "ABCF")
    usable = sum(1 for s in secs if s["quality_grade"] in "ABC" and s.get("gaze_conf", 0) >= 0.3) / max(len(secs), 1)

    # write
    cols10 = ["t_ms", "face", "h_raw", "v_raw", "sharp_h", "sharp_v", "conf", "cell", "col", "row", "state", "x_vp", "y_vp", "d_cm", "head_yaw", "head_pitch", "blink", "ear"]
    F.write_csv(os.path.join(out_dir, "frames.csv"), [{**{k: (round(r[k], 4) if isinstance(r.get(k), float) else r.get(k)) for k in cols10}} for r in rows], cols10)
    cols1 = ["t_s", "face_present", "gaze_h_deg", "gaze_v_deg", "gaze_h_sd", "gaze_x_vp", "gaze_y_vp", "gaze_col", "gaze_row", "gaze_cell", "gaze_state", "gaze_p_cell", "gaze_conf",
             "region_switches", "distinct_cells", "blink_count", "blink_rate_per_min", "head_yaw_deg", "head_pitch_deg", "distance_cm",
             "mouse_x", "mouse_y", "mouse_cell", "mouse_speed_px_s", "mouse_path_px", "mouse_idle", "dist_gaze_mouse_px", "clicks", "scroll_thrash", "page_url", "page_changed",
             "speech_text", "speaking", "silence_run_s", "speech_cues",
             "expr_browDown", "expr_browInnerUp", "expr_eyeSquint", "expr_eyeWide", "expr_mouthPress", "expr_mouthSmile", "expr_mouthFrown", "expr_jawOpen",
             "expr_browDown_z", "expr_eyeSquint_z", "expr_mouthPress_z", "expr_mouthSmile_z", "expr_label", "quality_score", "quality_grade", "flags"]
    F.write_csv(os.path.join(out_dir, "gaze-ai.csv"), secs, cols1)
    ecols = ["t_s"] + [f"expr_{k}" for k in F.AGG] + [f"expr_{k}_z" for k in F.AGG] + ["expr_label"]
    F.write_csv(os.path.join(out_dir, "expressions.csv"), expr, ecols)
    json.dump(dict(session=os.path.basename(session), moments=moments), open(os.path.join(out_dir, "moments.json"), "w"), ensure_ascii=False, indent=1)
    quality = dict(version=VERSION, session=os.path.basename(session), duration_s=round(dur_s, 1), display=disp, viewport=dict(w=vp.inner_w, h=vp.inner_h),
                   f_px=round(f_px, 1), ear_blink_threshold=round(ear_thr, 3), frames=dict(mediapipe=len(rows), l2cs=len(crops), face_present_frac=round(float(np.mean([r.get("face", 0) for r in rows])), 3), pts_gaps_over_200ms=len(gaps)),
                   signs=signs, calibration=calib, click_consistency=cc, grade_histogram=grades, gaze_usable_frac=round(usable, 3),
                   moments=dict((t, sum(1 for m in moments if m["type"] == t)) for t in ("SEARCHING", "FOUND_THEN_ACTED", "MISS_THEN_CORRECT", "LOOK_AWAY", "CONFUSION", "FRUSTRATION")),
                   flags=sorted(set(flags)), params=vars(args),
                   footer="Uncalibrated webcam gaze (L2CS-Net, Gaze360): typical error ≈10° ≈8 cm at 55 cm; a 3×3 cell is about one error radius. Column (L/C/R) statements are right roughly 7 in 10; specific cells about half the time; looking-away and keyboard glances are reliable.")
    json.dump(quality, open(os.path.join(out_dir, "quality.json"), "w"), ensure_ascii=False, indent=1)
    try:
        F.draw_debug(video, rows, out_dir)
    except Exception as e:  # noqa: BLE001
        log(f"debug frames skipped: {e}")
    log(f"done in {time.time()-t_all:.0f}s → {out_dir}/  gaze-ai.csv ({len(secs)} s) · expressions.csv · moments.json ({len(moments)}) · quality.json")
    log(f"quality: usable {usable:.0%} | grades {grades} | click-consistency {cc.get('col', {}).get('score', '—')} vs chance {cc.get('col', {}).get('chance', '—')} (lift {cc.get('col', {}).get('lift', '—')}, N={cc.get('n')}) → tier {cc.get('tier')} | orientation {signs.get('orientation', {}).get('status')}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bux-analyze-video", description="Offline AI analysis of a Boring UX session (face.webm → gaze/expressions/moments/quality)")
    ap.add_argument("session")
    ap.add_argument("--fps", type=float, default=10, help="L2CS sampling rate (default 10)")
    ap.add_argument("--mp-fps", type=float, default=15, help="MediaPipe sampling rate (default 15)")
    ap.add_argument("--device", default="mps", choices=["mps", "cpu"])
    ap.add_argument("--no-fp16", action="store_true")
    ap.add_argument("--display", default=None, help="'14.2' or 'WxH' cm; default: infer from viewport")
    ap.add_argument("--fpx", type=float, default=None, help="camera focal length in px (default 0.85*width)")
    ap.add_argument("--no-click-calib", action="store_true")
    ap.add_argument("--assume-mirrored", action="store_true", help="only after a model-free check proves the track is mirrored")
    ap.add_argument("--whisper-model", default=None, help="path to ggml-*.bin (default: auto-find under ~/Desktop/gaze-ai/models)")
    ap.add_argument("--lang", default="auto")
    ap.add_argument("--limit-s", type=float, default=0, help="process only the first N seconds")
    args = ap.parse_args(argv)
    return run(args)
