#!/usr/bin/env python3
"""
bux-report — build the deterministic scaffold of the Boring UX report from a session's analysis/ outputs.

Everything mechanical is generated here (design tokens, method & confidence header, attention
figures as inline SVG, pre-filled scorecard/latency/moments/intent tables, the complete fused
timeline appendix). The judgment parts (findings, insights, recommendations, RICE) are left as
{{PLACEHOLDERS}} for the analyst/model to fill — see .claude/skills/boring-ux-report/SKILL.md.

Usage:
  python3 tools/bux-report.py <session-folder> [--product "Name"]
Reads : <session>/analysis/{gaze-ai.csv, expressions.csv, moments.json, quality.json[, transcript.srt]}, session.json, events.csv
Writes: <session>/analysis/report-scaffold.html and report-data.json
"""
import argparse
import csv
import html
import json
import os
import re
from collections import Counter, defaultdict

CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1a2233;line-height:1.55;max-width:980px;margin:0 auto;padding:32px 22px;background:#fff}
h1{font-size:26px;margin:0 0 4px}h2{font-size:20px;margin:34px 0 8px;padding-bottom:6px;border-bottom:2px solid #e6eaf2}
h3{font-size:16.5px;margin:20px 0 4px;color:#1c2942}
.sub{color:#6b7488;font-size:14px}
.meta{background:#f5f7fb;border:1px solid #e6eaf2;border-radius:10px;padding:14px 16px;font-size:13.5px;margin:16px 0}
table{border-collapse:collapse;width:100%;margin:10px 0;font-size:13px}
th,td{border:1px solid #e0e5ee;padding:7px 9px;text-align:left;vertical-align:top}
th{background:#f0f3f9}
.ar{direction:rtl;font-size:14.5px;color:#0a3d2c;background:#eefaf3;border-right:3px solid #37b47a;padding:5px 10px;border-radius:4px;margin:5px 0;display:block}
.en{color:#555;font-size:12.5px;margin:0 0 6px 2px;font-style:italic}
.eyes{background:#eef4ff;border:1px solid #d6e2ff;border-radius:7px;padding:6px 11px;margin:6px 0;font-size:13px}
.eyes b{color:#2b52a0}
.p0{color:#fff;background:#d33;padding:2px 8px;border-radius:999px;font-size:11.5px;font-weight:700}
.p1{color:#fff;background:#e8871e;padding:2px 8px;border-radius:999px;font-size:11.5px;font-weight:700}
.p2{color:#fff;background:#4f8cff;padding:2px 8px;border-radius:999px;font-size:11.5px;font-weight:700}
.keep{color:#fff;background:#0a9d54;padding:2px 8px;border-radius:999px;font-size:11.5px;font-weight:700}
.rec{background:#fffdf5;border:1px solid #f0e6c0;border-radius:7px;padding:8px 12px;margin:6px 0;font-size:13.5px}
.win{color:#0a7d43;font-weight:600}
ul{margin:5px 0}li{margin:2px 0}
td.hot{background:#fff2f2}td.good{background:#f0faf3}
code{background:#eef1f6;padding:1px 5px;border-radius:4px;font-size:12.5px}
.caveat{background:#fff6f6;border:1px solid #f3d6d6;border-radius:8px;padding:10px 14px;font-size:12.5px;color:#7a2b2b}
.finding{margin:14px 0 4px;padding:10px 0;border-top:1px solid #eef0f5}
.bar{display:inline-block;height:9px;vertical-align:middle;border-radius:2px}
.tier{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;font-weight:700;background:#eef1f6;color:#26324a}
@media print{body{padding:0}h2,h3{page-break-after:avoid}tr{page-break-inside:avoid}.finding{page-break-inside:avoid}}
"""
COL_COLOR = {"L": "#e8871e", "C": "#4f8cff", "R": "#0a9d54", None: "#cbd2df", "": "#cbd2df", "?": "#cbd2df"}
MOMENT_COLOR = {"SEARCHING": "#e8871e", "CONFUSION": "#d33", "FRUSTRATION": "#8b0000", "LOOK_AWAY": "#9aa3b5", "CAMERA_FROZEN": "#5b6b8c", "DEAD_CLICKS": "#c0392b", "FOUND_THEN_ACTED": "#0a9d54", "MISS_THEN_CORRECT": "#b06f00"}
TIER_WORDS = {"regions": "gaze regions are validated for this session (click-consistency lift ≥ 0.5) — column and half statements may be made firmly, 3×3 cells with 'probably'",
              "likely": "gaze regions are partially validated (lift 0.25–0.5) — use 'likely' for columns; avoid specific-cell claims",
              "unvalidated": "gaze regions are NOT validated for this session — every gaze statement must read 'estimated (unvalidated)'; lean on mouse, clicks, transcript, and looking-away/keyboard events"}


def esc(s):
    return html.escape("" if s is None else str(s))


def mmss(sec):
    sec = int(round(float(sec))); return f"{sec // 60}:{sec % 60:02d}"


def read_csv(p):
    return list(csv.DictReader(open(p, encoding="utf-8"))) if os.path.exists(p) else []


def f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def collapse_repeats(text):
    """Keep one copy of consecutive duplicate clauses inside one whisper segment (stutter repeats)."""
    parts = re.split(r"(?<=[،,؟?.!])\s*", text or ""); out, prev = [], None
    for q in parts:
        n = re.sub(r"[\s\W]+", "", q).lower()
        if not n:
            continue
        if n != prev:
            out.append(q.strip())
        prev = n
    return " ".join(out) if out else (text or "").strip()


def cut(t, n=80):
    """Cut at a word boundary with an ellipsis instead of mid-word."""
    t = (t or "").strip()
    return t if len(t) <= n else (t[:n].rsplit(" ", 1)[0] + "…")


def click_summary(cl, sep="; "):
    """'What you get (dead) ×4' instead of four repeats."""
    from collections import Counter as _C
    labels = _C((c.get("target_text") or "click") + (" (dead)" if c.get("is_dead") else "") for c in (cl or []))
    return sep.join(f"{k} ×{v}" if v > 1 else k for k, v in labels.items())


def parse_srt(p):
    if not os.path.exists(p):
        return []
    segs = []
    blocks = re.split(r"\n\s*\n", open(p, encoding="utf-8").read().strip())
    _norm = lambda t: re.sub(r"[^\w\s]", "", t).strip().lower()
    freq = Counter(_norm(" ".join(b.strip().splitlines()[2:])) for b in blocks if len(b.strip().splitlines()) > 2)
    for blk in blocks:
        L = blk.strip().splitlines()
        if len(L) < 2:
            continue
        line = L[1] if "-->" in L[1] else L[0]
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", line)
        if not m:
            continue
        g = list(map(int, m.groups()))
        a = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000; b = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        text = " ".join(L[(2 if "-->" in L[1] else 1):]).strip()
        norm = _norm(text)
        if norm in HALLUCINATIONS or ((b - a) >= 20 and len(norm.split()) <= 3) or freq[norm] > 10:
            continue                                   # whisper hallucination on silence (stock phrase / repeated loop)
        if text and not (segs and segs[-1]["text"] == text):
            segs.append(dict(start=a, end=b, text=text))
    return segs


HALLUCINATIONS = {"thank you", "thanks for watching", "thank you for watching", "subtitles by the amaraorg community", "please subscribe",
                  "you", "bye", "so", "شكرا", "شكرا لكم", "ترجمة نانسي قنقر", "اشترك في القناة", "اشتركوا في القناة", "لا تنسوا الاشتراك في القناة"}


def is_arabic(s):
    return bool(re.search(r"[؀-ۿ]", s or ""))


# ---------------- figures ----------------
def attention_svg(rows, moments, dur):
    W, H = 900.0, 200.0
    n = max(len(rows), 1); bw = max(W / n, 1.0)
    parts = [f'<svg viewBox="0 0 {W:.0f} {H:.0f}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;font-family:sans-serif">']
    # ribbon (gaze column per second)
    for i, r in enumerate(rows):
        col = r.get("gaze_col") if r.get("gaze_state") == "on_screen" and f(r.get("gaze_conf")) >= 0.3 else None
        parts.append(f'<rect x="{i*bw:.1f}" y="6" width="{bw+0.3:.1f}" height="26" fill="{COL_COLOR.get(col, "#cbd2df")}"/>')
    # engagement line: on-screen & face present (1 → y=50, 0 → y=110)
    pts = []
    for i, r in enumerate(rows):
        on = f(r.get("face_present")) * (1.0 if r.get("gaze_state") == "on_screen" else 0.0)
        pts.append(f"{i*bw+bw/2:.1f},{110-60*on:.1f}")
    parts.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="#4f8cff" stroke-width="1.6"/>')
    # searching line: region switches per second normalised (band 120–170)
    mx = max([f(r.get("region_switches")) for r in rows] + [1])
    pts = [f"{i*bw+bw/2:.1f},{170-50*f(r.get('region_switches'))/mx:.1f}" for i, r in enumerate(rows)]
    parts.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="#e8871e" stroke-width="1.4"/>')
    # moment markers (phase bar)
    for m in moments:
        x0 = m["t_start_ms"] / 1000 / max(dur, 1) * W; x1 = m["t_end_ms"] / 1000 / max(dur, 1) * W
        parts.append(f'<rect x="{x0:.1f}" y="180" width="{max(x1-x0,2):.1f}" height="10" fill="{MOMENT_COLOR.get(m["type"], "#888")}" opacity="0.85"><title>{esc(m["type"])} {mmss(m["t_start_ms"]/1000)}–{mmss(m["t_end_ms"]/1000)}</title></rect>')
    # axes + labels
    for y, lab in ((32, "gaze column  ■L ■C ■R ■away"), (110, "on-screen / engagement"), (170, "searching (region switches/s)"), (190, "moments")):
        parts.append(f'<line x1="0" y1="{y}" x2="{W:.0f}" y2="{y}" stroke="#e6eaf2" stroke-width="1"/><text x="2" y="{y-3}" font-size="9" fill="#6b7488">{esc(lab)}</text>')
    for frac in (0, 0.25, 0.5, 0.75, 1.0):
        x = frac * W; parts.append(f'<line x1="{x:.0f}" y1="34" x2="{x:.0f}" y2="176" stroke="#f0f3f9"/><text x="{min(x+2, W-30):.0f}" y="199" font-size="9" fill="#6b7488">{mmss(frac*dur)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def lcr_bar(l, c, r):
    tot = (l + c + r) or 1
    seg = lambda w, col: f'<span class="bar" style="width:{max(1, 90*w/tot):.0f}px;background:{col}"></span>'
    return f'{seg(l, COL_COLOR["L"])}{seg(c, COL_COLOR["C"])}{seg(r, COL_COLOR["R"])} <span class="sub">L {100*l/tot:.0f} · C {100*c/tot:.0f} · R {100*r/tot:.0f}</span>'


# ---------------- main build ----------------
def build(session, product):
    A = os.path.join(session, "analysis")
    rows = read_csv(os.path.join(A, "gaze-ai.csv"))
    if not rows:
        raise SystemExit("analysis/gaze-ai.csv missing — run tools/bux-analyze-video.py first")
    Q = json.load(open(os.path.join(A, "quality.json")))
    M = json.load(open(os.path.join(A, "moments.json"))).get("moments", [])
    S = json.load(open(os.path.join(session, "session.json"))) if os.path.exists(os.path.join(session, "session.json")) else {}
    events = read_csv(os.path.join(session, "events.csv"))
    pages = [dict(t=f(e["t_ms"]) / 1000, title=e.get("detail") or e.get("extra") or "page") for e in events if e.get("type") == "page"]
    segs = parse_srt(os.path.join(A, "transcript.srt")) or parse_srt(os.path.join(session, "transcript.srt"))
    dur = f(Q.get("duration_s"), len(rows)); name = os.path.basename(session.rstrip("/"))
    tier = Q.get("click_consistency", {}).get("tier", "unvalidated"); cc = Q.get("click_consistency", {})
    site = S.get("site", name)

    # ---- session stats ----
    on = [r for r in rows if r.get("gaze_state") == "on_screen" and f(r.get("gaze_conf")) >= 0.3]
    cols = Counter(r.get("gaze_col") for r in on if r.get("gaze_col") in ("L", "C", "R"))
    away = 1 - len([r for r in rows if r.get("gaze_state") == "on_screen"]) / max(len(rows), 1)
    switches = sum(f(r.get("region_switches")) for r in rows); scan_rate = 60 * switches / max(dur, 1)
    clicks = [c for r in rows for c in json.loads(r.get("clicks") or "[]")]
    dead = sum(1 for c in clicks if c.get("is_dead")); rage = sum(1 for c in clicks if c.get("is_rage"))
    cells = Counter(r.get("gaze_cell") for r in on if r.get("gaze_cell") and r.get("gaze_cell") != "uncertain")

    # ---- phases (pages, else 60-s bins) ----
    if len(pages) >= 2:
        phases = [dict(name=p["title"][:50], t0=p["t"], t1=(pages[i + 1]["t"] if i + 1 < len(pages) else dur)) for i, p in enumerate(pages)]
    else:
        phases = [dict(name=f"minute {i+1}", t0=i * 60, t1=min((i + 1) * 60, dur)) for i in range(int(dur // 60) + 1) if i * 60 < dur]
    score_rows = []
    for ph in phases:
        w = [r for r in rows if ph["t0"] <= f(r["t_s"]) < ph["t1"]]
        won = [r for r in w if r.get("gaze_state") == "on_screen" and f(r.get("gaze_conf")) >= 0.3]
        cc_ = Counter(r.get("gaze_col") for r in won)
        onp = 100 * len([r for r in w if r.get("gaze_state") == "on_screen"]) / max(len(w), 1)
        spm = 60 * sum(f(r.get("region_switches")) for r in w) / max(len(w), 1)
        score_rows.append(dict(name=ph["name"], time=f"{mmss(ph['t0'])}–{mmss(ph['t1'])}", l=cc_["L"], c=cc_["C"], r=cc_["R"], on=onp, spm=spm,
                               clicks=sum(len(json.loads(r.get("clicks") or "[]")) for r in w)))

    # ---- latency / timing signals ----
    lat = []
    if segs:
        for i in range(1, len(segs)):
            gap = segs[i]["start"] - segs[i - 1]["end"]
            if gap >= 4:
                w = [r for r in rows if segs[i - 1]["end"] <= f(r["t_s"]) <= segs[i]["start"]]
                lat.append(dict(t=segs[i - 1]["end"], gap=gap, eyes=eyes_summary(w), next=cut(segs[i]["text"], 80)))
    for m in M:
        if m["type"] in ("LOOK_AWAY", "CAMERA_FROZEN", "SEARCHING", "CONFUSION"):
            lat.append(dict(t=m["t_start_ms"] / 1000, gap=(m["t_end_ms"] - m["t_start_ms"]) / 1000, eyes=m["type"].replace("_", " ").lower() + ((": " + ", ".join(f"{k} {v:.0%}" for k, v in sorted(m.get("gaze_dwell", {}).items(), key=lambda kv: -kv[1])[:3])) if m.get("gaze_dwell") else ""), next=m.get("transcript", "")[:80]))
    lat.sort(key=lambda x: x["t"])

    # ---- intent tables from moments ----
    intent = []
    for typ in ("SEARCHING", "CONFUSION", "FOUND_THEN_ACTED", "MISS_THEN_CORRECT", "FRUSTRATION", "DEAD_CLICKS", "LOOK_AWAY", "CAMERA_FROZEN"):
        ms = [m for m in M if m["type"] == typ]
        if not ms:
            continue
        dw = Counter()
        for m in ms:
            for k, v in m.get("gaze_dwell", {}).items():
                dw[k] += v
        top = dw.most_common(1)[0][0] if dw else "—"
        secs_ = sum((m["t_end_ms"] - m["t_start_ms"]) / 1000 for m in ms)
        intent.append(dict(intent=typ.replace("_", " ").title(), n=len(ms), secs=secs_, cell=top, col=(top[-1] if top and top != "—" else "—")))

    # ---- appendix rows ----
    appendix = []
    if segs:
        prev_end = 0.0
        for i, s in enumerate(segs, 1):
            w = [r for r in rows if s["start"] <= f(r["t_s"]) <= max(s["end"], s["start"] + 1)]
            appendix.append(dict(n=i, time=mmss(s["start"]), gap=f"{s['start']-prev_end:.1f}" if s["start"] - prev_end >= 1 else "", speech=collapse_repeats(s["text"]), ar=is_arabic(s["text"]),
                                 eyes=eyes_summary(w), on=on_pct(w), mouse=Counter(r.get("mouse_cell") for r in w if r.get("mouse_cell")).most_common(1)))
            prev_end = s["end"]
    else:
        for t0 in range(0, int(dur) + 1, 5):
            w = [r for r in rows if t0 <= f(r["t_s"]) < t0 + 5]
            if not w:
                continue
            cl = [c for r in w for c in json.loads(r.get("clicks") or "[]")]
            appendix.append(dict(n=t0 // 5 + 1, time=mmss(t0), gap="", speech="(no transcript)", ar=False, eyes=eyes_summary(w), on=on_pct(w),
                                 mouse=Counter(r.get("mouse_cell") for r in w if r.get("mouse_cell")).most_common(1), clicks=click_summary(cl, ", ")))

    data = dict(session=name, site=site, duration_s=dur, tier=tier, click_consistency=cc, signs=Q.get("signs"), flags=Q.get("flags"), grade_histogram=Q.get("grade_histogram"),
                gaze_usable_frac=Q.get("gaze_usable_frac"), footer=Q.get("footer"), stats=dict(eyes_away_frac=away, scan_rate_per_min=scan_rate, gaze_cols=dict(cols), gaze_cells=dict(cells),
                clicks=len(clicks), dead_clicks=dead, rage_clicks=rage, transcript_segments=len(segs)), phases=score_rows, latency=lat, intent=intent, moments=M,
                appendix=[dict(n=a["n"], time=a["time"], speech=a["speech"], ar=a["ar"]) for a in appendix],
                placeholders=["PRODUCT_NAME", "GRADE_TABLE", "JOURNEY_SUMMARY", "SCORECARD_JUDGMENT", "PRODUCT_INSIGHTS", "ROADMAP_ROWS", "INSTRUMENT_NEXT", "FINDINGS_P0",
                              "LATENCY_MEANING", "FINDINGS_P1", "DELIGHTERS", "FINDINGS_P2", "ACTION_LIST_ROWS", "INTENT_READS_AS", "PLACEMENT_TABLE", "PER_NEED_MAP", "APPENDIX_ENGLISH"])
    json.dump(data, open(os.path.join(A, "report-data.json"), "w"), ensure_ascii=False, indent=1)
    open(os.path.join(A, "report-scaffold.html"), "w", encoding="utf-8").write(render(data, rows, M, Q, product or site, appendix, segs))
    return data


def eyes_summary(w):
    on = [r for r in w if r.get("gaze_state") == "on_screen" and f(r.get("gaze_conf")) >= 0.3]
    if not w:
        return "—"
    if not on:
        st = Counter(r.get("gaze_state") for r in w).most_common(1)[0][0]
        return st.replace("_", " ")
    cell = Counter(r.get("gaze_cell") for r in on if r.get("gaze_cell") and r.get("gaze_cell") != "uncertain").most_common(1)
    col = Counter(r.get("gaze_col") for r in on).most_common(1)
    sw = sum(f(r.get("region_switches")) for r in w)
    return f"{(cell[0][0] if cell else col[0][0] if col else '?')} · ↔{sw:.0f}"


def on_pct(w):
    return f"{100*len([r for r in w if r.get('gaze_state')=='on_screen'])/max(len(w),1):.0f}%"


def render(D, rows, M, Q, product, appendix, segs):
    t = D["tier"]; cc = D["click_consistency"]; st = D["stats"]
    o = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>UX Findings — {esc(product)}</title><style>{CSS}</style></head><body>"]
    o.append(f"<h1>Detailed Usability Findings &amp; UX Recommendations</h1><div class='sub'>{esc(product)} · session {esc(D['session'])} · {mmss(D['duration_s'])} · generated by Boring UX</div>")
    # method & confidence
    ccs = (f"<b>click-consistency {cc['col']['score']:.0%}</b> vs {cc['col']['chance']:.0%} chance (lift {cc['col']['lift']:.2f}, N = {cc['n']})" if cc.get("col") else "<b>no positioned clicks</b> — gaze could not be validated against clicks")
    signs = Q.get("signs", {}); flip = signs.get("l2cs_channel_test", {}).get("status"); orient = signs.get("orientation", {}).get("status")
    o.append(f"<div class='meta'><b>Method &amp; confidence.</b> 1 session ({mmss(D['duration_s'])}), face video analysed at 10 Hz with L2CS-Net gaze + MediaPipe (blink, head pose, expression cues), "
             f"{'transcript joined on the shared clock' if st['transcript_segments'] else '<b>no transcript</b> (speech evidence unavailable)'}; mouse, clicks and page events time-locked (&lt;100 ms). "
             f"Gaze usable in <b>{100*f(D['gaze_usable_frac']):.0f}%</b> of seconds (grades {esc(json.dumps(D['grade_histogram']))}); {ccs}; sign self-test {esc(flip)}, orientation {esc(orient)}. "
             f"<span class='tier'>tier: {esc(t)}</span> — {esc(TIER_WORDS[t])}. Flags: <code>{esc(', '.join(D['flags'] or []) or 'none')}</code>.</div>")
    o.append(f"<div class='caveat'>{esc(D['footer'])}</div>")
    # grade table placeholder
    o.append("<h2>Overall grade</h2><table><tr><th>Dimension</th><th>Score /10</th><th>Why (evidence)</th></tr>{{GRADE_TABLE}}</table>")
    # journey
    o.append("<h2>The journey</h2><table><tr><th>Session</th><th>Length</th><th>Journey</th><th>Eyes-away</th><th>Scan rate</th><th>Clicks (dead · rage)</th></tr>"
             f"<tr><td>{esc(D['session'])}</td><td>{mmss(D['duration_s'])}</td><td>{{{{JOURNEY_SUMMARY}}}}</td><td>{100*st['eyes_away_frac']:.0f}%</td><td>{st['scan_rate_per_min']:.0f} switches/min</td><td>{st['clicks']} ({st['dead_clicks']} · {st['rage_clicks']})</td></tr></table>")
    # Part B
    o.append("<h2>Part B · Attention analytics — the session, graphed</h2><h3>Session · gaze column, engagement, searching, moments</h3>" + attention_svg(rows, M, D["duration_s"]))
    o.append("<p class='sub'>Ribbon = gaze column per second (orange L, blue C, green R, grey away/unknown). Blue line = on-screen engagement. Orange line = searching intensity. Bottom bar = detected moments (" + ", ".join(f"{k.replace('_', ' ').lower()} <span style=\"color:{c}\">■</span>" for k, c in MOMENT_COLOR.items()) + ").</p>")
    # Part C scorecard
    o.append("<h2>Part C · Feature-by-feature scorecard</h2><table><tr><th>Feature / phase</th><th>Time</th><th>Gaze L/C/R</th><th>On-screen</th><th>Search/min</th><th>Clicks</th><th>Signal</th><th>Friction</th><th>Recommendation</th></tr>")
    for i, p in enumerate(D["phases"], 1):
        o.append(f"<tr><td>{esc(p['name'])}</td><td>{esc(p['time'])}</td><td>{lcr_bar(p['l'], p['c'], p['r'])}</td><td>{p['on']:.0f}%</td><td>{p['spm']:.0f}</td><td>{p['clicks']}</td><td>{{{{SIGNAL_{i}}}}}</td><td>{{{{FRICTION_{i}}}}}</td><td>{{{{RECOMMENDATION_{i}}}}}</td></tr>")
    o.append("</table><!-- SCORECARD_JUDGMENT: fill Signal (Delight/Friction/Confusion/Request), Friction 0–100, Recommendation per row -->")
    # Part D
    o.append("<h2>Part D · What this means for the product</h2><h3>Feature attention × friction matrix</h3>{{PRODUCT_INSIGHTS}}"
             "<h3>Prioritised roadmap</h3><table><tr><th>When</th><th>Ship</th><th>Why (signal)</th><th>Impact</th><th>Effort</th></tr>{{ROADMAP_ROWS}}</table>"
             "<h3>What to instrument next</h3>{{INSTRUMENT_NEXT}}")
    # Part E
    o.append("<h2>Part E · Detailed findings &amp; recommendations</h2>"
             "<!-- FINDING BLOCK TEMPLATE (copy exactly):\n<div class=\"finding\">\n<h3>C1 · Title <span class=\"p0\">P0</span></h3>\n<p><b>Page:</b> … <b>Task:</b> …</p>\n<span class=\"ar\">\"original quote\"</span>\n<div class=\"en\">[m:ss] \"English translation\"</div>\n<div class=\"eyes\">👁 <b>Eyes:</b> cell/column · state · switches — ⏱ timing</div>\n<div class=\"rec\"><b>Fix:</b> …</div>\n</div> -->"
             "<h2>🔴 Critical issues (P0)</h2>{{FINDINGS_P0}}")
    # latency
    o.append("<h2>⏱ Latency, hesitation &amp; timing</h2><table><tr><th>Signal</th><th>Measurement</th><th>What the eyes were doing</th><th>Meaning</th></tr>")
    for i, l in enumerate(D["latency"][:20], 1):
        o.append(f"<tr><td>{'silence' if 'gap' in l and not l['eyes'].startswith(('searching','confusion','look')) else 'moment'} at {mmss(l['t'])}</td><td>{l['gap']:.1f} s</td><td>{esc(l['eyes'])}{(' · then: “'+esc(l['next'])+'”') if l.get('next') else ''}</td><td>{{{{LATENCY_MEANING_{i}}}}}</td></tr>")
    o.append("</table>")
    # moments
    o.append("<h2>Detected moments (from the fused signal)</h2><table><tr><th>Time</th><th>Moment</th><th>Score</th><th>Quality</th><th>Eyes (dwell)</th><th>Mouse</th><th>Clicks</th><th>Said</th></tr>")
    for m in M:
        dw = ", ".join(f"{k} {v:.0%}" for k, v in sorted(m.get("gaze_dwell", {}).items(), key=lambda kv: -kv[1])[:3])
        cl = click_summary(m.get("clicks", []))[:80]
        o.append(f"<tr><td>{mmss(m['t_start_ms']/1000)}–{mmss(m['t_end_ms']/1000)}</td><td><b>{esc(m['type'].replace('_',' '))}</b>{(' · '+esc(m['subtype'])) if m.get('subtype') else ''}</td><td>{m['score']:.2f}</td><td>{esc(m['min_quality_grade'])}</td><td>{esc(dw) or '—'}</td><td>{esc(', '.join(m.get('mouse_cells') or []))}</td><td>{esc(cl)}</td><td>{esc(m.get('transcript') or '')}</td></tr>")
    o.append("</table>")
    o.append("<h2>🟠 High-priority issues (P1)</h2>{{FINDINGS_P1}}<h2>🟢 What's working — keep &amp; promote (delighters)</h2>{{DELIGHTERS}}<h2>🔵 Nice-to-have (P2)</h2>{{FINDINGS_P2}}")
    o.append("<h2>Prioritised action list</h2><table><tr><th>#</th><th>Pri</th><th>Page</th><th>Action</th><th>Evidence (said · eyes · time)</th></tr>{{ACTION_LIST_ROWS}}</table>")
    # conclusion
    o.append("<h2>Conclusion · Where the eyes go, by intent</h2><h3>Behaviour by intent (the robust signal)</h3><table><tr><th>Intent</th><th>Episodes</th><th>Total time</th><th>Dominant cell</th><th>Column</th><th>Reads as</th></tr>")
    for k, it in enumerate(D["intent"], 1):
        o.append(f"<tr><td>{esc(it['intent'])}</td><td>{it['n']}</td><td>{it['secs']:.0f} s</td><td>{esc(it['cell'])}</td><td>{esc(it['col'])}</td><td>{{{{INTENT_READS_AS_{k}}}}}</td></tr>")
    o.append("</table><h3>Where to place actions, data &amp; filters</h3><table><tr><th>When the user wants to…</th><th>Their eyes concentrate…</th><th>Behaviour</th><th>So place it…</th></tr>{{PLACEMENT_TABLE}}</table>"
             "<h3>Per-need map — where they looked for each thing</h3><table><tr><th>Need</th><th>Intent</th><th>Where the eyes went</th><th>Verdict</th><th>Fix</th></tr>{{PER_NEED_MAP}}</table>")
    # appendix
    o.append(f"<h2>Appendix — complete fused timeline ({'every transcript segment' if segs else 'every 5 s — no transcript'})</h2><table><tr><th>#</th><th>Time</th><th>Gap</th><th>Speech (as transcribed)</th><th>English</th><th>Eyes</th><th>On</th><th>Mouse</th></tr>")
    for a in appendix:
        sp = f"<span class='ar'>{esc(a['speech'])}</span>" if a["ar"] else esc(a["speech"])
        en = f"{{{{APPENDIX_ENGLISH_{a['n']}}}}}" if a["ar"] else ""
        mouse = a["mouse"][0][0] if a.get("mouse") else "—"
        extra = (" · " + esc(a["clicks"])) if a.get("clicks") else ""
        o.append(f"<tr><td>{a['n']}</td><td>{a['time']}</td><td>{a['gap']}</td><td>{sp}{extra}</td><td>{en}</td><td>{esc(a['eyes'])}</td><td>{a['on']}</td><td>{esc(mouse)}</td></tr>")
    o.append("</table>")
    o.append(f"<div class='caveat' style='margin-top:24px'>{esc(D['footer'])} Tier: {esc(t)}.</div></body></html>")
    return "\n".join(o)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("session"); ap.add_argument("--product", default=None)
    a = ap.parse_args()
    D = build(os.path.abspath(os.path.expanduser(a.session)), a.product)
    print(f"[bux-report] scaffold → analysis/report-scaffold.html | data → analysis/report-data.json | tier={D['tier']} | phases={len(D['phases'])} moments={len(D['moments'])} latency={len(D['latency'])} placeholders={len(D['placeholders'])}")


if __name__ == "__main__":
    main()
