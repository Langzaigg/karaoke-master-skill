"""统一「字重→实例」解析器（engine.text.font_weight）的规则测试。

规则与 native 侧 ``d2d_font_fallback.cpp`` 的 unified weight resolution
逐条对应；改动任一侧时这里的口径断言就是同步契约。
"""

from __future__ import annotations

import os

import pytest
from PyQt6.QtGui import QFont, QFontDatabase, QFontInfo, QFontMetrics

from krok_helper.subtitle_render.engine.text.font_weight import (
    apply_weight_plan,
    bucket_weight,
    build_weight_font,
    family_weight_axis,
    physical_weight_styles,
    resolve_weight_plan,
)

_IWATA_FONT_PATH = (
    r"E:\KaraMaker\StrangeUtaGame\debugsource"
    r"\IwataUDGothic08StdNVFTTF-L\IwataUDGothic08StdNVFTTF-L.ttf"
)


@pytest.mark.parametrize(
    ("requested", "expected"),
    (
        (100, 100),
        (250, 100),
        (300, 300),
        (350, 300),
        (400, 400),
        (450, 400),
        (500, 500),
        (550, 500),
        (600, 600),
        (650, 600),
        (700, 700),
        (750, 700),
        (800, 800),
        (850, 800),
        (900, 900),
        (950, 900),
        (0, 100),
    ),
)
def test_bucket_weight_matches_standard_hundreds(requested, expected):
    assert bucket_weight(requested) == expected


def _require_family(family: str) -> None:
    if family not in QFontDatabase.families():
        pytest.skip(f"font family not installed: {family}")


def test_static_multiface_family_pins_exact_and_nearest():
    _require_family("Yu Gothic")
    faces = physical_weight_styles("Yu Gothic")
    weights = [weight for weight, _name in faces]
    assert 400 in weights and 700 in weights

    exact = resolve_weight_plan("Yu Gothic", 400)
    assert exact.style_name is not None
    assert exact.base_weight == 400
    assert exact.synthetic_bold is False
    assert exact.mark is None

    missing = resolve_weight_plan("Yu Gothic", 600)
    assert missing.style_name is not None
    assert missing.base_weight in weights
    assert missing.synthetic_bold is False
    assert missing.mark == "就近"

    font = build_weight_font("Yu Gothic", 64, 600)
    assert font.styleName() == missing.style_name
    assert QFontInfo(font).styleName() == missing.style_name


def test_static_single_face_family_simulates_bold_only_above_600():
    _require_family("MS Gothic")
    faces = physical_weight_styles("MS Gothic")
    assert len(faces) == 1
    assert faces[0][0] == 400

    light = resolve_weight_plan("MS Gothic", 500)
    assert light.synthetic_bold is False
    assert light.mark == "就近"

    bold = resolve_weight_plan("MS Gothic", 700)
    assert bold.synthetic_bold is True
    assert bold.mark == "模拟"
    assert bold.base_weight == 400

    plain = build_weight_font("MS Gothic", 64, 400)
    simulated = build_weight_font("MS Gothic", 64, 700)
    metrics_plain = QFontMetrics(plain)
    metrics_sim = QFontMetrics(simulated)
    # 合成粗体把 advance 撑大约 1px（DWrite SIMULATIONS_BOLD），
    # 与 native 侧模拟 face 的 advance 口径一致。
    assert metrics_sim.horizontalAdvance("あ") == metrics_plain.horizontalAdvance("あ") + 1


@pytest.mark.skipif(
    not os.path.exists(_IWATA_FONT_PATH), reason="Iwata VF font not present"
)
def test_variable_font_renders_true_axis_instances(qapp):
    QFontDatabase.addApplicationFont(_IWATA_FONT_PATH)
    family = "Iwata UD Gothic 08StdN VF TTF"
    _require_family(family)

    axis = family_weight_axis(family)
    assert axis is not None
    assert (axis.minimum, axis.maximum) == (300.0, 900.0)

    # DWrite 轴值真值（advance units/1000）：300=782, 540=820, 900=856
    for weight, expected_a_px in ((300, 50), (540, 52), (900, 55)):
        plan = resolve_weight_plan(family, weight)
        assert plan.axis_value == float(weight)
        assert plan.mark is None
        metrics = QFontMetrics(build_weight_font(family, 64, weight))
        assert metrics.horizontalAdvance("A") == expected_a_px

    # 中间字重（如 650）是真实插值，不触发任何模拟。
    interpolated = resolve_weight_plan(family, 650)
    assert interpolated.axis_value == 650.0
    assert interpolated.mark is None

    # 轴外请求钳制到端点并标注。
    below = resolve_weight_plan(family, 100)
    assert below.axis_value == 300.0
    assert below.mark == "越界"
    above = resolve_weight_plan(family, 950)
    assert above.axis_value == 900.0
    assert above.mark == "越界"


def test_missing_metadata_family_falls_back_to_plain_weight(monkeypatch):
    # 元数据缺失（字体不存在/headless 枚举为空）时保持旧的纯桶化行为；
    # 直接打桩两条元数据通道，避免平台默认字体回退的干扰。
    import krok_helper.subtitle_render.engine.text.font_weight as fw

    monkeypatch.setattr(fw, "family_weight_axis", lambda family: None)
    monkeypatch.setattr(fw, "physical_weight_styles", lambda family: ())
    plan = fw.resolve_weight_plan("__no_such_family__", 700)
    assert plan.axis_value is None
    assert plan.style_name is None
    assert plan.enum_weight == 700
    font = QFont("__no_such_family__")
    fw.apply_weight_plan(font, plan)
    assert int(font.weight()) == 700
