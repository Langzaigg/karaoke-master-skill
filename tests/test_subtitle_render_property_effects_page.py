"""Focused construction contracts for the subtitle effects-property page."""

from __future__ import annotations

from types import SimpleNamespace

from PyQt6.QtWidgets import QPushButton

from krok_helper.subtitle_render.frontend.properties.pages.effects import (
    EffectsPropertyPageBuilder,
)


class _Host:
    def __init__(self) -> None:
        self.updates: list[dict[str, object]] = []
        self._style = SimpleNamespace(
            volume_fill_color="#101010",
            volume_stroke_color="#202020",
            volume_overlay_fill_color="#303030",
            volume_overlay_stroke_color="#404040",
            lit_fill_color="#505050",
            lit_stroke_color="#606060",
        )

    def _update_style(self, **changes) -> None:
        self.updates.append(changes)

    def _choose_lit_image(self) -> None:
        self.updates.append({"lit_image_path": "chosen"})

    def _clear_lit_image(self) -> None:
        self.updates.append({"lit_image_path": ""})

    def _on_section_edge_toggled(self, checked: bool) -> None:
        # 真实宿主还读 self._style；builder 契约测试只关心启用联动与字段路由。
        self._section_head_anim_combo.setEnabled(checked)
        self._section_tail_anim_combo.setEnabled(checked)
        self._section_edge_both_check.setEnabled(checked)
        self._update_style(section_edge_anim_enabled=checked)

    def _on_section_edge_both_toggled(self, checked: bool) -> None:
        self._update_style(section_edge_both_animations=checked)

    def _color_button(self, field: str, color: str):
        button = QPushButton(color)
        button.setObjectName(field)
        return button

def test_effects_animation_builder_preserves_options_and_layout(qapp) -> None:
    host = _Host()
    section = EffectsPropertyPageBuilder(host).make_animation_section()

    assert section.header.text() == "入退场动画"
    assert host._animation_grid._max_columns == 2
    assert host._entry_anim_combo.count() == 16
    assert host._entry_anim_combo.itemData(5) == "char_drip"
    assert host._entry_anim_combo.itemData(8) == "tracking_in"
    assert host._exit_anim_combo.count() == 16
    assert host._exit_anim_combo.itemData(2) == "slide_out"
    assert host._exit_anim_combo.itemData(8) == "scatter_out"
    assert host._entry_lead_spin.maximum() == 3000
    assert host._exit_fade_spin.maximum() == 3000
    assert host._entry_lead_spin.toolTip() == "入场动画时长"
    assert host._exit_fade_spin.toolTip() == "退场动画时长"
    assert [
        host._karaoke_anim_combo.itemData(index)
        for index in range(host._karaoke_anim_combo.count())
    ] == [
        "none", "no_wipe", "utopia", "scanline", "utopia_scanline", "zoom_pulse",
        "zoom_pulse_scanline"
    ]
    assert [
        host._reverse_karaoke_anim_combo.itemData(index)
        for index in range(host._reverse_karaoke_anim_combo.count())
    ] == [
        "inherit", "none", "no_wipe", "utopia", "scanline", "utopia_scanline",
        "zoom_pulse", "zoom_pulse_scanline"
    ]
    assert [
        host._scanline_mode_combo.itemData(index)
        for index in range(host._scanline_mode_combo.count())
    ] == [
        "color",
        "brighten",
        "follow_before",
        "follow_after",
        "role",
    ]
    # 整字放大速度等级：0（线性）~5 六档下拉，每档带速度说明；默认 1。
    assert [
        host._zoom_pulse_curve_combo.itemData(index)
        for index in range(host._zoom_pulse_curve_combo.count())
    ] == [0, 1, 2, 3, 4, 5]
    assert host._zoom_pulse_curve_combo.itemText(0) == "0级（线性）"
    assert host._zoom_pulse_curve_combo.itemText(1) == "1级（匀速·默认）"
    assert host._zoom_pulse_curve_combo.itemText(3) == "3级（较快）"
    assert host._zoom_pulse_curve_combo.itemText(5) == "5级（极快）"
    # 扫字线独占一整行（与出入场动画两栏同宽）；参数永久激活，亮度与角色
    # 下拉默认隐藏（默认单独颜色模式），宿主回显按模式互换第三列。
    assert host._scanline_row is not None
    for control in (
        host._scanline_mode_combo,
        host._scanline_width_spin,
        host._scanline_color_btn,
        host._scanline_role_combo,
        host._scanline_glow_spin,
    ):
        assert control.isEnabled()
    assert not host._scanline_color_btn.isHidden()
    assert host._scanline_brightness_spin.isHidden()
    assert host._scanline_role_combo.isHidden()
    # 网格行序：唱字对（第 2 行第 1 栏）→ 段首尾区块（第 2 行第 2 栏）
    # → 扫字线整行（第 3 行）。
    items = host._animation_grid._items

    def grid_index(widget) -> int:
        for index, item in enumerate(items):
            node = widget
            while node is not None:
                if node is item:
                    return index
                node = node.parent()
        return -1

    assert (
        grid_index(host._karaoke_pair_row)
        < grid_index(host._section_edge_row)
        < grid_index(host._scanline_row)
    )


def test_effects_section_edge_builder_defaults_and_order(qapp) -> None:
    host = _Host()
    EffectsPropertyPageBuilder(host).make_animation_section()

    assert host._section_edge_check.text() == "段首尾独立动画"
    assert host._section_edge_both_check.text() == "同时设置出入场"
    assert not host._section_edge_check.isChecked()
    assert not host._section_edge_both_check.isChecked()
    assert host._section_head_anim_combo.count() == 16
    assert host._section_tail_anim_combo.count() == 16
    # 主开关默认关：两个下拉与子开关全部禁用；选项表含默认动画（具体显示值
    # 由 set_style 回显）。
    assert not host._section_head_anim_combo.isEnabled()
    assert not host._section_tail_anim_combo.isEnabled()
    assert not host._section_edge_both_check.isEnabled()
    assert host._section_head_anim_combo.findData("fade") != -1
    assert host._section_tail_anim_combo.findData("fade") != -1

    # 唱字特效在第三行（退场动画之后），段首尾区块最后。
    items = host._animation_grid._items

    def grid_index(widget) -> int:
        for index, item in enumerate(items):
            node = widget
            while node is not None:
                if node is item:
                    return index
                node = node.parent()
        return -1

    assert (
        grid_index(host._exit_anim_combo)
        < grid_index(host._karaoke_anim_combo)
        < grid_index(host._section_edge_row)
    )


def test_effects_section_edge_builder_routes_controls_to_style_fields(qapp) -> None:
    host = _Host()
    EffectsPropertyPageBuilder(host).make_animation_section()

    host._section_edge_check.setChecked(True)
    host._section_edge_both_check.setChecked(True)
    host._section_head_anim_combo.setCurrentIndex(3)
    host._section_tail_anim_combo.setCurrentIndex(2)

    assert host.updates == [
        {"section_edge_anim_enabled": True},
        {"section_edge_both_animations": True},
        {"section_head_anim": "rise"},
        {"section_tail_anim": "slide_out"},
    ]


def test_effects_animation_builder_routes_controls_to_style_fields(qapp) -> None:
    host = _Host()
    EffectsPropertyPageBuilder(host).make_animation_section()

    host._entry_anim_combo.setCurrentIndex(1)
    host._entry_lead_spin.setValue(250)
    host._exit_anim_combo.setCurrentIndex(2)
    host._exit_fade_spin.setValue(300)
    host._karaoke_anim_combo.setCurrentIndex(1)
    host._reverse_karaoke_anim_combo.setCurrentIndex(2)
    host._scanline_mode_combo.setCurrentIndex(1)
    host._scanline_width_spin.setValue(24)
    host._scanline_glow_spin.setValue(9)
    host._scanline_brightness_spin.setValue(75)

    assert host.updates == [
        {"entry_anim": "fade"},
        {"entry_lead_ms": 250},
        {"exit_anim": "slide_out"},
        {"exit_fade_ms": 300},
        {"karaoke_anim": "no_wipe"},
        {"reverse_karaoke_anim": "no_wipe"},
        {"scanline_mode": "brighten"},
        {"scanline_width_px": 24},
        {"scanline_glow_px": 9},
        {"scanline_brightness_pct": 75},
    ]


def test_effects_lit_builder_preserves_groups_ranges_and_initial_state(qapp) -> None:
    host = _Host()
    section = EffectsPropertyPageBuilder(host).make_lit_section()

    assert section.header.text() == "指示灯"
    assert section.header_switch is host._lit_enabled_switch
    assert not section.is_expanded()
    assert list(host._lit_group_grids) == ["布局", "时序", "外观", "转场"]
    # 圆形/方形/圆角 + 图片（素材模式）。
    assert host._lit_style_combo.count() == 4
    assert host._lit_transition_angle_spin.minimum() == -360
    assert host._lit_transition_distance_spin.maximum() == 800
    assert host._lit_stroke_btn.objectName() == "lit_stroke_color"


def test_effects_volume_builder_is_independent_and_compact(qapp) -> None:
    host = _Host()
    section = EffectsPropertyPageBuilder(host).make_volume_section()

    assert section.header.text() == "音量柱"
    assert section.header_switch is host._volume_enabled_switch
    assert list(host._volume_group_grids) == ["时序", "布局", "动画", "外观"]
    assert host._volume_column_count_spin.maximum() == 16
    assert host._volume_fill_btn.objectName() == "volume_fill_color"


def test_effects_lit_builder_routes_transformed_values(qapp) -> None:
    host = _Host()
    builder = EffectsPropertyPageBuilder(host)
    builder.make_volume_section()
    builder.make_lit_section()

    host._volume_enabled_switch.setChecked(True)
    host._lit_enabled_switch.setChecked(True)
    host._volume_ratio_spin.setValue(3)
    host._volume_flash_duration_spin.setValue(25)
    host._lit_transition_mode_combo.setCurrentIndex(2)
    host._lit_shadow_check.setChecked(True)

    assert host.updates == [
        {"volume_enabled": True},
        {"lit_enabled": True, "lit_style": "circle"},
        {"volume_ratio": 3.0},
        {"volume_flash_duration_ratio": 0.25},
        {"lit_transition_mode": "slide"},
        {"lit_shadow": True},
    ]
