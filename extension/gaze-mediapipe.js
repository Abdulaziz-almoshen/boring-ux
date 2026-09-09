/* Boring UX — MediaPipe gaze engine (runs in the PAGE MAIN world).
 *
 * Why main world: MediaPipe's bundle loads its wasm loader with
 * document.createElement("script"), which executes in the main world and sets
 * self.ModuleFactory there. The library then reads self.ModuleFactory from the
 * SAME world. In a content-script isolated world those two selves differ, so it
 * throws "ModuleFactory not set." Running here keeps loader + library in one world.
 *
 * The content script (isolated world) owns the camera, recording, UI, and
 * downloads. It shares the <video id="bux-mp-video"> element via the DOM and
 * talks to this engine over window.postMessage:
 *   content → engine : {source:"bux-content", cmd:"init"|"calibrate"|"stop", ...}
 *   engine  → content : {source:"bux-engine", evt:"ready"|"error"|"gaze", ...}
 */
(function () {
  if (window.__buxEngine) return; window.__buxEngine = true;

  const L = {
    Louter: 33, Linner: 133, Lup: 159, Ldn: 145, Liris: [468, 469, 470, 471, 472],
    Router: 263, Rinner: 362, Rup: 386, Rdn: 374, Riris: [473, 474, 475, 476, 477]
  };
  const DIM = 13, MAXN = 600, MINPTS = 6, LAMBDA = 1e-3, SMOOTH = 0.35, FEAT_EMA = 0.5;

  const G = {
    base: null, vision: null, landmarker: null, running: false, raf: 0,
    X: [], Yx: [], Yy: [], Wx: null, Wy: null,
    lastFeat: null, featEMA: null, sx: null, sy: null, lastTs: 0
  };

  const post = (evt, extra) => window.postMessage(Object.assign({ source: "bux-engine", evt }, extra || {}), "*");

  function mean(lms, idx) { let x = 0, y = 0; for (const i of idx) { x += lms[i].x; y += lms[i].y; } return { x: x / idx.length, y: y / idx.length }; }

  function extract(lms, matrix) {
    const li = mean(lms, L.Liris), ri = mean(lms, L.Riris);
    const lo = lms[L.Louter], lin = lms[L.Linner], lup = lms[L.Lup], ldn = lms[L.Ldn];
    const ro = lms[L.Router], rin = lms[L.Rinner], rup = lms[L.Rup], rdn = lms[L.Rdn];
    const sx = v => (Math.abs(v) < 1e-6 ? 1e-6 : v);
    const lx = (li.x - lin.x) / sx(lo.x - lin.x);
    const ly = (li.y - lup.y) / sx(ldn.y - lup.y);
    const rx = (ri.x - rin.x) / sx(ro.x - rin.x);
    const ry = (ri.y - rup.y) / sx(rdn.y - rup.y);
    let yaw = 0, pitch = 0;
    if (matrix && matrix.length >= 11) {
      yaw = Math.atan2(matrix[8], matrix[10]);
      pitch = Math.atan2(-matrix[9], Math.hypot(matrix[8], matrix[10]));
    }
    return [1, lx, ly, rx, ry, yaw, pitch, lx * yaw, rx * yaw, ly * pitch, ry * pitch, (lx + rx) / 2, (ly + ry) / 2];
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
    return { x: x * innerWidth, y: y * innerHeight };
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

  function beat(ok) { const now = performance.now(); if (now - (G.hb || 0) > 200) { G.hb = now; post("face", { ok }); } }

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
    const raw = extract(lms, mat);
    if (!G.featEMA) G.featEMA = raw.slice();
    else for (let i = 0; i < DIM; i++) G.featEMA[i] = FEAT_EMA * raw[i] + (1 - FEAT_EMA) * G.featEMA[i];
    G.lastFeat = G.featEMA;
    const p = predict(G.featEMA);
    if (p) {
      if (G.sx == null) { G.sx = p.x; G.sy = p.y; }
      else { G.sx = SMOOTH * p.x + (1 - SMOOTH) * G.sx; G.sy = SMOOTH * p.y + (1 - SMOOTH) * G.sy; }
      post("gaze", { x: G.sx, y: G.sy });
    } else { beat(true); }   // face found but not calibrated yet — let the UI show it's alive
  }

  function calibrate(px, py) {
    if (!G.lastFeat) return;
    G.X.push(G.lastFeat.slice()); G.Yx.push(px / innerWidth); G.Yy.push(py / innerHeight);
    if (G.X.length > MAXN) { G.X.shift(); G.Yx.shift(); G.Yy.shift(); }
    retrain();
  }

  function stop() {
    G.running = false; if (G.raf) cancelAnimationFrame(G.raf); G.raf = 0;
    try { G.landmarker && G.landmarker.close(); } catch (e) {}
    G.landmarker = null; G.X = []; G.Yx = []; G.Yy = []; G.Wx = G.Wy = null;
    G.lastFeat = G.featEMA = null; G.sx = G.sy = null; G.lastTs = 0;
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
