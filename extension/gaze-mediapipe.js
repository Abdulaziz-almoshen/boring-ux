/* Boring UX — MediaPipe gaze engine (runs in the PAGE MAIN world).
 *
 * Why main world: MediaPipe loads its wasm loader with a <script> tag that runs
 * in the main world and sets self.ModuleFactory there; the library reads it from
 * the same world. In a content-script isolated world those selves differ →
 * "ModuleFactory not set." Running here keeps loader + library in one world.
 *
 * The isolated content script owns camera/recording/UI/downloads and shares the
 * <video id="bux-mp-video"> via the DOM. Bridge over window.postMessage:
 *   content → engine : {source:"bux-content", cmd:"init"|"calibrate"|"stop", ...}
 *   engine  → content : {source:"bux-engine", evt:"ready"|"error"|"gaze"|"face", ...}
 *
 * Gaze model: per-eye iris offset (measured in a consistent screen-ward
 * direction, normalized by eye size) is combined, expanded to a 2nd-order
 * polynomial, and mapped to screen x/y by ridge regression trained on the
 * calibration dots (multiple frames per click, blinks rejected).
 */
(function () {
  if (window.__buxEngine) return; window.__buxEngine = true;

  const L = {
    Louter: 33, Linner: 133, Lup: 159, Ldn: 145, Liris: [468, 469, 470, 471, 472],
    Router: 263, Rinner: 362, Rup: 386, Rdn: 374, Riris: [473, 474, 475, 476, 477]
  };
  const DIM = 14;                 // polynomial feature vector length
  const MAXN = 900;               // calibration sample cap
  const MINPTS = 12;              // samples needed before predicting
  const LAMBDA = 5e-3;            // ridge regularization
  const SMOOTH = 0.4;             // EMA on the predicted point
  const OPEN_MIN = 0.10;          // eye-openness floor (reject blinks)
  const COLLECT_MS = 320;         // per-click sampling window
  const COLLECT_GAP = 45;         // min ms between collected samples

  const G = {
    base: null, vision: null, landmarker: null, running: false, raf: 0,
    X: [], Yx: [], Yy: [], Wx: null, Wy: null,
    sx: null, sy: null, lastTs: 0, hb: 0,
    collect: null, lastCollect: 0, lastRaw: null
  };

  const post = (evt, extra) => window.postMessage(Object.assign({ source: "bux-engine", evt }, extra || {}), "*");
  const mean = (lms, idx) => { let x = 0, y = 0; for (const i of idx) { x += lms[i].x; y += lms[i].y; } return { x: x / idx.length, y: y / idx.length }; };

  // Raw eye/head signal from one frame. Iris offset is measured relative to the
  // eye center and normalized by eye width/height, in a CONSISTENT direction for
  // both eyes (positive = iris toward image-right / down), so averaging is valid.
  function raw(lms, matrix) {
    const eye = (iris, inner, outer, up, dn) => {
      const cx = (inner.x + outer.x) / 2, w = Math.abs(outer.x - inner.x) || 1e-6;
      const cy = (up.y + dn.y) / 2, h = Math.abs(dn.y - up.y) || 1e-6;
      return { ex: (iris.x - cx) / w, ey: (iris.y - cy) / h, open: h / (w || 1e-6) };
    };
    const Le = eye(mean(lms, L.Liris), lms[L.Linner], lms[L.Louter], lms[L.Lup], lms[L.Ldn]);
    const Re = eye(mean(lms, L.Riris), lms[L.Rinner], lms[L.Router], lms[L.Rup], lms[L.Rdn]);
    let yaw = 0, pitch = 0;
    if (matrix && matrix.length >= 11) {
      yaw = Math.atan2(matrix[8], matrix[10]);
      pitch = Math.atan2(-matrix[9], Math.hypot(matrix[8], matrix[10]));
    }
    return {
      ex: (Le.ex + Re.ex) / 2, ey: (Le.ey + Re.ey) / 2,
      exL: Le.ex, exR: Re.ex, eyL: Le.ey, eyR: Re.ey,
      yaw, pitch, open: (Le.open + Re.open) / 2
    };
  }

  // 2nd-order polynomial feature vector (captures the curvature of gaze→screen).
  function feat(r) {
    const { ex, ey, exL, exR, eyL, eyR, yaw, pitch } = r;
    return [1, ex, ey, ex * ex, ey * ey, ex * ey, yaw, pitch, ex * yaw, ey * pitch, exL, exR, eyL, eyR];
  }

  function solve(A, b) {
    const n = b.length, M = A.map((r, i) => r.slice().concat(b[i]));
    for (let c = 0; c < n; c++) {
      let p = c; for (let r = c + 1; r < n; r++) if (Math.abs(M[r][c]) > Math.abs(M[p][c])) p = r;
      if (Math.abs(M[p][c]) < 1e-12) continue;
      [M[c], M[p]] = [M[p], M[c]];
      for (let r = 0; r < n; r++) if (r !== c) { const f = M[r][c] / M[c][c]; for (let k = c; k <= n; k++) M[r][k] -= f * M[c][k]; }
    }
    return M.map((r, i) => Math.abs(M[i][i]) < 1e-12 ? 0 : r[n] / M[i][i]);
  }

  function retrain() {
    const n = G.X.length; if (n < MINPTS) { G.Wx = G.Wy = null; return; }
    const A = Array.from({ length: DIM }, () => new Array(DIM).fill(0));
    const bx = new Array(DIM).fill(0), by = new Array(DIM).fill(0);
    for (let s = 0; s < n; s++) {
      const f = G.X[s];
      for (let i = 0; i < DIM; i++) {
        bx[i] += f[i] * G.Yx[s]; by[i] += f[i] * G.Yy[s];
        for (let j = 0; j < DIM; j++) A[i][j] += f[i] * f[j];
      }
    }
    for (let i = 0; i < DIM; i++) A[i][i] += LAMBDA;
    G.Wx = solve(A, bx); G.Wy = solve(A, by);
  }

  function predict(f) {
    if (!G.Wx) return null;
    let x = 0, y = 0; for (let i = 0; i < DIM; i++) { x += f[i] * G.Wx[i]; y += f[i] * G.Wy[i]; }
    return { x: Math.max(0, Math.min(innerWidth, x * innerWidth)), y: Math.max(0, Math.min(innerHeight, y * innerHeight)) };
  }

  function addSample(r, px, py) {
    G.X.push(feat(r)); G.Yx.push(px / innerWidth); G.Yy.push(py / innerHeight);
    if (G.X.length > MAXN) { G.X.shift(); G.Yx.shift(); G.Yy.shift(); }
  }

  async function init() {
    if (G.landmarker) return;
    G.vision = await import(G.base + "vision_bundle.mjs");
    const fs = await G.vision.FilesetResolver.forVisionTasks(G.base + "wasm");
    G.landmarker = await G.vision.FaceLandmarker.createFromOptions(fs, {
      baseOptions: { modelAssetPath: G.base + "face_landmarker.task" },
      runningMode: "VIDEO", numFaces: 1, outputFacialTransformationMatrixes: true, outputFaceBlendshapes: false
    });
  }

  function beat(ok) { const now = performance.now(); if (now - G.hb > 200) { G.hb = now; post("face", { ok }); } }

  function loop() {
    if (!G.running) return;
    G.raf = requestAnimationFrame(loop);
    const v = document.getElementById("bux-mp-video");
    if (!v || v.readyState < 2) return;
    const ts = performance.now(); if (ts <= G.lastTs) return; G.lastTs = ts;
    let res; try { res = G.landmarker.detectForVideo(v, ts); } catch (e) { return; }
    if (!res || !res.faceLandmarks || !res.faceLandmarks.length) { beat(false); return; }
    const lms = res.faceLandmarks[0];
    const mat = res.facialTransformationMatrixes && res.facialTransformationMatrixes[0]
      ? res.facialTransformationMatrixes[0].data : null;
    const r = raw(lms, mat);
    const blink = r.open < OPEN_MIN;
    if (!blink) G.lastRaw = r;

    // Collect calibration samples for a short window after each dot click.
    if (G.collect && !blink && ts - G.lastCollect >= COLLECT_GAP) {
      G.lastCollect = ts;
      addSample(r, G.collect.px, G.collect.py);
      if (ts >= G.collect.end) { G.collect = null; retrain(); }
      else retrain();
    } else if (G.collect && ts >= G.collect.end) { G.collect = null; retrain(); }

    const p = blink ? null : predict(feat(r));
    if (p) {
      if (G.sx == null) { G.sx = p.x; G.sy = p.y; }
      else { G.sx = SMOOTH * p.x + (1 - SMOOTH) * G.sx; G.sy = SMOOTH * p.y + (1 - SMOOTH) * G.sy; }
      post("gaze", { x: G.sx, y: G.sy });
    } else { beat(true); }
  }

  function calibrate(px, py) {
    // open a short sampling window; the loop pushes several frames for this dot
    G.collect = { px, py, end: performance.now() + COLLECT_MS };
    // also grab the current frame immediately so a fast click still yields a sample
    if (G.lastRaw) { addSample(G.lastRaw, px, py); retrain(); }
  }

  function stop() {
    G.running = false; if (G.raf) cancelAnimationFrame(G.raf); G.raf = 0;
    try { G.landmarker && G.landmarker.close(); } catch (e) {}
    G.landmarker = null; G.X = []; G.Yx = []; G.Yy = []; G.Wx = G.Wy = null;
    G.sx = G.sy = null; G.lastTs = 0; G.collect = null; G.lastRaw = null;
  }

  window.addEventListener("message", async (e) => {
    if (e.source !== window || !e.data || e.data.source !== "bux-content") return;
    const d = e.data;
    if (d.cmd === "init") {
      G.base = d.base;
      try { await init(); G.running = true; G.raf = requestAnimationFrame(loop); post("ready"); }
      catch (err) { post("error", { msg: String(err && err.message || err) }); }
    } else if (d.cmd === "calibrate") { calibrate(d.x, d.y); }
    else if (d.cmd === "stop") { stop(); }
  });
})();
