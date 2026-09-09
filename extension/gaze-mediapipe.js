/* Boring UX — MediaPipe gaze engine.
 * Replaces WebGazer's coarse 2D regression with MediaPipe FaceLandmarker
 * (iris + face landmarks + head-pose matrix), then maps those features to
 * screen coordinates with a ridge regression trained on the calibration dots.
 * Runs entirely in the content script (isolated world) — all assets are
 * bundled in the extension (vendor/mediapipe/*), no remote code.
 *
 * Public API (window.BUXGaze):
 *   await init()             load wasm + model (slow; call on "Enable camera")
 *   await startCamera()      getUserMedia + begin the per-frame loop → returns the video MediaStreamTrack
 *   onGaze = fn({x,y})       called each frame a face is found (screen px)
 *   calibrate(px,py)         record the current features → this target; retrains
 *   calibrated               true once enough calibration samples exist
 *   accuracyHint             null (the panel measures px separately)
 *   videoEl / track          the hidden <video> and its camera track
 *   stop()                   stop loop, close landmarker, release camera, remove video
 */
(function () {
  if (window.BUXGaze) return;

  // ---- landmark indices (MediaPipe FaceMesh, 478 pts with iris refine) ----
  const L = {
    Louter: 33, Linner: 133, Lup: 159, Ldn: 145, Liris: [468, 469, 470, 471, 472],
    Router: 263, Rinner: 362, Rup: 386, Rdn: 374, Riris: [473, 474, 475, 476, 477]
  };
  const DIM = 13;                    // feature vector length (incl. bias)
  const MAXN = 600;                  // calibration sample cap
  const MINPTS = 6;                  // samples needed before we predict
  const LAMBDA = 1e-3;               // ridge regularization (feature space)
  const SMOOTH = 0.35;               // EMA on the predicted point
  const FEAT_EMA = 0.5;              // EMA on raw features (for stable calibration)

  const G = {
    vision: null, fileset: null, landmarker: null, video: null, stream: null,
    track: null, raf: 0, running: false, onGaze: null,
    X: [], Yx: [], Yy: [],          // training buffer (features, normalized targets)
    Wx: null, Wy: null,             // solved weights
    lastFeat: null, featEMA: null,  // most recent (smoothed) feature vector
    sx: null, sy: null,             // smoothed predicted point
    lastTs: 0
  };

  function mean(lms, idx) { let x = 0, y = 0; for (const i of idx) { x += lms[i].x; y += lms[i].y; } return { x: x / idx.length, y: y / idx.length }; }

  function extract(lms, matrix) {
    const li = mean(lms, L.Liris), ri = mean(lms, L.Riris);
    const lo = lms[L.Louter], lin = lms[L.Linner], lup = lms[L.Lup], ldn = lms[L.Ldn];
    const ro = lms[L.Router], rin = lms[L.Rinner], rup = lms[L.Rup], rdn = lms[L.Rdn];
    const sx = v => (Math.abs(v) < 1e-6 ? 1e-6 : v);
    // iris position within each eye box, 0..1 (x: inner→outer, y: upper→lower)
    const lx = (li.x - lin.x) / sx(lo.x - lin.x);
    const ly = (li.y - lup.y) / sx(ldn.y - lup.y);
    const rx = (ri.x - rin.x) / sx(ro.x - rin.x);
    const ry = (ri.y - rup.y) / sx(rdn.y - rup.y);
    // head pose from the 4x4 facial transformation matrix (column-major)
    let yaw = 0, pitch = 0;
    if (matrix && matrix.length >= 11) {
      yaw = Math.atan2(matrix[8], matrix[10]);
      pitch = Math.atan2(-matrix[9], Math.hypot(matrix[8], matrix[10]));
    }
    // feature vector: bias + per-eye iris + head pose + head×iris cross terms + averages
    return [1, lx, ly, rx, ry, yaw, pitch, lx * yaw, rx * yaw, ly * pitch, ry * pitch, (lx + rx) / 2, (ly + ry) / 2];
  }

  // Solve (A) w = b for w, A is d×d (destructive Gaussian elimination w/ partial pivot).
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
    // A = XᵀX + λI ; bx = XᵀYx ; by = XᵀYy
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
    return { x: x * innerWidth, y: y * innerHeight };   // targets were stored normalized
  }

  function loop() {
    if (!G.running) return;
    G.raf = requestAnimationFrame(loop);
    const v = G.video; if (!v || v.readyState < 2) return;
    const ts = performance.now(); if (ts <= G.lastTs) return; G.lastTs = ts;
    let res; try { res = G.landmarker.detectForVideo(v, ts); } catch (e) { return; }
    if (!res || !res.faceLandmarks || !res.faceLandmarks.length) return;
    const lms = res.faceLandmarks[0];
    const mat = res.facialTransformationMatrixes && res.facialTransformationMatrixes[0]
      ? res.facialTransformationMatrixes[0].data : null;
    const raw = extract(lms, mat);
    // EMA-smooth the feature vector so calibration + prediction use a steady signal
    if (!G.featEMA) G.featEMA = raw.slice();
    else for (let i = 0; i < DIM; i++) G.featEMA[i] = FEAT_EMA * raw[i] + (1 - FEAT_EMA) * G.featEMA[i];
    G.lastFeat = G.featEMA;
    const p = predict(G.featEMA);
    if (p && G.onGaze) {
      if (G.sx == null) { G.sx = p.x; G.sy = p.y; }
      else { G.sx = SMOOTH * p.x + (1 - SMOOTH) * G.sx; G.sy = SMOOTH * p.y + (1 - SMOOTH) * G.sy; }
      G.onGaze({ x: G.sx, y: G.sy });
    }
  }

  const API = {
    onGaze: null,
    accuracyHint: null,
    get videoEl() { return G.video; },
    get track() { return G.track; },
    get calibrated() { return G.X.length >= MINPTS && !!G.Wx; },

    async init() {
      if (G.landmarker) return;
      const url = p => chrome.runtime.getURL("vendor/mediapipe/" + p);
      G.vision = await import(url("vision_bundle.mjs"));
      G.fileset = await G.vision.FilesetResolver.forVisionTasks(url("wasm"));
      G.landmarker = await G.vision.FaceLandmarker.createFromOptions(G.fileset, {
        baseOptions: { modelAssetPath: url("face_landmarker.task") },
        runningMode: "VIDEO",
        numFaces: 1,
        outputFacialTransformationMatrixes: true,
        outputFaceBlendshapes: false
      });
    },

    async startCamera() {
      await this.init();
      G.stream = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480, facingMode: "user" }, audio: false });
      G.track = G.stream.getVideoTracks()[0];
      const v = document.createElement("video");
      v.id = "bux-mp-video"; v.autoplay = true; v.playsInline = true; v.muted = true;
      v.srcObject = G.stream; document.documentElement.appendChild(v);
      G.video = v;
      await new Promise(res => { if (v.readyState >= 2) res(); else v.onloadeddata = () => res(); });
      try { await v.play(); } catch (e) {}
      G.onGaze = (...a) => API.onGaze && API.onGaze(...a);
      G.running = true; G.raf = requestAnimationFrame(loop);
      return G.track;
    },

    // Record the current (smoothed) features against a screen target, then retrain.
    calibrate(px, py) {
      if (!G.lastFeat) return false;
      G.X.push(G.lastFeat.slice()); G.Yx.push(px / innerWidth); G.Yy.push(py / innerHeight);
      if (G.X.length > MAXN) { G.X.shift(); G.Yx.shift(); G.Yy.shift(); }
      retrain();
      return true;
    },

    stop() {
      G.running = false; if (G.raf) cancelAnimationFrame(G.raf); G.raf = 0;
      try { G.landmarker && G.landmarker.close(); } catch (e) {}
      try { G.stream && G.stream.getTracks().forEach(t => t.stop()); } catch (e) {}
      try { G.track && G.track.stop(); } catch (e) {}
      if (G.video) { try { G.video.srcObject = null; G.video.remove(); } catch (e) {} }
      G.landmarker = null; G.stream = null; G.track = null; G.video = null;
      G.X = []; G.Yx = []; G.Yy = []; G.Wx = G.Wy = null;
      G.lastFeat = G.featEMA = null; G.sx = G.sy = null; G.lastTs = 0;
    }
  };

  window.BUXGaze = API;
})();
