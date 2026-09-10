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
STAGES = [("analyze", "Analyzing eyes, face & expressions", 0.62), ("scaffold", "Building the report structure", 0.03),
          ("fill", "Writing findings with Claude", 0.28), ("pdf", "Rendering the PDF", 0.04), ("open", "Opening the report", 0.03)]
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
    f = os.path.join(job["folder"], "face.webm"); s = os.path.join(job["folder"], "session.json")
    last, stable, t0 = -1, 0, now()
    while now() - t0 < 120:
        if CTRL[job["id"]]["cancel"]:
            return False
        if os.path.exists(f) and os.path.exists(s):
            sz = os.path.getsize(f)
            stable = stable + 1 if sz == last and sz > 0 else 0; last = sz
            if stable >= 2:
                return True
        time.sleep(1)
    return os.path.exists(f)


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


def stage_analyze(job):
    vs = job["video_s"] or 60
    whisper = glob.glob(os.path.join(AI, "models", "ggml-*.bin"))
    cmd = [PY, os.path.join(REPO, "tools", "bux-analyze-video.py"), job["folder"]] + (["--whisper-model", whisper[0]] if whisper else [])
    t0 = now()
    def on_line(line):
        m = re.search(r"… (\d+)s decoded", line)
        g = re.search(r"… gaze (\d+)/(\d+)", line)
        if m:                                              # decode + face pass = first 80 % of the stage
            frac = 0.8 * min(1.0, int(m.group(1)) / vs); elapsed = now() - t0
            set_progress(job, 0, frac, eta_s=(vs * RATE - elapsed) + FILL_EST_S + 20)
        elif g:                                            # gaze pass = last 20 %
            frac = 0.8 + 0.2 * int(g.group(1)) / max(int(g.group(2)), 1); elapsed = now() - t0
            set_progress(job, 0, frac, eta_s=max(10, vs * RATE - elapsed) + FILL_EST_S + 20)
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


def stage_fill(job):
    A = os.path.join(job["folder"], "analysis"); scaffold = os.path.join(A, "report-scaffold.html")
    html = open(scaffold, encoding="utf-8").read()
    names = sorted(set(re.findall(r"\{\{([A-Z_]+(?:_\d+)?)\}\}", html)))
    claude = which("claude")
    if not claude:
        job.setdefault("warnings", []).append("Claude Code CLI not found — findings not written; report contains the data scaffold only")
        out = re.sub(r"\{\{[A-Z_0-9]+\}\}", "", html)
        open(os.path.join(A, "report-filled.html"), "w", encoding="utf-8").write(out); return "skipped"
    def read(p, cap=None):
        try:
            t = open(p, encoding="utf-8").read(); return t[:cap] if cap else t
        except Exception:  # noqa: BLE001
            return ""
    csv_txt = read(os.path.join(A, "gaze-ai.csv"))
    lines = csv_txt.splitlines(); csv_txt = "\n".join(lines[:1] + lines[1:1201])          # cap 20 min of seconds
    prompt = (FILL_RULES + f"\n\nWORDING TIER: {job.get('quality', {}).get('tier', 'unvalidated')}\nPLACEHOLDERS: {json.dumps(names)}\n\n"
              f"=== report-data.json ===\n{read(os.path.join(A, 'report-data.json'), 60000)}\n\n=== moments.json ===\n{read(os.path.join(A, 'moments.json'), 40000)}\n\n"
              f"=== transcript.srt ===\n{read(os.path.join(A, 'transcript.srt'), 60000)}\n\n=== gaze-ai.csv (1 Hz) ===\n{csv_txt}\n")
    pf = os.path.join(A, "fill-prompt.txt"); open(pf, "w", encoding="utf-8").write(prompt)
    est = min(900, 90 + len(prompt.encode("utf-8")) / 1200)     # measured: ~300 KB prompt ≈ 5–6 min; scales with transcript length
    job["fill_est_s"] = int(est); save(job)
    t0 = now()
    env = dict(os.environ, PATH="/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", ""))
    with open(pf, encoding="utf-8") as fin:
        p = subprocess.Popen([claude, "-p", "--output-format", "json"], stdin=fin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=REPO)
    CTRL[job["id"]]["proc"] = p
    import math as _m
    while p.poll() is None:
        if CTRL[job["id"]]["cancel"] or CTRL[job["id"]]["pause"]:
            p.terminate(); return "interrupted"
        el = now() - t0
        set_progress(job, 2, min(0.97, 1 - _m.exp(-el / est)), eta_s=max(10, est - el) + 20)   # asymptotic: never looks frozen
        time.sleep(2)
    CTRL[job["id"]]["proc"] = None
    out, err = p.communicate()
    if p.returncode != 0:
        raise RuntimeError("claude failed: " + (err or out)[-300:])
    try:
        res = json.loads(out).get("result", out)
    except json.JSONDecodeError:
        res = out
    m = re.search(r"\{.*\}", res, re.S)
    mapping = {}
    if m:
        try:
            mapping = json.loads(m.group(0))
        except json.JSONDecodeError:
            mapping = {}
    if not mapping:
        job.setdefault("warnings", []).append("Claude returned no usable JSON — report contains the data scaffold only")
    filled = re.sub(r"\{\{([A-Z_0-9]+)\}\}", lambda mm: str(mapping.get(mm.group(1), "")), html)
    filled = re.sub(r"<!--.*?-->", "", filled, flags=re.S)          # drop the scaffold's template/instruction comments
    open(os.path.join(A, "report-filled.html"), "w", encoding="utf-8").write(filled)
    job["filled_placeholders"] = len([k for k in names if mapping.get(k)])
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
    target = job.get("report_pdf") or job.get("report_html")
    if target and sys.platform == "darwin":
        subprocess.run(["open", target], capture_output=True)
    return "ok"


RUNNERS = {"analyze": stage_analyze, "scaffold": stage_scaffold, "fill": stage_fill, "pdf": stage_pdf, "open": stage_open}


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
                    raise RuntimeError("face.webm never appeared in " + job["folder"])
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
            return self._send(200, dict(ok=True, version=VERSION, jobs=len(JOBSTATE), claude=bool(which("claude")), ai_dir=AI, repo=REPO))
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
