"""Ingest, per-frame extraction, screen mapping, 10 Hz/1 Hz fusion, quality, click-consistency, UX moments.
Implements docs/GAZE-FROM-VIDEO-DESIGN.md §3–§7 (pragmatic first version; thresholds are the design's starting points).
"""
import csv
import json
import math
import os
import re
import subprocess
import time
from collections import Counter, defaultdict

import cv2
import numpy as np

from . import geometry as G

EBML = b"\x1a\x45\xdf\xa3"
SEARCH_RE = re.compile(r"where is|where's the|can'?t find|don'?t see|how do i|hmm|وين|فين|ما ألقى|ما اشوف|ما أشوف|كيف", re.I)
NEG_RE = re.compile(r"\b(ugh|come on|why|wrong|error|broken|stupid|annoying|doesn'?t work|not working)\b|ليش|يا الله|ما يشتغل|خطأ|مو شغال", re.I)
HESIT_RE = re.compile(r"\b(uh+|um+|er+|hmm+)\b|امم|اه+", re.I)


# ---------------- ingest (§5.1) ----------------
def prepare_video(session, out_dir):
    src = os.path.join(session, "face.webm")
    with open(src, "rb") as f:
        head = f.read(4)
    if head != EBML:
        raise SystemExit(f"face.webm is not a WebM (starts {head.hex()}). Run tools/recover-webm.py first.")
    fixed = os.path.join(out_dir, "face_fixed.webm")
    if not os.path.exists(fixed) or os.path.getmtime(fixed) < os.path.getmtime(src):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", src, "-c", "copy", fixed], check=True)
    return fixed


def decode_frames(path):
    """Yield (t_ms, rgb) using container pts (t=0 = first packet of the recording)."""
    import av
    c = av.open(path); s = c.streams.video[0]; s.thread_type = "AUTO"
    last = -1
    for fr in c.decode(s):
        if fr.pts is None:
            continue
        t_ms = int(round(float(fr.pts * s.time_base) * 1000))
        if t_ms <= last:
            continue                                  # duplicates / backward jumps (recovered files)
        last = t_ms
        yield t_ms, fr.to_ndarray(format="rgb24")


# ---------------- per-frame extraction (§3) ----------------
def extract_frames(video, face, gaze, mp_hz=15, gaze_hz=10, limit_s=0, log=print):
    rows, crops, crop_rows = [], [], []
    mp_step, gz_step = 1000.0 / mp_hz, 1000.0 / gaze_hz
    next_mp = next_gz = 0.0
    t0 = time.time(); n_dec = 0; gaps = []; prev_t = None
    for t_ms, rgb in decode_frames(video):
        n_dec += 1
        if prev_t is not None and t_ms - prev_t > 200:
            gaps.append((prev_t, t_ms))
        prev_t = t_ms
        if limit_s and t_ms > limit_s * 1000:
            break
        if t_ms + 1 < next_mp:
            continue
        next_mp = t_ms + mp_step
        f = face.detect(rgb, t_ms)
        row = dict(t_ms=t_ms, face=0)
        if f:
            row.update(face=1, n_faces=f["n_faces"], bbox_w=f["bbox"][2], u_e=f["u_e"], v_e=f["v_e"], ipd_px=f["ipd_px"],
                       iris_px=f["iris_px"], ear=f["ear"], hR=f["hR"], hL=f["hL"], head_yaw=f["head_yaw"], head_pitch=f["head_pitch"],
                       head_roll=f["head_roll"], t_x=f["t_x"], t_y=f["t_y"], t_z=f["t_z"], H_bl=f["H_bl"], V_bl=f["V_bl"],
                       padded_frac=f["padded_frac"], face_lum=f["face_lum"], glare=f["glare"], img_w=f["img_w"], img_h=f["img_h"],
                       bs=f["bs"], blink_bs=max(f["bs"].get("eyeBlinkLeft", 0), f["bs"].get("eyeBlinkRight", 0)))
            if t_ms + 1 >= next_gz:
                next_gz = t_ms + gz_step
                crops.append(f["crop224"]); crop_rows.append(len(rows))
        rows.append(row)
        if len(rows) % 300 == 0:
            log(f"  … {t_ms/1000:.0f}s decoded, {len(crops)} gaze frames queued")
    # gaze in batches
    if crops:
        h, v, sh, sv = gaze.predict(crops)
        for k, ri in enumerate(crop_rows):
            rows[ri].update(h_raw=float(h[k]), v_raw=float(v[k]), sharp_h=float(sh[k]), sharp_v=float(sv[k]))
    log(f"  frames: {n_dec} decoded, {len(rows)} MediaPipe, {len(crops)} L2CS in {time.time()-t0:.0f}s; pts gaps>200ms: {len(gaps)}")
    return rows, crops, gaps


# ---------------- blinks (§3.1) ----------------
def mark_blinks(rows):
    ears = [r["ear"] for r in rows if r.get("face")]
    thr = max(0.12, 0.5 * float(np.median(ears))) if ears else 0.12
    for r in rows:
        r["blink"] = int(bool(r.get("face")) and (r["ear"] < thr or r.get("blink_bs", 0) > 0.5))
    # mask 1 before / 2 after
    idx = [i for i, r in enumerate(rows) if r["blink"]]
    for i in idx:
        for j in range(max(0, i - 1), min(len(rows), i + 3)):
            rows[j]["blink_mask"] = 1
    return thr


# ---------------- mapping + bias (§4) ----------------
def map_frames(rows, vp, f_px, cx, cy, b=(0.0, 0.0), assume_mirrored=False):
    sign = -1.0 if assume_mirrored else 1.0
    for r in rows:
        if not r.get("face") or "h_raw" not in r:
            continue
        d, dflags = G.distance_cm(r["ipd_px"], r["iris_px"], r["head_yaw"], r["t_z"], f_px, r["img_h"])
        r["d_cm"] = d; r.setdefault("flags", []).extend(dflags)
        E = G.eye_position(r["u_e"], r["v_e"], d, f_px, cx, cy); r["E_C"] = E
        h = math.radians(sign * r["h_raw"]) - b[0]; v = math.radians(r["v_raw"]) - b[1]
        r["h"] = h; r["v"] = v
        pt = G.gaze_to_screen(h, v, E, vp.W, vp.y_off)
        r["pt_cm"] = pt
        if pt:
            x_vp, y_vp = vp.cm_to_vp(*pt); r["x_vp"], r["y_vp"] = x_vp, y_vp
            r["col"], r["row"], r["cell"] = vp.cell(x_vp, y_vp)
        else:
            r["x_vp"] = r["y_vp"] = None; r["col"] = r["row"] = r["cell"] = None
        edges = vp.edge_angles(E)
        r["state"] = classify_state(r, edges)


def classify_state(r, edges):
    deg = math.degrees
    if abs(deg(r["head_yaw"])) > 35 or abs(deg(r["head_pitch"])) > 30:
        return "away"
    h, v = r["h"], r["v"]
    if v < edges["bottom"] - math.radians(8):
        return "down_keyboard"
    if h > edges["right"] + math.radians(3):
        return "off_right"
    if h < edges["left"] - math.radians(3):
        return "off_left"
    if v > edges["top"] + math.radians(3):
        return "off_up"
    return "on_screen"


def gaze_conf(r, img_w):
    if not r.get("face") or "h_raw" not in r:
        return 0.0
    head_pen = float(np.clip(1 - (abs(math.degrees(r["head_yaw"])) - 25) / 25, 0, 1))
    size_ok = 1.0 if r["bbox_w"] / img_w >= 0.15 else 0.5
    return float((0 if r.get("blink_mask") else 1) * np.mean([r["sharp_h"], r["sharp_v"]]) * head_pen * size_ok)


# ---------------- session data ----------------
def load_session_files(session):
    S = json.load(open(os.path.join(session, "session.json"))) if os.path.exists(os.path.join(session, "session.json")) else {}
    def read(name):
        p = os.path.join(session, name)
        return list(csv.DictReader(open(p, encoding="utf-8"))) if os.path.exists(p) else None
    mouse = read("mouse.csv"); events = read("events.csv")
    def num(v):
        try:
            return float(v) if v not in (None, "") else None
        except ValueError:
            return None
    clicks = []
    for e in (events or []):
        if e.get("type") != "click":
            continue
        clickable = str(e.get("clickable", "")).strip().lower()
        rect = [float(x) for x in str(e.get("rect", "")).split()] if e.get("rect") else None
        clicks.append(dict(t_ms=float(e["t_ms"]), x=num(e.get("x")), y=num(e.get("y")), text=(e.get("detail") or e.get("txt") or "")[:60],
                           dead=clickable in ("0", "false"), rect=rect))
    # older recordings exported clicks without coordinates: they still count as clicks (timing) but cannot be placed
    rage = [float(e["t_ms"]) for e in (events or []) if e.get("type") == "rage_click"]
    thrash = [float(e["t_ms"]) for e in (events or []) if e.get("type") == "scroll_thrash"]
    pages = [dict(t_ms=float(e["t_ms"]), url=e.get("extra") or e.get("url") or "", title=e.get("detail") or "") for e in (events or []) if e.get("type") == "page"]
    return S, mouse, events, clicks, rage, thrash, pages


def load_transcript(session, out_dir):
    for p in (os.path.join(out_dir, "transcript.srt"), os.path.join(session, "transcript.srt")):
        if os.path.exists(p):
            return parse_srt(open(p, encoding="utf-8").read())
    return None


def parse_srt(txt):
    segs = []
    for blk in re.split(r"\n\s*\n", txt.strip()):
        lines = blk.strip().splitlines()
        if len(lines) < 2:
            continue
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", lines[1] if "-->" in lines[1] else lines[0])
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        a = (g[0] * 3600 + g[1] * 60 + g[2]) * 1000 + g[3]; b = (g[4] * 3600 + g[5] * 60 + g[6]) * 1000 + g[7]
        text = " ".join(l for l in lines[(2 if "-->" in lines[1] else 1):]).strip()
        if text:
            segs.append(dict(start=a, end=b, text=text))
    # drop whisper hallucinations on silence: stock phrases, and long segments with ≤3 words; collapse repeats
    out = []
    for s in segs:
        norm = re.sub(r"[^\w\s]", "", s["text"]).strip().lower()
        dur = (s["end"] - s["start"]) / 1000
        if norm in HALLUCINATIONS or (dur >= 20 and len(norm.split()) <= 3):
            continue
        if out and s["text"] == out[-1]["text"]:
            continue
        out.append(s)
    return out


HALLUCINATIONS = {"thank you", "thanks for watching", "thank you for watching", "subtitles by the amaraorg community", "please subscribe",
                  "you", "bye", "so", "شكرا", "شكرا لكم", "ترجمة نانسي قنقر", "اشترك في القناة"}


# ---------------- click bias + consistency (§4.6, §7.2) ----------------
def pre_click_median(rows, t_c, lo=400, hi=50, min_conf=0.3):
    sel = [r for r in rows if r.get("h") is not None and t_c - lo <= r["t_ms"] <= t_c - hi and r.get("conf", 0) >= min_conf]
    if len(sel) < 3:
        return None
    hs = np.array([r["h"] for r in sel]); vs = np.array([r["v"] for r in sel])
    if np.median(np.abs(hs - np.median(hs))) > math.radians(3):
        return None
    return float(np.median(hs)), float(np.median(vs)), sel


def click_residuals(rows, clicks, vp, E_med):
    res, pairs = [], []
    for i, c in enumerate(clicks):
        if c["x"] is None or c["y"] is None:
            continue                                  # old recordings: click time known, position not
        if i and c["t_ms"] - clicks[i - 1]["t_ms"] < 400:
            continue
        if not (0 <= c["x"] <= vp.inner_w and 0 <= c["y"] <= vp.inner_h):
            continue
        m = pre_click_median(rows, c["t_ms"])
        if not m:
            continue
        h_m, v_m, sel = m
        E = sel[len(sel) // 2].get("E_C", E_med)
        x_cm, y_cm = vp.vp_to_cm(c["x"], c["y"])
        h_e, v_e = G.screen_to_angles(x_cm, y_cm, E, vp.W, vp.y_off)
        res.append((h_m - h_e, v_m - v_e)); pairs.append((h_m, c["x"]))
    return res, pairs


def click_consistency(rows, clicks, vp, rng_seed=7):
    """Fraction of clicks whose pre-click gaze cell/col/row matched; permutation chance; lift; Wilson CI."""
    clicks = [c for c in clicks if c["x"] is not None and c["y"] is not None]
    if not clicks:
        return dict(n=0, tier="unvalidated", note="clicks have no coordinates (old recording)")
    def score(shift):
        hits = defaultdict(int); n = 0
        for c in clicks:
            t = c["t_ms"] + shift
            sel = [r for r in rows if r.get("cell") and t - 400 <= r["t_ms"] <= t and r.get("conf", 0) >= 0.3]
            if len(sel) < 2:
                continue
            n += 1
            cc, cr, ccell = vp.cell(c["x"], c["y"])
            col = Counter(r["col"] for r in sel).most_common(1)[0][0]
            row = Counter(r["row"] for r in sel).most_common(1)[0][0]
            cell = Counter(r["cell"] for r in sel).most_common(1)[0][0]
            hits["col"] += col == cc; hits["row"] += row == cr; hits["cell"] += cell == ccell
        return n, {k: hits[k] / n for k in ("col", "row", "cell")} if n else {}
    n, sc = score(0)
    if not n:
        return dict(n=0, tier="unvalidated", note="no scorable clicks")
    rng = np.random.default_rng(rng_seed); dur = rows[-1]["t_ms"] if rows else 0
    chance = defaultdict(list)
    for _ in range(200):
        shift = float(rng.uniform(5000, 60000)) * rng.choice([-1, 1])
        m, s2 = score(shift)
        if m:
            for k in s2:
                chance[k].append(s2[k])
    out = dict(n=n)
    for k in ("col", "row", "cell"):
        ch = float(np.mean(chance[k])) if chance[k] else (1 / 3 if k != "cell" else 1 / 9)
        lift = (sc[k] - ch) / (1 - ch) if ch < 1 else 0
        z = 1.96; p = sc[k]; half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
        out[k] = dict(score=round(p, 3), chance=round(ch, 3), lift=round(lift, 3), ci95=round(half, 3))
    L = out["col"]["lift"]
    out["tier"] = "regions" if (n >= 10 and L >= 0.5) else "likely" if (n >= 10 and L >= 0.25) else "unvalidated"
    return out


# ---------------- grids (§5.2) ----------------
def bin_rows(rows, bin_ms):
    B = defaultdict(list)
    for r in rows:
        B[int(r["t_ms"] // bin_ms)].append(r)
    return B


def per_second(rows, mouse, clicks, rage, thrash, pages, transcript, vp, dur_s, quality_ctx):
    B1 = bin_rows(rows, 1000); B10 = bin_rows(rows, 100)
    mouse_pts = [(float(m["t_ms"]), float(m["x"]), float(m["y"])) for m in (mouse or []) if m.get("x")]
    mouse_pts.sort()
    # transcript per second
    speech = defaultdict(list)
    for s in (transcript or []):
        for sec in range(int(s["start"] // 1000), int(s["end"] // 1000) + 1):
            speech[sec].append(s["text"])
    silence_run = 0; out = []; prev_cell = None; page_i = 0
    blink_times = [r["t_ms"] for r in rows if r.get("blink")]
    for sec in range(int(dur_s) + 1):
        rs = B1.get(sec, []); valid = [r for r in rs if r.get("cell") and r.get("conf", 0) >= 0.3]
        face_present = float(np.mean([r.get("face", 0) for r in rs])) if rs else 0.0
        rec = dict(t_s=sec, face_present=round(face_present, 2))
        # gaze
        if valid:
            cells = Counter(r["cell"] for r in valid); cell, cnt = cells.most_common(1)[0]
            p_cell = cnt / len(valid)
            col = Counter(r["col"] for r in valid).most_common(1); row = Counter(r["row"] for r in valid).most_common(1)
            rec.update(gaze_h_deg=round(math.degrees(np.median([r["h"] for r in valid])), 1),
                       gaze_v_deg=round(math.degrees(np.median([r["v"] for r in valid])), 1),
                       gaze_h_sd=round(math.degrees(np.std([r["h"] for r in valid])), 1),
                       gaze_x_vp=round(float(np.median([r["x_vp"] for r in valid]))), gaze_y_vp=round(float(np.median([r["y_vp"] for r in valid]))),
                       gaze_cell=cell if p_cell >= 0.5 else "uncertain", gaze_p_cell=round(p_cell, 2),
                       gaze_col=col[0][0] if col[0][1] / len(valid) >= 0.6 or p_cell >= 0.5 else "?",
                       gaze_row=row[0][0] if row[0][1] / len(valid) >= 0.6 or p_cell >= 0.5 else "?",
                       gaze_conf=round(float(np.mean([r["conf"] for r in valid])), 2))
        else:
            rec.update(gaze_cell=None, gaze_col=None, gaze_row=None, gaze_conf=0.0)
        states = Counter(r.get("state") for r in rs if r.get("state"))
        rec["gaze_state"] = states.most_common(1)[0][0] if states else ("no_face" if face_present < 0.5 else "on_screen")
        if face_present < 0.5:
            rec["gaze_state"] = "no_face"
        # switches within the second (10 Hz majority)
        seq = []
        for k in range(sec * 10, sec * 10 + 10):
            v10 = [r["cell"] for r in B10.get(k, []) if r.get("cell") and r.get("conf", 0) >= 0.3]
            if v10:
                seq.append(Counter(v10).most_common(1)[0][0])
        rec["region_switches"] = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
        rec["distinct_cells"] = len(set(seq))
        rec["blink_count"] = sum(1 for t in blink_times if sec * 1000 <= t < sec * 1000 + 1000)
        rec["blink_rate_per_min"] = round(2 * sum(1 for t in blink_times if (sec - 30) * 1000 <= t < sec * 1000 + 1000), 1)
        if rs:
            hp = [r for r in rs if r.get("face")]
            if hp:
                rec["head_yaw_deg"] = round(math.degrees(np.median([r["head_yaw"] for r in hp])), 1)
                rec["head_pitch_deg"] = round(math.degrees(np.median([r["head_pitch"] for r in hp])), 1)
                if any("d_cm" in r for r in hp):
                    rec["distance_cm"] = round(float(np.median([r["d_cm"] for r in hp if "d_cm" in r])), 1)
        # mouse
        win = [m for m in mouse_pts if sec * 1000 <= m[0] < sec * 1000 + 1000]
        prev = [m for m in mouse_pts if m[0] < sec * 1000]
        if win or prev:
            last = (win or prev)[-1]
            rec["mouse_x"], rec["mouse_y"] = round(last[1]), round(last[2])
            _, _, rec["mouse_cell"] = vp.cell(last[1], last[2])
            path = sum(math.hypot(b[1] - a[1], b[2] - a[2]) for a, b in zip(win, win[1:]))
            rec["mouse_path_px"] = round(path); rec["mouse_speed_px_s"] = round(path)
            rec["mouse_idle"] = int(path < 20)
            if rec.get("gaze_x_vp") is not None:
                rec["dist_gaze_mouse_px"] = round(math.hypot(rec["gaze_x_vp"] - last[1], rec["gaze_y_vp"] - last[2]))
        cl = [c for c in clicks if sec * 1000 <= c["t_ms"] < sec * 1000 + 1000]
        rec["clicks"] = [dict(t_ms=c["t_ms"], x=c["x"], y=c["y"], cell=vp.cell(c["x"], c["y"])[2], target_text=c["text"],
                              is_dead=bool(c["dead"]), is_rage=any(abs(c["t_ms"] - t) < 1200 for t in rage)) for c in cl]
        rec["scroll_thrash"] = int(any(sec * 1000 <= t < sec * 1000 + 1000 for t in thrash))
        while page_i + 1 < len(pages) and pages[page_i + 1]["t_ms"] < sec * 1000 + 1000:
            page_i += 1
        rec["page_url"] = pages[page_i]["url"] if pages else ""
        rec["page_changed"] = int(any(sec * 1000 <= p["t_ms"] < sec * 1000 + 1000 for p in pages[1:]))
        # speech
        txt = " ".join(dict.fromkeys(speech.get(sec, [])))
        rec["speech_text"] = txt; rec["speaking"] = int(bool(txt))
        silence_run = 0 if txt else silence_run + 1
        rec["silence_run_s"] = silence_run
        cues = []
        if txt and SEARCH_RE.search(txt): cues.append("search_phrase")
        if txt and NEG_RE.search(txt): cues.append("negative")
        if txt and HESIT_RE.search(txt): cues.append("hesitation")
        rec["speech_cues"] = cues
        # quality (§7.4 subset)
        rec.update(quality(rs, valid, rec, quality_ctx))
        out.append(rec)
    return out


def quality(rs, valid, rec, ctx):
    if not rs:
        return dict(quality_score=0.0, quality_grade="F", flags=["no_face"])
    faces = [r for r in rs if r.get("face")]
    flags = []
    detect = 1 if len(faces) / len(rs) >= 0.6 else 0
    if not detect: flags.append("no_face")
    size = 1
    if faces:
        ratio = np.median([r["bbox_w"] / r["img_w"] for r in faces])
        size = 1 if ratio >= 0.25 else 0.5 if ratio >= 0.15 else 0
        if size < 1: flags.append("face_small")
        yaw = abs(np.median([math.degrees(r["head_yaw"]) for r in faces])); pit = abs(np.median([math.degrees(r["head_pitch"]) for r in faces]) - ctx["pitch_med"])
        pose = 1 if (yaw <= 20 and pit <= 20) else 0.5 if (yaw <= 30 and pit <= 25) else 0
        if pose < 1: flags.append("head_turned")
        if np.mean([r.get("glare", 0) for r in faces]) > 0.03: flags.append("glare")
        if np.mean([r.get("face_lum", 128) for r in faces]) < 50: flags.append("too_dark")
        if any(r.get("padded_frac", 0) > 0.1 for r in faces): flags.append("face_partial")
    else:
        pose = 0
    blink = 1 if np.mean([r.get("blink_mask", 0) for r in rs]) <= 0.3 else 0.5
    samples = 1 if len(valid) >= 6 else 0.5 if len(valid) >= 3 else 0
    stab = 1
    if valid:
        sd = math.degrees(np.std([r["h"] for r in valid])); stab = 1 if sd <= 6 else 0.5 if sd <= 10 else 0
    light = 0.5 if ("glare" in flags or "too_dark" in flags) else 1
    score = detect * size * pose * blink * samples * stab * light
    grade = "A" if score >= 0.8 else "B" if score >= 0.5 else "C" if score >= 0.2 else "F"
    return dict(quality_score=round(float(score), 2), quality_grade=grade, flags=flags)


# ---------------- expressions (§5.4) ----------------
AGG = {"browDown": ("browDownLeft", "browDownRight"), "browInnerUp": ("browInnerUp",), "eyeSquint": ("eyeSquintLeft", "eyeSquintRight"),
       "eyeWide": ("eyeWideLeft", "eyeWideRight"), "mouthPress": ("mouthPressLeft", "mouthPressRight"),
       "mouthSmile": ("mouthSmileLeft", "mouthSmileRight"), "mouthFrown": ("mouthFrownLeft", "mouthFrownRight"), "jawOpen": ("jawOpen",)}


def expressions(rows, secs):
    B = bin_rows([r for r in rows if r.get("face")], 1000)
    per = {}
    for s in secs:
        rs = B.get(s["t_s"], [])
        if not rs:
            continue
        per[s["t_s"]] = {k: float(np.mean([np.mean([r["bs"].get(n, 0) for n in names]) for r in rs])) for k, names in AGG.items()}
    base_secs = [t for t in per if t < 60] or list(per)
    med = {k: float(np.median([per[t][k] for t in base_secs])) for k in AGG} if base_secs else {k: 0 for k in AGG}
    mad = {k: max(1e-3, float(np.median([abs(per[t][k] - med[k]) for t in base_secs]) * 1.4826)) for k in AGG} if base_secs else {k: 1 for k in AGG}
    out = []
    for s in secs:
        t = s["t_s"]; e = dict(t_s=t)
        if t in per:
            z = {k: (per[t][k] - med[k]) / mad[k] for k in AGG}
            e.update({f"expr_{k}": round(per[t][k], 3) for k in AGG}); e.update({f"expr_{k}_z": round(z[k], 2) for k in AGG})
            label = "neutral"
            if z["mouthSmile"] >= 1.5: label = "amused"
            elif z["browInnerUp"] >= 1.5 and z["eyeWide"] >= 1.0: label = "surprised"
            elif z["mouthPress"] >= 1.5: label = "tense"
            elif (z["browDown"] >= 1.5 or z["eyeSquint"] >= 1.5) and s.get("region_switches", 0) < 3: label = "concentrating"
            e["expr_label"] = label
            s.update({k2: v2 for k2, v2 in e.items() if k2.startswith("expr_")})
        out.append(e)
    # 3-second majority smoothing: cue labels are noisy per second and must not flap in the report
    labels = [e.get("expr_label") for e in out]
    for i, e in enumerate(out):
        if not labels[i]:
            continue
        win = [l for l in labels[max(0, i - 1):i + 2] if l]
        e["expr_label"] = Counter(win).most_common(1)[0][0]
        secs[i]["expr_label"] = e["expr_label"]
    return out


# ---------------- moments (§6) ----------------
def detect_moments(secs, clicks, vp):
    M = []; mid = 0
    def add(kind, a, b, score, ev, extra=None):
        nonlocal mid; mid += 1
        w = [s for s in secs if a <= s["t_s"] <= b]
        grades = [s["quality_grade"] for s in w]; worst = "F" if "F" in grades else "C" if "C" in grades else "B" if "B" in grades else "A"
        if worst == "F" and kind != "LOOK_AWAY":
            return
        dwell = Counter(s["gaze_cell"] for s in w if s.get("gaze_cell") and s["gaze_cell"] != "uncertain")
        tot = sum(dwell.values()) or 1
        M.append(dict(id=mid, type=kind, t_start_ms=a * 1000, t_end_ms=(b + 1) * 1000, score=round(min(score, 1), 2), evidence=ev,
                      gaze_dwell={k: round(v / tot, 2) for k, v in dwell.items()}, mouse_cells=sorted({s.get("mouse_cell") for s in w if s.get("mouse_cell")}),
                      clicks=[c for s in w for c in s["clicks"]], transcript=" ".join(s["speech_text"] for s in w if s["speech_text"])[:300],
                      min_quality_grade=worst, **(extra or {})))
    n = len(secs)
    # SEARCHING: 3 s window ≥6 switches, ≥3 cells, mouse path ≥300, no click
    i = 0
    while i < n - 2:
        w = secs[i:i + 3]
        sw = sum(s["region_switches"] for s in w); cells = len({c for s in w for c in [s.get("gaze_cell")] if c and c != "uncertain"})
        path = sum(s.get("mouse_path_px", 0) for s in w); clk = any(s["clicks"] for s in w)
        if sw >= 6 and cells >= 3 and path >= 300 and not clk:
            j = i + 3
            while j < n and not secs[j]["clicks"] and secs[j]["region_switches"] > 1:
                j += 1
            bonus = 0.2 if any("search_phrase" in s["speech_cues"] for s in secs[i:j]) else 0
            sc = 0.4 * min(sw / 8, 1) + 0.3 * min(cells / 5, 1) + 0.3 * min(path / 800, 1) + bonus
            add("SEARCHING", i, j - 1, sc, [dict(signal="region_switches", value=sw, threshold=6), dict(signal="distinct_cells", value=cells, threshold=3), dict(signal="mouse_path_px", value=path, threshold=300)])
            i = j
        else:
            i += 1
    # FOUND-THEN-ACTED / MISS-THEN-CORRECT
    for k, c in enumerate(clicks):
        cs = int(c["t_ms"] // 1000); ccell = vp.cell(c["x"], c["y"])[2]
        pre = [s for s in secs if cs - 2 <= s["t_s"] <= cs and s.get("gaze_cell") == ccell and s["region_switches"] <= 1]
        if pre and ccell:
            lat = c["t_ms"] / 1000 - pre[0]["t_s"]
            conf = np.mean([s["gaze_conf"] for s in pre])
            add("FOUND_THEN_ACTED", pre[0]["t_s"], cs, 1 - 0.5 * min(lat / 2, 1) - (0.3 if conf < 0.5 else 0),
                [dict(signal="dwell_cell", value=ccell), dict(signal="latency_s", value=round(lat, 1), threshold=2), dict(signal="click_target", value=c["text"])])
        elif k + 1 < len(clicks) and clicks[k + 1]["t_ms"] - c["t_ms"] <= 3000:
            dcell = Counter(s.get("gaze_cell") for s in secs if cs - 2 <= s["t_s"] <= cs and s.get("gaze_cell") not in (None, "uncertain")).most_common(1)
            ncell = vp.cell(clicks[k + 1]["x"], clicks[k + 1]["y"])[2]
            if dcell and dcell[0][0] != ccell and dcell[0][0] == ncell:
                add("MISS_THEN_CORRECT", cs, int(clicks[k + 1]["t_ms"] // 1000), 0.6 + (0.2 if c["dead"] else 0),
                    [dict(signal="dwell_cell", value=dcell[0][0]), dict(signal="first_click_cell", value=ccell), dict(signal="second_click_cell", value=ncell)])
    # LOOK-AWAY
    i = 0
    while i < n:
        s = secs[i]
        trig = "no_face" if s["face_present"] < 0.5 else s["gaze_state"] if s["gaze_state"] in ("down_keyboard", "off_left", "off_right", "off_up", "away") else None
        if trig:
            j = i
            while j + 1 < n and (secs[j + 1]["face_present"] < 0.5 or secs[j + 1]["gaze_state"] == trig):
                j += 1
            add("LOOK_AWAY", i, j, 0.9 if trig in ("no_face", "down_keyboard", "away") else 0.7, [dict(signal="trigger", value=trig, duration_s=j - i + 1)],
                dict(subtype="keyboard-glance" if trig == "down_keyboard" else "off-screen"))
            i = j + 1
        else:
            i += 1
    # CONFUSION: 6 s window ≥4 switches AND face cue AND (silence ≥4 or search phrase) AND no page-changing click.
    # Without a transcript "silence" is meaningless, so the speech channel is dropped and the score capped.
    has_transcript = any(s["speaking"] for s in secs)
    for i in range(0, n - 5):
        w = secs[i:i + 6]
        sw = sum(s["region_switches"] for s in w)
        face = any((s.get("expr_browDown_z", 0) >= 1.5 or s.get("expr_eyeSquint_z", 0) >= 1.5) for s in w)
        phrase = any("search_phrase" in s["speech_cues"] for s in w)
        sp = phrase or (has_transcript and any(s["silence_run_s"] >= 4 for s in w))
        prod = any(s["page_changed"] for s in w)
        if sw >= 4 and face and (sp or not has_transcript) and not prod:
            sc = 0.35 * min(sw / 8, 1) + 0.35 + (0.3 if sp else 0.0)
            if sc >= 0.6 and not any(m["type"] == "CONFUSION" and m["t_end_ms"] >= i * 1000 for m in M):
                ev = [dict(signal="region_switches", value=sw, threshold=4), dict(signal="face_cue", value="browDown/eyeSquint z≥1.5")]
                ev.append(dict(signal="speech", value="search phrase" if phrase else "silence≥4s") if sp else dict(signal="speech", value="transcript_missing (channel skipped)"))
                add("CONFUSION", i, i + 5, sc, ev)
    # FRUSTRATION: ≥2 channels within 5 s
    for i in range(n):
        w = secs[max(0, i - 2):i + 3]
        beh = any(c["is_rage"] or c["is_dead"] for s in w for c in s["clicks"]) or any(s["scroll_thrash"] for s in w)
        fac = any(s.get("expr_mouthPress_z", 0) >= 1.5 or s.get("expr_browDown_z", 0) >= 1.5 or s.get("expr_jawOpen_z", 0) >= 2 for s in w)
        verb = any("negative" in s["speech_cues"] for s in w)
        ch = sum([beh, fac, verb])
        if ch >= 2 and not any(m["type"] == "FRUSTRATION" and abs(m["t_start_ms"] - i * 1000) < 5000 for m in M):
            add("FRUSTRATION", max(0, i - 2), min(n - 1, i + 2), 0.5 + 0.25 * (ch - 2), [dict(signal="channels", value=dict(behavioural=beh, facial=fac, verbal=verb))])
    M.sort(key=lambda m: m["t_start_ms"])
    return M


# ---------------- sign self-test (§7.1) ----------------
def selftest_intervals(events):
    """From `selftest` events (detail = LEFT/RIGHT/TOP/BOTTOM/END) → {phase: (t_start_ms, t_end_ms)}."""
    ev = [(float(e["t_ms"]), (e.get("detail") or e.get("txt") or "").strip().upper()) for e in (events or []) if e.get("type") == "selftest"]
    ev.sort()
    out = {}
    for i, (t, ph) in enumerate(ev):
        if ph in ("LEFT", "RIGHT", "TOP", "BOTTOM"):
            t_end = ev[i + 1][0] if i + 1 < len(ev) else t + 2000
            out[ph] = (t, t_end)
    return out


def evaluate_selftest(rows, intervals):
    """PASS if median_h(RIGHT)−median_h(LEFT) > +8° and > 2× pooled within-interval SD; vertical: median_v(TOP)−median_v(BOTTOM) > +8°.
    Also checks MediaPipe iris_right and head-yaw co-rotation. First 500 ms of each interval dropped; medians, not means."""
    if not intervals or not all(k in intervals for k in ("LEFT", "RIGHT")):
        return None
    fr = [r for r in rows if r.get("h") is not None]
    if len(fr) < 10:
        return dict(status="inconclusive", note="too few gaze frames")
    medR = float(np.median([r["hR"] for r in fr])); medL = float(np.median([r["hL"] for r in fr]))
    def sel(ph):
        a, b = intervals[ph]
        return [r for r in fr if a + 500 <= r["t_ms"] <= b]
    def stats(ph):
        s = sel(ph)
        if len(s) < 3:
            return None
        return dict(n=len(s), h=float(np.median([math.degrees(r["h"]) for r in s])), h_sd=float(np.std([math.degrees(r["h"]) for r in s])),
                    v=float(np.median([math.degrees(r["v"]) for r in s])),
                    iris_right=float(np.median([-(((r["hR"] - medR) + (r["hL"] - medL)) / 2) for r in s])),
                    head_yaw=float(np.median([math.degrees(r["head_yaw"]) for r in s])))
    P = {ph: stats(ph) for ph in intervals}
    out = dict(phases={k: ({kk: round(vv, 2) for kk, vv in v.items()} if v else None) for k, v in P.items()})
    L, R = P.get("LEFT"), P.get("RIGHT")
    if L and R:
        dh = R["h"] - L["h"]; pooled = math.sqrt((L["h_sd"] ** 2 + R["h_sd"] ** 2) / 2)
        out["horizontal"] = dict(delta_deg=round(dh, 1), pooled_sd=round(pooled, 1),
                                 status="PASS" if (dh > 8 and dh > 2 * pooled) else "INVERTED" if dh < -8 else "WEAK",
                                 iris_right_delta=round(R["iris_right"] - L["iris_right"], 3), iris_ok=(R["iris_right"] - L["iris_right"]) > 0.05,
                                 head_corotates=R["head_yaw"] > L["head_yaw"])
    T, B = P.get("TOP"), P.get("BOTTOM")
    if T and B:
        dv = T["v"] - B["v"]
        out["vertical"] = dict(delta_deg=round(dv, 1), status="PASS" if dv > 8 else "INVERTED" if dv < -8 else "WEAK")
    hs = out.get("horizontal", {}).get("status"); vs = out.get("vertical", {}).get("status")
    out["status"] = "FAIL" if "INVERTED" in (hs, vs) else "PASS" if hs == "PASS" and vs in ("PASS", None) else "WEAK"
    return out


# ---------------- writers ----------------
def write_csv(path, rows, cols):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(cols)
        for r in rows:
            w.writerow([json.dumps(r[c], ensure_ascii=False) if isinstance(r.get(c), (list, dict)) else ("" if r.get(c) is None else r.get(c)) for c in cols])


def draw_debug(video, rows, out_dir, k=20):
    """Annotated frames: arrow dx = −sin(h) in IMAGE space (points image-left when the participant looks right)."""
    os.makedirs(os.path.join(out_dir, "debug"), exist_ok=True)
    picks = [r for r in rows if r.get("h") is not None and r.get("conf", 0) > 0.3]
    picks = picks[:: max(1, len(picks) // k)][:k]
    want = {r["t_ms"]: r for r in picks}
    for t_ms, rgb in decode_frames(video):
        if t_ms in want:
            r = want[t_ms]; img = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            u, v = int(r["u_e"]), int(r["v_e"]); L = 120
            dx = -math.sin(r["h"]) * math.cos(r["v"]); dy = -math.sin(r["v"])
            cv2.arrowedLine(img, (u, v), (int(u + L * dx), int(v + L * dy)), (0, 255, 0), 2, tipLength=0.2)
            cv2.putText(img, f"t={t_ms/1000:.1f}s h={math.degrees(r['h']):+.0f} v={math.degrees(r['v']):+.0f} {r.get('cell') or r.get('state')}", (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)
            cv2.imwrite(os.path.join(out_dir, "debug", f"frame_{t_ms:07d}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 80])
            del want[t_ms]
            if not want:
                break
