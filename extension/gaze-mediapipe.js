/* Boring UX — MediaPipe gaze engine (runs in the PAGE MAIN world).
 *
 * Port of JEOresearch/EyeTracker's Webcam3DTracker (MonitorTracking.py) to JS:
 * an EYE-SPHERE 3D gaze model. A head coordinate frame is built from nose
 * landmarks by PCA; at calibration each eyeball center is locked as a fixed
 * head-local offset a little behind the iris; per frame the gaze direction is
 * (iris_3d - eyeball_center_3d), averaged over both eyes, converted to yaw/pitch.
 * Their fixed-gain single-point screen map is replaced with a 2nd-order ridge
 * fit over the 13-dot calibration (their README calls that mapping a prototype).
 *
 * Runs in the MAIN world so MediaPipe's wasm loader (<script> tag) and library
 * share one `self` (avoids "ModuleFactory not set"). The isolated content script
 * owns camera/recording/UI/downloads and shares <video id="bux-mp-video">.
 * Bridge (window.postMessage):
 *   content → engine : {source:"bux-content", cmd:"init"|"calibrate"|"stop", ...}
 *   engine  → content : {source:"bux-engine", evt:"ready"|"error"|"gaze"|"face", ...}
 */
(function () {
  if (window.__buxEngine) return; window.__buxEngine = true;

  // nose-region landmark indices (stable under lateral head motion) — from MonitorTracking.py
  const NOSE = [4, 45, 275, 220, 440, 1, 5, 51, 281, 44, 274, 241, 461, 125, 354, 218, 438, 195, 167, 393, 165, 391, 3, 248];
  const IRIS_L = 468, IRIS_R = 473;               // MediaPipe iris centers
  const LID_LU = 159, LID_LD = 145, LID_RU = 386, LID_RD = 374; // for blink rejection
  const BASE_RADIUS = 20;                          // eyeball radius at calib distance (px @640w)
  const DIM = 6;                                   // poly features of (yaw,pitch)
  const MAXN = 900, MINPTS = 10, LAMBDA = 2e-3;
  const SMOOTH = 0.4, DIR_EMA = 0.5, OPEN_MIN = 0.10;
  const COLLECT_MS = 320, COLLECT_GAP = 45, YN = 20, PN = 10; // yaw/pitch normalizers (deg)

  const G = {
    base: null, vision: null, landmarker: null, running: false, raf: 0,
    Rref: null, locked: false, offL: null, offR: null, calScale: 0,
    dirEMA: null, X: [], Yx: [], Yy: [], Wx: null, Wy: null,
    sx: null, sy: null, lastTs: 0, hb: 0, collect: null, lastCollect: 0, cur: null
  };

  const post = (evt, extra) => window.postMessage(Object.assign({ source: "bux-engine", evt }, extra || {}), "*");

  // ---- small 3D helpers ----
  const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
  const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
  const scl = (a, s) => [a[0] * s, a[1] * s, a[2] * s];
  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const norm = a => Math.hypot(a[0], a[1], a[2]) || 1e-9;
  const unit = a => scl(a, 1 / norm(a));
  const col = (R, k) => [R[0][k], R[1][k], R[2][k]];
  const matvec = (R, v) => [dot(R[0], v), dot(R[1], v), dot(R[2], v)];       // R·v
  const matTvec = (R, v) => [R[0][0]*v[0]+R[1][0]*v[1]+R[2][0]*v[2], R[0][1]*v[0]+R[1][1]*v[1]+R[2][1]*v[2], R[0][2]*v[0]+R[1][2]*v[1]+R[2][2]*v[2]]; // Rᵀ·v
  const det3 = R => R[0][0]*(R[1][1]*R[2][2]-R[1][2]*R[2][1]) - R[0][1]*(R[1][0]*R[2][2]-R[1][2]*R[2][0]) + R[0][2]*(R[1][0]*R[2][1]-R[1][1]*R[2][0]);

  // Jacobi eigen-decomposition of a symmetric 3×3 → {vec: columns are eigenvectors, val}
  function eig3(A) {
    const a = [A[0].slice(), A[1].slice(), A[2].slice()];
    const V = [[1,0,0],[0,1,0],[0,0,1]];
    for (let iter = 0; iter < 24; iter++) {
      // largest off-diagonal
      let p = 0, q = 1, m = Math.abs(a[0][1]);
      if (Math.abs(a[0][2]) > m) { m = Math.abs(a[0][2]); p = 0; q = 2; }
      if (Math.abs(a[1][2]) > m) { m = Math.abs(a[1][2]); p = 1; q = 2; }
      if (m < 1e-12) break;
      const app = a[p][p], aqq = a[q][q], apq = a[p][q];
      const phi = 0.5 * Math.atan2(2 * apq, aqq - app);
      const c = Math.cos(phi), s = Math.sin(phi);
      for (let k = 0; k < 3; k++) {
        const akp = a[k][p], akq = a[k][q];
        a[k][p] = c * akp - s * akq; a[k][q] = s * akp + c * akq;
      }
      for (let k = 0; k < 3; k++) {
        const apk = a[p][k], aqk = a[q][k];
        a[p][k] = c * apk - s * aqk; a[q][k] = s * apk + c * aqk;
      }
      for (let k = 0; k < 3; k++) {
        const vkp = V[k][p], vkq = V[k][q];
        V[k][p] = c * vkp - s * vkq; V[k][q] = s * vkp + c * vkq;
      }
    }
    return { vec: V, val: [a[0][0], a[1][1], a[2][2]] };
  }

  function headFrame(lms, w, h) {
    const P = NOSE.map(i => [lms[i].x * w, lms[i].y * h, lms[i].z * w]);
    const c = [0, 0, 0]; for (const p of P) { c[0]+=p[0]; c[1]+=p[1]; c[2]+=p[2]; }
    c[0]/=P.length; c[1]/=P.length; c[2]/=P.length;
    // covariance
    const C = [[0,0,0],[0,0,0],[0,0,0]];
    for (const p of P) { const d=[p[0]-c[0],p[1]-c[1],p[2]-c[2]];
      for (let i=0;i<3;i++) for (let j=0;j<3;j++) C[i][j]+=d[i]*d[j]; }
    const { vec, val } = eig3(C);
    // sort columns by descending eigenvalue
    const order = [0,1,2].sort((i,j)=>val[j]-val[i]);
    let R = [[0,0,0],[0,0,0],[0,0,0]];
    for (let r=0;r<3;r++) for (let k=0;k<3;k++) R[r][k]=vec[r][order[k]];
    if (det3(R) < 0) for (let r=0;r<3;r++) R[r][2] *= -1;   // right-handed
    // stabilize signs against the first frame (eigenvectors can flip)
    if (!G.Rref) G.Rref = R.map(row=>row.slice());
    else for (let k=0;k<3;k++){ if (dot(col(R,k), col(G.Rref,k)) < 0) for (let r=0;r<3;r++) R[r][k]*=-1; }
    return { center: c, R, pts: P };
  }

  function scaleOf(P) { let t=0,n=0; for (let i=0;i<P.length;i++) for (let j=i+1;j<P.length;j++){ t+=norm(sub(P[i],P[j])); n++; } return n?t/n:1; }

  // combined 3D gaze direction → (yaw_deg, pitch_deg), following convert_gaze_to_screen_coordinates
  function anglesOf(dir) {
    const d = unit(dir), ref = [0,0,-1];
    const xz = [d[0],0,d[2]]; if (Math.hypot(xz[0],xz[2]) < 1e-6) return null;
    const yz = [0,d[1],d[2]]; if (Math.hypot(yz[1],yz[2]) < 1e-6) return null;
    const xu = unit(xz), yu = unit(yz);
    let yaw = Math.acos(Math.max(-1,Math.min(1, dot(ref,xu)))); if (d[0] < 0) yaw = -yaw;
    let pitch = Math.acos(Math.max(-1,Math.min(1, dot(ref,yu)))); if (d[1] > 0) pitch = -pitch;
    return { yaw: -(yaw*180/Math.PI), pitch: pitch*180/Math.PI };  // their yaw sign-flip
  }

  const feat = (yaw, pitch) => { const u=yaw/YN, v=pitch/PN; return [1,u,v,u*u,v*v,u*v]; };

  function solve(A, b) {
    const n=b.length, M=A.map((r,i)=>r.slice().concat(b[i]));
    for (let c=0;c<n;c++){ let p=c; for(let r=c+1;r<n;r++) if(Math.abs(M[r][c])>Math.abs(M[p][c]))p=r;
      if(Math.abs(M[p][c])<1e-12) continue; [M[c],M[p]]=[M[p],M[c]];
      for(let r=0;r<n;r++) if(r!==c){ const f=M[r][c]/M[c][c]; for(let k=c;k<=n;k++) M[r][k]-=f*M[c][k]; } }
    return M.map((r,i)=>Math.abs(M[i][i])<1e-12?0:r[n]/M[i][i]);
  }
  function retrain() {
    const n=G.X.length; if(n<MINPTS){ G.Wx=G.Wy=null; return; }
    const A=Array.from({length:DIM},()=>new Array(DIM).fill(0));
    const bx=new Array(DIM).fill(0), by=new Array(DIM).fill(0);
    for(let s=0;s<n;s++){ const f=G.X[s];
      for(let i=0;i<DIM;i++){ bx[i]+=f[i]*G.Yx[s]; by[i]+=f[i]*G.Yy[s]; for(let j=0;j<DIM;j++) A[i][j]+=f[i]*f[j]; } }
    for(let i=0;i<DIM;i++) A[i][i]+=LAMBDA;
    G.Wx=solve(A,bx); G.Wy=solve(A,by);
  }
  function predictXY(yaw, pitch) {
    if(!G.Wx) return null; const f=feat(yaw,pitch); let x=0,y=0;
    for(let i=0;i<DIM;i++){ x+=f[i]*G.Wx[i]; y+=f[i]*G.Wy[i]; }
    return { x: Math.max(0,Math.min(innerWidth, x*innerWidth)), y: Math.max(0,Math.min(innerHeight, y*innerHeight)) };
  }

  // lock each eyeball center as a head-local offset a bit behind the iris (looking-forward pose)
  function lockSpheres(cur) {
    const camLocal = matTvec(cur.R, [0,0,1]);
    G.offL = add(matTvec(cur.R, sub(cur.irisL, cur.center)), scl(camLocal, BASE_RADIUS));
    G.offR = add(matTvec(cur.R, sub(cur.irisR, cur.center)), scl(camLocal, BASE_RADIUS));
    G.calScale = cur.scale; G.locked = true;
  }
  function combinedDir(cur) {
    const sr = G.calScale ? cur.scale / G.calScale : 1;
    const sphL = add(cur.center, matvec(cur.R, scl(G.offL, sr)));
    const sphR = add(cur.center, matvec(cur.R, scl(G.offR, sr)));
    const dl = unit(sub(cur.irisL, sphL)), dr = unit(sub(cur.irisR, sphR));
    return unit(add(dl, dr));
  }

  async function init() {
    if (G.landmarker) return;
    G.vision = await import(G.base + "vision_bundle.mjs");
    const fs = await G.vision.FilesetResolver.forVisionTasks(G.base + "wasm");
    G.landmarker = await G.vision.FaceLandmarker.createFromOptions(fs, {
      baseOptions: { modelAssetPath: G.base + "face_landmarker.task" },
      runningMode: "VIDEO", numFaces: 1, outputFacialTransformationMatrixes: false, outputFaceBlendshapes: false
    });
  }

  const beat = ok => { const now=performance.now(); if (now-G.hb>200){ G.hb=now; post("face",{ok}); } };

  function loop() {
    if (!G.running) return;
    G.raf = requestAnimationFrame(loop);
    const v = document.getElementById("bux-mp-video");
    if (!v || v.readyState < 2) return;
    const w = v.videoWidth || 640, h = v.videoHeight || 480;
    const ts = performance.now(); if (ts <= G.lastTs) return; G.lastTs = ts;
    let res; try { res = G.landmarker.detectForVideo(v, ts); } catch (e) { return; }
    if (!res || !res.faceLandmarks || !res.faceLandmarks.length) { beat(false); return; }
    const lms = res.faceLandmarks[0];
    // blink rejection
    const openL = Math.abs(lms[LID_LD].y - lms[LID_LU].y), openR = Math.abs(lms[LID_RD].y - lms[LID_RU].y);
    const eyeW = Math.abs(lms[263].x - lms[33].x) || 1e-6;
    const blink = ((openL + openR) / 2) / eyeW < OPEN_MIN;

    const hf = headFrame(lms, w, h);
    const cur = { center: hf.center, R: hf.R, scale: scaleOf(hf.pts),
      irisL: [lms[IRIS_L].x*w, lms[IRIS_L].y*h, lms[IRIS_L].z*w],
      irisR: [lms[IRIS_R].x*w, lms[IRIS_R].y*h, lms[IRIS_R].z*w] };
    if (!blink) G.cur = cur;

    if (!G.locked || blink) { beat(true); return; }

    const raw = combinedDir(cur);
    G.dirEMA = G.dirEMA ? unit(add(scl(raw, DIR_EMA), scl(G.dirEMA, 1 - DIR_EMA))) : raw;
    const ang = anglesOf(G.dirEMA); if (!ang) return;

    // collect calibration samples for a short window after each dot click
    if (G.collect && ts - G.lastCollect >= COLLECT_GAP) {
      G.lastCollect = ts;
      G.X.push(feat(ang.yaw, ang.pitch)); G.Yx.push(G.collect.px/innerWidth); G.Yy.push(G.collect.py/innerHeight);
      if (G.X.length > MAXN) { G.X.shift(); G.Yx.shift(); G.Yy.shift(); }
      retrain();
      if (ts >= G.collect.end) G.collect = null;
    } else if (G.collect && ts >= G.collect.end) G.collect = null;

    const p = predictXY(ang.yaw, ang.pitch);
    if (p) {
      if (G.sx == null) { G.sx = p.x; G.sy = p.y; }
      else { G.sx = SMOOTH*p.x + (1-SMOOTH)*G.sx; G.sy = SMOOTH*p.y + (1-SMOOTH)*G.sy; }
      post("gaze", { x: G.sx, y: G.sy });
    } else beat(true);
  }

  function calibrate(px, py) {
    if (!G.cur) return;
    if (!G.locked) lockSpheres(G.cur);          // lock eyeball spheres on the first calibration click
    G.collect = { px, py, end: performance.now() + COLLECT_MS };
  }

  function stop() {
    G.running = false; if (G.raf) cancelAnimationFrame(G.raf); G.raf = 0;
    try { G.landmarker && G.landmarker.close(); } catch (e) {}
    G.landmarker = null; G.Rref = null; G.locked = false; G.offL = G.offR = null; G.calScale = 0;
    G.dirEMA = null; G.X = []; G.Yx = []; G.Yy = []; G.Wx = G.Wy = null;
    G.sx = G.sy = null; G.lastTs = 0; G.collect = null; G.cur = null;
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
