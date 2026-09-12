#!/usr/bin/env python3
"""Read the screen recording with a local vision model.

The measurements say WHERE someone looked and clicked; they cannot say WHAT was there. This stage samples the screen
recording at the moments that matter, marks the exact point the participant clicked (or the cell they were reading),
and asks a local VLM what is under that mark. The writer then names real interface elements — "the status chip
'قيد المراجعة الطبية', which is not clickable" instead of "cell MR".

Everything stays on the machine: Ollama, qwen3-vl by default. Usage:
    bux-screen-read.py <session-dir> [--model qwen3-vl:8b-instruct] [--max-frames 24]
"""
import argparse
import base64
import csv
import json
import math
import os
import subprocess
import sys
import time
import urllib.request

OLLAMA = os.environ.get("BUX_OLLAMA_URL", "http://127.0.0.1:11434")
DEFAULT_MODEL = os.environ.get("BUX_VLM_MODEL", "qwen3-vl:8b-instruct")
PREFERRED = [DEFAULT_MODEL, "qwen3-vl:8b-instruct", "qwen3-vl:8b", "minicpm-v:latest", "qwen2.5vl:3b", "granite3.2-vision:latest"]


def log(m):
    print(f"[bux] {m}", flush=True)


def installed_models():
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=5) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except Exception:  # noqa: BLE001
        return []


def pick_model(explicit=None):
    have = installed_models()
    for want in ([explicit] if explicit else []) + PREFERRED:
        if not want:
            continue
        for h in have:
            if h == want or h.split(":")[0] == want.split(":")[0]:
                return h
    return None


def unload(model):
    try:
        req = urllib.request.Request(OLLAMA + "/api/generate", data=json.dumps(dict(model=model, keep_alive=0)).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:  # noqa: BLE001
        pass


def ask(model, prompt, jpg_path, timeout=180):
    with open(jpg_path, "rb") as f:
        img = base64.b64encode(f.read()).decode()
    body = json.dumps(dict(model=model, stream=False, format="json", think=False,
                           options=dict(temperature=0.1, num_ctx=8192, num_predict=600),
                           messages=[dict(role="user", content=prompt, images=[img])])).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.load(r)
    txt = (out.get("message") or {}).get("content") or ""
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        i, j = txt.find("{"), txt.rfind("}")
        try:
            return json.loads(txt[i:j + 1])
        except Exception:  # noqa: BLE001
            return None


# ---------------- geometry: page coordinates → pixels in the recording ----------------
def surface(session_json, rec_w, rec_h):
    """Which surface was shared? Returns (kind, x_off, y_off, scale) mapping page coords to recorded pixels."""
    S = session_json
    dpr = float(S.get("dpr") or 1)
    vp = S.get("viewport") or {}
    vw, vh = float(vp.get("stageW") or 0), float(vp.get("stageH") or 0)
    win = S.get("window") or {}
    scr = S.get("screen") or {}
    cands = []
    if vw and vh:                                   # a single tab: the page viewport fills the recording
        cands.append(("tab", vw, vh, 0, 0))
    if win.get("outerWidth"):                       # the browser window: the page sits below the browser chrome
        chrome = float(win.get("outerHeight") or 0) - float(win.get("innerHeight") or 0)
        cands.append(("window", float(win["outerWidth"]), float(win.get("outerHeight") or 0), 0, chrome))
    if scr.get("width"):                            # the whole screen: add where the window sits on it
        chrome = float(win.get("outerHeight") or 0) - float(win.get("innerHeight") or 0)
        cands.append(("screen", float(scr["width"]), float(scr["height"]), float(win.get("screenX") or 0),
                      float(win.get("screenY") or 0) + chrome))
    best, err = None, 1e9
    for kind, w, h, xo, yo in cands:
        if not w or not h:
            continue
        for s in (dpr, 1.0):                        # the capture may or may not be at device pixel ratio
            e = abs(rec_w - w * s) + abs(rec_h - h * s)
            if e < err:
                err, best = e, (kind, xo, yo, s)
    if best is None or err > 80:
        return ("unknown", 0, 0, dpr)
    return best


def grab(video, t_s, out_jpg, width=1400):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0, t_s):.2f}", "-i", video,
                    "-frames:v", "1", "-vf", f"scale={width}:-2", out_jpg], check=True, timeout=120)
    return os.path.exists(out_jpg) and os.path.getsize(out_jpg) > 2000


def grab_marked(video, t_s, out_jpg, point=None, box=None, rec_w=1920, width=1400):
    """Same frame, with a crosshair at `point` (recorded-pixel coords) or a rectangle around `box`."""
    import cv2  # noqa: PLC0415 — only needed when a mark is drawn
    tmp = out_jpg + ".full.png"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{max(0, t_s):.2f}", "-i", video,
                    "-frames:v", "1", tmp], check=True, timeout=120)
    im = cv2.imread(tmp)
    os.remove(tmp)
    if im is None:
        return False
    H, W = im.shape[:2]
    if point:
        x, y = int(point[0]), int(point[1])
        r = max(28, int(W * 0.022))
        cv2.circle(im, (x, y), r, (0, 0, 255), max(3, r // 12))
        cv2.line(im, (x - r * 2, y), (x + r * 2, y), (0, 0, 255), 2)
        cv2.line(im, (x, y - r * 2), (x, y + r * 2), (0, 0, 255), 2)
    if box:
        x0, y0, x1, y1 = (int(v) for v in box)
        cv2.rectangle(im, (x0, y0), (x1, y1), (0, 210, 255), max(3, W // 400))
    im = cv2.resize(im, (width, int(width * H / W)), interpolation=cv2.INTER_AREA)
    cv2.imwrite(out_jpg, im, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return True


PROMPT_CONTEXT = ("This is a screenshot from a usability test. Return JSON with keys: screen_name (a short English name for this "
                  "page or view), language, purpose (one sentence: what the user can do here), main_sections (up to 6 visible "
                  "areas, English), notable (anything visually unusual: a modal, a dimmed overlay, an empty state, an error, a "
                  "loading state — else empty string). Describe only what you can actually see.")

PROMPT_CLICK = ("A participant in a usability test clicked at the RED CROSSHAIR. Return JSON with keys: element_at_marker (the "
                "visible text of whatever is under the crosshair, in its original language), element_translation (English), "
                "element_kind (button|link|heading|label|text|input|row|icon|image|empty_space|other), looks_clickable (true or "
                "false), why (at most 12 words), screen_name (short English name of this page), nearby_actions (up to 3 real "
                "controls near the marker, with their visible text). If the crosshair is over plain text or empty space, say so.")

PROMPT_REGION = ("The ORANGE RECTANGLE marks the part of the screen a participant was looking at. Return JSON with keys: "
                 "region_contents (what is inside the rectangle — visible text and controls, original language), "
                 "region_translation (English), reading_target (the single most likely thing they were reading, English), "
                 "screen_name (short English name of this page).")

CELLS = {"TL": (0, 0), "TC": (1, 0), "TR": (2, 0), "ML": (0, 1), "MC": (1, 1), "MR": (2, 1), "BL": (0, 2), "BC": (1, 2), "BR": (2, 2)}


def contact_sheet(reads, out_path, cols=2, tile_w=760):
    """One image showing every frame the model looked at and the answer it gave — the quickest way to check it is right."""
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    tiles = []
    for r in reads:
        im = cv2.imread(r["_abs"]) if os.path.exists(r.get("_abs", "")) else None
        if im is None:
            continue
        h = int(tile_w * im.shape[0] / im.shape[1])
        im = cv2.resize(im, (tile_w, h), interpolation=cv2.INTER_AREA)
        k = r.get("read") or {}
        t = int(r.get("t_s") or 0)
        if r["kind"] == "click":
            lines = [f"{t//60}:{t%60:02d}  CLICK on: {k.get('element_at_marker','?')}",
                     f"   kind={k.get('element_kind','?')}  looks clickable={k.get('looks_clickable')}  ({k.get('why','')})",
                     f"   page: {k.get('screen_name','')}"]
        elif r["kind"] == "moment":
            lines = [f"{t//60}:{t%60:02d}  LOOKING AT cell {r.get('cell','-')} during {r.get('moment','')}",
                     f"   reading: {str(k.get('reading_target') or k.get('screen_name',''))[:70]}",
                     f"   {str(k.get('region_translation') or k.get('purpose',''))[:70]}"]
        else:
            lines = [f"{t//60}:{t%60:02d}  SCREEN: {k.get('screen_name','?')}",
                     f"   {str(k.get('purpose',''))[:72]}",
                     f"   {str(k.get('notable') or '')[:72]}"]
        cap = np.zeros((26 * len(lines) + 14, tile_w, 3), np.uint8) + 22
        for i, ln in enumerate(lines):
            cv2.putText(cap, ln[:96], (10, 24 + i * 25), cv2.FONT_HERSHEY_SIMPLEX, 0.46,
                        (90, 200, 255) if i == 0 else (215, 215, 215), 1, cv2.LINE_AA)
        tiles.append(np.vstack([im, cap]))
    if not tiles:
        return
    hmax = max(t.shape[0] for t in tiles)
    tiles = [np.vstack([t, np.zeros((hmax - t.shape[0], t.shape[1], 3), np.uint8) + 22]) for t in tiles]
    rows = [np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)]
    if len(rows) > 1 and rows[-1].shape[1] < rows[0].shape[1]:
        pad = np.zeros((rows[-1].shape[0], rows[0].shape[1] - rows[-1].shape[1], 3), np.uint8) + 22
        rows[-1] = np.hstack([rows[-1], pad])
    cv2.imwrite(out_path, np.vstack(rows), [cv2.IMWRITE_JPEG_QUALITY, 82])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-frames", type=int, default=0, help="0 = scale with the recording length")
    a = ap.parse_args()

    sess = os.path.abspath(os.path.expanduser(a.session))
    A = os.path.join(sess, "analysis")
    os.makedirs(A, exist_ok=True)
    video = os.path.join(sess, "screen.webm")
    if not os.path.exists(video) or os.path.getsize(video) < 20000:
        log("no screen recording in this session — skipping the visual pass")
        json.dump(dict(available=False, reason="no screen.webm"), open(os.path.join(A, "screens.json"), "w"))
        return 0

    model = pick_model(a.model)
    if not model:
        log("no local vision model installed (try: ollama pull qwen3-vl:8b-instruct) — skipping the visual pass")
        json.dump(dict(available=False, reason="no vision model"), open(os.path.join(A, "screens.json"), "w"))
        return 0

    S = json.load(open(os.path.join(sess, "session.json"), encoding="utf-8"))
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0", video], capture_output=True, text=True, timeout=60).stdout.strip()
    rec_w, rec_h = (int(v) for v in probe.split(",")[:2])
    kind, xo, yo, scale = surface(S, rec_w, rec_h)
    log(f"screen recording {rec_w}x{rec_h} = {kind} capture (offset {xo:.0f},{yo:.0f} scale {scale:g})")

    def to_px(x, y):
        return ((x + xo) * scale, (y + yo) * scale)

    try:
        moments = json.load(open(os.path.join(A, "moments.json"), encoding="utf-8")).get("moments", [])
    except Exception:  # noqa: BLE001
        moments = []
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from bux_gaze import fuse as F                    # noqa: PLC0415
        _S, _mouse, _events, clicks, _rage, _thrash, _pages = F.load_session_files(sess)
    except Exception as e:  # noqa: BLE001
        log(f"click list unavailable ({str(e)[:60]}) — reading context frames only")
        clicks = []
    try:
        secs = list(csv.DictReader(open(os.path.join(A, "gaze-ai.csv"), encoding="utf-8")))
    except Exception:  # noqa: BLE001
        secs = []
    dur = float(secs[-1]["t_s"]) if secs else 0

    # ---- choose the frames worth looking at ----
    shots = []
    seen_click = set()
    for c in clicks:
        if c.get("x") is None or c.get("y") is None:
            continue
        t = float(c["t_ms"]) / 1000
        key = (c.get("text") or "", round(t / 5))              # one look per target per 5 s: a rage burst is one question
        if key in seen_click:
            continue
        seen_click.add(key)
        shots.append(dict(kind="click", t_s=round(t, 2), label=c.get("text") or "", x=float(c["x"]), y=float(c["y"]),
                          dead=bool(c.get("dead"))))
    for m in moments:
        t = (m["t_start_ms"] + m["t_end_ms"]) / 2000
        cell = None
        dwell = m.get("gaze_dwell") or {}
        if dwell:
            cell = max(dwell.items(), key=lambda kv: kv[1])[0]
        shots.append(dict(kind="moment", t_s=round(t, 2), moment=m["type"], subtype=m.get("subtype") or "", cell=cell,
                          said=(m.get("transcript") or "")[:120]))
    step = max(20, int(dur / 8) if dur else 20)
    for t in range(3, int(dur) + 1, step):
        shots.append(dict(kind="context", t_s=float(t)))

    shots.sort(key=lambda s_: s_["t_s"])
    merged = []
    for s_ in shots:                                            # never ask twice about the same second
        if merged and abs(merged[-1]["t_s"] - s_["t_s"]) < 1.0 and merged[-1]["kind"] == s_["kind"]:
            continue
        merged.append(s_)
    order = {"click": 0, "moment": 1, "context": 2}
    max_frames = a.max_frames or max(6, min(24, int(6 + dur / 45)))    # ~9 s per frame: keep the visual pass proportionate
    if len(merged) > max_frames:
        merged = sorted(sorted(merged, key=lambda s_: (order[s_["kind"]], s_["t_s"]))[:max_frames], key=lambda s_: s_["t_s"])
    log(f"{len(merged)} frames to read with {model} ({sum(1 for s_ in merged if s_['kind']=='click')} clicks, "
        f"{sum(1 for s_ in merged if s_['kind']=='moment')} moments)")

    vp = S.get("viewport") or {}
    vw, vh = float(vp.get("stageW") or 0), float(vp.get("stageH") or 0)
    shots_dir = os.path.join(A, "screens")
    os.makedirs(shots_dir, exist_ok=True)
    out, t0 = [], time.time()
    for i, s_ in enumerate(merged, 1):
        jpg = os.path.join(shots_dir, f"{i:02d}_{s_['kind']}_{int(s_['t_s'])}s.jpg")
        try:
            if s_["kind"] == "click":
                px, py = to_px(s_["x"], s_["y"])
                ok = grab_marked(video, s_["t_s"], jpg, point=(px, py), rec_w=rec_w)
                prompt = PROMPT_CLICK
            elif s_["kind"] == "moment" and s_.get("cell") in CELLS and vw and vh:
                cx, cy = CELLS[s_["cell"]]
                x0, y0 = to_px(cx * vw / 3, cy * vh / 3)
                x1, y1 = to_px((cx + 1) * vw / 3, (cy + 1) * vh / 3)
                ok = grab_marked(video, s_["t_s"], jpg, box=(x0, y0, x1, y1), rec_w=rec_w)
                prompt = PROMPT_REGION
            else:
                ok = grab(video, s_["t_s"], jpg)
                prompt = PROMPT_CONTEXT
            if not ok:
                continue
            r = ask(model, prompt, jpg)
        except Exception as e:  # noqa: BLE001
            log(f"  frame {i} at {s_['t_s']:.0f}s failed: {str(e)[:90]}")
            continue
        if not r:
            continue
        rec = dict(s_)
        rec["read"] = r
        rec["image"] = os.path.relpath(jpg, sess); rec["_abs"] = jpg
        out.append(rec)
        log(f"  {i}/{len(merged)} {s_['kind']} at {int(s_['t_s'])//60}:{int(s_['t_s'])%60:02d} → "
            f"{str(r.get('element_at_marker') or r.get('reading_target') or r.get('screen_name'))[:60]}")

    unload(model)
    try:
        contact_sheet(out, os.path.join(shots_dir, "contact-sheet.jpg"))
        log("contact sheet → analysis/screens/contact-sheet.jpg (what the model saw, with its answer)")
    except Exception as e:  # noqa: BLE001
        log(f"contact sheet skipped: {str(e)[:80]}")
    for r in out:
        r.pop("_abs", None)
    json.dump(dict(available=True, model=model, surface=kind, frames=len(out), reads=out),
              open(os.path.join(A, "screens.json"), "w"), ensure_ascii=False, indent=1)
    log(f"screen reading done in {time.time()-t0:.0f}s → analysis/screens.json ({len(out)} frames)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
