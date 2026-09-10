#!/usr/bin/env python3
"""
bux-daemon — Boring UX local processing service (127.0.0.1:7331, loopback only).

The extension hands a finished recording to this service; it turns it into a report:
  analyze (bux-analyze-video) → scaffold (bux-report) → fill findings (claude -p) → PDF (headless Chrome) → open.
Jobs are persisted to disk (~/Desktop/gaze-ai/jobs/*.json) so they survive tab refreshes, browser restarts and
daemon restarts; they can be paused / resumed / cancelled / retried; any tab shows the same job.

API (JSON, CORS *):
  GET  /health                       → {ok, version, jobs, claude}
  POST /jobs {downloads_rel|folder, product?} → job          (waits for the download to finish landing)
  GET  /jobs                         → [job]      GET /jobs/<id> → job
  POST /jobs/<id>/pause|resume|cancel|retry|open → job
job = {id, folder, product, status: queued|running|paused|done|error|cancelled, stage, stage_label, progress,
       eta_s, video_s, log[], error, report_pdf, report_html, warnings[], created, updated}
Installed at login by tools/install-daemon.sh (launchd).  Run manually:  python3 tools/bux-daemon.py
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
HOME = os.path.expanduser("~")
# Everything the service reads/writes lives OUTSIDE Desktop/Documents/Downloads: macOS denies those folders to
# background services without a manual "Full Disk Access" grant. Sessions are uploaded here by the extension.
AI = os.environ.get("BUX_AI_DIR") or (os.path.join(HOME, ".boring-ux") if not os.path.isdir(os.path.join(HOME, "Desktop", "gaze-ai", ".venv")) or os.path.isdir(os.path.join(HOME, ".boring-ux", ".venv")) else os.path.join(HOME, "Desktop", "gaze-ai"))
REPO = os.environ.get("BUX_REPO", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PY = os.path.join(AI, ".venv", "bin", "python")
JOBS = os.path.join(AI, "jobs"); LOGS = os.path.join(AI, "logs"); SESSIONS = os.path.join(AI, "sessions")
MAX_UPLOAD = 2 * 1024 ** 3
PORT = int(os.environ.get("BUX_PORT", "7331"))
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
RATE = 0.30            # measured: analysis wall-clock ≈ 0.30 × video length on Apple Silicon (GPU)
FILL_EST_S = 150       # typical claude fill time
STAGES = [("analyze", "Analyzing eyes, face & expressions", 0.52), ("screen", "Reading the screen recording", 0.12),
          ("scaffold", "Building the report structure", 0.03), ("fill", "Writing the findings (local model)", 0.26),
          ("pdf", "Rendering the PDF", 0.04), ("open", "Opening the report", 0.03)]
for _d in (JOBS, LOGS, SESSIONS):
    os.makedirs(_d, exist_ok=True)

LOCK = threading.Lock()
JOBSTATE = {}          # id -> job dict
CTRL = {}              # id -> {"pause": bool, "cancel": bool, "proc": Popen|None}
WAKE = threading.Event()


def now():
    return time.time()


def save(job):
    with open(os.path.join(JOBS, job["id"] + ".json"), "w") as f:
        json.dump(job, f, ensure_ascii=False, indent=1)


def logj(job, msg):
    job["log"] = (job.get("log") or [])[-40:] + [f"{time.strftime('%H:%M:%S')} {msg}"]
    job["updated"] = now(); save(job)


def which(cmd):
    for p in os.environ.get("PATH", "").split(":") + ["/opt/homebrew/bin", "/usr/local/bin", os.path.join(HOME, ".local", "bin")]:
        c = os.path.join(p, cmd)
        if os.access(c, os.X_OK):
            return c
    return None


def video_seconds(folder):
    for f in ("audio.webm", "face.webm"):
        p = os.path.join(folder, f)
        if os.path.exists(p):
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p], capture_output=True, text=True).stdout.strip()
            try:
                return float(out)
            except ValueError:
                pass
    try:
        return float(json.load(open(os.path.join(folder, "session.json"))).get("durationSec") or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def set_progress(job, stage_i, frac_in_stage, eta_s=None):
    done = sum(w for _, _, w in STAGES[:stage_i]); w = STAGES[stage_i][2]
    job["stage"], job["stage_label"] = STAGES[stage_i][0], STAGES[stage_i][1]
    job["progress"] = round(min(0.999, done + w * max(0.0, min(1.0, frac_in_stage))), 3)
    if eta_s is not None:
        job["eta_s"] = int(max(0, eta_s))
    job["updated"] = now(); save(job)


# ---------------- stages ----------------
def wait_for_files(job):
    """The extension posts right after it starts the downloads; wait for face.webm + session.json to land and settle."""
    s = os.path.join(job["folder"], "session.json")
    def biggest():                      # the camera may not have recorded at all: wait on whatever media the session does have
        c = [os.path.join(job["folder"], n) for n in ("face.webm", "screen.webm", "audio.webm")]
        c = [p_ for p_ in c if os.path.exists(p_)]
        return max(c, key=os.path.getsize) if c else None
    last, stable, t0 = -1, 0, now()
    while now() - t0 < 120:
        if CTRL[job["id"]]["cancel"]:
            return False
        f = biggest()
        if f and os.path.exists(s):
            sz = os.path.getsize(f)
            stable = stable + 1 if sz == last and sz > 0 else 0; last = sz
            if stable >= 2:
                return True
        time.sleep(1)
    return biggest() is not None


def run_proc(job, cmd, on_line=None, timeout=3600):
    env = dict(os.environ, PATH="/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", ""), PYTORCH_ENABLE_MPS_FALLBACK="1",
               BUX_AI_DIR=AI, BUX_MODELS=os.path.join(AI, "models"))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, cwd=REPO)
    CTRL[job["id"]]["proc"] = p
    tail = []
    t0 = now()
    for line in p.stdout:
        line = line.rstrip()
        if line and not re.match(r"^(W0000|I0000|WARNING: All log|objc\[)", line):
            tail = (tail + [line])[-60:]
            if on_line:
                on_line(line)
        if CTRL[job["id"]]["cancel"] or CTRL[job["id"]]["pause"]:
            p.terminate(); break
        if now() - t0 > timeout:
            p.terminate(); tail.append("timeout"); break
    p.wait(); CTRL[job["id"]]["proc"] = None
    return p.returncode, tail


TRANSCRIBE_LOCK = threading.Lock()


def transcribe_pcm(raw, lang="auto"):
    """Live captions for the extension: int16 16 kHz mono PCM → whisper.cpp (VAD-gated) → {text, lang, p, ms}."""
    import tempfile, wave
    t0 = now()
    models = sorted(f for f in glob.glob(os.path.join(AI, "models", "ggml-*.bin")) if "silero" not in f)
    if not models or not which("whisper-cli"):
        return dict(text="", lang=None, p=0, error="whisper not installed")
    vad = os.path.join(AI, "models", "ggml-silero-v5.1.2.bin")
    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "c.wav")
        with wave.open(wav, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(raw)
        cmd = ["whisper-cli", "-m", models[0], "-f", wav, "-l", lang, "-bs", "5", "-nt", "-of", os.path.join(d, "o"), "-otxt"]
        if os.path.exists(vad):
            cmd += ["--vad", "-vm", vad, "-vt", "0.5", "-vspd", "250", "-vsd", "300", "-vp", "200"]
        with TRANSCRIBE_LOCK:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        txt = ""
        try:
            txt = open(os.path.join(d, "o.txt"), encoding="utf-8").read()
        except Exception:  # noqa: BLE001
            pass
    m = re.search(r"auto-detected language: (\w+) \(p = ([\d.]+)\)", (r.stderr or "") + (r.stdout or ""))
    txt = " ".join(txt.split())
    if txt.strip() in ("Thank you.", "اشتركوا في القناة", "ترجمة نانسي قنقر"):
        txt = ""
    return dict(text=txt, lang=m.group(1) if m else lang, p=float(m.group(2)) if m else None, ms=int((now() - t0) * 1000))


def ollama_unload():
    """Free the writer model (~14 GB resident) before the analysis stage — on a 24 GB Mac both together push the system into swap."""
    try:
        import urllib.request as _u
        loaded = json.loads(_u.urlopen("http://127.0.0.1:11434/api/ps", timeout=5).read() or b"{}").get("models") or []
        for m_ in loaded or [dict(name=OLLAMA_MODEL or "")]:
            body = json.dumps(dict(model=m_.get("name") or m_.get("model") or "", keep_alive=0)).encode()
            _u.urlopen(_u.Request("http://127.0.0.1:11434/api/generate", data=body, headers={"Content-Type": "application/json"}), timeout=10).read()
    except Exception:  # noqa: BLE001
        pass


def stage_analyze(job):
    ollama_unload()
    vs = job["video_s"] or 60
    whisper = sorted(f for f in glob.glob(os.path.join(AI, "models", "ggml-*.bin")) if "silero" not in f)
    cmd = [PY, os.path.join(REPO, "tools", "bux-analyze-video.py"), job["folder"]] + (["--whisper-model", whisper[0]] if whisper else [])
    t0 = now()
    def on_line(line):
        m = re.search(r"… (\d+)s decoded", line)
        g = re.search(r"… gaze (\d+)/(\d+)", line)
        def eta(frac, elapsed):
            # prior from the measured rate, replaced by extrapolation of the observed pace once we have some progress;
            # never negative even when a heavy file runs slower than the prior
            prior = vs * RATE - elapsed
            observed = (elapsed / frac) * (1 - frac) if frac > 0.05 else prior
            return max(10, observed if elapsed > vs * RATE * 0.5 else max(prior, observed * 0.5)) + FILL_EST_S + 20
        if m:                                              # decode + face pass = first 80 % of the stage
            frac = 0.8 * min(1.0, int(m.group(1)) / vs); elapsed = now() - t0
            set_progress(job, 0, frac, eta_s=eta(frac, elapsed))
        elif g:                                            # gaze pass = last 20 %
            frac = 0.8 + 0.2 * int(g.group(1)) / max(int(g.group(2)), 1); elapsed = now() - t0
            set_progress(job, 0, frac, eta_s=eta(frac, elapsed))
        elif "[bux]" in line:
            logj(job, line.replace("[bux] ", ""))
    rc, tail = run_proc(job, cmd, on_line)
    if CTRL[job["id"]]["pause"] or CTRL[job["id"]]["cancel"]:
        return "interrupted"
    if rc != 0 or not os.path.exists(os.path.join(job["folder"], "analysis", "quality.json")):
        raise RuntimeError("analysis failed: " + " | ".join(tail[-4:]))
    q = json.load(open(os.path.join(job["folder"], "analysis", "quality.json")))
    job["quality"] = dict(tier=q.get("click_consistency", {}).get("tier"), usable=q.get("gaze_usable_frac"), flags=q.get("flags"), moments=q.get("moments"))
    return "ok"


def stage_screen(job):
    """Ask a local vision model what was actually on screen where the participant clicked and looked.
    Optional by design: a session without a screen recording, or a machine without a vision model, just skips it."""
    ollama_unload()                                   # the writer model must not sit in memory next to the VLM
    rc, tail = run_proc(job, [PY, os.path.join(REPO, "tools", "bux-screen-read.py"), job["folder"]])
    if rc != 0:
        job.setdefault("warnings", []).append("screen reading failed: " + " | ".join(tail[-2:])[:200])
    return "ok"                                       # never fail the report over the visual pass


def stage_scaffold(job):
    rc, tail = run_proc(job, [PY, os.path.join(REPO, "tools", "bux-report.py"), job["folder"], "--product", job.get("product") or os.path.basename(job["folder"])])
    if rc != 0:
        raise RuntimeError("scaffold failed: " + " | ".join(tail[-3:]))
    return "ok"


FILL_RULES = """You are a senior UX researcher + product manager writing the findings of a moderated think-aloud usability test with
webcam eye tracking. You receive: the report scaffold's computed data (report-data.json), detected UX moments (moments.json),
the per-second fused timeline (gaze-ai.csv: gaze cell/state/conf, mouse cell, clicks, speech, expression z-scores, quality grade),
and the transcript. Fill ONLY the placeholders listed below and return ONE JSON object {placeholder_name: html_string}.
Rules: every finding cites said · eyes · time; quotes verbatim from the transcript (original language in the .ar box, English in .en
with [m:ss]); eyes from gaze cells/states/moments; NEVER invent quotes, clicks or events; expression cues are cue-level ("brow lowering
z 2.1"), never emotions as facts; obey the wording tier given (regions → firm columns; likely → 'likely'; unvalidated → every gaze
statement says 'estimated (unvalidated)' and findings lean on transcript, mouse, clicks, look-away). Use the finding-block HTML
template exactly for FINDINGS_*: <div class="finding"><h3>ID · Title <span class="p0|p1|p2|keep">P0|P1|P2|KEEP</span></h3>
<p><b>Page:</b> … <b>Task:</b> …</p><span class="ar">"quote"</span><div class="en">[m:ss] "translation"</div>
<div class="eyes">👁 <b>Eyes:</b> … ⏱ …</div><div class="rec"><b>Fix:</b> …</div></div>. Table placeholders take <tr>…</tr> rows only.
GRADE_TABLE: rows for AI & intelligence · Visual design · Data clarity · Delight · Task efficiency · Actionability · Discoverability.
If evidence is thin (no speech, no clicks), say so plainly inside the relevant placeholder instead of inventing. Output JSON only."""


# ---- report writer backends -------------------------------------------------------------------------------
# BUX_LLM = auto | ollama | claude | none.  Default "auto" = local Ollama when a model is present, otherwise "none"
# (data report with the judgment sections marked as not written). Claude is used ONLY when explicitly requested.
OLLAMA = os.environ.get("BUX_OLLAMA_URL", "http://127.0.0.1:11434")
# Preferred local writers, best first. BUX_LLM_MODEL pins one; otherwise the first one already pulled is used.
PREFERRED = [m for m in [os.environ.get("BUX_LLM_MODEL")] if m] + ["gemma3:12b", "qwen3:14b", "qwen2.5:14b", "gemma3:27b", "qwen3:8b", "llama3.1:8b", "qwen2.5:7b"]
OLLAMA_MODEL = PREFERRED[0]


def ollama_ready(start=True):
    """(ok, note). Starts the Ollama server if needed and picks the first preferred model that is pulled."""
    global OLLAMA_MODEL
    import urllib.request
    def tags():
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3) as r:
            return [m["name"] for m in json.loads(r.read()).get("models", [])]
    try:
        models = tags()
    except Exception:  # noqa: BLE001
        if not start or not which("ollama"):
            return False, "ollama not running"
        subprocess.Popen([which("ollama"), "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(20):
            time.sleep(1)
            try:
                models = tags(); break
            except Exception:  # noqa: BLE001
                continue
        else:
            return False, "ollama did not start"
    have = {m: m for m in models} | {m.split(":")[0]: m for m in models}
    for want in PREFERRED:
        hit = have.get(want) or have.get(want.split(":")[0])
        if hit:
            OLLAMA_MODEL = hit
            return True, "ok"
    return False, f"none of {', '.join(PREFERRED[:3])} is pulled (have: {', '.join(models) or 'none'})"


def llm_status(start=False):
    mode = os.environ.get("BUX_LLM", "auto")
    if mode == "claude":
        return dict(backend="claude", model=os.environ.get("BUX_CLAUDE_MODEL", "claude-opus-5"), available=bool(which("claude")))
    if mode == "none":
        return dict(backend="none", model=None, available=True)
    ok, why = ollama_ready(start=start)
    if ok or mode == "ollama":
        return dict(backend="ollama", model=OLLAMA_MODEL, available=ok, note=None if ok else why)
    return dict(backend="none", model=None, available=True, note="no local model — " + why)


def write_with_ollama(job, prompt, est, t0, out_dir=None, tag="", pbase=0.0, pspan=1.0, num_ctx=16384, num_predict=4096):
    """One chat call to the local model. Saves the raw response (+ token counts) to analysis/fill-response-<tag>.json.
    Progress is reported inside [pbase, pbase+pspan] of the fill stage. Returns (text, err)."""
    import urllib.request, math as _m
    body = json.dumps(dict(model=OLLAMA_MODEL, stream=False, format="json", keep_alive="3m", think=False,   # think=False: Qwen3 must not emit <think> preambles
                           options=dict(num_ctx=num_ctx, temperature=0.2, num_predict=num_predict),
                           messages=[dict(role="user", content=prompt)])).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body, headers={"Content-Type": "application/json"}, method="POST")
    result = {}
    def run():
        try:
            with urllib.request.urlopen(req, timeout=3600) as r:
                d = json.loads(r.read()); txt = d.get("message", {}).get("content", "")
                result["out"] = re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()
                result["meta"] = dict(prompt_tokens=d.get("prompt_eval_count"), out_tokens=d.get("eval_count"), done=d.get("done_reason"),
                                      prompt_bytes=len(prompt.encode("utf-8")), num_ctx=num_ctx, model=OLLAMA_MODEL)
        except Exception as e:  # noqa: BLE001
            result["err"] = str(e)
    th = threading.Thread(target=run, daemon=True); th.start()
    while th.is_alive():
        if CTRL[job["id"]]["cancel"] or CTRL[job["id"]]["pause"]:
            return None, "interrupted"
        el = now() - t0
        set_progress(job, 2, pbase + pspan * min(0.97, 1 - _m.exp(-el / est)), eta_s=max(10, est - el) + 20)
        th.join(2)
    if out_dir:
        try:
            json.dump(dict(meta=result.get("meta"), err=result.get("err"), out=result.get("out", "")[:400000]),
                      open(os.path.join(out_dir, f"fill-response-{tag or 'call'}.json"), "w"), ensure_ascii=False, indent=1)
        except Exception:  # noqa: BLE001
            pass
    if "err" in result:
        return None, result["err"]
    if result.get("meta", {}).get("prompt_tokens") and result["meta"]["prompt_tokens"] >= num_ctx - 64:
        logj(job, f"warning: prompt hit the context window ({result['meta']['prompt_tokens']} tokens) — answer may be truncated")
    return result.get("out", ""), None


def _parse_mapping(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        d = json.loads(m.group(0))
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError:
        return {}


def evidence_pack(A, max_transcript=14000, timeline_s=None):
    """Compact evidence for local models: computed stats, moments, transcript, and a 10-second timeline (not 1 Hz rows)."""
    import csv as _csv
    from collections import Counter as _C
    def rd(name):
        try:
            return json.load(open(os.path.join(A, name), encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    D = rd("report-data.json"); M = rd("moments.json").get("moments", [])
    data = {k: D.get(k) for k in ("session", "site", "duration_s", "tier", "stats", "phases", "intent", "flags", "gaze_usable_frac", "grade_histogram")}
    data["latency"] = (D.get("latency") or [])[:20]
    data["moments"] = [dict(type=m["type"], t=f"{m['t_start_ms']/1000:.0f}-{m['t_end_ms']/1000:.0f}s", score=m.get("score"), q=m.get("min_quality_grade"),
                            dwell=m.get("gaze_dwell"), said=(m.get("transcript") or "")[:120], clicks=[c.get("target_text") for c in (m.get("clicks") or [])][:3]) for m in M[:60]]
    tr = "\n".join(f"[{a['time']}] {a['speech']}" for a in (D.get("appendix") or []) if a.get("speech") and a["speech"] != "(no transcript)")
    if not tr:   # the raw whisper file is NOT a fallback: on silence it contains only hallucinations ("Thank you.")
        if any(f in ("transcript_missing", "whisper_failed") for f in (D.get("flags") or [])):
            tr = ("TRANSCRIPT UNAVAILABLE (no audio file or the transcriber failed). Do NOT claim the participant was silent and do NOT invent quotes; "
                  "write 'no transcript' where a quote would go.")
        else:
            tr = ("NO SPEECH. The participant did not talk during this session (whisper produced only silence/hallucinations, which were removed). "
                  "There are NO quotes to cite. Every statement must rest on eyes, mouse, clicks and timing; write 'no speech' where a quote would go.")
    if len(tr) > max_transcript:
        tr = tr[:max_transcript] + f"\n[… transcript truncated: {len(tr) - max_transcript} more characters not shown to the writer]"
    rows = []
    try:
        rows = list(_csv.DictReader(open(os.path.join(A, "gaze-ai.csv"), encoding="utf-8")))
    except Exception:  # noqa: BLE001
        pass
    lines = []
    step = timeline_s or (10 if len(rows) <= 300 else 20 if len(rows) <= 900 else 30)      # keep long sessions inside the local context window
    for b in range(0, len(rows), step):
        w = rows[b:b + step]
        cells = _C(r["gaze_cell"] for r in w if r.get("gaze_cell") and r["gaze_cell"] not in ("", "uncertain"))
        states = _C(r["gaze_state"] for r in w if r.get("gaze_state"))
        on = sum(1 for r in w if r.get("gaze_state") == "on_screen")
        sw = sum(int(float(r.get("region_switches") or 0)) for r in w)
        mouse = _C(r["mouse_cell"] for r in w if r.get("mouse_cell"))
        ccount = _C((c.get("target_text") or "click") + (" (dead)" if c.get("is_dead") else "") for r in w for c in json.loads(r.get("clicks") or "[]"))
        clicks = [f"{k} ×{v}" if v > 1 else k for k, v in ccount.items()]
        said = " ".join(dict.fromkeys(t for r in w for t in ((r.get("speech_segs_txt") or r.get("speech_text") or "").split(" ¦ ")) if t))[:120]
        expr = _C(r["expr_label"] for r in w if r.get("expr_label") and r["expr_label"] != "neutral")
        q = _C(r["quality_grade"] for r in w if r.get("quality_grade"))
        t = int(float(w[0]["t_s"]))
        lines.append(f"{t//60:02d}:{t%60:02d} eyes={cells.most_common(1)[0][0] if cells else (states.most_common(1)[0][0] if states else '-')} on={on}/{len(w)} sw={sw}"
                     f" mouse={mouse.most_common(1)[0][0] if mouse else '-'}{' clicks='+'|'.join(clicks[:3]) if clicks else ''}{' expr='+expr.most_common(1)[0][0] if expr else ''} q={q.most_common(1)[0][0] if q else '-'}{' said: '+said if said else ''}")
    timeline = "\n".join(lines)
    scr = rd("screens.json")
    seen = []
    for r in (scr.get("reads") or [])[:26]:
        t = int(r.get("t_s") or 0); stamp = f"{t//60}:{t%60:02d}"; k = r.get("read") or {}
        if r.get("kind") == "click":
            seen.append(f"[{stamp}] CLICK landed on: {k.get('element_at_marker','?')} ({k.get('element_translation','')}) — "
                        f"kind={k.get('element_kind','?')}, looks clickable={k.get('looks_clickable')}. Page: {k.get('screen_name','')}. "
                        f"Near it: {', '.join(str(x) for x in (k.get('nearby_actions') or [])[:3])}")
        elif r.get("kind") == "moment":
            seen.append(f"[{stamp}] LOOKING AT ({r.get('cell','?')}): {str(k.get('region_translation') or k.get('region_contents',''))[:160]} "
                        f"— most likely reading: {k.get('reading_target','?')}")
        else:
            seen.append(f"[{stamp}] SCREEN: {k.get('screen_name','?')} — {k.get('purpose','')} sections: "
                        f"{', '.join(str(x) for x in (k.get('main_sections') or [])[:5])}. {k.get('notable') or ''}")
    screen_block = ("\n\n=== what was on screen (read from the screen recording by a vision model) ===\n" + "\n".join(seen)) if seen else ""
    return f"=== computed data (JSON) ===\n{json.dumps(data, ensure_ascii=False)}\n\n=== transcript (verbatim, [m:ss] text) ===\n{tr}\n\n{screen_block}\n\n=== timeline, one line per 10 s (eyes=dominant 3x3 cell or state; on=seconds on-screen; sw=region switches; q=quality grade) ===\n{timeline}\n"


TRANSLATE_RULES = """Translate each numbered transcript line into natural English. Return ONE JSON object whose keys are EXACTLY the placeholder
names listed below (APPENDIX_ENGLISH_<n> for line n) and whose values are the plain-text English translation of that line (no HTML, no notes).
Keep it literal; keep UI words (button names, field labels) as said. Output JSON only."""


FILL_RULES_LOCAL = """You are a senior UX researcher writing part of a usability report from a moderated think-aloud session with webcam eye tracking.
Return ONE JSON object. Keys = EXACTLY the placeholder names listed below (all of them, none extra). Values = HTML strings (no markdown).
Rules: cite evidence as said · eyes · time; quote the transcript verbatim (original language) with [m:ss]; eyes come from the timeline/moments
(3x3 cells TL,TC,TR,ML,MC,MR,BL,BC,BR; states on_screen/off_left/off_right/down_keyboard/away/no_face/camera_frozen — camera_frozen = no camera frames arrived (tab hidden, app switch or recorder stall): NOT attention data, never a 'look away'; CAMERA_FROZEN moments report it; DEAD_CLICKS moments = bursts of clicks on something that did not respond); NEVER invent quotes, clicks or events;
expression cues are cue-level, never emotions as facts. Wording tier: 'no_gaze' = the camera did not record: NEVER write where the eyes were, drop every eye claim, and build every finding from speech, mouse, clicks and page changes (write 'no eye data' where an eyes line would go); 'regions' = firm columns/halves; 'likely' = say 'likely'; 'unvalidated' =
every gaze statement says 'estimated (unvalidated)' and findings lean on transcript/mouse/clicks/look-away. If evidence is thin, say so plainly.
Formats (rows only, EXACT cell counts): GRADE_TABLE 3 cells (dimension · score /10 or "no evidence" · why/evidence); ROADMAP_ROWS 5 (Now/Next/Later · ship · why · impact · effort); ACTION_LIST_ROWS 5 (# · P0/P1/P2 · page · action · evidence said·eyes·time); PLACEMENT_TABLE 4 (when the user wants to… · eyes concentrate… · behaviour · so place it…); PER_NEED_MAP 5 (need · intent · where the eyes went · verdict · fix).
FINDINGS_P0/FINDINGS_P1/FINDINGS_P2/DELIGHTERS = one or more blocks EXACTLY like:
<div class="finding"><h3>ID · Title <span class="p0">P0</span></h3><p><b>Page:</b> … <b>Task:</b> …</p><span class="ar">"quote"</span>
<div class="en">[m:ss] "translation"</div><div class="eyes">👁 <b>Eyes:</b> … ⏱ …</div><div class="rec"><b>Fix:</b> …</div></div>
(use class p1/p2/keep and labels P1/P2/KEEP accordingly; DELIGHTERS use keep). SIGNAL_i ∈ Delight/Friction/Confusion/Request; FRICTION_i = 0-100;
RECOMMENDATION_i, LATENCY_MEANING_i, INTENT_READS_AS_i, JOURNEY_SUMMARY = one or two sentences. APPENDIX_ENGLISH_i = English translation of the
given line i (plain text). GRADE_TABLE rows: AI & intelligence · Visual design · Data clarity · Delight · Task efficiency · Actionability · Discoverability
(score /10 or 'no evidence', with the evidence).
When "what was on screen" is present, NAME THE REAL ELEMENT the participant clicked or read (quote its visible label) instead of a grid cell,
and say when a click landed on something that only looks like a control (kind=heading/text with looks clickable=false) — that is a real defect.
HARD REQUIREMENTS (violations make the report unusable): (1) every GRADE_TABLE row's third cell contains either a verbatim quote with its [m:ss]
or the exact words "no evidence"; a row with no evidence must score "no evidence", never a number. (2) In tier 'unvalidated', every sentence
that mentions where the eyes were includes the words "estimated (unvalidated)". (3) No invented dates, sprints, or quarters in ROADMAP_ROWS —
use Now / Next / Later. (4) SIGNAL 'Delight' cannot have FRICTION above 30; 'Confusion'/'Friction' cannot be below 40. (5) Do not repeat the same
sentence across placeholders. (6) Prefer specific observed moments (timeline lines, moments list) over general UX advice. Output JSON only."""


FINDINGS_GUIDE = """
=== how to build this part ===
FINDINGS_P0 = 0-3 blocks (IDs C1, C2…) ONLY for moments where the participant could not proceed, repeated a step, or said so.
FINDINGS_P1 = 0-4 blocks (H1…): slowdowns and misreads. FINDINGS_P2 = 0-3 blocks (N1…): polish.
DELIGHTERS = 0-3 blocks (D1…, class keep, label KEEP): things that clearly worked or were praised.
Every block MUST start from one specific moment or transcript line in the evidence (its [m:ss] appears in the block). A tier with nothing
that qualifies MUST be exactly <p>No evidence for this tier in this session.</p> — an empty tier is correct; an invented quote or a padded
block is a failure. LOOK_AWAY / no_face moments are not findings unless they interrupt a task step. When there is no speech, the quote span
holds the words "no speech" and the block rests on eyes/mouse/clicks. ACTION_LIST_ROWS = one <tr><td>#</td><td>P0/P1/P2</td><td>page</td><td>action</td><td>evidence (said · eyes · time)</td></tr> per block.
"""


def plan_groups(names, appendix):
    """Split placeholders into focused calls a 14B model can answer well inside its context window."""
    g = lambda pred: [n for n in names if pred(n)]
    groups = []
    ov = g(lambda n: n in ("GRADE_TABLE", "JOURNEY_SUMMARY", "PRODUCT_INSIGHTS", "ROADMAP_ROWS", "INSTRUMENT_NEXT") or re.match(r"^(SIGNAL|FRICTION|RECOMMENDATION)_\d+$", n))
    fi = g(lambda n: n in ("FINDINGS_P0", "FINDINGS_P1", "DELIGHTERS", "FINDINGS_P2", "ACTION_LIST_ROWS"))
    ti = g(lambda n: n in ("PLACEMENT_TABLE", "PER_NEED_MAP") or re.match(r"^(LATENCY_MEANING|INTENT_READS_AS)_\d+$", n))
    if ov: groups.append(("overview", ov, ""))
    if fi: groups.append(("findings", fi, FINDINGS_GUIDE))
    if ti: groups.append(("timing", ti, ""))
    ap = g(lambda n: n.startswith("APPENDIX_ENGLISH_"))
    src = {a["n"]: a["speech"] for a in (appendix or [])}
    for i in range(0, len(ap), 25):
        chunk = ap[i:i + 25]
        extra = "\n=== lines to translate (index → original) ===\n" + "\n".join(f"{n.split('_')[-1]}: {src.get(int(n.split('_')[-1]), '')}" for n in chunk)
        groups.append((f"appendix{i//25+1}", chunk, extra))
    return groups



def write_with_claude(job, pf, est, t0):
    import math as _m
    claude = which("claude")
    if not claude:
        return None, "Claude Code CLI not found"
    env = dict(os.environ, PATH="/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", ""))
    model = os.environ.get("BUX_CLAUDE_MODEL", "claude-opus-5")
    with open(pf, encoding="utf-8") as fin:
        p = subprocess.Popen([claude, "-p", "--model", model, "--output-format", "json"], stdin=fin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=REPO)
    CTRL[job["id"]]["proc"] = p
    while p.poll() is None:
        if CTRL[job["id"]]["cancel"] or CTRL[job["id"]]["pause"]:
            p.terminate(); return None, "interrupted"
        el = now() - t0
        set_progress(job, 2, min(0.97, 1 - _m.exp(-el / est)), eta_s=max(10, est - el) + 20)
        time.sleep(2)
    CTRL[job["id"]]["proc"] = None
    out, err = p.communicate()
    if p.returncode != 0:
        return None, "claude failed: " + (err or out)[-300:]
    try:
        return json.loads(out).get("result", out), None
    except json.JSONDecodeError:
        return out, None


NOT_WRITTEN = ('<div class="caveat"><b>Findings not written.</b> No local language model is configured on this Mac, so this report contains '
               'the measured data (attention figure, scorecard, moments, timeline) without written findings. Install a local model '
               '(Ollama + <code>gemma3:12b</code>) and re-run, or set <code>BUX_LLM=claude</code> to opt in to Claude.</div>')


def _norm_txt(t):
    """Letters/digits only, Arabic orthography folded (أإآ→ا, ة→ه, ى→ي, no diacritics/tatweel/punctuation) so a verbatim quote survives re-punctuation."""
    import html as _h
    t = _h.unescape(re.sub(r"<[^>]+>", "", t or ""))
    t = re.sub(r"[\u064B-\u0652\u0640]", "", t).replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ة", "ه").replace("ى", "ي")
    return re.sub(r"[^\w]+", "", t).lower()


def verify_findings(mapping, speech_text, no_speech):
    """Honesty check that does not trust the writer: drop finding blocks whose quoted text is not verbatim in the transcript,
    blank grade rows that cite unknown quotes. Returns (dropped_blocks, blanked_rows)."""
    corpus = _norm_txt(speech_text); dropped = blanked = dupes = 0; seen_global = {}
    def _tn(b):
        h3 = re.search(r"<h3>(.*?)</h3>", b, re.S); t = re.sub(r"^[A-Z]?\d*\s*·\s*", "", re.sub(r"<[^>]+>", "", h3.group(1) if h3 else "")).strip().lower()
        return set(re.findall(r"\w+", t))
    def quote_ok(q):
        n = _norm_txt(q)
        if not n or n in ("nospeech", "notranscript", "noquote"):
            return True
        return (not no_speech) and len(n) >= 4 and n in corpus
    for key in ("FINDINGS_P0", "FINDINGS_P1", "DELIGHTERS", "FINDINGS_P2"):
        v = mapping.get(key) or ""
        if not isinstance(v, str):
            continue
        blocks = re.findall(r'<div class="finding">.*?</div>\s*</div>', v, re.S)
        if not blocks:
            continue
        keep, seen = [], set()
        for b in blocks:
            quotes = re.findall(r'<span class="ar">(.*?)</span>', b, re.S)
            if not all(quote_ok(q) for q in quotes):
                dropped += 1; continue
            sig = (_norm_txt(" ".join(quotes)), (re.search(r"\[\d+:\d\d\]", b) or [""])[0]); words = _tn(b)
            if sig in seen:                       # same tier, same quote+moment: the model returned one finding twice
                dupes += 1; continue
            prev = seen_global.get(sig)           # other tier: only a duplicate when the titles also overlap (distinct findings may share an anchor quote)
            if prev is not None and words and (len(words & prev) / max(1, len(words | prev)) >= 0.5 or words <= prev or prev <= words):
                dupes += 1; continue
            seen.add(sig); seen_global.setdefault(sig, words); keep.append(b)
        prefix = {"FINDINGS_P0": "C", "FINDINGS_P1": "H", "FINDINGS_P2": "N", "DELIGHTERS": "D"}[key]
        keep = [re.sub(r"<h3>\s*(?:ID|[A-Z]\d*)?\s*·", f"<h3>{prefix}{i+1} ·", b, count=1) for i, b in enumerate(keep)]   # sequential IDs, no literal "ID"
        mapping[key] = "\n".join(keep) if keep else "<p>No evidence for this tier in this session.</p>"
    for key in list(mapping):                                           # finding blocks only belong in the four finding placeholders
        if key not in ("FINDINGS_P0", "FINDINGS_P1", "FINDINGS_P2", "DELIGHTERS") and isinstance(mapping[key], str) and '<div class="finding">' in mapping[key]:
            mapping[key] = re.sub(r'<div class="finding">.*?</div>\s*</div>', "", mapping[key], flags=re.S).strip() or "no evidence"
    rows = re.findall(r"<tr>.*?</tr>", mapping.get("GRADE_TABLE") or "", re.S) if isinstance(mapping.get("GRADE_TABLE"), str) else []; out = []; cited = {}
    for r in rows:
        cells = re.findall(r"<td>(.*?)</td>", r, re.S)
        quotes = re.findall(r'[\"“](.{4,}?)[\"”]', re.sub(r"<[^>]+>", "", r))
        stamp = (re.search(r"\[\d+:\d\d\]", r) or [""])[0]
        bad = len(cells) >= 3 and quotes and not all(quote_ok(q) for q in quotes)
        lazy = len(cells) >= 3 and stamp and cited.get(stamp, 0) >= 2            # the same moment pasted into a third, fourth… row
        if bad or lazy:
            r = f"<tr><td>{cells[0]}</td><td>no evidence</td><td>no evidence</td></tr>"; blanked += 1
        elif stamp:
            cited[stamp] = cited.get(stamp, 0) + 1
        out.append(r)
    if rows:
        mapping["GRADE_TABLE"] = "\n".join(out)
    # LLM rows must match the scaffold's column count (ragged tables otherwise)
    for key, ncol in dict(GRADE_TABLE=3, ROADMAP_ROWS=5, ACTION_LIST_ROWS=5, PLACEMENT_TABLE=4, PER_NEED_MAP=5).items():
        v = mapping.get(key)
        if not isinstance(v, str) or "<tr" not in v:
            continue
        fixed = []
        for r in re.findall(r"<tr>.*?</tr>", v, re.S):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", r, re.S)
            if len(cells) == ncol or not cells:
                fixed.append(r); continue
            cells = cells[:ncol - 1] + [" · ".join(cells[ncol - 1:])] if len(cells) > ncol else cells + [""] * (ncol - len(cells))
            fixed.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
        mapping[key] = "\n".join(fixed)
    return dropped + dupes, blanked


THIN_EVIDENCE = ('<div class="caveat"><b>Thin evidence.</b> The participant did not speak and made no task clicks in this session, so the findings '
                 'are limited to where attention went (estimated from the webcam) and mouse movement. Record with think-aloud for a full report.</div>')


def stage_fill(job):
    A = os.path.join(job["folder"], "analysis"); scaffold = os.path.join(A, "report-scaffold.html")
    html = open(scaffold, encoding="utf-8").read()
    names = sorted(set(re.findall(r"\{\{([A-Z][A-Z0-9_]*)\}\}", html)))
    st = llm_status(start=True); job["llm"] = st; save(job)     # start Ollama if it isn't running
    if st["backend"] == "none" or not st["available"]:
        job.setdefault("warnings", []).append("findings not written: " + (st.get("note") or "no report-writing model available"))
        out = re.sub(r"<h2>Overall grade</h2>", NOT_WRITTEN + "<h2>Overall grade</h2>", html, count=1)
        out = re.sub(r"\{\{[A-Z_0-9]+\}\}", "", out)
        open(os.path.join(A, "report-filled.html"), "w", encoding="utf-8").write(out); return "skipped"
    def read(p, cap=None):
        try:
            t = open(p, encoding="utf-8").read(); return t[:cap] if cap else t
        except Exception:  # noqa: BLE001
            return ""
    tier = job.get("quality", {}).get("tier", "unvalidated"); job["model"] = st["model"]; save(job)
    t0 = now(); mapping = {}
    if st["backend"] == "ollama":
        # ---- local model: compact evidence + several focused calls (a 300 KB single prompt overflows a 32k window) ----
        try:
            appendix = json.load(open(os.path.join(A, "report-data.json"), encoding="utf-8")).get("appendix") or []
        except Exception:  # noqa: BLE001
            appendix = []
        pack = evidence_pack(A)
        groups = plan_groups(names, appendix)
        try:
            n_clicks = int(((json.load(open(os.path.join(A, "report-data.json"), encoding="utf-8")).get("stats") or {}).get("clicks") or 0))
        except Exception:  # noqa: BLE001
            n_clicks = 0
        if n_clicks == 0 and any(g[0] == "timing" for g in groups):
            for n_ in [n for g in groups if g[0] == "timing" for n in g[1]]:
                mapping[n_] = ('<tr><td colspan="4">No task clicks in this session — no click evidence to place.</td></tr>' if n_ in ("PLACEMENT_TABLE", "PER_NEED_MAP")
                               else "No click-latency evidence: the participant made no task clicks in this session.")
            groups = [g for g in groups if g[0] != "timing"]
            logj(job, "timing: no clicks in this session — filled deterministically, model pass skipped")
        per = min(900, 120 + len(pack.encode("utf-8")) / 110)          # measured: a 22 KB chunk takes about 5 min on qwen3:14b (M-series GPU)
        job["fill_est_s"] = int(per * len(groups)); save(job)
        CAPS = dict(overview=3500, findings=3000, timing=2000)
        for k, (gname, gnames, extra) in enumerate(groups):
            if gname.startswith("appendix"):      # translation only: no evidence pack needed (was 15k prompt tokens per pass)
                prompt = (TRANSLATE_RULES + f"\nPLACEHOLDERS (return all {len(gnames)} keys): {json.dumps(gnames)}\n{extra}\n")
            else:
                prompt = (FILL_RULES_LOCAL + f"\n\nWORDING TIER: {tier}{" — this session is click-validated: write firm columns/halves and do NOT use the words estimated or unvalidated anywhere" if tier == "regions" else ""}\nPLACEHOLDERS (return all {len(gnames)} keys): {json.dumps(gnames)}\n{extra}\n\n" + pack)
            cap = CAPS.get(gname, 2000)
            open(os.path.join(A, f"fill-prompt-{gname}.txt"), "w", encoding="utf-8").write(prompt)
            got = {}
            for attempt in (1, 2):
                res, err = write_with_ollama(job, prompt, per, now(), A, f"{gname}-{attempt}", pbase=k / len(groups), pspan=1 / len(groups), num_predict=cap)
                if err == "interrupted":
                    return "interrupted"
                if err:
                    raise RuntimeError(f"ollama failed: {err[-300:]}")
                got = {kk: vv for kk, vv in _parse_mapping(res).items() if kk in gnames and vv}
                if got and gname == "overview" and attempt == 1:
                    rows = re.findall(r"<tr>(.*?)</tr>", got.get("GRADE_TABLE", ""), re.S)
                    bad = [r for r in rows if not re.search(r"\[\d+:\d\d\]", r) and "no evidence" not in r.lower()]
                    if rows and bad:
                        logj(job, f"overview: {len(bad)}/{len(rows)} grade rows lack evidence — asking the model to correct")
                        prompt = prompt + ("\n\nCORRECTION: your previous GRADE_TABLE had rows without evidence. Rewrite ALL placeholders; every grade row "
                                           "must cite a verbatim quote with [m:ss] or say 'no evidence' (and then the score must be 'no evidence').")
                        continue
                if got:
                    break
            mapping.update(got)
            missing = [n for n in gnames if n not in mapping]
            logj(job, f"{gname}: {len(got)}/{len(gnames)} written" + (f" ({len(missing)} missing)" if missing else ""))
            if missing:
                job.setdefault("warnings", []).append(f"{gname}: {len(missing)} placeholder(s) not written by {st['model']}")
    else:
        # ---- Claude (opt-in): one call handles the full prompt ----
        csv_txt = read(os.path.join(A, "gaze-ai.csv")); lines = csv_txt.splitlines()
        csv_txt = "\n".join(lines[:1] + lines[1:1201])
        prompt = (FILL_RULES + f"\n\nWORDING TIER: {tier}\nPLACEHOLDERS: {json.dumps(names)}\n\n"
                  f"=== report-data.json ===\n{read(os.path.join(A, 'report-data.json'), 60000)}\n\n=== moments.json ===\n{read(os.path.join(A, 'moments.json'), 40000)}\n\n"
                  f"=== transcript.srt ===\n{read(os.path.join(A, 'transcript.srt'), 60000)}\n\n=== gaze-ai.csv (1 Hz) ===\n{csv_txt}\n")
        pf = os.path.join(A, "fill-prompt.txt"); open(pf, "w", encoding="utf-8").write(prompt)
        est = min(1200, 90 + len(prompt.encode("utf-8")) / 1200); job["fill_est_s"] = int(est); save(job)
        res, err = write_with_claude(job, pf, est, t0)
        if err == "interrupted":
            return "interrupted"
        if err:
            raise RuntimeError(f"claude failed: {err[-300:]}")
        open(os.path.join(A, "fill-response-claude.json"), "w", encoding="utf-8").write(res or "")
        mapping = _parse_mapping(res)
    if not mapping:
        job.setdefault("warnings", []).append(f"{st['model']} returned no usable JSON — report contains the data scaffold only")
    try:
        RD = json.load(open(os.path.join(A, "report-data.json"), encoding="utf-8"))
    except Exception:  # noqa: BLE001
        RD = {}
    speech_text = " ".join(a.get("speech", "") for a in (RD.get("appendix") or []) if a.get("speech") and a["speech"] != "(no transcript)")
    speech_text += " " + read(os.path.join(A, "transcript.srt"))            # Claude quotes the raw SRT: the corpus must contain it too
    flags_ = RD.get("flags") or []
    no_speech = ("no_speech" in flags_ or not speech_text.strip()) and not any(f in ("transcript_missing", "whisper_failed") for f in flags_)
    dropped, blanked = verify_findings(mapping, speech_text, no_speech)
    if tier == "no_gaze":                                               # no camera: an eyes claim would be fabricated
        for k_, v_ in list(mapping.items()):
            if isinstance(v_, str):
                mapping[k_] = re.sub(r'<div class="eyes">.*?</div>', '<div class="eyes">👁 <b>Eyes:</b> no eye data in this session</div>', v_, flags=re.S)
    if tier == "regions":                                               # validated session: the model must not hedge with the weaker tiers' wording
        for k_, v_ in mapping.items():
            if isinstance(v_, str):
                mapping[k_] = re.sub(r"\s*estimated \(unvalidated\)|\s*\(unvalidated\)", "", v_)      # only the hedge phrases, never the bare word
    elif tier == "likely":
        for k_, v_ in mapping.items():
            if isinstance(v_, str):
                mapping[k_] = v_.replace("estimated (unvalidated)", "likely")
    if dropped or blanked:
        msg = f"verifier: removed {dropped} finding block(s) (quote not in transcript, or duplicate), blanked {blanked} grade row(s)"
        logj(job, msg); job.setdefault("warnings", []).append(msg)
    filled = re.sub(r"\{\{([A-Z_0-9]+)\}\}", lambda mm: str(mapping.get(mm.group(1), "")), html)
    if no_speech and int((RD.get("stats") or {}).get("clicks") or 0) == 0:
        filled = re.sub(r"<h2>Overall grade</h2>", THIN_EVIDENCE + "<h2>Overall grade</h2>", filled, count=1)
    filled = re.sub(r"<!--.*?-->", "", filled, flags=re.S)          # drop the scaffold's template/instruction comments
    open(os.path.join(A, "report-filled.html"), "w", encoding="utf-8").write(filled)
    job["filled_placeholders"] = len([k for k in names if mapping.get(k)]); job["placeholders_total"] = len(names)
    return "ok"



def stage_pdf(job):
    A = os.path.join(job["folder"], "analysis")
    src = os.path.join(A, "report-filled.html")
    html_out = os.path.join(job["folder"], "report.html"); pdf_out = os.path.join(job["folder"], "report.pdf")
    shutil.copyfile(src, html_out)
    if os.path.exists(CHROME):
        subprocess.run([CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer", f"--print-to-pdf={pdf_out}", "file://" + html_out],
                       capture_output=True, timeout=180)
    job["report_html"] = html_out; job["report_pdf"] = pdf_out if os.path.exists(pdf_out) else None
    return "ok"


def stage_open(job):
    if job.get("reprocess"):
        logj(job, "report rebuilt (re-processing an existing session) — not opening it")
        return "ok"                      # only a freshly recorded session opens its report by itself
    target = job.get("report_pdf") or job.get("report_html")
    if target and sys.platform == "darwin":
        subprocess.run(["open", target], capture_output=True)
    return "ok"


RUNNERS = {"analyze": stage_analyze, "screen": stage_screen, "scaffold": stage_scaffold, "fill": stage_fill, "pdf": stage_pdf, "open": stage_open}


# ---------------- worker ----------------
def worker():
    while True:
        job = None
        with LOCK:
            for j in sorted(JOBSTATE.values(), key=lambda x: x["created"]):
                if j["status"] == "queued":
                    job = j; j["status"] = "running"; break
        if not job:
            WAKE.wait(2); WAKE.clear(); continue
        cid = job["id"]; CTRL.setdefault(cid, {"pause": False, "cancel": False, "proc": None})
        try:
            if not job.get("files_ready"):
                logj(job, "waiting for the recording to finish saving…")
                if not wait_for_files(job):
                    raise RuntimeError("no recording (face/screen/audio.webm) appeared in " + job["folder"])
                job["files_ready"] = True; job["video_s"] = round(video_seconds(job["folder"]), 1)
                job["eta_s"] = int(job["video_s"] * RATE + FILL_EST_S + 20); save(job)
            i = job.get("stage_index", 0)
            while i < len(STAGES):
                if CTRL[cid]["cancel"]:
                    job["status"] = "cancelled"; break
                if CTRL[cid]["pause"]:
                    job["status"] = "paused"; job["stage_index"] = i; save(job); break
                name = STAGES[i][0]; set_progress(job, i, 0.0); logj(job, STAGES[i][1])
                r = RUNNERS[name](job)
                if r == "interrupted":
                    job["status"] = "cancelled" if CTRL[cid]["cancel"] else "paused"; job["stage_index"] = i; save(job); break
                i += 1; job["stage_index"] = i; save(job)
            else:
                job["status"] = "done"; job["progress"] = 1.0; job["eta_s"] = 0; logj(job, "done")
        except Exception as e:  # noqa: BLE001
            job["status"] = "error"; job["error"] = str(e)[:400]; logj(job, "error: " + str(e)[:200])
        save(job)


# ---------------- HTTP ----------------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*"); self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type"); self.end_headers(); self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/health":
            return self._send(200, dict(ok=True, version=VERSION, jobs=len(JOBSTATE), llm=llm_status(), ai_dir=AI, repo=REPO))
        if p == "/jobs":
            return self._send(200, sorted(JOBSTATE.values(), key=lambda j: -j["created"])[:50])
        m = re.match(r"^/jobs/([\w-]+)$", p)
        if m and m.group(1) in JOBSTATE:
            return self._send(200, JOBSTATE[m.group(1)])
        # the finished report, fetched by the extension and saved into ~/Downloads (the service itself can't write there)
        m = re.match(r"^/jobs/([\w-]+)/report\.(pdf|html)$", p)
        if m and m.group(1) in JOBSTATE:
            job = JOBSTATE[m.group(1)]; path = job.get("report_pdf" if m.group(2) == "pdf" else "report_html")
            if path and os.path.exists(path):
                data = open(path, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf" if m.group(2) == "pdf" else "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data))); self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers(); self.wfile.write(data); return
            return self._send(404, dict(error="report not ready"))
        self._send(404, dict(error="not found"))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        p = self.path.split("?")[0]
        # Raw file upload from the extension: POST /sessions/<name>/<file>  (body = file bytes)
        if p == "/transcribe":
            if n > 64 * 1024 * 1024:
                return self._send(413, dict(error="chunk too large"))
            raw = self.rfile.read(n); q = dict(kv.split("=", 1) for kv in self.path.split("?", 1)[1].split("&") if "=" in kv) if "?" in self.path else {}
            return self._send(200, transcribe_pcm(raw, q.get("lang", "auto")))
        m = re.match(r"^/sessions/([A-Za-z0-9._\-]{1,120})/([A-Za-z0-9._\-]{1,80})$", p)
        if m:
            if n > MAX_UPLOAD:
                return self._send(413, dict(error="file too large"))
            d = os.path.join(SESSIONS, m.group(1)); os.makedirs(d, exist_ok=True)
            dest = os.path.join(d, m.group(2)); remaining = n
            with open(dest, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 8 * 1024 * 1024))
                    if not chunk:
                        break
                    f.write(chunk); remaining -= len(chunk)
            return self._send(200, dict(ok=True, file=dest, bytes=n))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            body = {}
        if p == "/jobs":
            if body.get("session"):
                folder = os.path.join(SESSIONS, re.sub(r"[^A-Za-z0-9._\-]", "_", body["session"]))
            else:
                folder = body.get("folder") or (os.path.join(HOME, "Downloads", body.get("downloads_rel", "")) if body.get("downloads_rel") else None)
            if not folder:
                return self._send(400, dict(error="session, folder or downloads_rel required"))
            folder = os.path.abspath(os.path.expanduser(folder))
            if not folder.startswith(HOME):
                return self._send(400, dict(error="folder must be under the home directory"))
            for j in JOBSTATE.values():
                if j["folder"] == folder and j["status"] in ("queued", "running", "paused"):
                    return self._send(200, j)
            jid = uuid.uuid4().hex[:10]
            job = dict(id=jid, folder=folder, product=body.get("product") or "", status="queued", stage="queued", stage_label="Queued",
                       progress=0.0, eta_s=None, video_s=None, log=[], error=None, report_pdf=None, report_html=None, warnings=[],
                       created=now(), updated=now(), stage_index=0, files_ready=False)
            # from_stage: re-run only part of the pipeline on an already-analysed session (e.g. "fill" after switching the writer model)
            names = [s[0] for s in STAGES]
            if body.get("from_stage") in names and os.path.isdir(os.path.join(folder, "analysis")):
                job.update(stage_index=names.index(body["from_stage"]), files_ready=True, video_s=round(video_seconds(folder), 1), reprocess=True)
                try:
                    q = json.load(open(os.path.join(folder, "analysis", "quality.json")))
                    job["quality"] = dict(tier=q.get("click_consistency", {}).get("tier"), usable=q.get("gaze_usable_frac"), flags=q.get("flags"), moments=q.get("moments"))
                except Exception:  # noqa: BLE001
                    pass
            with LOCK:
                JOBSTATE[jid] = job; CTRL[jid] = {"pause": False, "cancel": False, "proc": None}
            save(job); WAKE.set()
            return self._send(201, job)
        m = re.match(r"^/jobs/([\w-]+)/(pause|resume|cancel|retry|open)$", p)
        if not m or m.group(1) not in JOBSTATE:
            return self._send(404, dict(error="not found"))
        job, act = JOBSTATE[m.group(1)], m.group(2); c = CTRL.setdefault(job["id"], {"pause": False, "cancel": False, "proc": None})
        if act == "pause" and job["status"] in ("queued", "running"):
            c["pause"] = True
            if job["status"] == "queued":
                job["status"] = "paused"
            logj(job, "pause requested (current stage restarts on resume)")
        elif act == "resume" and job["status"] == "paused":
            c["pause"] = False; job["status"] = "queued"; logj(job, "resumed"); WAKE.set()
        elif act == "cancel" and job["status"] in ("queued", "running", "paused"):
            c["cancel"] = True; c["pause"] = False
            if job["status"] != "running":
                job["status"] = "cancelled"
            logj(job, "cancelled")
        elif act == "retry" and job["status"] in ("error", "cancelled"):
            c["cancel"] = False; c["pause"] = False; job["status"] = "queued"; job["error"] = None; logj(job, "retrying from stage " + str(job.get("stage_index", 0))); WAKE.set()
        elif act == "open":
            stage_open(job)
        save(job)
        self._send(200, job)


def main():
    for p in glob.glob(os.path.join(JOBS, "*.json")):
        try:
            j = json.load(open(p)); JOBSTATE[j["id"]] = j; CTRL[j["id"]] = {"pause": False, "cancel": False, "proc": None}
            if j["status"] == "running":
                j["status"] = "queued"        # daemon restarted mid-job: resume from the saved stage
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"[bux-daemon] v{VERSION} listening on http://127.0.0.1:{PORT}  jobs={len(JOBSTATE)}  repo={REPO}  ai={AI}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
