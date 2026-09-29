"""Karaoke scan-line highlight painted at the moving wipe front."""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath, QTransform

from krok_helper.subtitle_render.domain.models import (
    SCANLINE_GLOBAL_ROLE_KEY,
    Style,
)
from krok_helper.subtitle_render.domain.paint import (
    KaraokeColors,
    KaraokeColorState,
    PaintFill,
)
from krok_helper.subtitle_render.engine.style.style_semantics import (
    effective_karaoke_colors,
)
from krok_helper.subtitle_render.engine.render.elements.horizontal.wipe import (
    fill_clip_band,
    segment_fill_ratio,
)
from krok_helper.subtitle_render.engine.render.effects import (
    main_stroke2_width,
    paint_fill_path,
    paint_stroke_path,
    stroke2_pen_width,
    stroke_pen_width,
)


@dataclass(frozen=True)
class ScanlineParams:
    """Resolved scan-line visual parameters shared by main text and ruby."""

    width_px: float
    color: str
    glow_px: int
    mode: str = "color"
    brightness: float = 0.6
    role_fill: PaintFill | None = None
    """``role`` 模式解析出的来源「走字后-主文字」填充；整条带（含描边）
    都用这一份填充重绘。``None`` = 回退单色。"""

    @property
    def half_width(self) -> float:
        return max(self.width_px, 1.0) / 2.0

    @property
    def is_brighten(self) -> bool:
        return self.mode == "brighten"

    @property
    def is_role(self) -> bool:
        return self.mode == "role" and self.role_fill is not None

    @property
    def follow_state(self) -> str | None:
        """``follow_before`` / ``follow_after`` 模式的固定态；其余为 ``None``。"""

        if self.mode == "follow_before":
            return "before"
        if self.mode == "follow_after":
            return "after"
        return None

    @property
    def solid_half_width(self) -> float:
        """Hard core left after the requested inner feather is removed."""

        return max(self.half_width - float(self.glow_px), 0.0)


def scanline_role_fill(style: Style) -> PaintFill | None:
    """Resolve the ``role``-mode fill: the named source's after-state text fill.

    Only the text fill is taken from the source; the whole glyph stack (stroke
    and second stroke included) is repainted with this one fill, matching the
    solid-colour mode's uniform-state structure. ``__global__`` resolves to
    the main style's own after-state text fill; other names must match a
    ``custom_style_schemes`` entry. Returns ``None`` unless the mode is active
    and the name resolves; callers fall back to the solid ``color``-mode
    paint in that case.
    """

    if style.scanline_mode != "role":
        return None
    name = str(style.scanline_role_name or "").strip()
    if not name:
        return None
    if name == SCANLINE_GLOBAL_ROLE_KEY:
        return effective_karaoke_colors(style).after.text
    scheme = style.custom_style_schemes.get(name)
    if scheme is None:
        return None
    return effective_karaoke_colors(scheme).after.text


def scanline_params_for_style(style: Style) -> ScanlineParams:
    """Return the scan-line parameters carried by one style."""

    mode = (
        style.scanline_mode
        if style.scanline_mode
        in {
            "color",
            "brighten",
            "follow_before",
            "follow_after",
            "role",
        }
        else "color"
    )
    return ScanlineParams(
        width_px=max(float(style.scanline_width_px), 1.0),
        color=style.scanline_color or "#FFFFFF",
        glow_px=max(int(style.scanline_glow_px), 0),
        mode=mode,
        brightness=min(max(int(style.scanline_brightness_pct), 0), 100) / 100.0,
        role_fill=scanline_role_fill(style),
    )


def scanline_band_rect(
    rect: QRectF,
    front: float,
    params: ScanlineParams,
    *,
    vertical: bool,
) -> QRectF:
    """Return the highlight band centred on the wipe front."""

    half = params.half_width
    if vertical:
        return QRectF(
            rect.left(),
            front - half,
            max(rect.width(), 1.0),
            half * 2.0,
        )
    return QRectF(
        front - half,
        rect.top(),
        half * 2.0,
        max(rect.height(), 1.0),
    )


def band_touches_rect(band: QRectF, rect: QRectF, pad: float) -> bool:
    return band.intersects(
        QRectF(
            rect.left() - pad,
            rect.top() - pad,
            rect.width() + pad * 2.0,
            rect.height() + pad * 2.0,
        )
    )


def scanline_feather_slices(
    rect: QRectF,
    front: float,
    params: ScanlineParams,
    *,
    vertical: bool,
) -> list[tuple[QRectF, float]]:
    """Approximate an inner-only soft band with non-overlapping alpha slices."""

    half = params.half_width
    softness = min(max(float(params.glow_px), 0.0), half)
    core = half - softness
    count = max(1, min(int(round(half * 2.0)), 64))
    step = half * 2.0 / count
    result: list[tuple[QRectF, float]] = []
    for index in range(count):
        offset = -half + index * step
        distance = abs(offset + step * 0.5)
        if softness <= 0.0 or distance <= core:
            alpha = 1.0
        else:
            progress = max(0.0, min((half - distance) / softness, 1.0))
            alpha = progress * progress * (3.0 - 2.0 * progress)
        if alpha <= 0.0:
            continue
        slice_rect = (
            QRectF(rect.left(), front + offset, rect.width(), step)
            if vertical
            else QRectF(front + offset, rect.top(), step, rect.height())
        )
        result.append((slice_rect, alpha))
    return result


def _solid_scanline_fill(color: str) -> PaintFill:
    return PaintFill(
        mode="solid",
        color=color,
        start_color=color,
        end_color=color,
        gradient_stops=[(0, color), (100, color)],
        split_top_color=color,
        split_bottom_color=color,
        split_stops=[(0, color), (100, color)],
    )


def _brighten_color_hsv(value: str, amount: float) -> str:
    """Raise HSV value without changing the source hue or saturation."""

    color = QColor(value)
    if not color.isValid():
        return value
    hue, saturation, brightness, alpha = color.getHsvF()
    brightness = brightness + (1.0 - brightness) * max(0.0, min(amount, 1.0))
    result = QColor()
    result.setHsvF(hue, saturation, brightness, alpha)
    return result.name(
        QColor.NameFormat.HexArgb if alpha < 1.0 else QColor.NameFormat.HexRgb
    )


def _brighten_fill_hsv(fill: PaintFill, amount: float) -> PaintFill:
    """Return a fill whose authored colours have a higher HSV value."""

    brighten = lambda value: _brighten_color_hsv(value, amount)
    return replace(
        fill,
        color=brighten(fill.color),
        start_color=brighten(fill.start_color),
        end_color=brighten(fill.end_color),
        gradient_stops=[
            (position, brighten(color)) for position, color in fill.gradient_stops
        ],
        split_top_color=brighten(fill.split_top_color),
        split_bottom_color=brighten(fill.split_bottom_color),
        split_stops=[
            (position, brighten(color)) for position, color in fill.split_stops
        ],
    )


def _brighten_state_hsv(state: KaraokeColorState, amount: float) -> KaraokeColorState:
    return KaraokeColorState(
        text=_brighten_fill_hsv(state.text, amount),
        stroke=_brighten_fill_hsv(state.stroke, amount),
        stroke2=_brighten_fill_hsv(state.stroke2, amount),
        shadow=_brighten_fill_hsv(state.shadow, amount),
    )


def _uniform_scanline_state(fill: PaintFill) -> KaraokeColorState:
    """One fill applied to every layer, like the solid state but with a fill."""

    return KaraokeColorState(text=fill, stroke=fill, stroke2=fill, shadow=fill)


def _solid_scanline_state(color: str) -> KaraokeColorState:
    fill = _solid_scanline_fill(color)
    return KaraokeColorState(text=fill, stroke=fill, stroke2=fill, shadow=fill)


def _state_clip(
    rect: QRectF, front: float, *, vertical: bool, rtl: bool, after: bool
) -> QRectF:
    """Clip one side of the moving front to its actual before/after colour."""

    after_is_far_side = rtl
    far_side = after == after_is_far_side
    extent = 1_000_000.0
    if vertical:
        return (
            QRectF(rect.left() - extent, front, rect.width() + extent * 2.0, extent)
            if far_side
            else QRectF(
                rect.left() - extent,
                -extent,
                rect.width() + extent * 2.0,
                front + extent,
            )
        )
    return (
        QRectF(front, rect.top() - extent, extent, rect.height() + extent * 2.0)
        if far_side
        else QRectF(
            -extent, rect.top() - extent, front + extent, rect.height() + extent * 2.0
        )
    )


def paint_scanline_strip(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    *,
    front: float,
    params: ScanlineParams,
    style: Style,
    vertical: bool = False,
    rtl: bool = False,
    colors: KaraokeColors | None = None,
    opacity: float = 1.0,
) -> None:
    """Paint one glyph path's scan-line highlight band centred at ``front``.

    With a feather radius (``scanline_glow_px``) the band is painted from a
    blurred silhouette of the glyph stack: the highlight eases out of the
    stroke outline by the blur radius while closed counters stay transparent
    (the blurred source keeps its holes, and the radius is small relative to
    counter sizes). With ``scanline_glow_px == 0`` the band repaints the exact
    glyph geometry, so nothing ever expands outside it.
    ``path`` must already be in device space when a utopia transform is active;
    pass the mapped ``front`` accordingly (see
    :func:`map_front_through_transform`).
    """

    stroke_width = style.stroke_width_px
    stroke2_width = main_stroke2_width(style)
    band = scanline_band_rect(rect, front, params, vertical=vertical)
    if not band_touches_rect(band, path.boundingRect(), 0.0):
        return
    if params.is_brighten and params.brightness <= 0.0:
        return
    opacity = max(0.0, min(opacity, 1.0))
    if opacity <= 0.0:
        return
    follow_state = params.follow_state
    if params.is_brighten and colors is not None:
        states = (
            (False, _brighten_state_hsv(colors.before, params.brightness)),
            (True, _brighten_state_hsv(colors.after, params.brightness)),
        )
    elif follow_state is not None and colors is not None:
        # 跟随字体：与底色发光同一条通路（当前行实际配色、全层重绘、共用
        # 亮度提升），但整条带固定用一态——锋面前侧被提前染成走字后色
        # （follow_after）或后侧暂回走字前色（follow_before）。亮度 0 =
        # 原样颜色，依旧有视觉差异，因此不做 brighten 那种零亮度跳过。
        state = _brighten_state_hsv(
            colors.after if follow_state == "after" else colors.before,
            params.brightness,
        )
        states = ((False, state), (True, state))
    else:
        solid = _solid_scanline_state(params.color)
        states = ((False, solid), (True, solid))
    if params.is_role:
        # 复用配色方案：取来源「走字后-主文字」这一份填充，整条带（含描边
        # 在内的全层）统一用它重绘——与单独颜色模式的全层同色结构一致。
        state = _uniform_scanline_state(params.role_fill)
        states = ((False, state), (True, state))
    if params.glow_px > 0:
        _paint_adjustment_strip(
            painter,
            path,
            rect,
            front=front,
            params=params,
            style=style,
            vertical=vertical,
            rtl=rtl,
            opacity=opacity,
            layer_states=states,
        )
        return
    for after, state in states:
        painter.save()
        try:
            painter.setOpacity(max(painter.opacity() * opacity, 0.0))
            painter.setClipRect(
                _state_clip(rect, front, vertical=vertical, rtl=rtl, after=after)
            )
            _paint_strip_body(
                painter,
                path,
                rect,
                band=band,
                state=state,
                params=params,
                style=style,
                stroke_width=stroke_width,
                stroke2_width=stroke2_width,
                vertical=vertical,
                front=front,
            )
        finally:
            painter.restore()


def _paint_adjustment_strip(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    *,
    front: float,
    params: ScanlineParams,
    style: Style,
    vertical: bool,
    opacity: float,
    layer_states: list[tuple[bool, KaraokeColorState]] | None,
    rtl: bool = False,
) -> None:
    """Adjustment-layer band: colour crossfade eased by a continuous mask.

    PR-style mask feather: the band is an adjustment whose strength fades
    with a continuous smoothstep along the sweep axis. Every pixel's **RGB**
    lerps between the original and the repainted stack while its **alpha
    stays exactly the original's** — a feathered adjustment never changes
    opacity, so translucent source colours cannot make the band look more
    transparent than the surrounding glyphs (the repaint's own alpha would
    otherwise drag it down: the original composites extra layers beneath the
    glyph). Anti-aliased edges keep their coverage; closed counters stay
    transparent; nothing expands outside the glyph geometry.
    """

    device = painter.device()
    if not isinstance(device, QImage):
        return
    stroke_width = style.stroke_width_px
    stroke2_width = main_stroke2_width(style)
    stroke_pad = float(max(stroke_width, stroke2_width) + 2)
    bounds = path.boundingRect()
    band = scanline_band_rect(rect, front, params, vertical=vertical)
    layer_rect = band.intersected(
        bounds.adjusted(-stroke_pad, -stroke_pad, stroke_pad, stroke_pad)
    ).toAlignedRect()
    if layer_rect.isEmpty():
        return
    width = max(layer_rect.width(), 1)
    height = max(layer_rect.height(), 1)

    # A：扫字线版（不透明全层），前后态按锋面分色（role 两侧同 state）。
    adjusted = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    adjusted.fill(Qt.GlobalColor.transparent)
    offset_x = float(layer_rect.left())
    offset_y = float(layer_rect.top())
    local_path = QPainterPath(path)
    local_path.translate(-offset_x, -offset_y)
    local_rect = rect.translated(-offset_x, -offset_y)
    local_front = front - (offset_y if vertical else offset_x)
    source_painter = QPainter(adjusted)
    try:
        source_painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if layer_states is not None:
            for after, state in layer_states:
                source_painter.save()
                try:
                    source_painter.setClipRect(
                        _state_clip(
                            local_rect,
                            local_front,
                            vertical=vertical,
                            rtl=rtl,
                            after=after,
                        )
                    )
                    if stroke2_width > 0:
                        paint_stroke_path(
                            source_painter,
                            local_path,
                            state.stroke2,
                            local_rect,
                            stroke2_pen_width(stroke_width, stroke2_width),
                        )
                    if stroke_width > 0:
                        paint_stroke_path(
                            source_painter,
                            local_path,
                            state.stroke,
                            local_rect,
                            stroke_pen_width(stroke_width),
                        )
                    paint_fill_path(source_painter, local_path, state.text, local_rect)
                finally:
                    source_painter.restore()
    finally:
        source_painter.end()

    # D：同区域的已合成原画面（含原始字形与下层内容）。混合模式只在
    # 预乘格式上定义良好，目标画布（RGBA8888 直通）必须先转换。
    original = device.copy(layer_rect).convertToFormat(
        QImage.Format.Format_ARGB32_Premultiplied
    )

    # 颜色 crossfade、alpha 保持原画面（半透明配色不会把带内压得更透明）。
    composite = _crossfade_keep_alpha(
        adjusted, original, params, local_front, vertical, opacity
    )
    painter.save()
    try:
        painter.setClipRect(layer_rect)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(layer_rect, Qt.GlobalColor.transparent)
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceOver
        )
        painter.drawImage(QPointF(offset_x, offset_y), composite)
    finally:
        painter.restore()


def _bayer_matrix(size: int) -> np.ndarray:
    """Recursively build a Bayer ordered-dither matrix (power of two)."""

    matrix = np.array([[0.0]], dtype=np.float32)
    base = np.array([[0.0, 2.0], [3.0, 1.0]], dtype=np.float32)
    n = 1
    while n < size:
        n *= 2
        matrix = np.block(
            [
                [4 * matrix + base[0, 0], 4 * matrix + base[0, 1]],
                [4 * matrix + base[1, 0], 4 * matrix + base[1, 1]],
            ]
        )
    return matrix / float(n * n) - 0.5


_DITHER_MATRIX = _bayer_matrix(16)
"""Ordered-dither offsets in [-0.5, 0.5): break 8-bit quantisation plateaus
into high-frequency texture instead of visible banding. 16x8/16x16 keeps the
pattern's own periodicity below the eye's structure threshold even on the
gentlest falloff slopes and under preview scaling."""


def _feather_strength(
    count: int, front: float, params: ScanlineParams
) -> np.ndarray:
    """Per-index crossfade strength along the sweep axis (continuous)."""

    softness = min(max(float(params.glow_px), 0.0), params.half_width)
    core = params.half_width - softness
    columns = np.empty(count, dtype=np.float32)
    for index in range(count):
        distance = abs(index + 0.5 - front)
        if softness <= 0.0 or distance <= core:
            strength = 1.0
        else:
            progress = max(0.0, min((params.half_width - distance) / softness, 1.0))
            strength = progress * progress * (3.0 - 2.0 * progress)
        columns[index] = max(0.0, min(strength, 1.0))
    return columns


def _crossfade_keep_alpha(
    adjusted: QImage,
    original: QImage,
    params: ScanlineParams,
    front: float,
    vertical: bool,
    opacity: float,
) -> QImage:
    """RGB lerp between two premultiplied layers; alpha comes from original."""

    width, height = adjusted.width(), adjusted.height()

    def straight(image: QImage):
        bits = image.constBits()
        bits.setsize(image.sizeInBytes())
        stride = image.bytesPerLine() // 4
        px = np.frombuffer(
            bytes(bits), dtype=np.uint8
        ).reshape(image.height(), stride, 4)[:, :image.width()].astype(np.float32)
        premult = px / 255.0
        alpha = premult[:, :, 3]
        rgb = np.divide(
            premult[:, :, :3],
            alpha[:, :, None],
            out=np.zeros_like(premult[:, :, :3]),
            where=alpha[:, :, None] > 0.0,
        )
        return rgb, alpha

    adjust_rgb, adjust_alpha = straight(adjusted)
    original_rgb, original_alpha = straight(original)
    count = height if vertical else width
    columns = _feather_strength(count, front, params) * opacity
    np.clip(columns, 0.0, 1.0, out=columns)
    strength = (
        np.repeat(columns.astype(np.float32), width).reshape(height, width)
        if vertical
        else np.broadcast_to(columns.astype(np.float32), (height, width))
    )
    # 有序抖动：羽化低斜率区的 8bit 量化平台会显成一条条竖线，抖动把
    # 平台打散成人眼不敏感的高频纹理。
    tile = _DITHER_MATRIX.shape[0]
    dither = np.tile(
        _DITHER_MATRIX, ((height + tile - 1) // tile, (width + tile - 1) // tile)
    )[:height, :width]
    strength = np.clip(strength + dither / 255.0, 0.0, 1.0)
    # 重绘层未覆盖的像素（其 alpha 为 0）保持原画面。
    effective = strength * (adjust_alpha > 0.0)
    out_rgb = (
        original_rgb * (1.0 - effective[:, :, None])
        + adjust_rgb * effective[:, :, None]
    )
    out = np.empty((height, width, 4), dtype=np.uint8)
    out[:, :, :3] = np.clip(
        out_rgb * original_alpha[:, :, None] * 255.0 + 0.5, 0.0, 255.0
    )
    out[:, :, 3] = np.clip(original_alpha * 255.0 + 0.5, 0.0, 255.0)
    result = QImage(
        out.tobytes(), width, height, width * 4,
        QImage.Format.Format_ARGB32_Premultiplied,
    )
    return result.copy()


def _paint_strip_body(
    painter: QPainter,
    path: QPainterPath,
    rect: QRectF,
    *,
    band: QRectF,
    state: KaraokeColorState,
    params: ScanlineParams,
    style: Style,
    stroke_width: int,
    stroke2_width: int,
    vertical: bool,
    front: float,
) -> None:
    del band, style
    for slice_rect, alpha in scanline_feather_slices(
        rect, front, params, vertical=vertical
    ):
        painter.save()
        try:
            painter.setOpacity(painter.opacity() * alpha)
            painter.setClipRect(slice_rect)
            if stroke2_width > 0:
                paint_stroke_path(
                    painter,
                    path,
                    state.stroke2,
                    rect,
                    stroke2_pen_width(stroke_width, stroke2_width),
                )
            if stroke_width > 0:
                paint_stroke_path(
                    painter,
                    path,
                    state.stroke,
                    rect,
                    stroke_pen_width(stroke_width),
                )
            paint_fill_path(painter, path, state.text, rect)
        finally:
            painter.restore()


def map_front_through_transform(
    front: float,
    baseline_y: float,
    transform: QTransform | None,
) -> float:
    """Map a logical wipe-front x through a utopia character transform."""

    if transform is None or transform.isIdentity():
        return front
    return float(transform.map(QPointF(front, baseline_y)).x())


def main_scanline_front(
    segments: list,
    t_ms: int,
    rtl: bool,
) -> float | None:
    """Return the main-text wipe front, or ``None`` when no front is moving.

    ``None`` covers "nothing sung yet", "the whole line is complete" and the
    timing gaps in between: the highlight only exists while the front travels,
    so a front resting at a finished segment's endpoint between gaps draws
    nothing. Segment boundary frames (``t == start/end``) still count as
    travelling — the hand-off arrival keeps its band (相控口径，与逐单元注音
    扫字线 / GPU 的逐字相判定一致).
    """

    band = fill_clip_band(segments, t_ms, rtl)
    if band is None:
        return None
    if all(segment_fill_ratio(segment, t_ms) >= 1.0 for segment in segments):
        return None
    if not any(
        int(segment.start_ms) <= t_ms <= int(segment.end_ms)
        for segment in segments
    ):
        return None
    return float(band[0] if rtl else band[1])


__all__ = [
    "SCANLINE_GLOBAL_ROLE_KEY",
    "ScanlineParams",
    "band_touches_rect",
    "main_scanline_front",
    "map_front_through_transform",
    "paint_scanline_strip",
    "scanline_feather_slices",
    "scanline_band_rect",
    "scanline_params_for_style",
    "scanline_role_fill",
]
