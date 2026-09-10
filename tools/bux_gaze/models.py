"""Model wrappers: MediaPipe FaceLandmarker (§3.1) and L2CS-Net gaze (§3.2–3.3).

L2CS: heads are bound BY NAME via forward hooks (fc_yaw_gaze = horizontal, fc_pitch_gaze = vertical),
immune to the library's swapped return names. Startup flip self-test guards the assignment (§2.6).
"""
import math
import os
import cv2
import numpy as np

MODELS = os.environ.get("BUX_MODELS", os.path.expanduser("~/Desktop/gaze-ai/models"))
L2CS_WEIGHTS = os.path.join(MODELS, "L2CSNet_gaze360.pkl")
MP_MODEL = os.path.join(MODELS, "face_landmarker.task")

# anatomical indices on the RAW (unmirrored) track: the "R" set (33/133/468) is the image-LEFT eye = subject's right
R_OUT, R_IN, R_UP, R_DN, R_UP2, R_DN2, R_IRIS = 33, 133, 159, 145, 158, 153, 468
L_IN, L_OUT, L_UP, L_DN, L_UP2, L_DN2, L_IRIS = 362, 263, 386, 374, 385, 380, 473
BLEND = ["browDownLeft", "browDownRight", "browInnerUp", "eyeSquintLeft", "eyeSquintRight", "eyeWideLeft", "eyeWideRight",
         "mouthPressLeft", "mouthPressRight", "mouthSmileLeft", "mouthSmileRight", "mouthFrownLeft", "mouthFrownRight",
         "jawOpen", "eyeBlinkLeft", "eyeBlinkRight",
         "eyeLookOutLeft", "eyeLookInLeft", "eyeLookOutRight", "eyeLookInRight",
         "eyeLookUpLeft", "eyeLookUpRight", "eyeLookDownLeft", "eyeLookDownRight"]


class FaceModel:
    def __init__(self):
        import mediapipe as mp
        from mediapipe.tasks import python as mpp
        from mediapipe.tasks.python import vision
        self.mp = mp
        self.lm = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
            base_options=mpp.BaseOptions(model_asset_path=MP_MODEL),
            running_mode=vision.RunningMode.VIDEO, num_faces=2,
            output_face_blendshapes=True, output_facial_transformation_matrixes=True,
            min_face_detection_confidence=0.5, min_face_presence_confidence=0.5, min_tracking_confidence=0.5))
        self._last_t = -1

    def detect(self, rgb, t_ms):
        """Returns None (no face) or a dict of per-frame features (§3.1) + the exact L2CS crop (§3.3)."""
        t_ms = int(t_ms)
        if t_ms <= self._last_t:
            t_ms = self._last_t + 1                  # MediaPipe VIDEO mode needs strictly increasing ms
        self._last_t = t_ms
        res = self.lm.detect_for_video(self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb), t_ms)
        if not res.face_landmarks:
            return None
        h, w = rgb.shape[:2]
        # largest face
        faces = res.face_landmarks
        areas = [(max(p.x for p in L) - min(p.x for p in L)) * (max(p.y for p in L) - min(p.y for p in L)) for L in faces]
        k = int(np.argmax(areas)); L = faces[k]
        P = np.array([[p.x * w, p.y * h] for p in L])
        x0, y0 = P.min(0); x1, y1 = P.max(0)
        bw, bh = x1 - x0, y1 - y0
        # eyes
        pR, pL = P[R_IRIS], P[L_IRIS]
        u_e, v_e = float((pR[0] + pL[0]) / 2), float((pR[1] + pL[1]) / 2)
        ipd_px = float(np.linalg.norm(pR - pL))
        iris_px = float(np.mean([self._ring_diam(P, 469, 472), self._ring_diam(P, 474, 477)]))
        ear_R = (np.linalg.norm(P[R_UP] - P[R_DN]) + np.linalg.norm(P[R_UP2] - P[R_DN2])) / (2 * max(np.linalg.norm(P[R_OUT] - P[R_IN]), 1e-6))
        ear_L = (np.linalg.norm(P[L_UP] - P[L_DN]) + np.linalg.norm(P[L_UP2] - P[L_DN2])) / (2 * max(np.linalg.norm(P[L_IN] - P[L_OUT]), 1e-6))
        # iris ratios (image frame); positive-right conversion happens after session-median centring (§3.1)
        hR = (P[R_IRIS, 0] - P[R_OUT, 0]) / ((P[R_IN, 0] - P[R_OUT, 0]) or 1e-6)
        hL = (P[L_IRIS, 0] - P[L_IN, 0]) / ((P[L_OUT, 0] - P[L_IN, 0]) or 1e-6)
        # head pose from the forward vector of the metric matrix (§2.3) — never generic Euler
        M = np.asarray(res.facial_transformation_matrixes[k]).reshape(4, 4) if res.facial_transformation_matrixes else None
        head_yaw = head_pitch = head_roll = 0.0; t_x = t_y = t_z = None
        if M is not None:
            R = M[:3, :3] / (np.linalg.norm(M[:3, :3], axis=0) + 1e-9)
            n = R[:, 2]
            head_yaw = math.atan2(-n[0], n[2]); head_pitch = math.asin(float(np.clip(n[1], -1, 1)))
            yv = R[:, 1]; head_roll = math.atan2(yv[0], yv[1])
            t_x, t_y, t_z = (float(x) for x in M[:3, 3])
        bs = {b.category_name: float(b.score) for b in res.face_blendshapes[k]} if res.face_blendshapes else {}
        # blendshape eye-in-head proxies (§2.3): H is user-LEFT positive -> screen_right = -H
        H_bl = 0.5 * ((bs.get("eyeLookOutLeft", 0) - bs.get("eyeLookInLeft", 0)) + (bs.get("eyeLookInRight", 0) - bs.get("eyeLookOutRight", 0)))
        V_bl = 0.5 * (bs.get("eyeLookUpLeft", 0) + bs.get("eyeLookUpRight", 0)) - 0.5 * (bs.get("eyeLookDownLeft", 0) + bs.get("eyeLookDownRight", 0))
        # exact L2CS crop: 1.3x bbox square, edge replicate, 224 INTER_AREA
        s = 1.3 * max(bw, bh); cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        X0, Y0, X1, Y1 = int(round(cx - s / 2)), int(round(cy - s / 2)), int(round(cx + s / 2)), int(round(cy + s / 2))
        pad = [max(0, -Y0), max(0, Y1 - h), max(0, -X0), max(0, X1 - w)]
        img = cv2.copyMakeBorder(rgb, *pad, cv2.BORDER_REPLICATE) if any(pad) else rgb
        crop = img[Y0 + pad[0]:Y1 + pad[0], X0 + pad[2]:X1 + pad[2]]
        crop224 = cv2.resize(crop, (224, 224), interpolation=cv2.INTER_AREA)
        padded_frac = float(sum(pad) / (2 * s + 1e-6))
        # photometric flags (cheap): face mean luminance, eye-ROI glare
        gray = cv2.cvtColor(crop224, cv2.COLOR_RGB2GRAY)
        eye_roi = gray[60:120, 40:184]
        return dict(
            n_faces=len(faces), bbox=(float(x0), float(y0), float(bw), float(bh)), u_e=u_e, v_e=v_e,
            ipd_px=ipd_px, iris_px=iris_px, ear=float((ear_R + ear_L) / 2), hR=float(hR), hL=float(hL),
            head_yaw=head_yaw, head_pitch=head_pitch, head_roll=head_roll, t_x=t_x, t_y=t_y, t_z=t_z,
            bs=bs, H_bl=float(H_bl), V_bl=float(V_bl), crop224=crop224, padded_frac=padded_frac,
            face_lum=float(gray.mean()), glare=float((eye_roi > 240).mean()), img_w=w, img_h=h)

    @staticmethod
    def _ring_diam(P, a, b):
        pts = P[a:b + 1]
        return max(np.linalg.norm(p - q) for i, p in enumerate(pts) for q in pts[i + 1:])


class GazeModel:
    def __init__(self, device="mps", fp16=True):
        import torch
        import torch.nn.functional as F
        from l2cs import getArch
        self.torch, self.F = torch, F
        self.dev = torch.device(device if (device == "cpu" or torch.backends.mps.is_available()) else "cpu")
        self.model = getArch("ResNet50", 90)
        self.model.load_state_dict(torch.load(L2CS_WEIGHTS, map_location="cpu")); self.model.eval().to(self.dev)
        self.heads = {}
        self.model.fc_yaw_gaze.register_forward_hook(lambda m, i, o: self.heads.__setitem__("h", o))    # HORIZONTAL (+right)
        self.model.fc_pitch_gaze.register_forward_hook(lambda m, i, o: self.heads.__setitem__("v", o))  # VERTICAL (+up)
        self.idx = torch.arange(90, device=self.dev, dtype=torch.float32)
        self.mean = torch.tensor([0.485, 0.456, 0.406], device=self.dev).view(1, 3, 1, 1)
        self.std = torch.tensor([0.229, 0.224, 0.225], device=self.dev).view(1, 3, 1, 1)
        self.fp16 = bool(fp16 and self.dev.type == "mps")
        if self.fp16:
            self.model.half()

    def _decode(self, logits):
        p = self.F.softmax(logits.float(), 1)
        deg = (p * self.idx).sum(1) * 4 - 180
        ent = -(p * self.torch.log(p.clamp_min(1e-9))).sum(1)
        return deg, 1 - ent / math.log(90)

    def predict(self, crops224):
        """crops224: list of RGB uint8 224x224. Returns h_deg, v_deg, sharp_h, sharp_v (numpy)."""
        outs = [[], [], [], []]
        with self.torch.no_grad():
            for i in range(0, len(crops224), 16):
                x = self.torch.from_numpy(np.stack(crops224[i:i + 16])).to(self.dev).permute(0, 3, 1, 2).float() / 255
                x = self.F.interpolate(x, size=448, mode="bilinear", align_corners=False)
                x = (x - self.mean) / self.std
                if self.fp16:
                    x = x.half()
                self.model(x)
                h, sh = self._decode(self.heads["h"]); v, sv = self._decode(self.heads["v"])
                for o, t in zip(outs, (h, v, sh, sv)):
                    o.append(t.cpu().numpy())
        return tuple(np.concatenate(o) if o else np.array([]) for o in outs)

    def flip_selftest(self, crops224):
        """§2.6 item 2: horizontal channel must invert under a mirror flip (r<-0.5), vertical must not (r>+0.5)."""
        if len(crops224) < 8:
            return dict(status="inconclusive", n=len(crops224))
        h0, v0, _, _ = self.predict(crops224)
        hf, vf, _, _ = self.predict([cv2.flip(c, 1) for c in crops224])
        rh = float(np.corrcoef(h0, hf)[0, 1]) if h0.std() > 0 else 0.0
        rv = float(np.corrcoef(v0, vf)[0, 1]) if v0.std() > 0 else 0.0
        status = "pass" if (rh < -0.5 and rv > 0.5) else ("inconclusive" if abs(rh) < 0.5 else "FAIL")
        return dict(status=status, r_horizontal=round(rh, 3), r_vertical=round(rv, 3), n=len(crops224))
