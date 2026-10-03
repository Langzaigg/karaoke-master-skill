"""Thread-local lifetime for one immutable subtitle layout pass."""

from __future__ import annotations

from contextlib import contextmanager
from threading import local as thread_local


_LAYOUT_PASS = thread_local()


@contextmanager
def layout_pass():
    """Mark a re-entrant interval where track and style inputs stay immutable.

    Painter's measurement helpers share scratch maps during this interval.  The
    outermost exit releases every owner reference, while nested calls reuse the
    same per-thread maps.
    """

    depth = getattr(_LAYOUT_PASS, "depth", 0)
    if depth == 0:
        _LAYOUT_PASS.page_maps = {}
        _LAYOUT_PASS.line_styles = {}
        _LAYOUT_PASS.line_indices = {}
        _LAYOUT_PASS.active_rubies = {}
        _LAYOUT_PASS.ruby_gaps = {}
        _LAYOUT_PASS.line_widths = {}
        _LAYOUT_PASS.render_lines = {}
        _LAYOUT_PASS.signatures = {}
        _LAYOUT_PASS.char_layout_metrics = {}
        _LAYOUT_PASS.char_advances = {}
        _LAYOUT_PASS.char_ink_widths = {}
        _LAYOUT_PASS.ink_rects = {}
        _LAYOUT_PASS.sayatoo_layouts = {}
        _LAYOUT_PASS.signal_heads = {}
        _LAYOUT_PASS.section_edges = {}
        _LAYOUT_PASS.tracks = []
        _LAYOUT_PASS.styles = []
        _LAYOUT_PASS.lines = []
        _LAYOUT_PASS.ruby_lists = []
        _LAYOUT_PASS.metrics = []
        _LAYOUT_PASS.signature_refs = []
    _LAYOUT_PASS.depth = depth + 1
    try:
        yield
    finally:
        _LAYOUT_PASS.depth = depth
        if depth == 0:
            _LAYOUT_PASS.page_maps = None
            _LAYOUT_PASS.line_styles = None
            _LAYOUT_PASS.line_indices = None
            _LAYOUT_PASS.active_rubies = None
            _LAYOUT_PASS.ruby_gaps = None
            _LAYOUT_PASS.line_widths = None
            _LAYOUT_PASS.render_lines = None
            _LAYOUT_PASS.signatures = None
            _LAYOUT_PASS.char_layout_metrics = None
            _LAYOUT_PASS.char_advances = None
            _LAYOUT_PASS.char_ink_widths = None
            _LAYOUT_PASS.ink_rects = None
            _LAYOUT_PASS.sayatoo_layouts = None
            _LAYOUT_PASS.signal_heads = None
            _LAYOUT_PASS.section_edges = None
            _LAYOUT_PASS.tracks = []
            _LAYOUT_PASS.styles = []
            _LAYOUT_PASS.lines = []
            _LAYOUT_PASS.ruby_lists = []
            _LAYOUT_PASS.metrics = []
            # signature_refs 是「memo 按 id 缓存」的防复用锚：嵌套 pass 退出
            # 不能清——外层的 signatures 字典还活着，锚一断，已 memo 的对象被
            # 回收、地址复用后，外层会对同 id 的新对象返回旧签名（P5 调试中
            # 由归一化签名的临时对象触发必现：副轨签名里出现主轨的字符）。
            _LAYOUT_PASS.signature_refs = []


__all__ = ["layout_pass"]
