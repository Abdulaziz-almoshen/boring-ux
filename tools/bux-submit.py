#!/usr/bin/env python3
"""
bux-submit — hand an existing session folder to the local processing service, exactly like the extension does.

Use it for recordings the service cannot read itself (macOS blocks background services from Downloads/Desktop/Documents):
it uploads the files over localhost to ~/.boring-ux/sessions/<name>/ and submits the job.

  python3 tools/bux-submit.py ~/Downloads/boring-ux/<site>-<time> [--product "Name"] [--watch]
  python3 tools/bux-submit.py ~/Desktop/UX/session-2026-07-16T09-45-24 --product "Seha Medical" --watch
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error

HOST = os.environ.get("BUX_HOST", "http://127.0.0.1:7331")
SKIP_SUFFIX = (".b64.bak", ".recovered.webm", ".jpg", ".wav")
SKIP_DIRS = {"analysis", "debug"}


def req(method, path, data=None, headers=None):
    r = urllib.request.Request(HOST + path, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(r, timeout=600) as resp:
        body = resp.read()
        return json.loads(body) if body else {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session"); ap.add_argument("--product", default=None); ap.add_argument("--watch", action="store_true")
    ap.add_argument("--name", default=None, help="session name on the service (default: folder name)")
    a = ap.parse_args()
    folder = os.path.abspath(os.path.expanduser(a.session)); name = a.name or os.path.basename(folder.rstrip("/"))
    try:
        h = req("GET", "/health")
    except (urllib.error.URLError, ConnectionError) as e:
        sys.exit(f"processing service not reachable at {HOST} ({e}). Start it: bash tools/install-daemon.sh")
    print(f"service v{h.get('version')} · claude={'yes' if h.get('claude') else 'no'}")
    # prefer the repaired video when the original is unreadable
    files = []
    for f in sorted(os.listdir(folder)):
        p = os.path.join(folder, f)
        if os.path.isdir(p) or f.startswith(".") or f.endswith(SKIP_SUFFIX):
            continue
        files.append((f, p))
    fixed = os.path.join(folder, "analysis", "face_fixed.webm")
    if os.path.exists(fixed) and any(f == "face.webm" for f, _ in files):
        with open(os.path.join(folder, "face.webm"), "rb") as fh:
            if fh.read(4) != b"\x1a\x45\xdf\xa3":
                files = [(f, p) for f, p in files if f != "face.webm"] + [("face.webm", fixed)]
                print("  using analysis/face_fixed.webm as face.webm (original is not a valid WebM)")
    total = sum(os.path.getsize(p) for _, p in files)
    print(f"uploading {len(files)} files ({total/1e6:.1f} MB) as session '{name}' …")
    for f, p in files:
        with open(p, "rb") as fh:
            data = fh.read()
        req("POST", f"/sessions/{name}/{f}", data=data, headers={"Content-Type": "application/octet-stream"})
        print(f"  ✓ {f} ({len(data)/1e6:.1f} MB)")
    job = req("POST", "/jobs", data=json.dumps({"session": name, "product": a.product or name}).encode(), headers={"Content-Type": "application/json"})
    print(f"job {job['id']} {job['status']}")
    if not a.watch:
        print(f"watch: curl {HOST}/jobs/{job['id']}")
        return 0
    last = ""
    while True:
        j = req("GET", f"/jobs/{job['id']}")
        line = f"{j['status']:<8} {j.get('stage',''):<9} {j.get('progress',0):.0%} eta={j.get('eta_s')} | {(j.get('log') or ['-'])[-1][9:100]}"
        if line != last:
            print(time.strftime("%H:%M:%S"), line); last = line
        if j["status"] in ("done", "error", "cancelled"):
            print(json.dumps({k: j.get(k) for k in ("status", "report_pdf", "filled_placeholders", "warnings", "error")}, ensure_ascii=False))
            return 0 if j["status"] == "done" else 1
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
