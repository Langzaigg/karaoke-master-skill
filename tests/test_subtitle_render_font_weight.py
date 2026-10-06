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
    embolden_glyph_path,
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


@pytest.fixture(autouse=True, scope="module")
def _cleanup_application_fonts():
    """模块级应用字体注册不泄漏到同进程的后续测试模块（painter 用例对
    字体库状态敏感）。"""
    yield
    QFontDatabase.removeAllApplicationFonts()
    from krok_helper.subtitle_render.engine.text.font_weight import (
        clear_font_weight_cache,
    )

    clear_font_weight_cache()


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


_YUGOTH_FONT_PATH = r"C:\Windows\Fonts\YuGothR.ttc"


def _register_yu_gothic() -> str:
    if not os.path.exists(_YUGOTH_FONT_PATH):
        pytest.skip("Yu Gothic font file not present")
    QFontDatabase.addApplicationFont(_YUGOTH_FONT_PATH)
    family = "Yu Gothic"
    _require_family(family)
    return family


def test_static_multiface_family_pins_exact_and_missing():
    family = _register_yu_gothic()
    faces = physical_weight_styles(family)
    weights = [weight for weight, _name in faces]
    assert 400 in weights

    exact = resolve_weight_plan(family, 400)
    assert exact.style_name is not None
    assert exact.base_weight == 400
    assert exact.synthetic_bold is False
    assert exact.mark is None

    # v6：凡比基 face 重的缺档一律模拟放大（基 face 取其下最重真实
    # face，Δ=桶化值−基 face 字重），「就近」概念取消。face 集随注册
    # 环境变化（YuGothR.ttc 在 offscreen 下只见部分 face），按运行时
    # 清单推导期望值。
    runtime_weights = [weight for weight, _name in physical_weight_styles(family)]
    floors = [weight for weight in runtime_weights if weight < 600]
    expected_base = max(floors)
    missing = resolve_weight_plan(family, 600)
    assert missing.base_weight == expected_base
    assert missing.embolden_delta == 600 - expected_base
    assert missing.mark == "模拟"
    assert missing.synthetic_bold is False


@pytest.mark.skipif(
    not os.path.exists(_YUGOTH_FONT_PATH), reason="Yu Gothic font file not present"
)
def test_missing_weight_font_pins_floor_face(qapp):
    """v6 构造不变量：模拟档钉住基 face（ QFontInfo 可证），墨迹比纯基
    face 宽（膨胀生效），advance 与基 face 一致（native faceWeight 对齐）。"""
    from PyQt6.QtGui import QPainterPath

    family = _register_yu_gothic()
    runtime_weights = [weight for weight, _name in physical_weight_styles(family)]
    floors = [weight for weight in runtime_weights if weight < 600]
    expected_base = max(floors)
    base = build_weight_font(family, 48, expected_base)
    planned = build_weight_font(family, 48, 600)
    assert QFontInfo(planned).styleName() == QFontInfo(base).styleName()
    assert QFontMetrics(planned).horizontalAdvance("教") == QFontMetrics(
        base
    ).horizontalAdvance("教")

    def ink(font):
        path = QPainterPath()
        path.addText(0.0, 0.0, font, "教科書")
        return embolden_glyph_path(path, font).boundingRect().width()

    assert ink(planned) > ink(base)


def test_static_single_face_family_emboldens_every_heavier_level():
    """v6：{400} 族 500/600/700 全部模拟放大（Δ=100/200/300）。"""
    _require_family("MS Gothic")
    faces = physical_weight_styles("MS Gothic")
    assert len(faces) == 1
    assert faces[0][0] == 400

    for weight, delta in ((500, 100), (600, 200), (700, 300)):
        plan = resolve_weight_plan("MS Gothic", weight)
        assert plan.base_weight == 400
        assert plan.embolden_delta == delta
        assert plan.mark == "模拟"
        assert plan.synthetic_bold is False

    lighter = resolve_weight_plan("MS Gothic", 200)
    assert lighter.base_weight == 400
    assert lighter.embolden_delta == 0
    assert lighter.mark is None


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

    # 轴下限之下钳制到端点并标「越界」；950 桶化为 900=轴上限，落在
    # 真实端点上（无标注、无膨胀）。
    below = resolve_weight_plan(family, 100)
    assert below.axis_value == 300.0
    assert below.mark == "越界"
    above = resolve_weight_plan(family, 950)
    assert above.axis_value == 900.0
    assert above.mark is None
    assert above.embolden_delta == 0


def test_fake_variable_axis_falls_back_to_static(monkeypatch, qapp):
    """伪可变字体（带 fvar 的 wght 轴但渲染恒定）按静态族处理。

    打桩 ``_wght_axis_is_effective`` 为恒 False，模拟「轴两端指纹一致」
    （无真实变体数据 / 当前环境无法应用轴值），此时不得走轴值路径。
    """
    import krok_helper.subtitle_render.engine.text.font_weight as fw

    monkeypatch.setattr(fw, "_wght_axis_is_effective", lambda family, axis: False)
    fw.clear_font_weight_cache()
    msgothic = r"C:\Windows\Fonts\msgothic.ttc"
    if not os.path.exists(msgothic):
        pytest.skip("MS Gothic font file not present")
    QFontDatabase.addApplicationFont(msgothic)
    _require_family("MS Gothic")
    plan = fw.resolve_weight_plan("MS Gothic", 600)
    assert plan.axis_value is None
    assert plan.style_name == "Regular"
    assert plan.mark == "模拟"


def test_constant_axis_variable_packaging_treated_as_static(qapp):
    """恒定轴（min==max 的"可变"包装，单字重 VF 文件）必须按静态处理。

    2026-10-07 用户报「全字重无效且无就近/模拟标注」的根因：恒定轴
    曾跳过有效性检测，被当成真可变后所有字重 clamp 到唯一值且全档
    标真实（无标注）。两端同值的轴指纹必相同→无效→静态。
    """
    import krok_helper.subtitle_render.engine.text.font_weight as fw

    msgothic = r"C:\Windows\Fonts\msgothic.ttc"
    if not os.path.exists(msgothic):
        pytest.skip("MS Gothic font file not present")
    QFontDatabase.addApplicationFont(msgothic)
    _require_family("MS Gothic")
    constant = fw.WeightAxis(minimum=700.0, default=700.0, maximum=700.0)
    assert fw._wght_axis_is_effective("MS Gothic", constant) is False


def test_bold_cut_family_emboldens_above_top_weight(qapp):
    """粗体单字重族：请求更重时按统一公式膨胀轮廓（粗上加粗）。

    Qt 的合成粗体只补齐"非粗→粗"，不会在已是粗体（≥600）的 face 上
    再加粗；DWrite 的 SIMS_BOLD 可以但 CPU 无法跟进。改为两后端按同一
    公式（字号 x Δ/3000 圆形膨胀）自绘加粗——plan 携带 embolden_delta，
    GPU 端 textRealizationFor 对轮廓 Widen+Union。
    """
    ttc = "C:/Windows/Fonts/UDDIGIKYOKASHON-B_0.TTC"
    if not os.path.exists(ttc):
        pytest.skip("UD Digi Kyokasho NK-B font file not present")
    QFontDatabase.addApplicationFont(ttc)
    _require_family("UD Digi Kyokasho NK-B")
    heavier = resolve_weight_plan("UD Digi Kyokasho NK-B", 900)
    assert heavier.base_weight == 700
    assert heavier.synthetic_bold is False
    assert heavier.embolden_delta == 200
    assert heavier.mark == "模拟"
    mid = resolve_weight_plan("UD Digi Kyokasho NK-B", 800)
    assert mid.embolden_delta == 100
    lighter = resolve_weight_plan("UD Digi Kyokasho NK-B", 400)
    assert lighter.base_weight == 700
    assert lighter.embolden_delta == 0
    assert lighter.mark is None
    # 膨胀公式：48px x 200/3000 = 3.2px。
    from krok_helper.subtitle_render.engine.text.font_weight import embolden_width_px

    assert embolden_width_px(48, 200) == pytest.approx(3.2)


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
    # 元数据缺失不产生任何模拟（无法判定基 face），不标注。
    assert plan.mark is None
    font = QFont("__no_such_family__")
    fw.apply_weight_plan(font, plan)
    assert int(font.weight()) == 700
