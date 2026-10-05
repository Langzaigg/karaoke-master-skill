"""Frame-independent timing semantics for subtitle guide signals."""

from __future__ import annotations

from typing import Protocol

from krok_helper.subtitle_render.engine.layout.layout_context import _LAYOUT_PASS
from krok_helper.subtitle_render.engine.layout.page.plan import (
    page_plan_signature,
    section_head_line_indices,
)
from krok_helper.subtitle_render.engine.timing.timeline import DisplayLine
from krok_helper.subtitle_render.domain.models import Style
from krok_helper.subtitle_render.domain.timing import TimingTrack


class VisibleLinesResolver(Protocol):
    def __call__(
        self,
        track: TimingTrack,
        t_ms: int,
        style: Style,
        *,
        logical_w: int | None = None,
        logical_h: int | None = None,
    ) -> list[DisplayLine]: ...


def display_style_for_signal_window(style: Style) -> Style:
    """Return the style used to resolve signal-aware display windows."""
    # Signal lead is applied only to hosts by ``signal_host_context``;
    # no whole-style transformation is currently required.
    return style


def lit_signal_active(style: Style) -> bool:
    """Return whether a horizontal guide signal participates in layout."""
    return bool(style.lit_enabled or style.volume_enabled) and not style.vertical


def signal_head_context(
    track: TimingTrack,
    style: Style,
) -> frozenset[int] | None:
    """Return track indexes that own a signal, or ``None`` when disabled.

    这是**段首基线**（每段第一页第一行），不含行级挂载覆盖；分模块宿主
    集见 :func:`volume_signal_head_context` / :func:`lit_signal_head_context`，
    渲染/调度消费方一律走分模块口径或 :func:`signal_host_context` 并集。
    """
    if not lit_signal_active(style):
        return None
    cache = getattr(_LAYOUT_PASS, "signal_heads", None)
    key = None
    if cache is not None:
        key = (id(track), page_plan_signature(track), max(style.section_gap_ms, 0))
        hit = cache.get(key)
        if hit is not None:
            return hit
    heads = section_head_line_indices(
        track,
        style,
        section_gap_ms=max(style.section_gap_ms, 0),
    )
    if cache is not None:
        cache[key] = heads
        # The key contains id(track), so retain the owner for the pass.
        _LAYOUT_PASS.tracks.append(track)
    return heads


def _apply_head_overrides(
    base: frozenset[int],
    track: TimingTrack,
    field_name: str,
) -> frozenset[int]:
    """段首基线叠加行级挂载覆盖：True 强制挂、False 强制不挂、None 跟随默认。"""
    forced_off: set[int] = set()
    forced_on: set[int] = set()
    for index, line in enumerate(track.lines):
        override = getattr(line, field_name, None)
        if override is True:
            forced_on.add(index)
        elif override is False:
            forced_off.add(index)
    if not forced_on and not forced_off:
        return base
    return (base - forced_off) | forced_on


def _volume_module_active(style: Style) -> bool:
    """独立音量柱或 legacy ``lit_style="volume"`` 口径（用户看到的都是音量柱）。"""
    return bool(style.volume_enabled) or (
        bool(style.lit_enabled) and style.lit_style == "volume"
    )


def _shape_lamp_active(style: Style) -> bool:
    return bool(style.lit_enabled) and style.lit_style != "volume"


def volume_signal_head_context(
    track: TimingTrack,
    style: Style,
) -> frozenset[int] | None:
    """音量柱宿主行（段首基线 + ``volume_head_override`` 行级覆盖）。

    模块未启用或竖排返回 ``None``。legacy ``lit_style="volume"`` 工程的
    柱组也走本口径——用户视角它是同一个「音量柱特效」行开关。
    """
    if not lit_signal_active(style) or not _volume_module_active(style):
        return None
    base = signal_head_context(track, style)
    if base is None:
        return None
    cache = getattr(_LAYOUT_PASS, "signal_heads", None)
    key = None
    if cache is not None:
        key = (
            "volume",
            id(track),
            page_plan_signature(track),
            max(style.section_gap_ms, 0),
        )
        hit = cache.get(key)
        if hit is not None:
            return hit
    heads = _apply_head_overrides(base, track, "volume_head_override")
    if cache is not None:
        cache[key] = heads
        _LAYOUT_PASS.tracks.append(track)
    return heads


def lit_signal_head_context(
    track: TimingTrack,
    style: Style,
) -> frozenset[int] | None:
    """指示灯（形状灯）宿主行（段首基线 + ``lit_head_override`` 行级覆盖）。"""
    if not lit_signal_active(style) or not _shape_lamp_active(style):
        return None
    base = signal_head_context(track, style)
    if base is None:
        return None
    cache = getattr(_LAYOUT_PASS, "signal_heads", None)
    key = None
    if cache is not None:
        key = (
            "lit",
            id(track),
            page_plan_signature(track),
            max(style.section_gap_ms, 0),
        )
        hit = cache.get(key)
        if hit is not None:
            return hit
    heads = _apply_head_overrides(base, track, "lit_head_override")
    if cache is not None:
        cache[key] = heads
        _LAYOUT_PASS.tracks.append(track)
    return heads


def signal_host_context(
    track: TimingTrack,
    style: Style,
) -> frozenset[int] | None:
    """任一模块（音量柱/指示灯）的宿主行并集；两模块都无宿主返回 ``None``。

    显示行过滤（哪些行进入 signal 渲染管线）与显示窗口 lead 扩展都以
    本并集为准——某模块被行级覆盖关掉的行仍可能挂着另一个模块。
    """
    volume_heads = volume_signal_head_context(track, style)
    lit_heads = lit_signal_head_context(track, style)
    if volume_heads is None and lit_heads is None:
        return None
    return (volume_heads or frozenset()) | (lit_heads or frozenset())


def signal_lead_in_ms(
    style: Style,
    *,
    volume_host: bool = True,
    lit_host: bool = True,
) -> int:
    """How far before singing a configured signal must become visible.

    灯窗口实际起点 = line_start + offset − (duration − waiting)，即提前量
    为 duration − waiting − offset（与渲染端 resolve_signal_lit_groups /
    native signal_state 同口径）。waiting 是「倒计时提前结束」的保留段，
    要从总时长里扣除，不能加回去，否则文字会比灯早出现 2×waiting。

    ``volume_host`` / ``lit_host`` 描述**本行**实际挂载了哪些模块（行级
    挂载覆盖后两模块的宿主行可以不同）；缺省两者都算 = 旧行为（全部
    启用模块的最大提前量）。
    """
    leads = []
    if volume_host and _volume_module_active(style):
        if style.volume_enabled:
            leads.append(
                max(
                    0,
                    int(style.volume_duration_ms)
                    - max(int(style.volume_waiting_time_ms), 0)
                    - int(style.volume_time_offset_ms),
                )
            )
        else:
            # legacy lit_style="volume"：柱组时序存在 lit/signals 字段里。
            leads.append(
                max(
                    0,
                    int(style.signals_duration_ms)
                    - max(int(style.lit_waiting_time_ms), 0)
                    - int(style.lit_time_offset_ms),
                )
            )
    if lit_host and _shape_lamp_active(style):
        leads.append(
            max(
                0,
                int(style.signals_duration_ms)
                - max(int(style.lit_waiting_time_ms), 0)
                - int(style.lit_time_offset_ms),
            )
        )
    return max(leads, default=0)


def signal_host_lead_map(
    track: TimingTrack,
    style: Style,
) -> dict[int, int] | None:
    """宿主行 → 该行**实际挂载模块**的最大信号提前量；无宿主返回 ``None``。

    特效行开关让两模块宿主行可以不同（例如段首行强制关柱只挂灯），各行
    的显示窗口 lead 只按自己挂到的模块取提前量，不再整轨共用一个标量。
    """
    hosts = signal_host_context(track, style)
    if hosts is None:
        return None
    volume_heads = volume_signal_head_context(track, style)
    lit_heads = lit_signal_head_context(track, style)
    return {
        index: signal_lead_in_ms(
            style,
            volume_host=volume_heads is not None and index in volume_heads,
            lit_host=lit_heads is not None and index in lit_heads,
        )
        for index in hosts
    }


def resolve_signal_display_lines(
    track: TimingTrack,
    t_ms: int,
    style: Style,
    visible_lines: VisibleLinesResolver,
    *,
    logical_w: int | None = None,
    logical_h: int | None = None,
) -> list[DisplayLine]:
    """Filter visible candidates to hosts that own a guide signal.

    本函数是渲染层（Painter 的 signal_lines / native 的窗口过滤）获取
    灯组宿主行的唯一入口，只能按「模块是否启用」过滤。宿主行 = 音量柱/
    指示灯任一模块挂载行的并集（含行级覆盖）。时间偏移 ≥ 有效持续
    时间时信号只是从歌词起点开始（提前量 0，窗口不早于行起点），并非
    禁用；提前量只归显示窗口调度管（schedule 的 ``max(lead, signal_lead)``
    天然兼容 0），在这里按提前量短路会把灯组整体过滤掉。
    """

    if not lit_signal_active(style):
        return []
    signal_hosts = signal_host_context(track, style)
    if signal_hosts is None:
        return []
    index_of = {id(line): index for index, line in enumerate(track.lines)}
    return [
        item
        for item in visible_lines(
            track,
            t_ms,
            style,
            logical_w=logical_w,
            logical_h=logical_h,
        )
        if index_of.get(id(item.line)) in signal_hosts
    ]


__all__ = [
    "display_style_for_signal_window",
    "lit_signal_active",
    "lit_signal_head_context",
    "resolve_signal_display_lines",
    "signal_head_context",
    "signal_host_context",
    "signal_host_lead_map",
    "signal_lead_in_ms",
    "volume_signal_head_context",
]
