# Boring UX — `gaze-from-video` Post-Processing Pipeline
## Definitive Design Document (implementation-ready)

Status: approved for implementation. Date: 2026-09-09.
Targets: macOS / Apple Silicon, Python 3.12, `~/Desktop/gaze-ai/.venv` (torch 2.14 + MPS, mediapipe 0.10.14, l2cs 0.0.1 @ edavalosanaya/L2CS-Net commit `4a0f978`, weights `~/Desktop/gaze-ai/models/L2CSNet_gaze360.pkl`, `~/Desktop/gaze-ai/models/face_landmarker.task`, ffmpeg/ffprobe 8.x, whisper-cli).

Confidence tags: **[HIGH]** verified in source and/or empirically by two independent verifiers; **[MED]** derived/consistent but not yet confirmed on a controlled recording; **[LOW]** assumption or prior.

Notation used throughout: `h` = horizontal gaze angle (positive = participant's own right), `v` = vertical gaze angle (positive = up), both relative to the eye→camera ray; `d` = eye–camera distance (cm); `E_C` = eye midpoint in camera coordinates; `W, H` = physical display width/height (cm); `y_off` = camera height above the top edge of the active display (cm). L2CS returns radians; convert to degrees once at ingest.

---

## 1. Executive answer

**What a MacBook webcam recording can support (no calibration dots):**

| Signal | Reliability | Basis |
|---|---|---|
| Face present / attention on-screen vs away, head turned > 30°, look-down to keyboard (> 8° below the bottom edge) | High (> 90 % per second) | Large angles, far above the ~10° error |
| Horizontal column L / C / R | 65–80 % per second | Column ≈ 10–11° wide; L2CS median over 1 s; click bias correction |
| Upper vs lower half | 60–70 % | Vertical span of the whole screen is only ~20° |
| 3×3 cell | 35–45 % per frame raw; 50–60 % per second after bias correction (chance 11 %) | Monte-Carlo with 8–10° MAE + 3–4° bias; 1 cell ≈ 1σ |
| Approximate screen point | σ ≈ 6–8 cm only when click calibration passes (≥ 6 inliers) | WebGazer/PACE-style implicit calibration |
| Blink, eye openness, head pose (yaw/pitch/roll), distance (±10–20 %) | High | MediaPipe FaceLandmarker |
| Coarse expression cues (brow lowering, squint, lip press, smile) as per-session z-scores | Medium, cue-level only | ARKit blendshapes; not validated emotion recognition |
| Time-locking of gaze to mouse, clicks, keys, scroll, speech | High (< 100 ms) once container PTS are used | Verified on real sessions |

**What it cannot support:** fixations on individual UI elements smaller than ~8 cm, line-by-line reading paths, pupil-based load, reliable affect classification, any gaze claim during blinks, |head yaw| > 30°, or glasses glare.

**Recommended output granularity:** a 10 Hz working grid (detectors run here) and a 1 Hz report grid. Report language: firm at column + half level, "probably" at 3×3 level, approximate point only with a stated σ and a passed click-consistency check. Every gaze statement in reports carries the session's click-consistency lift and the second's quality grade.

**Expected accuracy (honest):** uncalibrated Gaze360-trained L2CS on a laptop webcam ≈ 9–12° mean angular error (7–10 cm at 50–55 cm) plus a per-person bias of several degrees [HIGH: L2CS paper 10.4° Gaze360 front-180; Gaze360→MPIIFaceGaze cross-dataset 12.1°]; with per-session bias removal ≈ 5–8° [MED]. Compute: ≈ 4–5 min wall clock per 10 min of video with the recipe in §3.

---

## 2. Coordinate frames and THE MIRRORING RESOLUTION

### 2.1 Frames

| Frame | Axes | Origin | Where it appears |
|---|---|---|---|
| **IMAGE (I)** | u right, v down (pixels); MediaPipe x = u/W_img, y = v/H_img | top-left | face.webm frames, MediaPipe landmarks, face boxes |
| **CAMERA (C)** (OpenCV) | X right (= image right), Y down, Z forward out of the lens toward the participant | camera optical centre | pinhole: u = f·X/Z + c_x, v = f·Y/Z + c_y; screen plane is Z = 0 (lid tilt rotates camera and screen together) |
| **MEDIAPIPE METRIC (M)** | X right, Y **up**, Z **toward** the camera (face at Z < 0), cm | camera | `facial_transformation_matrixes` (rotation R, translation t). M→C: X_C = X_M, Y_C = −Y_M, Z_C = −Z_M |
| **GAZE360 EYE (G)** | x = camera's **left**, y = up, z = away from camera; −z axis points from the eye to the lens | eye midpoint | L2CS training labels: `h = atan2(g_x, −g_z)`, `v = asin(g_y)` |
| **USER / SCREEN (S)** | x = participant's right = screen +x = mouse +x; y = down = screen +y = mouse +y; z toward participant | camera (angles) / top-left of display (px, cm) | all outputs; x_S = −X_C, y_S = +Y_C |

### 2.2 The trap in five lines
1. The camera faces the participant; two people facing each other have opposite rights. Camera +X (image-right) is the participant's **left**. [HIGH: pinhole geometry]
2. `face.webm` is the **raw, unmirrored** track: Chromium's macOS backend (`video_capture_device_avfoundation.mm`) never sets `AVCaptureConnection.isVideoMirrored`, W3C closed the mirrored-constraint request (mediacapture-main #452), and MediaRecorder records the track, not the CSS-mirrored `<video>`. [HIGH]
3. Therefore in `face.webm` a participant looking/turning toward **their right** (screen +x) moves toward **image-left** (smaller u). Vertical is not flipped. Printed text held to the camera reads **normally** (a photograph), not reversed.
4. Image-space quantities (iris offset, face position, MediaPipe t_x) need one horizontal negation to become screen-x. L2CS angles do **not**, because the Gaze360 label convention already has +x = camera-left = participant-right.
5. Neither MediaPipe nor L2CS can detect mirroring: landmark index 33 lands on the image-left eye in both raw and flipped frames (verified), so `x33 < x263` is **not** a mirror test. Only external references (mouse/clicks, readable text, a raised right hand) detect a mirrored track.

### 2.3 Definitive sign table (raw track)

| Quantity | Native frame | + means | Map to S (x = user right, y = down) | Conf. |
|---|---|---|---|---|
| L2CS `results.pitch` → **h** (HORIZONTAL) | G | gaze toward image-left = participant's **right** | `h_S = +h` (**no flip**) | HIGH |
| L2CS `results.yaw` → **v** (VERTICAL) | G | **up** (above the eye→lens ray) | `down_angle = −v` | HIGH (channel), MED (zero = lens ray) |
| MediaPipe landmark x (u) | I | image-right = user's left | `x_S ∝ −(u − u_ref)` | HIGH |
| MediaPipe landmark y (v) | I | down | `y_S ∝ +(v − v_ref)` | HIGH |
| Iris offset within eye | I | iris toward image-right = looking toward user's **left** | `eye_in_head_right = −offset` | HIGH |
| MediaPipe t = M[:3,3] | M | t_x: image-right; t_y: up; t_z: toward camera (face negative) | `x_S = −t_x`, `y_S(down) = −t_y`, `d ≈ −t_z` | HIGH |
| Head forward n = R[:,2] (R = M[:3,:3], column-normalised) | M | canonical nose = +Z toward camera | `head_yaw_right = atan2(−n_x, n_z)`; `head_pitch_up = asin(n_y)`; roll from R[:,1] | HIGH (verified through MediaPipe's own FaceGeometry calculator: ±25° yaw / ±15° pitch recovered exactly) |
| Blendshapes `eyeLook*` (idx 11–18) | subject | Left/Right = subject side **iff unmirrored**; Out = temporal, In = nasal | `H = 0.5·((OutL−InL)+(InR−OutR))` is user-**left** positive → `screen_right = −H`; `V = 0.5·(UpL+UpR) − 0.5·(DownL+DownR)` up-positive | HIGH (H), MED (V weak) |
| `eyeBlinkLeft`=9 / `eyeBlinkRight`=10 | subject | Left = image-right eye (263 set) on raw track | as is | HIGH |
| mouse.csv / events.csv x, y | viewport CSS px | right, down | identity | HIGH |

Generic Euler decompositions (`cv2.RQDecomp3x3`, scipy `'xyz'`) return user-LEFT-positive yaw and pitch-DOWN-positive; always derive from the forward vector. [HIGH]

### 2.4 The L2CS naming swap (bind by head, not by name)
- `model.py:70` returns `(fc_yaw_gaze, fc_pitch_gaze)`; `pipeline.py:122` unpacks `gaze_pitch, gaze_yaw = self.model(img)`; GazeHub's Gaze360 label is `[yaw, pitch] = [atan2(g_x,−g_z), asin(g_y)]` and `datasets.py` calls column 0 "pitch"; `train.py` has no horizontal-flip augmentation. Net: **`results.pitch` = horizontal, `results.yaw` = vertical**; the head *names* (`fc_yaw_gaze` = horizontal) are the only correct labels. [HIGH: forward hooks on the heads reproduce `results.*`; horizontal flip of 73 real frames negates `results.pitch` (r = −0.95) and preserves `results.yaw` (r = +0.93)]
- Sign evidence for `+h` = user's right: label chain; `vis.py` draws `dx = −L·sin(h)·cos(v)` (arrow toward image-left for +h); on a real session `corr(h, iris_x_offset_image) = −0.87` (iris toward image-left when h > 0) and `corr(h, extension gaze_x) = +0.79`. [HIGH]
- Decode: `deg = Σ softmax(logits)·idx · 4 − 180` over 90 bins (Gaze360 weights only; MPII weights use 28 bins × 3 − 42), then radians in the Pipeline. We decode to degrees ourselves.
- Open upstream PR #44 swaps the return order; if ever merged the *names* would invert. We are immune because we read the heads via forward hooks by name (§3.2) and run the flip self-test at startup.
- `utils.gazeto3d([h, v]) = (−cos v sin h, −sin v, −cos v cos h)` **is** the correct gaze vector in OpenCV camera coordinates (the earlier "do not use it" note was wrong); the Gaze360-frame vector is `(cos v sin h, sin v, −cos v cos h)`.

### 2.5 Confirmed vs disputed
**CONFIRMED (both verifiers, source + empirical):** raw track unmirrored; L2CS name swap; `+h` = participant's right, `+v` = up; MediaPipe landmarks in raw image coordinates, 478 points, index 33/133/468 = image-left eye; metric frame axes (translation moves +7 cm for +120 px image-right, +y for image-up, t_z ≈ −25…−45 cm); head yaw/pitch formulas; blendshape indices/semantics (wink photos: eyeBlinkLeft tracks the 263 eye; +8 px iris shift image-right raises eyeLookOutLeft/eyeLookInRight); MediaRecorder VFR/pts facts; audio identity across the three files.

**DISPUTED → RESOLVED:**
- "screen-right = −results.pitch" (timeline-fusion researcher): **wrong**, internally inconsistent with its own premise; refuted by the confirmed sign chain and the iris/gaze correlations.
- "`x33 < x263` in every frame proves the video is unmirrored": **wrong**; holds on mirrored frames too. Removed from the self-test.
- "utils.gazeto3d is not the inverse; do not use": **wrong** (it is the OpenCV-frame vector).
- "`eyeBlink* > 0.5` is a reliable blink detector": **unreliable**; full closure scored only 0.49–0.75. Use EAR (adaptive) OR blendshape.
- "Eye is 10–15 cm below the camera": typical desk posture has the eye at or above camera height; irrelevant once E_C is measured per frame (§4.3) — never assume it.
- "L2CS zero = camera optical axis": zero is the **eye→lens ray** (crops are not perspective-normalised; a face anywhere in the frame looking into the lens looks the same to the network). [MED — absorbed by the bias term either way]
- One verifier wrote that printed text "should read backwards" on the raw track: **wrong**; a photograph of text reads normally; a mirror reverses it.

**UNCERTAIN [MED], resolved by the first real session (§9):** exact magnitude compression of L2CS on eye-only movements (expect edges to read 10–17° instead of 13–21°); how close the network's zero is to the lens ray for off-axis faces; f_px of the specific camera.

### 2.6 Built-in guards against a wrong sign (all mandatory, results in `quality.json.signs`)
1. **Head binding**: forward hooks on `model.fc_yaw_gaze` (→ h) and `model.fc_pitch_gaze` (→ v). Immune to return-order changes.
2. **Startup flip test** (model-internal): on the first 30 detected faces run crop and `cv2.flip(crop, 1)`; per channel compute `r = corr(a_flip, a_orig)`. Horizontal channel must have r < −0.5, vertical r > +0.5. Mismatch → abort with "L2CS channel assignment changed". All frontal (|r| < 0.5 both) → `inconclusive`, keep head binding.
3. **Cross-path consistency** (catches code bugs, NOT track mirroring — all three flip together with the image): over the session `corr(h, −iris_offset_image) > 0.3` and `corr(h, −H_blend) > 0.3`, and `corr(v, V_blend) > 0.3`. Fail → abort.
4. **External orientation check** (detects a mirrored track): Spearman ρ between pre-click `h` (median over [t_c−400, t_c−50] ms) and click x. N ≥ 10 and ρ < −0.3 → `orientation = INVERTED`; N ≥ 10 and ρ > +0.3 → `OK`; else `UNTESTED`. Secondary: ρ(gaze x_S, mouse x) over all seconds with mouse motion (expected > 0; weak). INVERTED ⇒ all lateral gaze claims suppressed (grade F for regions), loud warning in report. **Never auto-flip.** A human may re-run with `--assume-mirrored` after inspecting frames (§7.1).
5. **Explicit prompt check** when `events.csv` contains `selftest` markers (LEFT/RIGHT/TOP/BOTTOM): PASS/FAIL per axis (§7.1).
6. **One-time per-machine model-free check** (documented procedure, not code): printed word reads normally in decoded `face.webm`; a raised RIGHT hand appears image-LEFT; covering the RIGHT eye makes the image-LEFT eye vanish and `eyeBlinkRight` (idx 10) peak. Re-run whenever the camera device/label changes (virtual cameras such as OBS/Camo/Snap can pre-mirror upstream of Chrome).

---

## 3. Models and roles

### 3.1 MediaPipe FaceLandmarker (per processed frame) — detection, blink, head pose, eye position, distance, expressions
```python
opts = FaceLandmarkerOptions(
    base_options=BaseOptions(model_asset_path=FACE_TASK),
    running_mode=VisionRunningMode.VIDEO, num_faces=2,
    output_face_blendshapes=True, output_facial_transformation_matrixes=True,
    min_face_detection_confidence=0.5, min_face_presence_confidence=0.5, min_tracking_confidence=0.5)
res = lm.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), int(t_ms))  # strictly increasing int ms
```
- Input RGB (PyAV `to_ndarray(format='rgb24')`). Timestamps must be strictly increasing integers in ms; skip frames whose pts is ≤ the previous (raises otherwise). [HIGH]
- Face selection: largest landmark bbox; second face → flag `multi_face`. Identity switch (IoU < 0.3 vs previous) → flag.
- Derived per frame (indices are anatomical on the raw track):
  - Landmark bbox (px) → square crop for L2CS (§3.3).
  - Eye midpoint `(u_e, v_e)` = mean of iris centres 468 and 473. IPD px = |p468 − p473|. Iris diameter px = mean over both eyes of max pairwise distance among ring points (469–472, 474–477).
  - EAR_R = (|p159−p145| + |p158−p153|) / (2|p33−p133|); EAR_L = (|p386−p374| + |p385−p380|) / (2|p362−p263|). **Blink** = EAR_mean < max(0.12, 0.5·median_session(EAR_mean)) OR max(bs[9], bs[10]) > 0.5. Blink episodes 60–400 ms; > 400 ms = `eyes_closed`.
  - Iris ratios `h_R = (x468−x33)/(x133−x33)`, `h_L = (x473−x362)/(x263−x362)`; centre each at its session median; `iris_right = −((h_R−med_R)+(h_L−med_L))/2` (positive = user's right). Vertical iris offset stored but not used for decisions (eyelid-confounded).
  - Head pose from the matrix (§2.3). Head roll for quality only.
  - t_x, t_y, t_z (cm; assumes 63° vertical FOV and canonical head size — ±15 %).
  - 52 blendshapes stored raw; aggregates in §5.4.
  - Photometric flags: glare = fraction of eye-ROI pixels with V > 240 (> 3 % flag); backlight = border-ring luminance / face luminance (> 2.0 flag); face mean < 50/255 "too dark"; left/right half intensity asymmetry > 0.35 flag.
- Cost: 5–18 ms/frame CPU.

### 3.2 L2CS-Net (gaze direction; the only true gaze estimator)
```python
import sys, types, torch, numpy as np, torch.nn.functional as F
sys.modules.setdefault('scipy.io', types.ModuleType('scipy.io'))   # l2cs.utils imports scipy.io; scipy is broken under numpy 2.5 in this venv
from l2cs import getArch
model = getArch('ResNet50', 90); model.load_state_dict(torch.load(W, map_location='cpu')); model.eval()
heads = {}
model.fc_yaw_gaze.register_forward_hook(lambda m,i,o: heads.__setitem__('h', o))    # HORIZONTAL (user-right +)
model.fc_pitch_gaze.register_forward_hook(lambda m,i,o: heads.__setitem__('v', o))  # VERTICAL (up +)
model.to(dev)  # dev = torch.device('mps'); fp16: model.half()
idx = torch.arange(90, device=dev, dtype=torch.float32)
def decode(logits):
    p = F.softmax(logits.float(), dim=1)
    deg = (p * idx).sum(1) * 4 - 180
    ent = -(p * torch.log(p.clamp_min(1e-9))).sum(1)   # nats; uniform = ln 90 = 4.50
    return deg, 1 - ent / np.log(90)                     # sharpness in [0,1]
with torch.no_grad():
    model(batch); h_deg, sharp_h = decode(heads['h']); v_deg, sharp_v = decode(heads['v'])
```
- Do **not** use `l2cs.Pipeline`: it builds RetinaFace with `torch.device('cuda', …)` on any non-CPU device (fails on MPS), RetinaFace is CPU-only at 134–280 ms/frame (the actual bottleneck), `include_detector=False` crashes (`softmax`/`idx_tensor` only defined in the detector branch), `step()` raises `ValueError` on frames without a face, and its bbox is clamped only at the top/left edge.
- Pin `l2cs @ 4a0f978`; log the commit hash into `quality.json`.

### 3.3 Exact preprocessing (replicates training: 224 crop → Resize(448) → ImageNet normalise)
1. Square crop: side = 1.3 × max(w, h) of the 468-landmark bbox, centred on the bbox centre; pad with edge replication where it exceeds the frame (flag `face_partial` if > 10 % padded).
2. `cv2.resize(crop, (224, 224), interpolation=cv2.INTER_AREA)` on RGB (if frames were decoded BGR, convert; wrong channel order degrades accuracy, not signs).
3. `x = torch.from_numpy(crop).permute(2,0,1).float()/255`; `x = F.interpolate(x[None], size=448, mode='bilinear', align_corners=False)`; normalise mean (0.485, 0.456, 0.406), std (0.229, 0.224, 0.225).
4. Batch 16 (fixed shape; pad the last batch), fp16 on MPS after a one-time check that |Δ| < 0.2° vs fp32 on 300 real crops (else fp32).

### 3.4 Sampling, device, budget
- Decode every frame (PyAV, single producer thread). MediaPipe on frames nearest to a 15 Hz grid (blink resolution); L2CS on frames nearest to the 10 Hz grid (default `--fps 10`), choosing the nearest non-blink frame within ±33 ms when the grid frame is a blink.
- Measured on this machine: L2CS 448-px ResNet50 on MPS ≈ 24 ms/img fp32, ≈ 11.5 ms/img fp16 at batch 8–16; MediaPipe 5–18 ms/frame; VP9 640×480 decode 20–40 s per 10 min. Budget for a 10-min session: decode 0.5 min + MediaPipe 9 000 × 12 ms ≈ 1.8 min + L2CS 6 000 × 12 ms ≈ 1.2 min ≈ **3.5–5 min**. `PYTORCH_ENABLE_MPS_FALLBACK=1`; keep the model resident; `torch.mps.synchronize()` only when timing.
- Record 1280×720 if the participant machine allows (iris ≈ 24 px vs 12 px at 640×480); 640×480 is acceptable for L2CS and head pose, marginal for iris-based checks.

---

## 4. Screen mapping without calibration

### 4.1 Inputs
- **Display profile** (cm) from `session.json.display` if set by the researcher; else lookup by `screen.width × screen.height` CSS px at DPR 2 (default scaling only; flag `display_assumed`):

| CSS px | Model | W × H cm | y_off cm |
|---|---|---|---|
| 1470×956 | 13.6" Air (notch) | 29.3 × 18.3 | −0.45 |
| 1512×982 | 14.2" Pro (notch) | 30.6 × 19.1 | −0.45 |
| 1710×1112 | 15.3" Air (notch) | 32.9 × 20.6 | −0.45 |
| 1728×1117 | 16.2" Pro (notch) | 34.9 × 21.8 | −0.45 |
| 1440×900 / 1280×800 | 13.3" (bezel) | 28.6 × 17.9 | +0.9 |
| 1792×1120 | 16" 2019 (bezel) | 34.4 × 21.5 | +0.9 |

  y_off effect is < 1° either way. `ppcm_x = screen.width/W`, `ppcm_y = screen.height/H`.
- **Focal length f_px** at the recorded resolution, priority: camera profile file keyed by `track.label` + `W_img×H_img` (measured once per device: ISO ID-1 card, 85.6 mm wide, at tape-measured d₀ → `f = w_px·d₀/8.56 cm`) → else fallback `f = 0.85·W_img` (HFOV ≈ 61°, ±15 %, flag `fpx_assumed`). Principal point = image centre. **Center Stage must be off** (check: face bbox centre nearly constant while background moves ⇒ flag `center_stage_suspected`, geometry invalid); Continuity Camera (label not containing "FaceTime"/"MacBook") ⇒ flag `foreign_camera`, no screen mapping.
- Use the **same f** for E_C and for d: a wrong f then cancels in the lateral eye position and only scales the offset from the camera point (10 % f error ≈ 1.5 cm at the screen edge).

### 4.2 Distance d (cm), per frame, median over 0.5 s, clamped to [30, 100]
- `d_ipd = f · 6.3 / (ipd_px / cos(head_yaw))` (63 mm mean IPD, ±5–6 % population σ).
- `d_iris = f · 1.17 / iris_px` (11.7 ± 0.5 mm; weight 0.5 at 640×480).
- `d_mp = −t_z · tan(31.5°) / tan(vfov_true/2)`, `vfov_true = 2·atan(H_img / (2f))`.
- `d = weighted median`; if `d_ipd` and `d_iris` disagree by > 25 % flag `distance_inconsistent`.

### 4.3 Eye position
`E_C = ((u_e − c_x)·d/f, (v_e − c_y)·d/f, d)`. Never assume a fixed eye height: the vertical row boundaries depend on it directly.

### 4.4 Angles → screen point (the core mapping) [HIGH geometry, MED that L2CS zero is exactly the lens ray]
```python
def eye_frame_axes(E_C):
    z_e = E_C / np.linalg.norm(E_C)              # camera→eye = Gaze360 +z ("away from camera")
    up  = np.array([0., -1., 0.])                # camera Y is down
    y_e = up - (up @ z_e) * z_e; y_e /= np.linalg.norm(y_e)
    x_e = np.cross(y_e, z_e)                     # on-axis eye: (-1,0,0) = image-left = Gaze360 +x
    return x_e, y_e, z_e

def gaze_to_screen(h, v, E_C, W, y_off):         # h, v in radians (bias-corrected)
    g_G = np.array([np.cos(v)*np.sin(h), np.sin(v), -np.cos(v)*np.cos(h)])
    x_e, y_e, z_e = eye_frame_axes(E_C)
    g_C = g_G[0]*x_e + g_G[1]*y_e + g_G[2]*z_e
    if g_C[2] > -1e-3: return None               # not heading toward the screen plane
    P = E_C + (-E_C[2] / g_C[2]) * g_C           # intersection with Z_C = 0
    return W/2 - P[0], P[1] - y_off              # (x_cm from left edge, y_cm from top edge), user frame

def screen_to_angles(x_cm, y_cm, E_C, W, y_off): # inverse, used for clicks and boundaries
    Q_C = np.array([W/2 - x_cm, y_cm + y_off, 0.])
    dv = Q_C - E_C; dv /= np.linalg.norm(dv)
    x_e, y_e, z_e = eye_frame_axes(E_C)
    g = np.array([dv @ x_e, dv @ y_e, dv @ z_e])
    return np.arctan2(g[0], -g[2]), np.arcsin(np.clip(g[1], -1, 1))
```
Sanity (13.3"/14" 30×19.5 cm, d = 55, eye 10 cm above the camera axis): h = v = 0 → hits the camera (x = W/2, y = −y_off); screen centre → v = −10.3°, bottom edge ≈ −19.5°, right edge → h ≈ +15.3°. Vertical thirds at ≈ −6.7°/−13.3°, horizontal thirds at ±5.2°; all scale with atan(·/d). Round-trip test `screen_to_angles(gaze_to_screen(h,v)) == (h,v)` is a unit test.

Then: `x_css_screen = x_cm·ppcm_x`, `y_css_screen = y_cm·ppcm_y`; viewport: `x_vp = x_css_screen − (screenX + (outerWidth − innerWidth)/2)`, `y_vp = y_css_screen − (screenY + (outerHeight − innerHeight))`. If window geometry is missing assume a maximised window (screenX = 0, screenY = menu-bar height 24/37 px, toolbar = 53 px measured) and flag `window_geometry_assumed`.

### 4.5 Bias model
Corrected angles: `h' = (h − b_h)/a_h`, `v' = (v − b_v)/a_v`, defaults `b = 0`, `a = 1`. `b` absorbs the per-person L2CS offset, the "zero ≠ exactly lens ray" residual, and f/d errors — all near-constant while posture is constant.

**Without clicks (self-centring prior):** compute raw screen points over valid samples (§5.6 gaze_conf ≥ 0.3); `b₀ = screen_to_angles(median P) − screen_to_angles(screen centre)` at the median E_C; clip each component to ±5°; use `b = b₀`. Known limitation: web content biases the median toward upper-left by a few degrees; that is why it is clipped and why clicks override it. Never assume "median pitch = middle row" without the geometry — every on-screen point is below the lens.

### 4.6 Click-based implicit bias correction (WebGazer/PACE/Huang-2012 logic) [HIGH method]
1. Candidate click k at t_k with viewport (x_k, y_k) → cm via §4.4 inverse → expected `(h*_k, v*_k) = screen_to_angles(…, E_C(t_k))`.
2. Measured `(h_k, v_k)` = median of raw angles over frames in **[t_k − 400 ms, t_k − 50 ms]** with gaze_conf ≥ 0.3; require ≥ 3 samples and dispersion (MAD) < 3° (a fixation), else skip.
3. Exclude: second click of a double-click (< 400 ms), drag ends, clicks < 1 s after a keydown, clicks outside the viewport, clicks during blink/no-face.
4. Residual `r_k = measured − expected`. Posture segments: split when d changes > 15 %, the eye moves > 5 cm, or |Δhead_yaw| > 15° (sustained > 2 s). Per segment: `b = component-wise median(r)` over the segment's accepted clicks (offline ⇒ non-causal, use all clicks in the segment; segments with < 3 clicks borrow the global median).
5. Gates: reject `|r_k − b| > 10°`; cap `|b| ≤ 12°` (larger ⇒ flag `bias_out_of_range`, do not apply); apply only if `n_accepted ≥ 3` and `MAD(r) ≤ 6°`.
6. Gain: only if `n_accepted ≥ 8` spanning ≥ 15° horizontally (≥ 10° vertically): fit `measured = a·expected + b` with a ridge prior toward a = 1, clamp a ∈ [0.7, 1.3]; else bias-only.
7. Optional mouse-rest samples (off by default): after a cursor move ≥ 40 px, a rest of 300–1500 ms with gaze MAD < 2° adds a residual at weight 0.3. Never use typing, Enter, scroll, or page loads as labels.
8. Orientation self-check (§2.6 item 4) runs on these pairs; a negative correlation is a flag, never an automatic flip.
9. Report: `n_candidates, n_accepted, b, a, MAD, segments`; click-consistency (§7.2) is computed **leave-one-out** when b is applied.

### 4.7 Grid, hysteresis, states (10 Hz state machine)
- Grid = viewport thirds (identical function for gaze and mouse; labels rows T/M/B × cols L/C/R → `TL … BR`, matching the extension's `gaze.csv`). Off-screen decisions use the physical display, not the viewport.
- Hysteresis margin `m_px = d·tan(1.5°)·ppcm` (≈ 70 CSS px at 55 cm on a 14"). Switch from cell c to c' only if the sample lies inside c' by more than m_px from the shared boundary, or if 2 consecutive samples (200 ms) fall in c'.
- States (sustained ≥ 300 ms unless noted): `on_screen`; `off_left/off_right` (beyond the edge by > 3°); `off_up` (above the top edge by > 3°); `down_keyboard` (below the bottom edge by > 8°, or head_pitch below the session median by > 20°; ≥ 500 ms); `away` (|head_yaw| > 35° or |head_pitch| > 30°); `no_face` (face fraction < 0.5 in the bin); `eyes_closed` (closure > 400 ms). Single blinks (< 400 ms) are masked, not a state.
- Per second: majority cell with `p_cell`; require ≥ 40 % valid samples. If `p_cell < 0.5`, report `col`/`row` separately when their own majorities ≥ 0.6, else `cell = uncertain`.

---

## 5. Time alignment and fused record schema

### 5.1 Clock and per-frame timestamps [HIGH, measured on three real sessions]
- **t = 0 is the first packet of the MediaRecorder recording** (Opus audio starts at pts 0.000). `gaze.csv` t_ms shares this origin within < 100 ms. `session.json.startedAt/durationSec` are on a different clock (one session was 12 s off) — never use them to place or scale anything.
- The constant-fps assumption fails twice: the container says 30000/1001 but the camera runs 29.995 fps (−60 ms after 72 s, ~0.7 s after 14 min) and dropped frames made N/30 18 s wrong at the end of an 833 s recording.
- Ingest:
  1. Validate: file starts with EBML magic `1A 45 DF A3`; a video stream exists (one session's `face.webm` was byte-identical to `audio.webm`). Missing header → transplant the 189-byte header from a healthy file with the same codec config, start at the first Cluster (`1F 43 B6 75`), then **sort by pts and drop duplicates** (recovered files had 234 backward jumps, 77 duplicates); gaps > 300 ms = missing.
  2. `ffmpeg -v error -i face.webm -c copy face_fixed.webm` (restores duration/cues, pts unchanged; raw files have no duration, `CAP_PROP_FRAME_COUNT` returns garbage, seeking fails).
  3. `ffprobe -v error -select_streams v:0 -show_entries packet=pts_time -of csv=p=0 face_fixed.webm > frames_pts.csv`; decode with PyAV: `t_ms = round(float(frame.pts * stream.time_base) * 1000)`. Assert decoded count == packet count and monotonic pts. (OpenCV alternative: `CAP_PROP_POS_MSEC + stream start_time`; measured constant −14 ms otherwise.) Do **not** rebase to the first video pts (typically 14–65 ms; it is real capture latency on the shared clock). Flag if the first video pts > 500 ms.
- Audio: gap-free 60 ms Opus packets ⇒ `ffmpeg -i audio.webm -ar 16000 -ac 1 -c:a pcm_s16le audio16k.wav`; whisper offsets are t_ms directly. Verify the audio packet (pts,size) MD5 is identical across face/screen/audio.webm (it was); if not, cross-correlate decoded audio to find the offset.
- Screen: change-driven ~15 fps, no frames during static periods ⇒ index by "last frame with pts ≤ t". Screen-video px = `((screenX + x_vp)·DPR, (screenY + toolbar + y_vp)·DPR)`.
- mouse.csv / events.csv: trusted only if stamped `performance.now() − perfT0` with perfT0 captured in the face recorder's `onstart` (§8.5). Residual check per session: first click with a visible effect in screen.webm vs events t_ms, |Δ| < 100 ms; a start-up sync flash/beep event is the hard check.

### 5.2 Grids
10 Hz bins `[k·100, k·100+100)` ms (detectors), 1 Hz bins (report). Per source: face samples → per-bin median of angles/points, mean of blendshapes, max of blink, fraction present; empty bin → carry forward ≤ 300 ms then missing. Mouse → linear interpolation when neighbours are ≤ 250 ms apart, else hold; speed = path/Δt. Clicks/keys/scroll → assigned to bins as lists, never averaged. Words → every bin overlapping [from, to); 1 Hz text = words whose midpoint falls in the second. Screen → last pts ≤ t.

### 5.3 `frames.parquet` (raw per processed frame; cache so detector tuning never re-runs models)
`t_ms, frame_idx, face_present, n_faces, bbox_x,y,w,h, crop_padded_frac, u_e, v_e, ipd_px, iris_px, ear_l, ear_r, blink, head_yaw, head_pitch, head_roll, t_x, t_y, t_z, d_ipd, d_iris, d_mp, d_cm, h_deg_raw, v_deg_raw, sharp_h, sharp_v, iris_right, blend_H, blend_V, bs_00…bs_51, glare, backlight, asym, flags`.

### 5.4 Fused record (1 Hz `gaze-ai.csv`; identical fields at 10 Hz in `gaze-10hz.csv` minus the per-second aggregates; list-valued columns JSON-encoded)
```
t_s, t_ms_start,
face_present (0–1), gaze_h_deg, gaze_v_deg (bias-corrected, user frame: +h right, +v up),
gaze_h_sd, gaze_v_sd, gaze_x_cm, gaze_y_cm, gaze_x_vp, gaze_y_vp, gaze_sigma_px (null unless calibrated),
gaze_col {L,C,R}, gaze_row {T,M,B}, gaze_cell {TL…BR, uncertain}, gaze_state {on_screen, off_left, off_right, off_up, down_keyboard, away, no_face, eyes_closed},
gaze_p_cell, gaze_conf (0–1), region_switches, blink_count, blink_rate_per_min (rolling 30 s),
head_yaw_deg, head_pitch_deg, distance_cm,
mouse_x, mouse_y, mouse_cell, mouse_speed_px_s, mouse_path_px, mouse_idle (speed < 20 px/s whole second), dist_gaze_mouse_px,
clicks [{t_ms,x,y,cell,target_text,target_rect,is_rage,is_dead}], keys [{t_ms,key}], scroll_dy_px, scroll_reversals,
page_url, page_changed,
speech_text, speaking, silence_run_s, speech_cues [search_phrase|negative|hesitation],
expr_browDown, expr_browInnerUp, expr_eyeSquint, expr_eyeWide, expr_mouthPress, expr_mouthSmile, expr_mouthFrown, expr_jawOpen (+ *_z per-session z-scores), expr_label,
quality_score, quality_grade {A,B,C,F}, flags
```
`expressions.csv` = `t_s` + the 52 blendshape means + the aggregates/z-scores + `expr_label`. Aggregates: browDown = mean(L,R); eyeSquint = mean(L,R); mouthPress = mean(L,R); mouthSmile = mean(L,R); mouthFrown = mean(L,R); baseline = median/MAD over the first 60 s with a face (fallback: whole session). `expr_label` ∈ {neutral, concentrating (browDown_z ≥ 1.5 or eyeSquint_z ≥ 1.5, no scanning), confused (see §6), tense (mouthPress_z ≥ 1.5 or jawClench), amused (mouthSmile_z ≥ 1.5), surprised (browInnerUp_z ≥ 1.5 and eyeWide_z ≥ 1.0)} — heuristic cues, explicitly not validated emotion recognition.

### 5.5 Per-sample gaze confidence
`gaze_conf = present × mean(sharp_h, sharp_v) × blink_mask × head_pen × size_ok`, `head_pen = clip(1 − (|head_yaw| − 25°)/25°, 0, 1)`, `size_ok = 1 if bbox_w/W_img ≥ 0.15 else 0.5`. Blink mask drops the blink frame plus 1 before / 2 after. Measured sharpness on real frames ≈ 0.64–0.76 (it is a relative signal; also z-score it per session). A second is unusable when `gaze_conf < 0.3` or `face_present < 0.5`.

---

## 6. Inference rules → UX moments

Common schema: `{id, type, t_start_ms, t_end_ms, score 0–1, evidence: [{signal, value, threshold, t_ms}], gaze_dwell: {TL:…}, mouse_cells, clicks, transcript, screenshot_pts_ms, min_quality_grade}`. Detectors run on the 10 Hz grid and only on samples with gaze_conf ≥ 0.3; a moment inherits the worst quality grade of its window and is dropped if that grade is F (except LOOK-AWAY/no_face, which is itself the finding). Thresholds are starting points to be re-tuned on 3–5 labelled sessions.

| Moment | Rule (sliding window) | Score | Required evidence |
|---|---|---|---|
| **SEARCHING** | 3 s window: ≥ 6 region switches (after 2-sample majority filter) AND ≥ 3 distinct cells AND mouse path ≥ 300 px with no click AND no scroll burst (|Σdy| < 600 px). Extend while true; end at a click or when ≤ 1 switch in 1.5 s. Speech bonus +0.2 for en "where is/where's the/can't find/don't see/how do I/hmm", ar "وين/فين/ما ألقى/ما أشوف/كيف". | 0.4·min(switches/8,1) + 0.3·min(cells/5,1) + 0.3·min(path/800,1) (+bonus, cap 1) | switch count, cell list with dwell, mouse path, transcript words, first/last cell |
| **FOUND-THEN-ACTED** | Gaze dwell ≥ 500 ms in cell R (≤ 1 switch) → mouse enters R (or within 150 px of the eventual click point) after dwell start → click ≤ 2 s after dwell start with click cell == R. | 1 − 0.5·(latency/2 s), −0.3 if mean gaze_conf < 0.5 | dwell start/end, cell, click target text/rect, latency |
| **MISS-THEN-CORRECT** | First click lands in a cell ≠ dwell cell, second click within 3 s in the dwell cell. | 0.6 (+0.2 if dead click) | both clicks, dwell cell |
| **READING** (low reliability, grade A seconds only) | ≥ 3 s with constant row, column progression monotonic (L→C→R for LTR; R→C→L when page `dir=rtl` or transcript language is Arabic) with return sweeps, gaze_h_sd < 6° per second, mouse speed < 50 px/s, no clicks, scroll < 300 px per 2 s. | 0.3 + 0.4·monotonic_fraction + 0.3·min(duration/8 s, 1) | row, column sequence, duration |
| **LOOK-AWAY** | (a) face_present < 0.5 for ≥ 1 s; or (b) below bottom edge by > 8° ≥ 500 ms; or (c) head_pitch below session median by > 20° ≥ 500 ms; or (d) beyond a side edge by > 10° ≥ 500 ms. Subtype `keyboard-glance` if a keydown/Enter occurs within [−0.5, +1.5] s of onset; `off-screen` otherwise. | 0.9 for (a)/(b)/(c), 0.7 for (d) | which trigger, angle/pitch value, key event |
| **CONFUSION** | 6 s window: ≥ 4 region switches AND (browDown_z ≥ 1.5 or eyeSquint_z ≥ 1.5 sustained ≥ 1 s) AND (silence_run ≥ 4 s OR search/negation phrase) AND no productive click (a click that changes the page counts against). | 0.35·scan + 0.35·face + 0.3·speech; emit ≥ 0.6 | switch count, z-values, transcript/silence, absence of productive click |
| **FRUSTRATION** | Channels: behavioural (rage click ≥ 3 mousedown in 1 s within 30 px; dead click = no DOM mutation/nav/scroll within 1 s; scroll thrash ≥ 3 reversals in 3 s — reuse the extension's counters), facial (mouthPress_z ≥ 1.5 or browDown_z ≥ 1.5 or jawOpen spike), verbal (negative/expletive lexicon, "ugh/come on/why", ar "ليش/يا الله/ما يشتغل"). Emit when ≥ 2 channels fire within 5 s. | 0.5 + 0.25 per extra channel | per-channel values and times |

**Answering "was the user looking for the button at top-right?"**
1. Window W = from page load / task question / previous click to the click on the target (or give-up); require ≥ 3 s.
2. Target cell: click coordinates + logged `target_rect`; fallback: read the last screen frame with pts ≤ t_click.
3. Soft dwell: each 10 Hz sample spreads probability over cells with a 2-D Gaussian in angle space (σ_h = 8°, σ_v = 6° after bias correction; 10°/8° uncalibrated), weight = gaze_conf. `dwell[c] = Σ w·P(c) / Σ w`.
4. `p_target = dwell[cell]`, `p_col`, `p_row`; baseline = participant's whole-session dwell in that cell/column (controls for personal offset), floor 1/9 and 1/3. `LR = p_target / baseline`.
5. Verdict: **yes** if LR ≥ 2 and p_col ≥ 0.5 and FOUND-THEN-ACTED fires on the final click; **partly** if only column or only row matches (say which — vertical is weaker); **no/other** if another cell has LR ≥ 2.
6. Confidence = `sigmoid(ln LR) × min(1, N_valid/30) × mean gaze_conf × (0.6 if uncalibrated else 1) × (1.0 if mouse dwelled in the cell ≥ 30 % of W else 0.8)`, capped at 0.9 for a column claim and 0.6 for a specific cell without click calibration.
7. Template: "During the 7.3 s before clicking *Submit*, 61 % of confident gaze samples were in the right column (top-right 34 %, middle-right 27 %; session baseline right 38 %; LR 1.6); the mouse entered top-right 0.9 s before the click; the participant said 'where is the…' at 12.4 s — likely searching on the right side; top-right unconfirmed (confidence 0.55)."

---

## 7. Validation and quality

### 7.1 Sign self-tests
- **Automatic, every session** (§2.6 items 1–4). Outputs in `quality.json.signs`: `l2cs_channel_test`, `cross_path`, `orientation` (OK/INVERTED/UNTESTED with ρ and N).
- **Explicit prompt, recommended (8 s, logged as `selftest` events)**: LEFT 2 s, RIGHT 2 s, TOP 2 s, BOTTOM 2 s with an audible tick. Offline: drop the first 500 ms of each interval, per-interval medians (not means). PASS if `median_h(RIGHT) − median_h(LEFT) > +8°` and > 2× pooled within-interval SD; `median_v(TOP) − median_v(BOTTOM) > +8°`; MediaPipe `iris_right(RIGHT) − iris_right(LEFT) > +0.05` eye-widths and `head_yaw_right(RIGHT) > head_yaw_right(LEFT)` (participants usually co-rotate the head). Record the magnitudes: RIGHT/LEFT medians near ±10–17° are normal; ±5° means eye-only movement and under-reading ⇒ gain fitting is warranted.
- **One-time per machine, model-free** (§2.6 item 6). If the readable-text/right-hand check fails, the capture path is mirrored: fix the extension (canvas/CSS) or, for a virtual camera, record a per-session `mirrored=true` flag and re-run with `--assume-mirrored` (negates h, iris_right, blend H, head_yaw and t_x at ingest — one place only).

### 7.2 Click-consistency score (mandatory in every report)
For each click: gaze cell = cell of the median corrected gaze point over [t_c − 400, t_c] ms (≥ 2 valid samples, else unscored; sensitivity run with [−600, +100]). `score_g` = fraction matched, for grids 3×3, col×half, L/C/R, T/M/B. **Chance** = mean matched fraction over 500 permutations shifting all click times by random 5–60 s offsets (respects the session's own click distribution). `lift = (score − chance)/(1 − chance)`. Wilson 95 % CI (N=10: ±0.26; N=20: ±0.20; N=40: ±0.15). Report the ceiling honestly: even a perfect tracker scores ~80–90 % (gaze departs before predictable clicks). When the click bias is applied, compute leave-one-out. **Gating:** lift ≥ 0.5 on L/C/R → region claims allowed; 0.25–0.5 → "likely"; < 0.25 or N < 10 → gaze not validated this session, session grades capped at C, report restricted to mouse/transcript plus away/keyboard events. No clicks at all → cap at B, wording "estimated (unvalidated)".

### 7.3 Optional 5-point glance test (10 s, off for the strict no-calibration pitch, recommended in the UI)
Dots at TL, TR, BR, BL, C (5 % inset), 2 s each, logged as `calib` events. Use samples 500–2000 ms after onset, ≥ 8 per point. Per-axis 1-D fit (offset + gain) → per-session bias/gain (feeds §4.5 with priority over clicks until ≥ 8 clicks exist); residual RMS in degrees/cm; discriminability `D = |median_h(R) − median_h(L)| / pooled SD`: D ≥ 4 → 3×3 defensible; 2–4 → columns/halves only; < 2 → gaze unusable this session. Ordering violations (TL right of TR, etc.) = sign bug ⇒ abort. Expected effect: 10–12° → 5–8° for that participant (most new-user error is systematic bias).

### 7.4 Per-second quality score and grade
Sub-scores (1 / 0.5 / 0): detect (face in ≥ 60 % of sampled frames), size (bbox/frame width ≥ 0.25 / ≥ 0.15), pose (|yaw| ≤ 20°/30°, |pitch| ≤ 20°/25° from session median), light (no glare, backlight < 2, asymmetry < 0.35), blink (≤ 30 % masked), samples (≥ 6 / ≥ 3 valid at 10 Hz), stability (h SD ≤ 6°/10° while the head is still), timing (no pts gap > 200 ms). `quality_score = product`. Grades: A ≥ 0.8 (3×3 with hedge; column + half firm), B 0.5–0.8 (column/half only, "likely"), C 0.2–0.5 (left/right half or on/off-screen, "may have"), F < 0.2 (no gaze claim). Session cap from §7.2 applies on top. Report header: "gaze usable in 71 % of seconds; click-consistency 68 % vs 41 % chance (lift 0.46, N = 34)".

### 7.5 Failure-mode flags (`flags` column and `quality.json.summary`)
`no_face, multi_face, identity_switch, face_small, face_partial, head_turned, glare, backlit, too_dark, asym_light, blink_burst, eyes_closed, pts_gap, dropped_frames_pct, center_stage_suspected, foreign_camera, fpx_assumed, display_assumed, window_geometry_assumed, distance_inconsistent, bias_out_of_range, orientation_inverted, calib_missing, mouse_missing, events_missing, transcript_missing`.

### 7.6 Honest accuracy expectations (to print in the report footer)
"Uncalibrated webcam gaze (L2CS-Net, Gaze360 weights): typical error ≈ 10° ≈ 8 cm at 55 cm; a 3×3 cell is about one error radius. Column (L/C/R) statements are right roughly 7 times in 10; specific cells about half the time; looking-away and keyboard glances are reliable." Replace with measured numbers after the in-house validation (§9).

---

## 8. Implementation plan

### 8.1 Package layout
```
bux_gaze/
  cli.py          # bux-analyze-video
  ingest.py       # EBML/header repair, remux, pts extraction, PyAV decode, session.json + profiles, fallbacks
  mp_face.py      # FaceLandmarker wrapper → per-frame landmarks, EAR/blink, iris, head pose, t, distance, photometric flags
  l2cs_gaze.py    # getArch load, head hooks, flip self-test, fp16 check, batched MPS inference, preprocessing
  geometry.py     # frames, display/camera profiles, eye_frame_axes, gaze_to_screen, screen_to_angles, viewport mapping
  calib.py        # self-centring prior, click residuals, posture segmentation, bias/gain fit, glance-test fit, orientation check
  regions.py      # grid + hysteresis state machine, states, per-second votes
  fuse.py         # 10 Hz / 1 Hz grids, mouse/events/screen/transcript joins, schema writers
  moments.py      # detectors + question answering
  quality.py      # gaze_conf, per-second quality, click-consistency, permutation chance, signs → quality.json
  transcribe.py   # ffmpeg → wav16k, whisper-cli -oj -ml 1 -sow, word list
  profiles/       # displays.yaml, cameras.yaml (f_px per label × resolution)
tests/            # geometry round-trip, synthetic-pose head-yaw signs, flip-test on a stored crop, schema checks
```

### 8.2 CLI
`bux-analyze-video <session-folder> [--fps 10] [--mp-fps 15] [--device mps|cpu] [--fp16/--no-fp16] [--display auto|14.2|"30.6x19.1"] [--fpx auto|<px>] [--no-click-calib] [--assume-mirrored] [--whisper-model <path>] [--lang auto|en|ar] [--reprocess]`
Outputs into `<session>/analysis/`: `face_fixed.webm`, `frames_pts.csv`, `frames.parquet`, `transcript.json`, `gaze-10hz.csv`, `gaze-ai.csv`, `expressions.csv`, `moments.json`, `quality.json` (signs, calibration, click-consistency, grade histogram, flags summary, versions/commit hashes, parameters used), `debug/` (sign-test plots, 20 annotated frames with gaze arrow drawn in image space using dx = −sin(h), i.e. the arrow points image-left when the participant looks right).

### 8.3 Step order (each step idempotent, cached by input hash)
1. Load session.json; resolve display/camera profiles; record assumptions as flags.
2. Validate/repair/remux face.webm; extract pts; verify video stream; audio identity across files.
3. Transcribe (word-level) in parallel with vision.
4. Decode → MediaPipe (15 Hz) → crops → L2CS (10 Hz, batched) → `frames.parquet`. Startup: flip self-test, fp16 check.
5. Distance, E_C, raw screen points; session self-centring prior.
6. Load mouse/events (degrade gracefully if absent); click residuals; posture segments; bias/gain; orientation check; cross-path check.
7. Re-map with corrected angles; regions/states at 10 Hz; per-second votes; gaze_conf and quality grades.
8. Fuse mouse/clicks/keys/scroll/page/screen/transcript/expressions → `gaze-10hz.csv`, `gaze-ai.csv`, `expressions.csv`.
9. Detectors → `moments.json`; click-consistency (leave-one-out) → `quality.json`.
10. Debug artefacts; exit non-zero on any sign FAIL.

### 8.4 What the report skill (`boring-ux-report`) consumes
- `quality.json` first: `signs.orientation`, click-consistency lift/N/CI, grade histogram, calibration status ⇒ selects wording tier (§7.2/7.4) and prints the footer disclaimer.
- `gaze-ai.csv`: per-second narrative (gaze cell/state + mouse cell + clicks + speech), heat-maps of dwell per page, gaze–mouse distance.
- `moments.json`: timeline chips with evidence and screenshot pts; question answering uses the dwell/LR method with the stored dwell maps.
- `expressions.csv`: z-score sparklines and cue markers only (never "the user felt X").

### 8.5 Recorder-side changes required for full fidelity (small, high leverage)
1. `perfT0 = performance.now()` in the face recorder's `onstart`; stamp every mouse/click/scroll/key/page/live-gaze row as `performance.now() − perfT0`; write perfT0 and `Date.now()` to session.json.
2. Start all recorders from the same tracks on the same tick (audio already shared); write chunks sequentially (await each `write`), assert the first chunk begins with `1A 45 DF A3` and that the face stream has a live video track; `requestData()/stop()` and await `stop` before closing.
3. Per click: viewport x,y, scrollX/Y, target text, CSS selector, `getBoundingClientRect()`, DOM-mutation-within-1-s flag. Keydown names only (Enter/Tab/Backspace/'char').
4. Log `devicePixelRatio, screenX/Y, inner/outerWidth/Height, screen.width/height, screen.availHeight`, track `label` and `getSettings()` (width, height, frameRate, deviceId); display model from a settings dropdown.
5. Checklist screen: Center Stage OFF, built-in camera selected, window maximised; optional 8 s LEFT/RIGHT/TOP/BOTTOM prompt (`selftest` events) and optional 5-point glance test (`calib` events).
6. Sync event at start: one-frame white flash in the page + short beep (visible in screen.webm, audible in audio.webm).
7. Record face at 1280×720 if CPU allows, 30 fps requested (`frameRate: {ideal: 30, min: 24}`).

---

## 9. Open risks and the empirical tests for the first real session

| # | Risk | Test on session 1 (pass criterion) |
|---|---|---|
| 1 | Absolute L2CS horizontal sign rests on the label chain + one real-session correlation | Explicit LEFT/RIGHT prompt: `median_h(RIGHT) − median_h(LEFT) > +8°`; click orientation ρ > +0.3 (N ≥ 10) |
| 2 | Track mirrored by a virtual camera or extension canvas | Model-free check (readable text, right hand image-left) on decoded frames; track label logged |
| 3 | L2CS zero ≠ eye→lens ray for off-axis faces; vertical bias larger than expected | TOP/BOTTOM prompt: `median_v(TOP) ∈ [−6°, +6°]`, `median_v(BOTTOM) < −12°`; click residual MAD ≤ 6° |
| 4 | Magnitude compression on eye-only movements (edges read 10° instead of 15°) | Prompt magnitudes; if |median_h(edge)| < 8°, enable gain fit by default |
| 5 | f_px / Center Stage / display profile wrong | Card measurement at the recorded resolution; face bbox vs background motion check; d_ipd vs d_iris within 25 % and vs tape measure ±15 % |
| 6 | Timing: pts drift, first-pts offset, mouse clock ≠ media clock | Flash/beep sync event Δ < 50 ms; first visible click effect vs events t_ms Δ < 100 ms; dropped-frame % logged |
| 7 | Blink detector thresholds (EAR scale differs per face/resolution) | "Blink twice / close each eye" prompt: 2 blink events detected, correct side (idx 10 peaks for the right eye) |
| 8 | fp16 on MPS changes angles | 300-frame fp16 vs fp32 max |Δ| < 0.2° |
| 9 | Region accuracy lower than the Monte-Carlo estimate; detectors mis-tuned | In-house "click the highlighted target" task (30 targets on a 3×3 grid, 3–5 people incl. one glasses wearer, one back-lit): confusion matrices for 3×3 / col×half / L/C/R; click-consistency ceiling; replace §1 numbers with measured ones |
| 10 | Older sessions without mouse/events (header-only events.csv, no mouse.csv) | Pipeline completes with `mouse_missing/events_missing`, grades capped at B, click sections omitted |
| 11 | The extension's live `gaze.csv` shows 79 % "R" — likely mirrored or uncalibrated | Compare its column against the L2CS column after the sign test; use it only as a weight-0.3 column prior if ρ > 0.5 |
| 12 | Corrupted recordings (missing EBML header, out-of-order clusters, audio-only face file) | Repair path exercised on the three existing sessions; recorder fixes in §8.5 verified on a 15-min dry run |

---

### Key sources
L2CS-Net: Abdelrahman et al., arXiv:2203.03339; repo `Ahmednull/L2CS-Net` (train.py, issue #32, PR #44); installed fork `edavalosanaya/L2CS-Net@4a0f978` (`l2cs/model.py:70`, `pipeline.py:122–133`, `datasets.py:54–59`, `vis.py:13–14`, `utils.py:19–27,58–63`). Gaze360: Kellnhofer et al., arXiv:1910.10088; dataset README (eye coordinate frame); GazeHub `data_processing_gaze360.py` (`GazeTo2d`, header `Face Left Right Origin 3DGaze 2DGaze`). MPIIGaze: Zhang et al., TPAMI (arXiv:1711.09017); Zhang et al., CHI 2019 (arXiv:1901.10906). MediaPipe: `docs/solutions/face_mesh.md`, `face_landmarker.py` (Blendshapes enum 9–18), `face_mesh_connections.py`, `canonical_face_model.obj`, `face_geometry.proto`, `face_geometry_from_landmarks_graph.cc` (63° vFOV, TOP_LEFT origin), `geometry_pipeline.cc`, iris docs (11.7 mm; 4.3 % depth error). Mirroring: W3C mediacapture-main #452; Chromium `media/capture/video/apple/video_capture_device_avfoundation.mm`; Apple `AVCaptureConnection.isVideoMirrored`; webrtcHacks/cssMirror. Implicit calibration: Papoutsaki et al., WebGazer, IJCAI 2016; Huang, White & Buscher, CHI 2012; Huang et al., PACE, CHI 2016; Hornof & Halverson 2002. Recording/timing: W3C MediaStream Recording; w3c/mediacapture-record #177; measurements on `/Users/abdulaziz/Desktop/UX/session-2026-07-16T*`. Facial cues: Craig et al. 2008; Grafsgaard et al. 2013; Microsoft Clarity rage/dead-click definitions. Local benchmarks and verification scripts: `/private/tmp/claude-501/-Users-abdulaziz-Desktop/56960ba1-c4f4-4d6d-9c5f-534b88c0f84d/scratchpad/{mirror_selftest.py, flip_test*.py, headcheck.py, mp_check.py, bench_l2cs.py, sim_regions.py, cv_ts.py}`.