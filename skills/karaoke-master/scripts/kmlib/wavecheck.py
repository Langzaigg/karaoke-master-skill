"""Post-timing optional review: zoomed vocal spectrogram + current axis.

``km wavecheck`` renders, for every line, two stacked zoomed spectrograms of
the separated vocal — a 10 ms wideband panel (consonant onsets, plosive
bursts) and a 64 ms harmonic panel with an autocorrelation pitch contour
(vowels, note changes, held-note tails, breaths) — with the current timing
axis on top (line start, each character's first checkpoint, releases / breath
marks) and, underneath, the dB energy curve of the 500–4000 Hz vocal band
with the voice threshold.  One PNG per line, plus ``axis.json`` with the same
numbers.  An agent reads the strips and refines the axis by eye; corrections
go back through ``km edit`` (``set_char`` / ``set_pause`` / ``set_line_end``
/ ``shift_lines`` / ``shift_chars`` / ``set_line_span``).  Optional pass,
like ``smooth``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from . import media
from .jobstore import JobStore, write_json

# --------------------------------------------------------------------- layout
SR = 16000
NFFT_WIDE = 160                     # 10 ms analysis — onsets / transients
NFFT_HARM = 1024                    # 64 ms analysis — harmonics / tails
F_LO_HZ = 50                        # display range 50 Hz … F_HI_HZ (log axis)
F_HI_HZ = 5500
BAND_LO_HZ, BAND_HI_HZ = 500, 4000  # vocal band for the dB energy curve
PITCH_WIN_S = 0.04                  # autocorrelation window for the pitch line
PITCH_LO_HZ, PITCH_HI_HZ = 70, 1000
PITCH_MIN_R = 0.35                  # voicing: minimum normalised autocorrelation peak

W_MIN, W_MAX = 1200, 2300           # PNG width bounds
PX_PER_S = 300                      # target horizontal resolution
H = 608                             # PNG height
ML = MR = 28                        # side margins
PANEL_H = 170                       # each spectrogram panel

Y_RULER = 26                        # ruler tick baseline
Y_EVENT = 42                        # 行起 / 收尾 / 换气 label baseline
Y_LANE_A, Y_LANE_B = 58, 72         # character label lanes (staggered)
Y_WIDE0, Y_WIDE1 = 82, 252          # wideband panel (10 ms)
Y_HARM0, Y_HARM1 = 262, 432         # harmonic panel (64 ms) + pitch contour
Y_WIPE0, Y_WIPE1 = 438, 458         # character wipe bar
Y_EN0, Y_EN1 = 466, 536             # dB energy curve band
Y_FOOT = 554                        # footer first line; +16 per line

GUIDE_HZ = (200, 500, 1000, 2000, 4000)

C_WAVE = (109, 156, 201)
C_AXIS = (63, 118, 181)
C_AXIS_SPEC = (200, 235, 255)
C_START = (26, 152, 80)
C_END = (215, 48, 39)
C_THR = (201, 162, 39)
C_ONSET = (210, 154, 42)

# magma-style colour ramp (dark purple → orange → yellow)
_MAGMA = [(0.0, (0, 0, 4)), (0.13, (28, 16, 68)), (0.25, (79, 18, 123)), (0.38, (129, 37, 129)),
          (0.5, (181, 54, 122)), (0.63, (229, 80, 100)), (0.75, (251, 135, 97)),
          (0.88, (254, 194, 135)), (1.0, (252, 253, 191))]
_LUT = None


def _magma_lut() -> np.ndarray:
    global _LUT
    if _LUT is None:
        xs = np.array([p[0] for p in _MAGMA])
        cs = np.array([p[1] for p in _MAGMA], dtype=np.float64)
        t = np.linspace(0, 1, 256)
        _LUT = np.stack([np.interp(t, xs, cs[:, k]) for k in range(3)], axis=1).astype(np.uint8)
    return _LUT


# --------------------------------------------------------------------- helpers
def _parse_lines(spec: object, n: int) -> list[int]:
    """``"3-12,15"`` → 0-based indices. Empty → every line."""
    if spec is None or str(spec).strip() == "":
        return list(range(n))
    raw: list[int] = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            raw += list(range(int(a) - 1, int(b)))
        else:
            raw.append(int(part) - 1)
    out: list[int] = []
    for i in raw:
        if 0 <= i < n and i not in out:
            out.append(i)
    return out


def _audio_path(store: JobStore, st: dict, override: str | None) -> Path:
    if override:
        p = Path(override)
        p = p if p.is_absolute() else store.dir / p
        if not p.is_file():
            raise SystemExit(f"音频不存在：{p}")
        return p
    m = st.get("media") or {}
    for key in ("vocals", "timing_audio", "audio"):
        rel = m.get(key)
        if rel:
            p = store.abs(rel)
            if p.is_file():
                return p
    raise SystemExit("找不到可用作波形的音频（media.vocals / timing_audio / audio 都不存在）")


def _line_data(li: int, sentence) -> dict | None:
    """Axis numbers for one line: per char ``s / e / cp / se`` in seconds."""
    from .sugbridge import _ruby_parts, char_windows

    wins = char_windows(sentence)
    chars = []
    for ch, (s, e, cps) in zip(sentence.characters, wins):
        chars.append({
            "c": ch.char,
            "r": "".join(_ruby_parts(ch)),
            "s": None if s is None else round(s, 3),
            "e": None if e is None else round(e, 3),
            "cp": [round(t, 3) for t in cps],
            "se": bool(ch.is_sentence_end and ch.sentence_end_ts is not None),
        })
    timed = [c for c in chars if c["cp"]]
    if not timed:
        return None
    return {
        "i": li,
        "text": sentence.text if hasattr(sentence, "text") else "".join(c["c"] for c in chars),
        "start": timed[0]["cp"][0],
        "end": max(c["e"] for c in timed if c["e"] is not None),
        "chars": chars,
    }


def _stft_frames(mono: np.ndarray, hop: int, nfft: int) -> np.ndarray:
    """Magnitude STFT with a hann window, computed in chunks (low memory)."""
    win = np.hanning(nfft).astype(np.float32)
    total = 1 + max(0, (len(mono) - nfft) // hop)
    out = np.zeros((total, nfft // 2 + 1), dtype=np.float32)
    step = 4000
    for s in range(0, total, step):
        e = min(total, s + step)
        idx = np.arange(nfft)[None, :] + hop * np.arange(s, e)[:, None]
        out[s:e] = np.abs(np.fft.rfft(mono[idx] * win, axis=1)).astype(np.float32)
    return out


def _freq_edges(f_lo: float, f_hi: float, h: int, n_bins: int, nyq: float) -> list[tuple[int, int]]:
    """Log-frequency display rows (top row = highest) → source bin ranges."""
    ratio = (f_hi / f_lo) ** (1.0 / h)
    out: list[tuple[int, int]] = []
    for k in range(h):
        f1 = f_hi / (ratio ** k)
        f0 = f_hi / (ratio ** (k + 1))
        b0 = int(math.floor(f0 / nyq * n_bins))
        b1 = int(math.ceil(f1 / nyq * n_bins))
        b0 = max(0, min(n_bins - 1, b0))
        b1 = max(b0 + 1, min(n_bins, b1))
        out.append((b0, b1))
    return out


def _freq_y(f: float, y0: int, y1: int) -> float:
    """Frequency → y on the log axis (y0 = top, y1 = bottom)."""
    t = math.log(max(f, F_LO_HZ) / F_LO_HZ) / math.log(F_HI_HZ / F_LO_HZ)
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return y1 - t * (y1 - y0)


def _pitch_track(mono: np.ndarray, hop: int) -> np.ndarray:
    """Autocorrelation pitch contour (Hz, NaN = unvoiced), one value per hop.

    A 40 ms window (vibrato-friendly), peak of the normalised autocorrelation
    inside 70–1000 Hz, earliest strong local peak wins (octave guard), with a
    parabolic refine.  Noise / silence stay NaN.
    """
    n_win = int(round(PITCH_WIN_S * SR))
    if len(mono) < n_win:
        return np.empty(0, dtype=np.float64)
    total = 1 + (len(mono) - n_win) // hop
    out = np.full(total, np.nan, dtype=np.float64)
    lag_lo = max(2, int(math.floor(SR / PITCH_HI_HZ)))
    lag_hi = min(n_win - 1, int(math.ceil(SR / PITCH_LO_HZ)) + 2)
    win = np.hanning(n_win).astype(np.float32)
    lags = np.arange(lag_lo, lag_hi)
    step = 3000
    for s in range(0, total, step):
        e = min(total, s + step)
        idx = np.arange(n_win)[None, :] + hop * np.arange(s, e)[:, None]
        x = mono[idx].astype(np.float32)
        rms = np.sqrt((x ** 2).mean(axis=1) + 1e-16)
        x = (x - x.mean(axis=1, keepdims=True)) * win
        X = np.fft.rfft(x, n=2 * n_win, axis=1)
        ac = np.fft.irfft(X.real ** 2 + X.imag ** 2, n=2 * n_win, axis=1)[:, :lag_hi]
        norm = ac / np.maximum(ac[:, :1], 1e-12)
        seg = norm[:, lag_lo:lag_hi]
        k = np.argmax(seg, axis=1)
        vmax = seg[np.arange(len(k)), k]
        thr = vmax * 0.88                       # octave guard: earliest strong peak
        le = np.zeros_like(seg, dtype=bool)
        le[:, 1:] = seg[:, 1:] >= seg[:, :-1]
        ri = np.zeros_like(seg, dtype=bool)
        ri[:, :-1] = seg[:, :-1] >= seg[:, 1:]
        cand = le & ri & (seg >= thr[:, None])
        kk = np.where(cand.any(axis=1), np.argmax(cand, axis=1), k)
        a = np.clip(kk - 1, 0, seg.shape[1] - 1)
        b = np.clip(kk + 1, 0, seg.shape[1] - 1)
        rows = np.arange(len(kk))
        y0_, y1_, y2_ = seg[rows, a], seg[rows, kk], seg[rows, b]
        den = y0_ - 2.0 * y1_ + y2_
        d = np.where(np.abs(den) > 1e-9, 0.5 * (y0_ - y2_) / den, 0.0)
        d = np.clip(d, -1.0, 1.0)
        f0 = SR / np.maximum(lags[kk].astype(np.float64) + d, 1e-6)
        good = (vmax >= PITCH_MIN_R) & (rms > 2e-3)
        out[s:e] = np.where(good, f0, np.nan)
    return out


def _font(size: int, bold: bool = False):
    from PyQt6.QtGui import QFont

    f = QFont()
    try:
        f.setFamilies(["Microsoft YaHei UI", "Yu Gothic UI", "Meiryo UI", "Segoe UI"])
    except Exception:
        f.setFamily("Microsoft YaHei UI")
    f.setPixelSize(size)
    f.setBold(bold)
    return f


# -------------------------------------------------------------------- drawing
def _panel_rgb(spec_db: np.ndarray, slo: float, shi: float, edges, w0: float,
               hop_s: float, pps: float, ww: int) -> np.ndarray:
    n = spec_db.shape[0]
    fidx = np.clip(((w0 + (np.arange(ww) + 0.5) / pps) / hop_s).astype(np.int64), 0, max(0, n - 1))
    src = spec_db[fidx]
    rows = np.empty((len(edges), ww), dtype=np.float32)
    for k, (b0, b1) in enumerate(edges):
        rows[k] = src[:, b0:b1].max(axis=1)
    t = np.clip((rows - slo) / max(1e-6, shi - slo), 0.0, 1.0)
    return _magma_lut()[(t * 255).astype(np.uint8)]


def _draw_line(data: dict, env: dict, total: int, out_path: Path) -> None:
    from .render import qt_app

    qt_app()
    from PyQt6.QtCore import QPoint, QPointF, Qt
    from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QPolygon, QPolygonF

    db, hop_s = env["db"], env["hop_s"]
    thr, lo, hi = env["thr"], env["lo"], env["hi"]
    ndb = len(db)

    w0, w1 = data["window"]
    dur = max(0.1, w1 - w0)
    W = int(min(W_MAX, max(W_MIN, round(dur * PX_PER_S) + ML + MR)))
    pps = (W - ML - MR) / dur
    ww = W - ML - MR

    def X(t: float) -> float:
        return ML + (t - w0) * pps

    img = QImage(W, H, QImage.Format.Format_RGB32)
    img.fill(QColor(255, 255, 255))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

    def text(x: float, y: float, s: str, color, size: int = 12, center: bool = True) -> None:
        f = _font(size)
        p.setFont(f)
        tw = p.fontMetrics().horizontalAdvance(s)
        xx = x - tw / 2 if center else x
        xx = max(2.0, min(W - 2.0 - tw, xx))
        p.setPen(QColor(*color))
        p.drawText(int(round(xx)), int(round(y)), s)

    def stroke_pitch(pts: list[QPointF]) -> None:
        if len(pts) < 2:
            return
        poly = QPolygonF(pts)
        p.setPen(QPen(QColor(8, 10, 18, 120), 3.4))
        p.drawPolyline(poly)
        p.setPen(QPen(QColor(255, 255, 255, 235), 1.5))
        p.drawPolyline(poly)

    # --- spectrogram panels (numpy → RGB → QImage) -------------------------
    for (y0, y1), (spec_db, slo, shi), edges in (
            ((Y_WIDE0, Y_WIDE1), env["wide"], env["edges_wide"]),
            ((Y_HARM0, Y_HARM1), env["harm"], env["edges_harm"])):
        rgb = _panel_rgb(spec_db, slo, shi, edges, w0, hop_s, pps, ww)
        buf = rgb.tobytes()
        q = QImage(buf, ww, y1 - y0, 3 * ww, QImage.Format.Format_RGB888)
        p.drawImage(ML, y0, q)
    text(ML + 5, Y_WIDE0 + 13, "宽带 10ms（辅音起音）", (235, 235, 235, 185), 10, center=False)
    text(ML + 5, Y_HARM0 + 13, "谐波 64ms（元音/尾音）· 白线=音高", (235, 235, 235, 185), 10, center=False)
    # log-frequency guides, labelled at the left edge
    p.setFont(_font(9))
    for fk in GUIDE_HZ:
        lbl = f"{fk // 1000}k" if fk % 1000 == 0 else f"{fk}"
        for y0, y1 in ((Y_WIDE0, Y_WIDE1), (Y_HARM0, Y_HARM1)):
            y = int(round(_freq_y(fk, y0, y1)))
            p.setPen(QPen(QColor(255, 255, 255, 46), 1))
            p.drawLine(ML, y, W - MR, y)
            p.setPen(QColor(255, 255, 255, 135))
            p.drawText(ML + 3, y - 2, lbl)

    # --- ruler -------------------------------------------------------------
    t0r = math.ceil(w0 * 10 - 1e-6) / 10
    while t0r <= w1 + 1e-6:
        x = X(t0r)
        major = abs(t0r * 2 - round(t0r * 2)) < 1e-6       # 0.5 s grid
        if major:
            p.setPen(QPen(QColor(180, 186, 194), 1))
            p.drawLine(int(x), Y_RULER - 10, int(x), Y_RULER)
            text(x, Y_RULER - 15, f"{t0r:.1f}", (110, 116, 124), 11)
        else:
            p.setPen(QPen(QColor(222, 226, 231), 1))
            p.drawLine(int(x), Y_RULER - 5, int(x), Y_RULER)
        t0r = round(t0r + 0.1, 6)
    p.setPen(QPen(QColor(180, 186, 194), 1))
    p.drawLine(ML, Y_RULER, W - MR, Y_RULER)

    chars = data["chars"]
    timed = [(i, c) for i, c in enumerate(chars) if c["cp"]]
    last_timed = max(i for i, c in timed)

    # --- neighbour boundaries (context: whose voice is that at the edges?) --
    dash = QPen(QColor(135, 135, 135), 1)
    dash.setStyle(Qt.PenStyle.DashLine)
    if data.get("next_start") is not None and data["next_start"] <= w1 + 0.05:
        x = X(data["next_start"])
        p.setPen(dash)
        p.drawLine(int(x), Y_RULER + 2, int(x), Y_WIPE1)
        text(x, Y_EVENT, f"下行 {data['next_start']:.3f}", (125, 125, 125), 12)
    if data.get("prev_end") is not None and data["prev_end"] >= w0 - 0.05:
        x = X(data["prev_end"])
        p.setPen(dash)
        p.drawLine(int(x), Y_RULER + 2, int(x), Y_WIPE1)
        text(x, Y_LANE_A, f"上行止 {data['prev_end']:.3f}", (125, 125, 125), 11)

    # --- line start / release / breath -------------------------------------
    p.setPen(QPen(QColor(*C_START), 2))
    x = X(data["start"])
    p.drawLine(int(x), Y_RULER + 2, int(x), Y_WIPE1)
    text(x, Y_EVENT, f"起 {data['start']:.3f}", C_START, 13)
    for i, c in timed:
        if not c["se"] or c["e"] is None:
            continue
        x = X(c["e"])
        p.setPen(QPen(QColor(*C_END), 2))
        p.drawLine(int(x), Y_RULER + 2, int(x), Y_WIPE1)
        kind = "收尾" if i == last_timed else "换气"
        text(x, Y_EVENT, f"{kind} {c['e']:.3f}", C_END, 13)

    # --- character ticks + staggered labels --------------------------------
    for k, (i, c) in enumerate(timed):
        x = X(c["cp"][0])
        p.setPen(QPen(QColor(*C_AXIS, 160), 1))
        p.drawLine(int(x), Y_LANE_B + 2, int(x), Y_WIDE0)
        p.setPen(QPen(QColor(*C_AXIS_SPEC, 150), 1))
        p.drawLine(int(x), Y_WIDE0, int(x), Y_WIDE1)
        p.drawLine(int(x), Y_HARM0, int(x), Y_HARM1)
        p.setPen(QPen(QColor(*C_AXIS, 160), 1))
        p.drawLine(int(x), Y_HARM1, int(x), Y_WIPE1)
        prev_x = X(timed[k - 1][1]["cp"][0]) if k else ML
        dx = x - prev_x if k else 999
        t_s = c["cp"][0]
        if dx >= 104:
            label = f"{i + 1}{c['c']} {t_s:.3f}"
        elif dx >= 64:
            label = f"{i + 1} {t_s:.3f}"
        elif dx >= 42:
            label = f"{i + 1} {t_s:.2f}"
        else:
            label = f"{i + 1}"
        text(x, Y_LANE_A if k % 2 == 0 else Y_LANE_B, label, (47, 88, 141), 12)

    # --- pitch contour on the harmonic panel -------------------------------
    pitch = env["pitch"]
    gate = env.get("pitch_gate", -1e9)
    if len(pitch):
        i0 = max(0, int((w0 - 0.005) / hop_s))
        i1 = min(len(pitch), int((w1 + 0.005) / hop_s) + 1)
        pts: list[QPointF] = []
        for i in range(i0, i1):
            t = i * hop_s
            f = pitch[i]
            di = min(ndb - 1, max(0, int(round(t / hop_s))))
            if math.isfinite(f) and db[di] >= gate:
                pts.append(QPointF(X(t), _freq_y(float(f), Y_HARM0, Y_HARM1)))
            else:
                stroke_pitch(pts)
                pts = []
        stroke_pitch(pts)

    # --- wipe bar ----------------------------------------------------------
    for k, (i, c) in enumerate(timed):
        x1 = X(c["cp"][0])
        x2 = X(c["e"]) if c["e"] is not None else x1 + 3
        if x2 - x1 < 3:
            x2 = x1 + 3
        x1 = max(ML, min(W - MR - 1, x1))
        x2 = max(x1 + 1, min(W - MR, x2))
        fill = QColor(232, 240, 250) if k % 2 == 0 else QColor(219, 232, 247)
        p.setPen(QPen(QColor(157, 184, 217), 1))
        p.setBrush(fill)
        p.drawRect(int(x1), Y_WIPE0, int(x2 - x1), Y_WIPE1 - Y_WIPE0)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if x2 - x1 >= 14:
            text((x1 + x2) / 2, Y_WIPE1 - 6, str(i + 1), (90, 122, 158), 9)

    # --- dB energy curve (500–4000 Hz vocal band) + threshold --------------
    pts2: list[QPointF] = []
    for xx in range(ML, W - MR + 1):
        ta = w0 + (xx - ML) / pps
        ia = min(ndb - 1, max(0, int(ta / hop_s)))
        ib = min(ndb, max(ia + 1, int((ta + 1.0 / pps) / hop_s)))
        v = float(db[ia:ib].max())
        ratio = (v - lo) / max(1e-6, hi - lo)
        ratio = 0.0 if ratio < 0 else (1.0 if ratio > 1 else ratio)
        pts2.append(QPointF(xx, Y_EN1 - ratio * (Y_EN1 - Y_EN0)))
    poly = QPolygonF(pts2)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(*C_WAVE, 70))
    p.drawPolygon(QPolygonF(pts2 + [QPointF(W - MR, Y_EN1), QPointF(ML, Y_EN1)]))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(*C_WAVE), 2))
    p.drawPolyline(poly)
    ratio = (thr - lo) / max(1e-6, hi - lo)
    ratio = 0.0 if ratio < 0 else (1.0 if ratio > 1 else ratio)
    y_thr = Y_EN1 - ratio * (Y_EN1 - Y_EN0)
    pen = QPen(QColor(*C_THR), 1)
    pen.setStyle(Qt.PenStyle.DashLine)
    p.setPen(pen)
    p.drawLine(ML, int(y_thr), W - MR, int(y_thr))
    p.setPen(QPen(QColor(214, 221, 229), 1))
    p.drawLine(ML, Y_EN1, W - MR, Y_EN1)

    # --- onset triangles (voiced run starts inside the window) -------------
    voiced = db > thr
    ia0 = max(0, int(w0 / hop_s) - 3)
    ib0 = min(ndb, int(w1 / hop_s) + 1)
    run = 0
    for k in range(ia0, ib0):
        if voiced[k]:
            if run == 0:
                begin = k
            run += 1
            if run == 3 and w0 <= begin * hop_s <= w1:
                x = int(X(begin * hop_s))
                p.setPen(QPen(QColor(*C_ONSET), 1))
                p.setBrush(QColor(*C_ONSET))
                p.drawPolygon(QPolygon([QPoint(x - 4, Y_EN1 - 1), QPoint(x + 4, Y_EN1 - 1),
                                        QPoint(x, Y_EN1 - 8)]))
                p.setBrush(Qt.BrushStyle.NoBrush)
        else:
            run = 0

    # --- footer ------------------------------------------------------------
    text(ML, Y_FOOT, f"行 {data['i'] + 1}/{total} · {data['text']}", (38, 38, 38), 13, center=False)
    foot2 = (f"窗口 {w0:.2f}–{w1:.2f} · 起 {data['start']:.3f} · 止 {data['end']:.3f} "
             f"· {len(timed)} 字 · 人声覆盖 {data['voiced']:.0%} · 对数 50Hz–5.5kHz "
             f"· 能量带 500–4000Hz · 音源 {env['audio_name']}")
    text(ML, Y_FOOT + 16, foot2, (85, 85, 85), 11, center=False)
    text(ML, Y_FOOT + 32, "青线=字亮起 · 绿=行起 · 红=收尾/换气 · 灰虚=上行止/下行起 · 黄虚=人声门限 · △=起唱点 · 白线=音高(自相关)",
         (150, 150, 150), 10, center=False)

    p.end()
    img.save(str(out_path))


# ------------------------------------------------------------------- command
def cmd_wavecheck(args) -> int:
    from . import sugbridge
    from .render import qt_app

    store = JobStore(args.job)
    st = store.load()
    sug_path = store.dir / "timing" / "project.sug"
    if not sug_path.is_file():
        raise SystemExit("还没有打轴结果（timing/project.sug 不存在）——先跑 km timing")

    audio = _audio_path(store, st, getattr(args, "audio", None))
    hop_s = float(getattr(args, "hop", None) or 0.01)
    mono = media.load_mono(audio, SR)
    if mono.size == 0:
        raise SystemExit(f"音频为空：{audio}")
    hop = max(1, int(round(SR * hop_s)))
    hop_s = hop / SR

    mag_wide = _stft_frames(mono, hop, NFFT_WIDE)
    mag_harm = _stft_frames(mono, hop, NFFT_HARM)
    wide_db = 20 * np.log10(mag_wide + 1e-6)
    harm_db = 20 * np.log10(mag_harm + 1e-6)

    # dB curve from the 500–4000 Hz vocal band (the separated vocal's core)
    b0 = int(BAND_LO_HZ / (SR / 2) * (NFFT_WIDE // 2 + 1))
    b1 = int(BAND_HI_HZ / (SR / 2) * (NFFT_WIDE // 2 + 1)) + 1
    pwr = (mag_wide[:, b0:b1].astype(np.float64) ** 2).sum(axis=1)
    db = 10 * np.log10(pwr + 1e-9)
    floor, peak = float(np.percentile(db, 20)), float(np.percentile(db, 98))
    thr = floor + (peak - floor) * 0.32

    wslo, wshi = (float(x) for x in np.percentile(wide_db[::4, ::2], [60, 99.8]))
    hslo, hshi = (float(x) for x in np.percentile(harm_db[::4, ::2], [60, 99.8]))

    pitch = _pitch_track(mono, hop)

    env = {
        "db": db, "hop_s": hop_s, "thr": thr, "lo": floor - 5.0, "hi": peak + 5.0,
        "wide": (wide_db, wslo, wshi), "harm": (harm_db, hslo, hshi),
        "edges_wide": _freq_edges(F_LO_HZ, F_HI_HZ, Y_WIDE1 - Y_WIDE0, NFFT_WIDE // 2 + 1, SR / 2),
        "edges_harm": _freq_edges(F_LO_HZ, F_HI_HZ, Y_HARM1 - Y_HARM0, NFFT_HARM // 2 + 1, SR / 2),
        "pitch": pitch, "pitch_gate": floor + 6.0, "audio_name": Path(audio).name,
    }

    project = sugbridge.load_project(sug_path)
    total = len(project.sentences)
    selected = _parse_lines(getattr(args, "lines", None), total)
    if not selected:
        raise SystemExit(f"没有匹配的行（共 {total} 行）")

    pad = getattr(args, "pad", None)
    pad = max(0.0, float(pad if pad is not None else 0.8))
    out_dir = Path(args.out) if getattr(args, "out", None) else store.path("timing", "wavecheck")
    out_dir.mkdir(parents=True, exist_ok=True)

    qt_app()
    meta, skipped = [], []
    for li in selected:
        data = _line_data(li, project.sentences[li])
        if data is None or data["end"] is None:
            skipped.append(li + 1)
            continue
        data["window"] = [max(0.0, data["start"] - pad), data["end"] + pad]
        prev_d = _line_data(li - 1, project.sentences[li - 1]) if li > 0 else None
        nxt_d = _line_data(li + 1, project.sentences[li + 1]) if li + 1 < total else None
        data["prev_end"] = None if prev_d is None else prev_d["end"]
        data["next_start"] = None if nxt_d is None else nxt_d["start"]
        # 邻行边界在 2.5 s 内就自动纳进窗口——尾巴 / 起唱复核直接能在图里做
        lo, hi = data["window"]
        if data["prev_end"] is not None and data["prev_end"] >= data["start"] - 2.5:
            lo = min(lo, max(0.0, data["prev_end"] - 0.35))
        if data["next_start"] is not None and data["next_start"] <= data["end"] + 2.5:
            hi = max(hi, data["next_start"] + 0.35)
        data["window"] = [lo, hi]
        a, b = int(data["start"] / hop_s), max(int(data["start"] / hop_s) + 1, int(data["end"] / hop_s))
        seg = db[a:min(len(db), b)]
        data["voiced"] = float((seg > thr).mean()) if len(seg) else 0.0
        name = f"line_{li + 1:02d}.png"
        _draw_line(data, env, total, out_dir / name)
        meta.append({"i": li, "file": name, "text": data["text"], "window": data["window"],
                     "start": data["start"], "end": data["end"], "voiced": round(data["voiced"], 3),
                     "prev_end": data["prev_end"], "next_start": data["next_start"],
                     "chars": data["chars"]})

    write_json(out_dir / "axis.json", {
        "audio": str(audio), "hop_s": hop_s, "threshold_db": round(thr, 2),
        "floor_db": round(floor, 2), "peak_db": round(peak, 2),
        "spectrogram": {"sr": SR, "log_axis": True, "f_lo_hz": F_LO_HZ, "f_hi_hz": F_HI_HZ,
                        "wide": {"n_fft": NFFT_WIDE, "window_ms": round(NFFT_WIDE / SR * 1000)},
                        "harm": {"n_fft": NFFT_HARM, "window_ms": round(NFFT_HARM / SR * 1000)},
                        "wide_db": [round(wslo, 2), round(wshi, 2)],
                        "harm_db": [round(hslo, 2), round(hshi, 2)]},
        "energy_band_hz": [BAND_LO_HZ, BAND_HI_HZ],
        "pitch": {"window_ms": round(PITCH_WIN_S * 1000), "min_hz": PITCH_LO_HZ,
                  "max_hz": PITCH_HI_HZ, "min_r": PITCH_MIN_R},
        "note": "上=宽频10ms（辅音起音）· 下=谐波64ms+音高白线 · 青线=字亮起(首个检查点) · 绿=行起 · "
                "红=收尾/换气 · 灰虚=上行止/下行起边界 · 黄虚=500–4000Hz人声门限 · △=起唱点；"
                "修正走 km edit（set_char / set_line_end / set_pause / shift_lines / shift_chars / set_line_span）",
        "lines": meta,
    })
    print(json.dumps({"out": str(out_dir), "audio": str(audio), "count": len(meta),
                      "skipped": skipped, "lines": meta}, ensure_ascii=False, indent=1))
    return 0
