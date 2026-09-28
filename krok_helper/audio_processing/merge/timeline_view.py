"""音频合成页的「合成时间轴」画布：多剪辑条 + 时间刻度 + 播放头。

替代原先「选中一条换一条」的单波形视图：全部剪辑以可拖动的条目排在同一条
时间线上（条与条之间按拼接间隔留缝），整体效果一目了然。交互契约：

- 点按条目主体 → 选中（``clipSelected``），下方时间码框随之切换目标；
- 拖动条目主体 → 松手时按指针落点相对各条目中心的位置重排
  （``clipsReordered`` 携带新顺序）；
- 拖动条目左右边缘 → 调整该条首尾裁剪，拖动过程直接改剪辑对象实时预览，
  松手提交（``clipTrimChanged``，携带新 start / 新 end，end 为 None 表示
  到文件末尾）；
- 点按/拖动上方刻度尺 → 移动播放头（``playheadChanged``），供页面定位试听；
- 播放期间页面用 ``set_playhead(..., from_player=True)`` 回推进度（同普通
  写入，本控件不发信号，信号只在用户操作时发）。
"""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from krok_helper.audio_processing.merge.model import MergeClip

__all__ = ["ArrangementView"]

#: 拖动条目主体（重排）与命中条目的抓取半径（像素）。
_GRAB_RADIUS_PX = 6
#: 小于该拖动距离视为纯点击（选择/无操作），不尝试换位（像素）。
_REORDER_DRAG_THRESHOLD_PX = 6.0
#: 换位滞回：候选槽位必须比当前位置近这么多像素才允许换（防重叠区点击/微抖误换）。
_REORDER_HYSTERESIS_PX = 20.0
#: 选中条目裁剪把手的尺寸与命中膨胀（像素）。
_HANDLE_WIDTH = 12
_HANDLE_HEIGHT = 18
_HANDLE_GRAB_PAD = 4
#: 裁剪后片段至少保留的时长（秒），与 analysis/commands 共用口径。
_MIN_SELECTION_SECONDS = 0.05
_RULER_HEIGHT = 24
_BAR_V_MARGIN = 10
_BAR_RADIUS = 6
_LEFT_MARGIN = 8.0
#: 条目循环配色（页面剪辑徽章同款）。
DEFAULT_BAR_COLORS = ("#2F6BFF", "#F04452", "#2e9e5b", "#c07f1a")
_PLAYHEAD_COLOR = "#F04452"


def _tint(color: str, alpha: float) -> QColor:
    c = QColor(color)
    c.setAlpha(int(255 * alpha))
    return c


class ArrangementView(QWidget):
    """多剪辑合成时间轴。"""

    playheadChanged = pyqtSignal(float)
    clipSelected = pyqtSignal(object)
    clipsReordered = pyqtSignal(list)
    clipTrimChanged = pyqtSignal(object, float, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._clips: list[MergeClip] = []
        self._gap_seconds = 0.0
        self._selected: MergeClip | None = None
        self._playhead = 0.0
        self._bar_colors = DEFAULT_BAR_COLORS
        self._drag_kind = ""  # "" / "bar" / "edge-left" / "edge-right" / "playhead"
        self._drag_clip: MergeClip | None = None
        self._drag_start_x = 0.0
        self._drag_current_x = 0.0
        self._drag_trim_start = 0.0
        self._drag_trim_end: float | None = None
        #: 外置横向滚动条（内容比视口宽时由视图驱动其范围）。
        self._hscroll = None
        #: 当前平移偏移（像素，内容坐标系）。
        self._offset_px = 0.0
        self._scrollbar_syncing = False
        #: 缩放档（像素/秒）；None = 适应宽度（默认，随窗口重算）。
        self._zoom_pps: float | None = None
        self.setMinimumHeight(230)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

    def sizeHint(self):  # noqa: N802 - Qt API
        from PyQt6.QtCore import QSize

        # 无布局的 QWidget 默认 sizeHint 无效，包在 QScrollArea 里会把视口
        # 压成 ~100px；给一个像样的提示尺寸让滚动区正常撑开。
        return QSize(1000, 260)

    # ── 缩放与滚动 ─────────────────────────────────────────────

    def attach_scrollbar(self, scrollbar) -> None:
        """挂上外置横向滚动条：范围由本视图按内容宽度驱动；传 None 卸挂。"""
        if self._hscroll is not None:
            try:
                self._hscroll.valueChanged.disconnect(self._on_scrollbar_changed)
            except TypeError:
                pass
        self._hscroll = scrollbar
        if scrollbar is not None:
            scrollbar.valueChanged.connect(self._on_scrollbar_changed)
            self._sync_scrollbar()

    def _on_scrollbar_changed(self, value: int) -> None:
        if self._scrollbar_syncing:
            return
        self._offset_px = max(0.0, float(value))
        self.update()

    def _pan_maximum(self) -> float:
        return max(0.0, self._content_width() - float(self.width()))

    def _sync_scrollbar(self) -> None:
        maximum = int(self._pan_maximum())
        if self._offset_px > maximum:
            self._offset_px = float(maximum)
        if self._hscroll is None:
            return
        self._scrollbar_syncing = True
        try:
            self._hscroll.setRange(0, maximum)
            self._hscroll.setPageStep(max(1, int(float(self.width()) * 0.9)))
            self._hscroll.setSingleStep(60)
            self._hscroll.setValue(int(self._offset_px))
        finally:
            self._scrollbar_syncing = False

    def _set_offset(self, offset: float) -> None:
        maximum = self._pan_maximum()
        self._offset_px = min(max(0.0, offset), maximum)
        if self._hscroll is not None and not self._scrollbar_syncing:
            self._scrollbar_syncing = True
            try:
                self._hscroll.setValue(int(self._offset_px))
            finally:
                self._scrollbar_syncing = False
        self.update()

    def zoom_in(self) -> None:
        self._apply_zoom(self._effective_pps() * 1.25, anchor_x=self.width() / 2.0)

    def zoom_out(self) -> None:
        self._apply_zoom(self._effective_pps() / 1.25, anchor_x=self.width() / 2.0)

    def fit_width(self) -> None:
        """回到「适应宽度」档。"""
        self._zoom_pps = None
        self._set_offset(0.0)
        self._sync_scrollbar()
        self.update()

    def zoomed(self) -> bool:
        return self._zoom_pps is not None

    def _apply_zoom(self, target_pps: float, *, anchor_x: float) -> None:
        pps = max(1.0, min(4000.0, target_pps))
        anchor_time = self._x_to_time(anchor_x)
        self._zoom_pps = pps
        self._sync_scrollbar()
        # 锚点时间保持在同一屏幕横坐标：按新比例重算偏移。
        desired_x = _LEFT_MARGIN + anchor_time * pps
        self._set_offset(desired_x - anchor_x)

    def _content_width(self) -> float:
        return self._timeline_end() * self._effective_pps() + _LEFT_MARGIN * 2.0

    def _ensure_playhead_visible(self) -> None:
        playhead_x = self._time_to_x(self._playhead)
        width = float(self.width())
        if playhead_x < 24.0:
            self._set_offset(self._offset_px + playhead_x - width * 0.1)
        elif playhead_x > width - 24.0:
            self._set_offset(self._offset_px + playhead_x - width * 0.9)

    def wheelEvent(self, event) -> None:  # noqa: N802
        if not self._clips:
            event.ignore()
            return
        delta = event.angleDelta().y()
        if delta == 0:
            event.ignore()
            return
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            # Shift+滚轮：横向平移。
            self._set_offset(self._offset_px - delta * 0.6)
            event.accept()
            return
        factor = 1.25 if delta > 0 else (1 / 1.25)
        self._apply_zoom(self._effective_pps() * factor, anchor_x=event.position().x())
        event.accept()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_scrollbar()
        self.update()

    def set_clips(self, clips: list[MergeClip], *, gap_seconds: float) -> None:
        """同步剪辑与接缝参数；``gap_seconds`` 为负表示相邻条目重叠（交叉淡化）。"""
        self._clips = list(clips)
        self._gap_seconds = float(gap_seconds)
        if self._selected is not None and self._selected not in self._clips:
            self._selected = None
        self._drag_kind = ""
        self._drag_clip = None
        self._clamp_playhead()
        self._sync_scrollbar()
        self.update()

    def set_selected(self, clip: MergeClip | None) -> None:
        if self._selected is clip:
            return
        self._selected = clip
        self.update()

    def selected_clip(self) -> MergeClip | None:
        return self._selected

    def set_bar_colors(self, colors: tuple[str, ...]) -> None:
        self._bar_colors = tuple(colors) or DEFAULT_BAR_COLORS
        self.update()

    def set_playhead(self, seconds: float, *, from_player: bool = False) -> None:
        self._playhead = max(0.0, seconds)
        if from_player:
            self._ensure_playhead_visible()
        self.update()

    def playhead(self) -> float:
        return self._playhead

    def clip_count(self) -> int:
        return len(self._clips)

    def total_duration(self) -> float:
        return self._timeline_end()

    # ── 布局换算 ───────────────────────────────────────────────

    def _trimmed_duration(self, clip: MergeClip) -> float:
        return max(0.0, clip.effective_end() - clip.trim_start)

    def _timeline_end(self) -> float:
        spans = self._bar_spans()
        return spans[-1][1] if spans else 0.0

    def _step_between(self, prev_item: MergeClip, next_item: MergeClip) -> float:
        """相邻两条之间的时间轴步进：正=留缝，负=重叠。

        交叉淡化（负 gap）时按音频图同口径钳制：重叠不超过相邻两条时长各半。
        """
        if self._gap_seconds >= 0:
            return self._gap_seconds
        overlap = min(
            -self._gap_seconds,
            self._trimmed_duration(prev_item) / 2.0,
            self._trimmed_duration(next_item) / 2.0,
        )
        return -overlap

    def _plot_bounds(self) -> tuple[float, float]:
        width = max(20.0, float(self.width()) - _LEFT_MARGIN * 2.0)
        return _LEFT_MARGIN, width

    def _pixels_per_second(self) -> float:
        _left, width = self._plot_bounds()
        if self._zoom_pps is not None:
            return self._zoom_pps
        return max(0.5, width / max(0.5, self._timeline_end()))

    def _effective_pps(self) -> float:
        return self._pixels_per_second()

    def _bar_spans(self) -> list[tuple[float, float]]:
        spans: list[tuple[float, float]] = []
        cursor = 0.0
        for index, clip in enumerate(self._clips):
            duration = self._trimmed_duration(clip)
            spans.append((cursor, cursor + duration))
            if index + 1 < len(self._clips):
                cursor += duration + self._step_between(clip, self._clips[index + 1])
        return spans

    def _time_to_x(self, seconds: float) -> float:
        left, _width = self._plot_bounds()
        return left + seconds * self._pixels_per_second() - self._offset_px

    def _x_to_time(self, x: float) -> float:
        left, _width = self._plot_bounds()
        return max(0.0, (x - left + self._offset_px) / self._pixels_per_second())

    def _bar_rect(self, span: tuple[float, float]) -> tuple[float, float, float, float]:
        x0 = self._time_to_x(span[0])
        x1 = self._time_to_x(span[1])
        top = _RULER_HEIGHT + _BAR_V_MARGIN
        height = max(24.0, float(self.height()) - top - _BAR_V_MARGIN)
        return x0, top, max(2.0, x1 - x0), height

    def _handle_rects(self) -> dict[str, QRectF]:
        """选中条目的左右裁剪把手（凸出在条目上沿、时间刻度区域）。

        只有选中条目有把手——重叠接缝处想拖哪首就先点选哪首，
        避免误抓到相邻条目。
        """
        if self._selected is None or self._selected not in self._clips:
            return {}
        index = self._clips.index(self._selected)
        span = self._bar_spans()[index]
        x, top, width, _h = self._bar_rect(span)
        half = _HANDLE_WIDTH / 2.0
        top_y = float(top)
        return {
            "edge-left": QRectF(x - half, top_y - _HANDLE_HEIGHT + 2.0, _HANDLE_WIDTH, _HANDLE_HEIGHT),
            "edge-right": QRectF(x + width - half, top_y - _HANDLE_HEIGHT + 2.0, _HANDLE_WIDTH, _HANDLE_HEIGHT),
        }

    def _handle_hit(self, x: float, y: float) -> str:
        for kind, rect in self._handle_rects().items():
            hit = rect.adjusted(
                -_HANDLE_GRAB_PAD, -_HANDLE_GRAB_PAD, _HANDLE_GRAB_PAD, _HANDLE_GRAB_PAD
            )
            if hit.contains(x, y):
                return kind
        return ""

    def _bar_index_at(self, x: float) -> int:
        # 交叉淡化下相邻条目重叠，后画的条目在上层，从后往前命中。
        spans = self._bar_spans()
        for index in range(len(spans) - 1, -1, -1):
            x0, _t, width, _h = self._bar_rect(spans[index])
            if x0 - _GRAB_RADIUS_PX <= x <= x0 + width + _GRAB_RADIUS_PX:
                return index
        return -1

    def _in_ruler(self, y: float) -> bool:
        return y <= _RULER_HEIGHT

    def _clamp_playhead(self) -> None:
        self._playhead = min(self._playhead, self._timeline_end())

    # ── 交互 ────────────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        x, y = event.position().x(), event.position().y()
        if self._in_ruler(y):
            self._drag_kind = "playhead"
            self._apply_playhead_from_x(x)
            return
        # 选中条目的把手优先命中（重叠接缝处防误触：裁剪只认把手）。
        handle_kind = self._handle_hit(x, y)
        if handle_kind:
            self._drag_kind = handle_kind
            self._drag_clip = self._selected
            self._drag_start_x = x
            self._drag_current_x = x
            self._drag_trim_start = self._selected.trim_start
            self._drag_trim_end = self._selected.trim_end
            self.update()
            return
        index = self._bar_index_at(x)
        if index < 0:
            self._drag_kind = ""
            self._drag_clip = None
            if self._selected is not None:
                self._selected = None
                self.clipSelected.emit(None)
            self.update()
            return
        clip = self._clips[index]
        self._drag_kind = "bar"
        self._drag_clip = clip
        self._drag_start_x = x
        self._drag_current_x = x
        self._drag_trim_start = clip.trim_start
        self._drag_trim_end = clip.trim_end
        if self._selected is not clip:
            self._selected = clip
            self.clipSelected.emit(clip)
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        x, y = event.position().x(), event.position().y()
        if not self._drag_kind:
            if self._in_ruler(y):
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                return
            if self._handle_hit(x, y):
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                return
            index = self._bar_index_at(x)
            if index < 0:
                self.setCursor(Qt.CursorShape.ArrowCursor)
                return
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            return
        if self._drag_kind == "playhead":
            self._apply_playhead_from_x(x)
            return
        self._drag_current_x = x
        if self._drag_kind == "bar":
            self.update()
            return
        clip = self._drag_clip
        if clip is None:
            return
        delta = self._x_to_time(x) - self._x_to_time(self._drag_start_x)
        drag_end = clip.duration if self._drag_trim_end is None else self._drag_trim_end
        if self._drag_kind == "edge-left":
            clip.trim_start = min(
                max(0.0, self._drag_trim_start + delta),
                drag_end - _MIN_SELECTION_SECONDS,
            )
        else:
            end = min(
                max(drag_end + delta, self._drag_trim_start + _MIN_SELECTION_SECONDS),
                clip.duration,
            )
            clip.trim_end = None if end >= clip.duration - 1e-6 else end
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        kind, clip = self._drag_kind, self._drag_clip
        release_x = event.position().x()
        self._drag_kind = ""
        self._drag_clip = None
        if kind == "bar" and clip is not None:
            # 微量位移视为纯点击（选择已在按下时完成），不做换位判断——
            # 交叉淡化下其他条目的中心可能落在本条版图内，靠中心判定会
            # 让原地小拖动/点击抖动误触发换位。
            if abs(release_x - self._drag_start_x) >= _REORDER_DRAG_THRESHOLD_PX:
                others = [item for item in self._clips if item is not clip]
                insert_at = self._reorder_insert_index(
                    clip, others, release_x - self._drag_start_x
                )
                new_order = list(others)
                new_order.insert(insert_at, clip)
                if new_order != self._clips:
                    self._clips = new_order
                    self.clipsReordered.emit(list(new_order))
            self.update()
        elif kind in ("edge-left", "edge-right") and clip is not None:
            self.clipTrimChanged.emit(clip, clip.trim_start, clip.trim_end)
            self.update()

    def _reorder_insert_index(self, clip: MergeClip, others: list, drag_delta: float) -> int:
        """条目跟随模型：拖动位移把条目中心推到哪，就换到最近的槽位。

        交叉淡化下指针绝对位置贴哪个槽位中心是个糟糕的意图信号（两个布局
        的条目几乎占同一段横坐标，微小拖动就会"贴近"另一个槽位）。改为：
        幽灵中心 = 当前槽位中心 + 拖动位移，条目必须"物理挪"到候选槽位
        （比留在原位近出滞回量）才换位——原地小拖、点击抖动永远不换。
        """
        clip_order_pos = sum(
            1 for item in others if self._clips.index(item) < self._clips.index(clip)
        )

        def _dragged_center(order: list) -> float:
            cursor = 0.0
            for index, item in enumerate(order):
                duration = self._trimmed_duration(item)
                if item is clip:
                    return self._time_to_x(cursor + duration / 2.0)
                step = (
                    self._step_between(order[index], order[index + 1])
                    if index + 1 < len(order)
                    else 0.0
                )
                cursor += duration + step
            return self._time_to_x(cursor)

        current_order = others[:clip_order_pos] + [clip] + others[clip_order_pos:]
        ghost_center = _dragged_center(current_order) + drag_delta
        best_index = clip_order_pos
        best_dist = abs(drag_delta)  # 留在原位的代价就是位移本身
        for candidate in range(len(others) + 1):
            if candidate == clip_order_pos:
                continue
            order = others[:candidate] + [clip] + others[candidate:]
            distance = abs(_dragged_center(order) - ghost_center)
            if distance + _REORDER_HYSTERESIS_PX < best_dist:
                best_index = candidate
                best_dist = distance
        return best_index

    def _apply_playhead_from_x(self, x: float) -> None:
        seconds = min(max(0.0, self._x_to_time(x)), self._timeline_end())
        if abs(seconds - self._playhead) < 1e-6:
            return
        self._playhead = seconds
        self.playheadChanged.emit(seconds)
        self.update()

    # ── 绘制 ────────────────────────────────────────────────────

    def paintEvent(self, _event) -> None:  # noqa: N802
        from krok_helper.theme_workbench import palette

        p = palette()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), QColor(p.card_bg))
        if not self._clips:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QColor(p.text_hint))
            painter.setFont(QFont("Microsoft YaHei UI", 10))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, "拖入多条音频后，在这里排列剪辑并试听整体效果"
            )
            return

        self._draw_ruler(painter, p)
        top = _RULER_HEIGHT + _BAR_V_MARGIN
        lane_height = max(24.0, float(self.height()) - top - _BAR_V_MARGIN)
        painter.fillRect(
            int(_LEFT_MARGIN), int(top), int(self.width() - _LEFT_MARGIN * 2), int(lane_height),
            QColor(p.input_bg),
        )
        painter.setPen(QColor(p.table_border))
        painter.drawRect(
            int(_LEFT_MARGIN), int(top), int(self.width() - _LEFT_MARGIN * 2) - 1, int(lane_height) - 1
        )
        # 先画未拖动的条目，再画拖动中的（虚影浮在最上层）。
        dragging = self._drag_clip if self._drag_kind == "bar" else None
        spans = self._bar_spans()
        for index, clip in enumerate(self._clips):
            if clip is not dragging:
                self._draw_bar(painter, p, clip, spans[index], index, lane_height)
        if dragging is not None:
            self._draw_bar(painter, p, dragging, spans[self._clips.index(dragging)], self._clips.index(dragging), lane_height)
        self._draw_handles(painter, p)

        # 播放头（贯穿刻度尺与轨道）
        playhead_x = int(self._time_to_x(self._playhead))
        painter.setPen(QPen(QColor(_PLAYHEAD_COLOR), 2))
        painter.drawLine(playhead_x, 2, playhead_x, int(top + lane_height))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(_PLAYHEAD_COLOR))
        painter.drawPolygon(
            QPoint(playhead_x - 5, 2),
            QPoint(playhead_x + 5, 2),
            QPoint(playhead_x, 10),
        )

    def _draw_bar(
        self,
        painter: QPainter,
        p,
        clip: MergeClip,
        span: tuple[float, float],
        index: int,
        lane_height: float,
    ) -> None:
        color = self._bar_colors[index % len(self._bar_colors)]
        x, top, width, _h = self._bar_rect(span)
        rect = QRectF(x, float(top), width, float(lane_height))
        dragging = self._drag_kind == "bar" and self._drag_clip is clip
        if dragging:
            rect = rect.translated(self._drag_current_x - self._drag_start_x, 0.0)

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_tint(color, 0.16))
        painter.drawRoundedRect(rect, _BAR_RADIUS, _BAR_RADIUS)
        if dragging:
            painter.setPen(QPen(QColor(p.text_hint), 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(x, float(top), width, float(lane_height)), _BAR_RADIUS, _BAR_RADIUS)

        # 迷你波形：把裁剪区间内的峰值映射到条目内部。
        if clip.waveform is not None and width > 8:
            waveform = clip.waveform
            center_y = rect.center().y()
            usable = rect.height() * 0.36
            painter.setPen(QPen(QColor(color), 1))
            source_start = clip.trim_start
            source_span = max(1e-6, self._trimmed_duration(clip))
            px = int(rect.left()) + 1
            while px < int(rect.right()) - 1:
                fraction = (px - rect.left()) / rect.width()
                source_time = source_start + fraction * source_span
                if source_time >= waveform.duration:
                    break
                peak_index = int(source_time * waveform.peaks_per_second)
                if 0 <= peak_index < len(waveform.peaks):
                    amplitude = waveform.peaks[peak_index]
                    painter.drawLine(
                        px, int(center_y - amplitude * usable), px, int(center_y + amplitude * usable)
                    )
                px += 2

        if self._selected is clip and not dragging:
            painter.setPen(QPen(QColor(p.accent_primary), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), _BAR_RADIUS, _BAR_RADIUS)

        painter.setPen(QColor("#FFFFFF"))
        badge_font = QFont("Microsoft YaHei UI", 9)
        badge_font.setBold(True)
        painter.setFont(badge_font)
        painter.drawText(
            QRectF(rect.left() + 6, rect.top() + 2, 20.0, 16.0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            str(index + 1),
        )
        if width > 90:
            name_font = QFont("Microsoft YaHei UI", 8)
            painter.setFont(name_font)
            metrics = QFontMetrics(name_font)
            elided = metrics.elidedText(clip.path.name, Qt.TextElideMode.ElideRight, int(width - 34))
            painter.setPen(QColor(p.text_primary))
            painter.drawText(
                QRectF(rect.left() + 26, rect.bottom() - 16.0, width - 32, 14.0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                elided,
            )
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    def _draw_handles(self, painter: QPainter, p) -> None:
        """选中条目的左右裁剪把手：条目色实心块 + 白描边 + 连到条目边的竖线。"""
        rects = self._handle_rects()
        if not rects:
            return
        index = self._clips.index(self._selected)
        color = self._bar_colors[index % len(self._bar_colors)]
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for kind, rect in rects.items():
            bar_top = rect.bottom() - 2.0
            edge_x = rect.center().x()
            painter.setPen(QPen(QColor(p.card_bg), 2))
            painter.drawLine(int(edge_x), int(bar_top), int(edge_x), int(bar_top + 8.0))
            painter.setPen(QPen(QColor("#FFFFFF"), 1.5))
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(rect, 3.0, 3.0)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    def _draw_ruler(self, painter: QPainter, p) -> None:
        total = self._timeline_end()
        pixels_per_second = self._pixels_per_second()
        step = 1.0
        for candidate in (0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 30.0, 60.0, 120.0, 300.0):
            if candidate * pixels_per_second >= 72.0:
                step = candidate
                break
            step = candidate
        painter.setPen(QColor("#94a3b8"))
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        left, width = self._plot_bounds()
        tick = 0
        while tick * step <= total + 1e-9:
            seconds = tick * step
            x = int(self._time_to_x(seconds))
            if left <= x <= left + width:
                painter.drawLine(x, _RULER_HEIGHT - 5, x, _RULER_HEIGHT - 1)
                label = f"{seconds:.1f}s" if step < 10 else f"{seconds:.0f}s"
                painter.drawText(x + 2, _RULER_HEIGHT - 7, label)
            tick += 1
        _ = p
