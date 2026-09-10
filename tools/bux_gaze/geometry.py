"""Coordinate frames, display/camera profiles, angle<->screen mapping, bias correction.

Conventions (docs/GAZE-FROM-VIDEO-DESIGN.md §2, all CONFIRMED):
  h = horizontal gaze angle, + = participant's own RIGHT  (screen +x). No flip needed for L2CS.
  v = vertical gaze angle,   + = UP.                       Zero = eye->lens ray.
  E_C = eye midpoint in OpenCV camera frame (X right = image right, Y down, Z toward participant).
  Screen plane is Z_C = 0; user/screen frame: x_S = -X_C (user right), y_S = +Y_C (down).
"""
import math
import numpy as np

# §4.1 display profiles keyed by CSS px (DPR 2 default scaling): W, H (cm), y_off (camera height above top edge, cm)
DISPLAYS = {
    (1470, 956): ("13.6in Air (notch)", 29.3, 18.3, -0.45),
    (1512, 982): ("14.2in Pro (notch)", 30.6, 19.1, -0.45),
    (1710, 1112): ("15.3in Air (notch)", 32.9, 20.6, -0.45),
    (1728, 1117): ("16.2in Pro (notch)", 34.9, 21.8, -0.45),
    (1440, 900): ("13.3in (bezel)", 28.6, 17.9, 0.9),
    (1280, 800): ("13.3in (bezel)", 28.6, 17.9, 0.9),
    (1792, 1120): ("16in 2019 (bezel)", 34.4, 21.5, 0.9),
}
MENUBAR_PX, TOOLBAR_PX = 24, 53   # assumed when window geometry is not logged (§4.4)


def resolve_display(screen_w, screen_h, override=None):
    """Return dict(name, W, H, y_off, flags). override = 'WxH' cm or a key like '14.2'."""
    flags = []
    if override:
        if "x" in str(override):
            W, H = (float(x) for x in str(override).lower().split("x"))
            return dict(name="override", W=W, H=H, y_off=-0.45, flags=flags)
        for k, (name, W, H, yo) in DISPLAYS.items():
            if str(override) in name:
                return dict(name=name, W=W, H=H, y_off=yo, flags=flags)
    if (screen_w, screen_h) in DISPLAYS:
        name, W, H, yo = DISPLAYS[(screen_w, screen_h)]
        return dict(name=name, W=W, H=H, y_off=yo, flags=flags)
    # match by width only (viewport width == screen width when the window is maximised)
    for (w, h), (name, W, H, yo) in DISPLAYS.items():
        if w == screen_w:
            flags.append("display_assumed")
            return dict(name=name, W=W, H=H, y_off=yo, flags=flags)
    flags += ["display_assumed", "display_unknown"]
    return dict(name="unknown->14.2in", W=30.6, H=19.1, y_off=-0.45, flags=flags)


def focal_px(img_w, override=None):
    """§4.1 fallback f = 0.85*W_img (HFOV≈61°) unless measured."""
    if override:
        return float(override), []
    return 0.85 * img_w, ["fpx_assumed"]


# ---------- §4.4 core mapping ----------
def eye_frame_axes(E_C):
    z_e = E_C / np.linalg.norm(E_C)                 # camera->eye  (= Gaze360 +z, away from camera)
    up = np.array([0.0, -1.0, 0.0])                 # camera Y is down
    y_e = up - (up @ z_e) * z_e; y_e /= np.linalg.norm(y_e)
    x_e = np.cross(y_e, z_e)                        # on-axis: (-1,0,0) = image-left = Gaze360 +x = user right
    return x_e, y_e, z_e


def gaze_to_screen(h, v, E_C, W, y_off):
    """h, v radians (bias-corrected). Returns (x_cm from left edge, y_cm from top edge) or None."""
    g_G = np.array([math.cos(v) * math.sin(h), math.sin(v), -math.cos(v) * math.cos(h)])
    x_e, y_e, z_e = eye_frame_axes(E_C)
    g_C = g_G[0] * x_e + g_G[1] * y_e + g_G[2] * z_e
    if g_C[2] > -1e-3:
        return None                                  # not heading toward the screen plane
    P = E_C + (-E_C[2] / g_C[2]) * g_C               # intersection with Z_C = 0
    return W / 2 - P[0], P[1] - y_off


def screen_to_angles(x_cm, y_cm, E_C, W, y_off):
    Q_C = np.array([W / 2 - x_cm, y_cm + y_off, 0.0])
    dv = Q_C - E_C; dv /= np.linalg.norm(dv)
    x_e, y_e, z_e = eye_frame_axes(E_C)
    g = np.array([dv @ x_e, dv @ y_e, dv @ z_e])
    return math.atan2(g[0], -g[2]), math.asin(float(np.clip(g[1], -1, 1)))


def eye_position(u_e, v_e, d, f, cx, cy):
    """§4.3 E_C from image eye midpoint (px), distance d (cm), focal f (px)."""
    return np.array([(u_e - cx) * d / f, (v_e - cy) * d / f, d])


def distance_cm(ipd_px, iris_px, head_yaw_rad, t_z_cm, f, img_h):
    """§4.2 weighted median of three estimates, clamped to [30,100]. Returns (d, flags)."""
    est, w = [], []
    if ipd_px and ipd_px > 1:
        est.append(f * 6.3 / (ipd_px / max(math.cos(head_yaw_rad), 0.5))); w.append(1.0)
    if iris_px and iris_px > 1:
        est.append(f * 1.17 / iris_px); w.append(0.5)
    if t_z_cm is not None and t_z_cm < -5:
        vfov = 2 * math.atan(img_h / (2 * f))
        est.append(-t_z_cm * math.tan(math.radians(31.5)) / math.tan(vfov / 2)); w.append(0.7)
    if not est:
        return 55.0, ["distance_unknown"]
    flags = []
    if len(est) >= 2 and est[0] > 0 and abs(est[0] - est[1]) / est[0] > 0.25:
        flags.append("distance_inconsistent")
    order = np.argsort(est); c = np.cumsum(np.array(w)[order]); d = est[order[int(np.searchsorted(c, c[-1] / 2))]]
    return float(np.clip(d, 30, 100)), flags


# ---------- viewport / grid ----------
class Viewport:
    """CSS-px mapping between screen cm and the recorded viewport (§4.4, window geometry assumed if missing)."""
    def __init__(self, session, display):
        vp = session.get("viewport", {}) or {}
        self.inner_w = int(vp.get("winW") or vp.get("stageW") or 1512)
        self.inner_h = int(vp.get("winH") or vp.get("stageH") or 805)
        self.screen_w = int(session.get("screen", {}).get("width", self.inner_w))
        self.screen_h = int(session.get("screen", {}).get("height", self.inner_h + MENUBAR_PX + TOOLBAR_PX))
        self.W, self.H, self.y_off = display["W"], display["H"], display["y_off"]
        self.ppcm_x = self.screen_w / self.W
        self.ppcm_y = self.screen_h / self.H
        win = session.get("window") or {}
        self.flags = [] if win else ["window_geometry_assumed"]
        self.screen_x = float(win.get("screenX", 0))
        self.screen_y = float(win.get("screenY", MENUBAR_PX))
        outer_w = float(win.get("outerWidth", self.inner_w)); outer_h = float(win.get("outerHeight", self.inner_h + TOOLBAR_PX))
        self.dx = self.screen_x + (outer_w - self.inner_w) / 2
        self.dy = self.screen_y + (outer_h - self.inner_h)

    def cm_to_vp(self, x_cm, y_cm):
        return x_cm * self.ppcm_x - self.dx, y_cm * self.ppcm_y - self.dy

    def vp_to_cm(self, x_vp, y_vp):
        return (x_vp + self.dx) / self.ppcm_x, (y_vp + self.dy) / self.ppcm_y

    def cell(self, x_vp, y_vp):
        """Viewport thirds -> (col, row, cell). None if outside the viewport."""
        if x_vp is None or y_vp is None or not (0 <= x_vp <= self.inner_w and 0 <= y_vp <= self.inner_h):
            return None, None, None
        col = "LCR"[min(2, int(3 * x_vp / self.inner_w))]
        row = "TMB"[min(2, int(3 * y_vp / self.inner_h))]
        return col, row, row + col

    def edge_angles(self, E_C):
        """Angles (rad) of the physical display edges seen from the eye: dict(left,right,top,bottom)."""
        hl, _ = screen_to_angles(0, self.H / 2, E_C, self.W, self.y_off)
        hr, _ = screen_to_angles(self.W, self.H / 2, E_C, self.W, self.y_off)
        _, vt = screen_to_angles(self.W / 2, 0, E_C, self.W, self.y_off)
        _, vb = screen_to_angles(self.W / 2, self.H, E_C, self.W, self.y_off)
        return dict(left=hl, right=hr, top=vt, bottom=vb)


# ---------- §4.5 / §4.6 bias ----------
def self_centering_bias(points_cm, E_C_med, W, H, y_off):
    """b0 = angles(median raw point) - angles(screen centre), clipped ±5° (radians).
    Returns (b_h, b_v, raw_h, raw_v) — raw = unclipped, for diagnostics (a saturated clip means clicks are needed)."""
    if len(points_cm) < 20:
        return 0.0, 0.0, 0.0, 0.0
    med = np.median(np.array(points_cm), axis=0)
    hm, vm = screen_to_angles(med[0], med[1], E_C_med, W, y_off)
    hc, vc = screen_to_angles(W / 2, H / 2, E_C_med, W, y_off)
    lim = math.radians(5)
    return float(np.clip(hm - hc, -lim, lim)), float(np.clip(vm - vc, -lim, lim)), float(hm - hc), float(vm - vc)


def click_bias(residuals):
    """residuals: list of (r_h, r_v) radians (measured - expected) per accepted click.
    §4.6 gates: reject |r-b|>10°, cap |b|<=12°, apply only if n>=3 and MAD<=6°. Returns dict."""
    out = dict(n_candidates=len(residuals), n_accepted=0, b_h=0.0, b_v=0.0, mad_deg=None, applied=False, flags=[])
    if len(residuals) < 3:
        return out
    R = np.array(residuals)
    b = np.median(R, axis=0)
    keep = np.all(np.abs(R - b) <= math.radians(10), axis=1)
    R = R[keep]
    if len(R) < 3:
        return out
    b = np.median(R, axis=0)
    mad = float(np.degrees(np.median(np.abs(R - b))))
    out.update(n_accepted=int(len(R)), b_h=float(b[0]), b_v=float(b[1]), mad_deg=mad)
    if max(abs(np.degrees(b))) > 12:
        out["flags"].append("bias_out_of_range"); return out
    if mad <= 6:
        out["applied"] = True
    return out
