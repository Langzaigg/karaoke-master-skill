"""音频合成页 UI 测试（offscreen；ffmpeg 层全部打桩）。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtTest import QTest

from krok_helper.audio_alignment import WaveformData
from krok_helper.audio_processing.merge import page as merge_page_module
from krok_helper.audio_processing.merge.analysis import ClipAnalysis
from krok_helper.audio_processing.merge.model import TRIM_SOURCE_AUTO, TRIM_SOURCE_MANUAL
from krok_helper.audio_processing.merge.page import MergePage
from krok_helper.settings import AppSettings


class _FakeHost:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.tracked: list = []

    def track_background_task(self, task):
        self.tracked.append(task)
        return task

    def resolve_ffmpeg_dir(self):
        return None


def _waveform(duration: float = 10.0, seed: float = 1.0) -> WaveformData:
    count = int(duration * 80)
    peaks = [0.0 if (i / 80) < 1.0 or (i / 80) > duration - 1.0 else abs(seed) * 0.8 for i in range(count)]
    return WaveformData(path=Path("fake.wav"), duration=duration, peaks_per_second=80, peaks=peaks)


def _analysis(trim_start: float = 1.0, trim_end: float | None = 9.0, duration: float = 10.0) -> ClipAnalysis:
    return ClipAnalysis(
        waveform=_waveform(duration),
        sample_rate=48000,
        channels=2,
        trim_start=trim_start,
        trim_end=trim_end,
    )


@pytest.fixture
def page():
    settings = AppSettings()
    host = _FakeHost(settings)
    saved: list = []
    widget = MergePage(host, settings, lambda: saved.append(True))
    widget.resize(960, 800)
    yield widget
    widget.close()


def _wait_for_idle(page: MergePage, timeout_ms: float = 8000.0) -> None:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while page.is_busy():
        QTest.qWait(20)
        if time.monotonic() > deadline:
            pytest.fail("后台任务超时未结束")
    QTest.qWait(20)


def _fake_wav(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_bytes(b"RIFF")
    return path


class TestMergePageBasics:
    def test_initial_state(self, page: MergePage) -> None:
        assert page._merge_button.isEnabled() is False
        assert page._play_button.isEnabled() is False
        assert page._stop_playback_button.isEnabled() is False
        assert page._status_label.text() == "就绪"
        assert page._preview_dirty is True

    def test_add_files_analyzes_and_applies_suggested_trim(
        self, page: MergePage, tmp_path, monkeypatch
    ) -> None:
        seen: list = []

        def _fake_analyze(path, ffmpeg_dir, logger, *, label, threshold_db, min_silence_seconds, margin_seconds, should_cancel):
            seen.append((Path(path).name, threshold_db, margin_seconds))
            return _analysis(trim_start=1.2, trim_end=8.5)

        monkeypatch.setattr(merge_page_module, "analyze_clip", _fake_analyze)
        page._on_files_dropped([str(_fake_wav(tmp_path, "a.wav")), str(_fake_wav(tmp_path, "b.wav"))])
        _wait_for_idle(page)

        assert [name for name, *_ in seen] == ["a.wav", "b.wav"]
        assert len(page._clips) == 2
        first = page._clips[0]
        assert first.trim_source == TRIM_SOURCE_AUTO
        assert first.trim_start == pytest.approx(1.2)
        assert first.trim_end == pytest.approx(8.5)
        assert first.sample_rate == 48000 and first.channels == 2
        # 按钮放开、时间轴与时间码框同步。
        assert page._merge_button.isEnabled()
        assert page._timeline.clip_count() == 2
        assert page._timeline.total_duration() == pytest.approx(14.6, abs=0.01)
        assert page._trim_start_edit.seconds() == pytest.approx(1.2)
        assert page._trim_start_edit.text() == "00:00:01:200"
        assert page._trim_end_edit.seconds() == pytest.approx(8.5)
        assert page._trim_end_edit.text() == "00:00:08:500"

    def test_duplicate_paths_are_ignored(self, page: MergePage, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(merge_page_module, "analyze_clip", lambda *a, **k: _analysis())
        path = str(_fake_wav(tmp_path, "a.wav"))
        page._on_files_dropped([path, path])
        _wait_for_idle(page)
        assert len(page._clips) == 1

    def test_unsupported_extension_rejected(self, page: MergePage, tmp_path) -> None:
        page._on_files_dropped([str(tmp_path / "notes.txt")])
        assert page._clips == []

    def test_spin_edit_marks_manual(self, page: MergePage, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(merge_page_module, "analyze_clip", lambda *a, **k: _analysis())
        page._on_files_dropped([str(_fake_wav(tmp_path, "a.wav"))])
        _wait_for_idle(page)

        page._trim_start_edit.setText("00:00:02:000")
        page._trim_start_edit.editingFinished.emit()
        clip = page._clips[0]
        assert clip.trim_source == TRIM_SOURCE_MANUAL
        assert clip.trim_start == pytest.approx(2.0)
        assert page._preview_dirty is True
        assert page._trim_start_edit.text() == "00:00:02:000"

    def test_timecode_edit_accepts_shorthand_and_rejects_garbage(self, page: MergePage) -> None:
        from krok_helper.audio_processing.merge.page import format_timecode, parse_timecode

        assert format_timecode(2.0) == "00:00:02:000"
        assert format_timecode(3723.456) == "01:02:03:456"
        assert parse_timecode("83") == pytest.approx(83.0)
        assert parse_timecode("83.45") == pytest.approx(83.45)
        assert parse_timecode("1:23") == pytest.approx(83.0)
        assert parse_timecode("1:23.45") == pytest.approx(83.45)
        assert parse_timecode("1:02:03") == pytest.approx(3723.0)
        assert parse_timecode("1:02:03:450") == pytest.approx(3723.45)
        assert parse_timecode("01：02：03：450") == pytest.approx(3723.45)  # 全角冒号
        assert parse_timecode("") is None
        assert parse_timecode("abc") is None
        assert parse_timecode("1:2:3:4:5") is None
        assert parse_timecode("-3") is None

        edit = page._trim_start_edit
        edit.set_seconds(5.0)
        edit.setText("不是时间")
        edit.editingFinished.emit()
        assert edit.seconds() == pytest.approx(5.0)  # 非法输入回退
        assert edit.text() == "00:00:05:000"

    def test_audition_does_not_move_timeline_playhead(self, page: MergePage) -> None:
        page._playback_mode = "clip"  # 模拟素材列表单曲试听中
        page._timeline.set_playhead(0.0)
        page._on_player_position(2500)
        assert page._timeline.playhead() == 0.0
        page._playback_mode = "preview"  # 整体预览播放才驱动播放头
        page._on_player_position(2500)
        assert page._timeline.playhead() == pytest.approx(2.5)

    def test_splice_param_ranges_allow_fifteen_seconds(self, page: MergePage) -> None:
        assert page._fade_row._combo.maximum() == 15000  # 淡化放宽到 15s
        assert page._fade_row._combo.minimum() == 0
        assert page._gap_row._combo.maximum() == 5000
        page._fade_row.setValue(15.0)
        assert page._fade_row.value() == pytest.approx(15.0)
        page._fade_row.setValue(0.0)
        assert page._fade_row.value() == pytest.approx(0.0)

    def test_splice_mode_switch_reorders_timeline_and_persists(self, page: MergePage) -> None:
        from krok_helper.audio_processing.merge.model import MergeClip
        from krok_helper.audio_alignment import WaveformData
        from pathlib import Path as _P

        wf = WaveformData(path=_P("a.wav"), duration=10.0, peaks_per_second=80, peaks=[0.5] * 800)
        page._clips = [
            MergeClip(path=_P("C:/fake/a.wav"), duration=10.0, sample_rate=44100, channels=2, waveform=wf),
            MergeClip(path=_P("C:/fake/b.wav"), duration=10.0, sample_rate=44100, channels=2, waveform=wf),
        ]
        page._rebuild_clip_rows()
        page._fade_row.setValue(2.0)
        # 直拼 + 间隔 1s → 总 21s
        page._gap_row.setValue(1.0)
        page._on_splice_param_changed()
        assert page._timeline.total_duration() == pytest.approx(21.0)
        # 交叉淡化 + 接缝淡化 2s → 重叠 2s → 总 18s；间隔行被禁用
        page._splice_combo.setCurrentIndex(1)
        page._on_splice_param_changed()
        assert page.splice_mode() == "crossfade"
        assert page._timeline.total_duration() == pytest.approx(18.0)
        assert page._gap_row.isEnabled() is False
        assert page._settings.audio_merge["splice_mode"] == "crossfade"
        # 切回直拼：恢复 21s、间隔行可用
        page._splice_combo.setCurrentIndex(0)
        page._on_splice_param_changed()
        assert page._timeline.total_duration() == pytest.approx(21.0)
        assert page._gap_row.isEnabled() is True

    def test_live_change_while_playing_triggers_rerender_and_resume(self, page: MergePage) -> None:
        calls: list = []
        page._player_playback_active = lambda: True
        page._mergeable = lambda: True
        page._is_busy = lambda: False
        page._start_export = lambda **kwargs: calls.append(kwargs)
        page._mark_preview_dirty()
        assert calls and calls[0].get("preview") is True
        assert page._resume_after_render is True

        # 手动停止后自动续播意图被清除，不会在渲染完成时突然恢复播放。
        page._stop_playback()
        assert page._resume_after_render is False

    def test_start_playback_requests_strict_seek(self, page: MergePage, tmp_path) -> None:
        from PyQt6.QtMultimedia import QMediaPlayer

        preview = tmp_path / "preview.wav"
        preview.write_bytes(b"RIFF")
        page._preview_path = preview
        page._timeline.set_clips([], gap_seconds=0.0)
        page._timeline.set_playhead(3.5)
        player = page._ensure_player()
        page._start_playback_at_playhead()
        assert page._pending_seek_ms == 3500 or player.position() == 3500
        page._stop_playback()

    def test_reset_restores_full_length(self, page: MergePage, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(merge_page_module, "analyze_clip", lambda *a, **k: _analysis())
        page._on_files_dropped([str(_fake_wav(tmp_path, "a.wav"))])
        _wait_for_idle(page)

        page._on_reset_trim()
        clip = page._clips[0]
        assert clip.trim_start == 0.0
        assert clip.trim_end is None

    def test_merge_autotreats_only_untreated_clips(self, page: MergePage, tmp_path, monkeypatch) -> None:
        analyzed_targets: list = []
        monkeypatch.setattr(
            merge_page_module,
            "analyze_clip",
            lambda path, ffmpeg_dir, logger, **kwargs: (
                analyzed_targets.append(Path(path).name),
                _analysis(trim_start=1.0, trim_end=9.0),
            )[1],
        )
        page._on_files_dropped([str(_fake_wav(tmp_path, "a.wav")), str(_fake_wav(tmp_path, "b.wav"))])
        _wait_for_idle(page)
        assert len(page._clips) == 2

        # 第二条改成手动裁剪 → 视为已调整；第一条重置为未处理。
        page._clips[1].apply_manual(0.5, None)
        page._clips[0].reset_to_full()

        exports: list = []
        monkeypatch.setattr(
            merge_page_module,
            "run_merge",
            lambda clips, output_path, ffmpeg_dir, logger, **kwargs: (
                output_path.parent.mkdir(parents=True, exist_ok=True),
                output_path.write_bytes(b"merged"),
                exports.append(tuple(c.path.name for c in clips)),
            ),
        )
        monkeypatch.setattr(merge_page_module, "play_completion_sound", lambda: None)
        monkeypatch.setattr(merge_page_module, "show_fluent_info", lambda *a, **k: None)
        page._output_card.set_output_dir(str(tmp_path / "out"), emit=False)
        analyzed_targets.clear()
        page._on_merge_clicked()
        _wait_for_idle(page)

        # 只补测了未处理的第一条；手动调整的第二条未被重测；随后自动导出。
        assert analyzed_targets == ["a.wav"]
        assert page._clips[1].trim_start == pytest.approx(0.5)
        assert exports and exports[0] == ("a.wav", "b.wav")

    def test_output_card_title_and_button(self, page: MergePage) -> None:
        from qfluentwidgets import PrimaryPushButton

        assert page._merge_button.text() == "合成"
        assert isinstance(page._merge_button, PrimaryPushButton)
        assert page._merge_button.parent() is page._output_card

    def test_reorder_keeps_focus_on_dragged_clip(self, page: MergePage) -> None:
        from krok_helper.audio_processing.merge.model import MergeClip
        from krok_helper.audio_alignment import WaveformData
        from pathlib import Path as _P

        wf = WaveformData(path=_P("a.wav"), duration=4.0, peaks_per_second=80, peaks=[0.5] * 320)
        clip_a = MergeClip(path=_P("C:/fake/a.wav"), duration=4.0, sample_rate=44100, channels=2, waveform=wf)
        clip_b = MergeClip(path=_P("C:/fake/b.wav"), duration=4.0, sample_rate=44100, channels=2, waveform=wf)
        page._clips = [clip_a, clip_b]
        page._rebuild_clip_rows()
        # 模拟用户按住条目 2（索引 1）拖到最前。
        page._selected_index = 1
        page._on_timeline_reordered([clip_b, clip_a])
        assert page._clips == [clip_b, clip_a]
        assert page._selected_clip() is clip_b  # 焦点在拖动的那条（新 1）
        assert page._timeline.selected_clip() is clip_b
        assert page._clip_rows[0].clip is clip_b
        # 行高亮也应落在第 1 行。
        assert page._clip_rows[0]._selected if hasattr(page._clip_rows[0], "_selected") else True

    def test_move_and_remove(self, page: MergePage, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(merge_page_module, "analyze_clip", lambda *a, **k: _analysis())
        page._on_files_dropped([str(_fake_wav(tmp_path, "a.wav")), str(_fake_wav(tmp_path, "b.wav"))])
        _wait_for_idle(page)

        second = page._clips[1]
        page._move_clip(second, -1)
        assert page._clips[0].path.name == "b.wav"

        page._remove_clip(page._clips[0])
        assert len(page._clips) == 1
        assert page._clips[0].path.name == "a.wav"

    def test_settings_persist_to_namespace(self, page: MergePage) -> None:
        page._fade_row.setValue(0.12, emit=True)
        page._gap_row.setValue(0.5, emit=True)
        page._head_fade_check.setChecked(True)
        ns = page._settings.audio_merge
        assert ns["joint_fade_s"] == pytest.approx(0.12)
        assert ns["gap_s"] == pytest.approx(0.5)
        assert ns["head_fade"] is True

    def test_export_runs_run_merge_and_unblocks(self, page: MergePage, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(merge_page_module, "analyze_clip", lambda *a, **k: _analysis())
        page._on_files_dropped([str(_fake_wav(tmp_path, "a.wav"))])
        _wait_for_idle(page)

        outputs: list = []

        def _fake_run_merge(clips, output_path, ffmpeg_dir, logger, **kwargs):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"merged")
            outputs.append((tuple(clip.path.name for clip in clips), output_path, kwargs))
            return output_path

        monkeypatch.setattr(merge_page_module, "run_merge", _fake_run_merge)
        monkeypatch.setattr(merge_page_module, "play_completion_sound", lambda: None)
        monkeypatch.setattr(merge_page_module, "show_fluent_info", lambda *a, **k: None)

        page._output_card.set_output_dir(str(tmp_path / "out"), emit=False)
        page._output_card.name_edit.setText("我的合成")
        page._on_merge_clicked()
        _wait_for_idle(page)

        assert len(outputs) == 1
        clips_names, output_path, kwargs = outputs[0]
        assert clips_names == ("a.wav",)
        assert output_path.name == "我的合成.wav"
        assert kwargs["joint_fade_seconds"] == pytest.approx(0.03)
        assert page._open_output_button.isEnabled()
        assert page._status_label.text() == "合成完成"


class TestArrangementView:
    def _clips(self) -> list:
        from krok_helper.audio_processing.merge.timeline_view import ArrangementView  # noqa: F401
        from krok_helper.audio_processing.merge.model import MergeClip

        return [
            MergeClip(
                path=Path("a.wav"),
                duration=4.0,
                sample_rate=44100,
                channels=2,
                waveform=_waveform(duration=4.0),
                trim_start=1.0,
                trim_end=3.0,
            ),
            MergeClip(
                path=Path("b.wav"),
                duration=4.0,
                sample_rate=44100,
                channels=2,
                waveform=_waveform(duration=4.0),
                trim_start=0.0,
                trim_end=None,
            ),
        ]

    def _view(self):
        from krok_helper.audio_processing.merge.timeline_view import ArrangementView

        view = ArrangementView()
        view.resize(816, 260)
        view.set_clips(self._clips(), gap_seconds=0.0)
        return view

    @staticmethod
    def _send(view: QWidget, event_type: QEvent.Type, x: float, y: float) -> None:
        if event_type == QEvent.Type.MouseButtonPress:
            button = Qt.MouseButton.LeftButton
            buttons = Qt.MouseButton.LeftButton
        elif event_type == QEvent.Type.MouseButtonRelease:
            button = Qt.MouseButton.LeftButton
            buttons = Qt.MouseButton.NoButton
        else:
            button = Qt.MouseButton.NoButton
            buttons = Qt.MouseButton.LeftButton
        event = QMouseEvent(
            event_type,
            QPointF(x, y),
            QPointF(x, y),
            button,
            buttons,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(view, event)

    def test_paint_does_not_crash(self) -> None:
        view = self._view()
        view.set_playhead(1.5)
        view.grab()

    def test_empty_state_paints(self) -> None:
        from krok_helper.audio_processing.merge.timeline_view import ArrangementView

        view = ArrangementView()
        view.resize(400, 200)
        view.grab()

    def test_ruler_click_moves_playhead(self) -> None:
        view = self._view()
        emitted: list = []
        view.playheadChanged.connect(lambda value: emitted.append(value))
        # 总时长 2+4=6s，pps=(816-16)/6≈133；3s → x≈8+399≈407。
        self._send(view, QEvent.Type.MouseButtonPress, 407.0, 10.0)
        assert emitted
        assert view.playhead() == pytest.approx(3.0, abs=0.05)

    def test_bar_drag_reorders(self) -> None:
        view = self._view()
        emitted: list = []
        view.clipsReordered.connect(lambda order: emitted.append(list(order)))
        # 条 1（2s）中心 x≈8+133≈141；条 2 中心 x≈8+133*4≈540。从条 1 中心拖到条 2 右侧。
        self._send(view, QEvent.Type.MouseButtonPress, 141.0, 120.0)
        self._send(view, QEvent.Type.MouseMove, 620.0, 120.0)
        self._send(view, QEvent.Type.MouseButtonRelease, 620.0, 120.0)
        assert emitted
        assert [clip.path.name for clip in emitted[0]] == ["b.wav", "a.wav"]
        assert [clip.path.name for clip in view._clips] == ["b.wav", "a.wav"]

    def test_handle_drag_trims_selected_clip(self) -> None:
        view = self._view()
        emitted: list = []
        view.clipTrimChanged.connect(lambda clip, start, end: emitted.append((clip, start, end)))
        clip = view._clips[0]
        # 先点选条 1，使其出现左右把手。
        self._send(view, QEvent.Type.MouseButtonPress, 141.0, 120.0)
        self._send(view, QEvent.Type.MouseButtonRelease, 141.0, 120.0)
        assert view.selected_clip() is clip
        # 左把手中心在条 1 左边缘 x≈8、条顶 y≈34 上方。
        handles = view._handle_rects()
        assert set(handles) == {"edge-left", "edge-right"}
        left = handles["edge-left"].center()
        self._send(view, QEvent.Type.MouseButtonPress, left.x(), left.y())
        self._send(view, QEvent.Type.MouseMove, left.x() + 66.0, left.y())  # +0.5s
        self._send(view, QEvent.Type.MouseButtonRelease, left.x() + 66.0, left.y())
        assert emitted
        _clip, start, end = emitted[0]
        assert start == pytest.approx(1.5, abs=0.05)
        assert end == pytest.approx(3.0)
        assert clip.trim_start == pytest.approx(1.5, abs=0.05)

    def test_unselected_clip_edges_are_not_grabbable(self) -> None:
        view = self._view()
        emitted: list = []
        view.clipTrimChanged.connect(lambda clip, start, end: emitted.append((clip, start, end)))
        # 未选中任何条目：条目边缘没有把手，按住边缘拖动只算选中/拖动主体。
        assert view._handle_rects() == {}
        # 在条 1 左边缘按住并拖动：不应产生裁剪提交。
        self._send(view, QEvent.Type.MouseButtonPress, 10.0, 120.0)
        self._send(view, QEvent.Type.MouseMove, 76.0, 120.0)
        self._send(view, QEvent.Type.MouseButtonRelease, 76.0, 120.0)
        assert emitted == []
        assert view._clips[0].trim_start == pytest.approx(1.0)

    def test_zoom_and_fit_width(self) -> None:
        view = self._view()
        fit_pps = view._pixels_per_second()
        view.zoom_in()
        assert view.zoomed() is True
        assert view._pixels_per_second() > fit_pps * 1.2
        view.zoom_out()
        assert view._pixels_per_second() == pytest.approx(fit_pps, rel=0.01)
        view.fit_width()
        assert view.zoomed() is False
        # 真实场景由横向滚动区把画布压回视口宽度，这里手动模拟一次 resize。
        view.resize(816, 260)
        assert view._pixels_per_second() == pytest.approx(fit_pps)

    def test_zoom_is_clamped(self) -> None:
        view = self._view()
        view._apply_zoom(99_999.0, anchor_x=100.0)
        assert view._pixels_per_second() <= 4000.0
        view._apply_zoom(0.001, anchor_x=100.0)
        assert view._pixels_per_second() >= 1.0

    def test_micro_drag_in_overlap_does_not_reorder(self) -> None:
        view = self._view()
        # 最大重叠（交叉淡化钳制 -2s）：在重叠区按住并小拖 40px，不应换位。
        view.set_clips(self._clips(), gap_seconds=-2.0)
        spans = view._bar_spans()
        press_x = view._time_to_x((spans[0][0] + spans[0][1]) / 2.0)
        self._send(view, QEvent.Type.MouseButtonPress, press_x, 120.0)
        self._send(view, QEvent.Type.MouseMove, press_x + 40.0, 120.0)
        self._send(view, QEvent.Type.MouseButtonRelease, press_x + 40.0, 120.0)
        assert [c.path.name for c in view._clips] == ["a.wav", "b.wav"]

    def test_click_jitter_after_failed_drag_does_not_reorder(self) -> None:
        view = self._view()
        view.set_clips(self._clips(), gap_seconds=-2.0)
        spans = view._bar_spans()
        press_x = view._time_to_x((spans[0][0] + spans[0][1]) / 2.0)
        second_x = view._time_to_x((spans[1][0] + spans[1][1]) / 2.0)
        # 先做一次不换位的小拖，再"点击另一条"（带 4px 抖动）——都不应换位。
        self._send(view, QEvent.Type.MouseButtonPress, press_x, 120.0)
        self._send(view, QEvent.Type.MouseMove, press_x + 40.0, 120.0)
        self._send(view, QEvent.Type.MouseButtonRelease, press_x + 40.0, 120.0)
        self._send(view, QEvent.Type.MouseButtonPress, second_x, 120.0)
        self._send(view, QEvent.Type.MouseMove, second_x + 4.0, 120.0)
        self._send(view, QEvent.Type.MouseButtonRelease, second_x + 4.0, 120.0)
        assert [c.path.name for c in view._clips] == ["a.wav", "b.wav"]

    def test_large_drag_still_reorders_in_overlap(self) -> None:
        view = self._view()
        view.set_clips(self._clips(), gap_seconds=-2.0)
        spans = view._bar_spans()
        press_x = view._time_to_x((spans[0][0] + spans[0][1]) / 2.0)
        # 按住第一条的非重叠区（前半段），整体拖过第二条版图之后 → 应换位到末尾。
        non_overlap_x = view._time_to_x(spans[0][0] + (spans[0][1] - spans[0][0]) * 0.3)
        far_x = view._time_to_x(spans[1][1]) + 20.0
        self._send(view, QEvent.Type.MouseButtonPress, non_overlap_x, 120.0)
        self._send(view, QEvent.Type.MouseMove, far_x, 120.0)
        self._send(view, QEvent.Type.MouseButtonRelease, far_x, 120.0)
        assert [c.path.name for c in view._clips] == ["b.wav", "a.wav"]

    def test_negative_gap_overlaps_bars(self) -> None:
        view = self._view()
        # 两条各 2s/4s，重叠（交叉淡化）1s：总时长 5s，第二条起点 1s。
        view.set_clips(self._clips(), gap_seconds=-1.0)
        assert view.total_duration() == pytest.approx(5.0)
        spans = view._bar_spans()
        assert spans[0] == (pytest.approx(0.0), pytest.approx(2.0))
        assert spans[1][0] == pytest.approx(1.0)
        # 重叠钳制：-99s 也最多重叠相邻时长各半（第一条 2s → 重叠 1s）。
        view.set_clips(self._clips(), gap_seconds=-99.0)
        assert view._step_between(view._clips[0], view._clips[1]) == pytest.approx(-1.0)
        view.grab()

    def test_bar_click_selects(self) -> None:
        view = self._view()
        emitted: list = []
        view.clipSelected.connect(lambda clip: emitted.append(clip))
        self._send(view, QEvent.Type.MouseButtonPress, 141.0, 120.0)
        self._send(view, QEvent.Type.MouseButtonRelease, 141.0, 120.0)
        assert emitted and emitted[0] is view._clips[0]
        assert view.selected_clip() is view._clips[0]


class TestPreviewTempCleanup:
    """整体预览临时目录的自行清扫：目录内任何时刻最多保留一个在用的 wav。"""

    def test_init_purges_stale_previews(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("KARAOKE_STUDIO_TEMP_DIR", str(tmp_path))
        stale_dir = tmp_path / "merge-preview"  # 统一根 %TEMP%\LinKLyrics 下的子目录
        stale_dir.mkdir()
        (stale_dir / "preview.wav").write_bytes(b"RIFF")
        (stale_dir / "preview (2).wav").write_bytes(b"RIFF")
        (stale_dir / "unrelated.txt").write_bytes(b"keep")

        settings = AppSettings()
        widget = MergePage(_FakeHost(settings), settings, lambda: None)
        widget.close()

        assert list(stale_dir.glob("preview*.wav")) == []
        assert (stale_dir / "unrelated.txt").is_file()

    def test_preview_render_reuses_single_name_and_purges(
        self, page: MergePage, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("KARAOKE_STUDIO_TEMP_DIR", str(tmp_path))
        stale_dir = tmp_path / "merge-preview"
        stale_dir.mkdir()
        (stale_dir / "preview.wav").write_bytes(b"RIFF")  # 上次崩溃残留
        (stale_dir / "preview (3).wav").write_bytes(b"RIFF")

        monkeypatch.setattr(merge_page_module, "analyze_clip", lambda *a, **k: _analysis())
        page._on_files_dropped([str(_fake_wav(tmp_path, "a.wav"))])
        _wait_for_idle(page)

        def _fake_run_merge(clips, output_path, ffmpeg_dir, logger, **kwargs):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"merged")
            return output_path

        monkeypatch.setattr(merge_page_module, "run_merge", _fake_run_merge)
        started: list = []
        monkeypatch.setattr(page, "_start_playback_at_playhead", lambda: started.append(True))

        page._on_play_clicked()  # 预览脏 → 触发整体预览渲染
        _wait_for_idle(page)
        assert started == [True]  # 渲染完成自动起播
        assert page._preview_dirty is False
        assert sorted(p.name for p in stale_dir.glob("preview*.wav")) == ["preview.wav"]
        assert page._preview_path == stale_dir / "preview.wav"

        # 再次置脏重渲染：旧文件先删再取名，不递增成 preview (2).wav。
        page._mark_preview_dirty()
        page._on_play_clicked()
        _wait_for_idle(page)
        assert sorted(p.name for p in stale_dir.glob("preview*.wav")) == ["preview.wav"]
        assert page._preview_path == stale_dir / "preview.wav"
