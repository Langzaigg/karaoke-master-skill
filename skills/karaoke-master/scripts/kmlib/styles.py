"""Built-in subtitle templates / effects mapped onto the subtitle_render ``Style``.

The renderer ships no style presets, so the skill defines its own catalog.
Every template only touches appearance fields; layout stays the engine's
N3-like two line default. Fonts are resolved against the installed font list
with fallbacks, so templates work on machines without the first choice.
"""

from __future__ import annotations

import colorsys
import re
from dataclasses import replace
from typing import Any

FONT_FALLBACK = ["UD デジタル 教科書体 N-B", "BIZ UDPGothic", "Yu Gothic UI", "Meiryo", "Noto Sans JP", "MS Gothic"]

TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "classic",
        "name": "经典卡拉OK",
        "desc": "白字深蓝描边，唱过变为亮蓝——最常见的 NicoKara 风格",
        "fonts": ["UD デジタル 教科書体 N-B", "BIZ UDPGothic", "Meiryo"],
        "weight": 700, "size": 96, "spacing": 4,
        "before": {"text": "#FFFFFF", "stroke": "#14204A", "stroke2": "#FFFFFF", "shadow": "#000000"},
        "after": {"text": "#2F6BFF", "stroke": "#FFFFFF", "stroke2": "#14204A", "shadow": "#000000"},
        "stroke": 12, "stroke2": 4, "stroke2_on": True, "decoration": "shadow", "shadow_offset": 4,
        "recommended": True,
    },
    {
        "id": "tactic",
        "name": "TACTIC 蓝白发光",
        "desc": "N3 TACTIC 同款：细描边 + 三档柔光，唱过转为发光蓝",
        "fonts": ["Meiryo", "Yu Gothic UI", "Noto Sans JP"],
        "weight": 700, "size": 92, "spacing": 7,
        "before": {"text": "#FFFFFF", "stroke": "#122348", "stroke2": "#FFFFFF", "shadow": "#FFFFFF"},
        "after": {"text": "#3E8DFF", "stroke": "#122348", "stroke2": "#FFFFFF", "shadow": "#2B72FF"},
        "stroke": 5, "stroke2": 0, "stroke2_on": False, "decoration": "glow", "glow": 12, "glow_level": 1,
    },
    {
        "id": "sakura",
        "name": "樱色偶像",
        "desc": "圆体 + 粉色渐变，白色双描边，适合偶像 / 女声组合",
        "fonts": ["FOT-UDMarugo_Large Pr6N DB", "UD デジタル 教科書体 N-B", "Meiryo"],
        "weight": 700, "size": 94, "spacing": 3,
        "before": {"text": "#FFFFFF", "stroke": "#B03A72", "stroke2": "#FFFFFF", "shadow": "#5A1A3A"},
        "after": {"text": ["#FF4FA0", "#FFC2E0"], "stroke": "#FFFFFF", "stroke2": "#D23C82", "shadow": "#FF8AC4"},
        "stroke": 10, "stroke2": 5, "stroke2_on": True, "decoration": "glow", "glow": 10, "glow_level": 0,
    },
    {
        "id": "neon",
        "name": "霓虹夜",
        "desc": "粗黑体，冷色霓虹辉光，适合电子 / 摇滚 / 夜景素材",
        "fonts": ["Source Han Sans JP Heavy", "Source Han Sans Heavy", "Noto Sans JP", "Meiryo"],
        "weight": 800, "size": 92, "spacing": 2,
        "before": {"text": "#EAF6FF", "stroke": "#0B1530", "stroke2": "#3BE7FF", "shadow": "#3BE7FF"},
        "after": {"text": "#6CF7FF", "stroke": "#0B1530", "stroke2": "#FF4FD8", "shadow": "#00D5FF"},
        "stroke": 9, "stroke2": 3, "stroke2_on": True, "decoration": "glow", "glow": 10, "glow_level": 1,
    },
    {
        "id": "mincho",
        "name": "明朝·抒情",
        "desc": "宋 / 明朝体，金色渐变与柔和阴影，适合抒情 / 叙事曲",
        "fonts": ["Source Han Serif JP Bold", "Yu Mincho Demibold", "Yu Mincho", "BIZ UDPMincho", "MS Mincho"],
        "weight": 700, "size": 92, "spacing": 6,
        "before": {"text": "#FFFDF6", "stroke": "#2A1B05", "stroke2": "#FFFFFF", "shadow": "#000000"},
        "after": {"text": ["#FFE69A", "#E39A2E"], "stroke": "#2A1B05", "stroke2": "#FFF2C8", "shadow": "#7A4A00"},
        "stroke": 7, "stroke2": 0, "stroke2_on": False, "decoration": "shadow", "shadow_offset": 5,
    },
    {
        "id": "anime_op",
        "name": "动画 OP 风",
        "desc": "超粗黑体，黄橙渐变 + 黑白双层描边，冲击力强",
        "fonts": ["Source Han Sans JP Heavy", "FOT-UDKakugo_Large Pr6N E", "Noto Sans JP", "Meiryo"],
        "weight": 900, "size": 100, "spacing": 0,
        "before": {"text": "#FFFFFF", "stroke": "#000000", "stroke2": "#FFFFFF", "shadow": "#000000"},
        "after": {"text": ["#FFF35C", "#FF7A1A"], "stroke": "#000000", "stroke2": "#FFFFFF", "shadow": "#000000"},
        "stroke": 14, "stroke2": 6, "stroke2_on": True, "decoration": "shadow", "shadow_offset": 6,
    },
    {
        "id": "minimal",
        "name": "简洁白",
        "desc": "无粗描边的细字，唱前半透明灰，唱过纯白发光，适合 MV 风素材",
        "fonts": ["Noto Sans JP", "Yu Gothic UI", "Meiryo"],
        "weight": 600, "size": 84, "spacing": 4,
        "before": {"text": "#AEB6C8", "stroke": "#1A1D26", "stroke2": "#FFFFFF", "shadow": "#000000"},
        "after": {"text": "#FFFFFF", "stroke": "#1A1D26", "stroke2": "#FFFFFF", "shadow": "#FFFFFF"},
        "stroke": 3, "stroke2": 0, "stroke2_on": False, "decoration": "glow", "glow": 8, "glow_level": 0,
    },
]

EFFECTS: list[dict[str, Any]] = [
    {"id": "classic", "name": "经典淡入淡出", "desc": "整行淡入，逐字擦色，唱完淡出",
     "fields": {"entry_anim": "fade", "exit_anim": "fade", "karaoke_anim": "none", "sing_fx": "none"}, "recommended": True},
    {"id": "utopia", "name": "Utopia 柔光擦色", "desc": "N3 Utopia：擦色边缘带柔光过渡",
     "fields": {"entry_anim": "fade", "exit_anim": "fade", "karaoke_anim": "utopia", "sing_fx": "none"}},
    {"id": "char_fade", "name": "逐字浮现", "desc": "文字逐个渐显入场，逐个渐隐退场",
     "fields": {"entry_anim": "char_fade", "exit_anim": "char_fade", "karaoke_anim": "none", "sing_fx": "none"}},
    {"id": "glow", "name": "辉光", "desc": "辉光浮现入场 / 辉光消散，唱到的字放光",
     "fields": {"entry_anim": "glow_in", "exit_anim": "glow_out", "karaoke_anim": "utopia", "sing_fx": "none"}},
    {"id": "sparkle", "name": "星光闪烁", "desc": "星光入场，演唱时字上闪烁星点",
     "fields": {"entry_anim": "sparkle", "exit_anim": "fade", "karaoke_anim": "none", "sing_fx": "twinkle"}},
    {"id": "petal", "name": "花瓣飘落", "desc": "花瓣飘入，演唱时洒落花瓣，适合抒情 / 春日",
     "fields": {"entry_anim": "petal", "exit_anim": "petal", "karaoke_anim": "none", "sing_fx": "petal",
                "fx_particle_color_mode": "sakura"}},
    {"id": "scanline", "name": "扫描光", "desc": "擦色前沿带一条发光扫描线",
     "fields": {"entry_anim": "fade", "exit_anim": "fade", "karaoke_anim": "scanline", "sing_fx": "none"}},
    {"id": "zoom_pulse", "name": "跳动放大", "desc": "唱到的字轻微放大回弹，节奏感强",
     "fields": {"entry_anim": "rise", "exit_anim": "fade", "karaoke_anim": "zoom_pulse", "sing_fx": "none"}},
    {"id": "notes", "name": "音符飘出", "desc": "音符入场，演唱时飘出音符粒子",
     "fields": {"entry_anim": "note", "exit_anim": "fade", "karaoke_anim": "none", "sing_fx": "note",
                "fx_particle_color_mode": "follow_after"}},
    {"id": "wave", "name": "波浪 + 粒子消散", "desc": "波浪上浮入场，唱完化为粒子消散",
     "fields": {"entry_anim": "wave_in", "exit_anim": "dissolve_out", "karaoke_anim": "none", "sing_fx": "none"}},
]


def template(tid: str | None) -> dict:
    return next((t for t in TEMPLATES if t["id"] == tid), TEMPLATES[0])


def effect(eid: str | None) -> dict:
    return next((e for e in EFFECTS if e["id"] == eid), EFFECTS[0])


def catalog() -> dict:
    strip = lambda d: {k: v for k, v in d.items() if k not in ("fields",)}  # noqa: E731
    return {"templates": [strip(t) for t in TEMPLATES], "effects": [strip(e) for e in EFFECTS]}


# ------------------------------------------------------------------ fonts
_FAMILIES: set[str] | None = None


def resolve_font(candidates: list[str]) -> str:
    global _FAMILIES
    if _FAMILIES is None:
        from PyQt6.QtGui import QFontDatabase

        _FAMILIES = set(QFontDatabase.families())
    for name in list(candidates) + FONT_FALLBACK:
        if name in _FAMILIES:
            return name
    return candidates[0]


# ------------------------------------------------------------------ colors
def _fill(value):
    from krok_helper.subtitle_render.domain.paint import PaintFill

    if isinstance(value, dict) and value.get("split"):  # 拼色: hard-edged bands, top to bottom
        cols = list(value["split"])
        n = len(cols)
        stops = [(round(100.0 * k / n, 3), c) for k, c in enumerate(cols)] + [(100, cols[-1])]
        return PaintFill(mode="split_vertical", color=cols[0], start_color=cols[0], end_color=cols[-1],
                         gradient_stops=[(0, cols[0]), (100, cols[-1])], split_top_color=cols[0],
                         split_bottom_color=cols[-1], split_position_pct=100.0 / n, split_stops=stops)
    if isinstance(value, (list, tuple)):
        a, b = value[0], value[-1]
        return PaintFill(mode="gradient_vertical", color=a, start_color=a, end_color=b,
                         gradient_stops=[(0, a), (100, b)], split_top_color=a, split_bottom_color=b,
                         split_stops=[(0, a), (50, b), (100, b)])
    return PaintFill(mode="solid", color=value, start_color=value, end_color=value,
                     gradient_stops=[(0, value), (100, value)], split_top_color=value,
                     split_bottom_color=value, split_stops=[(0, value), (50, value), (100, value)])


def karaoke_colors(before: dict, after: dict):
    from krok_helper.subtitle_render.domain.paint import KaraokeColors, KaraokeColorState

    def state(d):
        return KaraokeColorState(text=_fill(d["text"]), stroke=_fill(d["stroke"]),
                                 stroke2=_fill(d["stroke2"]), shadow=_fill(d["shadow"]))

    return KaraokeColors(before=state(before), after=state(after))


def _rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def _hex(rgb) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02X}" for c in rgb)


def shade(hex_color: str, light: float = 0.0, sat: float = 1.0) -> str:
    r, g, b = _rgb(hex_color)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = max(0.0, min(1.0, l + light))
    s = max(0.0, min(1.0, s * sat))
    return _hex(colorsys.hls_to_rgb(h, l, s))


# ------------------------------------------------------------------ build
def apply_template(style, tpl: dict):
    font = resolve_font(tpl["fonts"])
    fields = dict(
        font_family=font,
        font_family_latin=font,
        font_weight=tpl.get("weight", 700),
        font_size_px=tpl.get("size", 96),
        letter_spacing_px=tpl.get("spacing", 0),
        stroke_width_px=tpl.get("stroke", 10),
        stroke2_enabled=bool(tpl.get("stroke2_on")),
        stroke2_width_px=tpl.get("stroke2", 0),
        decoration_kind=tpl.get("decoration", "shadow"),
        karaoke_colors=karaoke_colors(tpl["before"], tpl["after"]),
        base_color=tpl["before"]["text"],
        fill_color=tpl["after"]["text"] if isinstance(tpl["after"]["text"], str) else tpl["after"]["text"][0],
        stroke_color=tpl["before"]["stroke"],
        ruby_font_size_px=int(tpl.get("size", 96) * 0.45),
        ruby_stroke_width_px=max(2, int(tpl.get("stroke", 10) * 0.6)),
        ruby_stroke2_enabled=bool(tpl.get("stroke2_on")),
        ruby_stroke2_width_px=max(0, int(tpl.get("stroke2", 0) * 0.6)),
        ruby_colors_follow_main=True,
    )
    if tpl.get("decoration") == "glow":
        g = tpl.get("glow", 10)
        fields.update(glow_radius_px=g, glow_before_radius_px=g, glow_after_radius_px=g,
                      glow_concentration_level=tpl.get("glow_level", 0))
    else:
        off = tpl.get("shadow_offset", 4)
        fields.update(shadow_offset_x=off, shadow_offset_y=off)
    return replace(style, **fields)


# karaoke animations that scale the glyphs (engine: Utopia intro 130 % / wipe 115 %, zoom pulse);
# the furigana does not move with them, so give it room or the popping glyph runs into it
POP_ANIMS = {"utopia", "zoom_pulse"}
POP_RUBY_GAP = 0.16  # x font size: clears the 130 % intro pop of a full-height glyph


def apply_effect(style, eff: dict):
    style = replace(style, **eff["fields"])
    if style.karaoke_anim in POP_ANIMS:
        gap = round(style.font_size_px * POP_RUBY_GAP)
        style = replace(style, ruby_gap_px=max(int(style.ruby_gap_px or 0), gap))
    return style


def singer_colors(singers: list[dict], custom: dict | None = None) -> dict[str, dict]:
    """name -> {"color", "bands"} for every singer. ``bands`` (2+ colours) means 拼色: parts sung
    together show each member's colour as a horizontal band of the glyph. Explicit ``colors`` on
    the singer win; a singer named after other singers (「A＆B」, "A&B", "A・B") gets theirs."""
    custom = custom or {}
    base = {s["name"]: (custom.get(s.get("id")) or {}).get("color") or s.get("color") or "#FF5FA2"
            for s in singers}
    out = {}
    for s in singers:
        bands = [c for c in (s.get("colors") or []) if c]
        if not bands:
            parts = [p.strip() for p in re.split(r"[＆&・+＋]", s["name"]) if p.strip()]
            if len(parts) > 1 and all(p in base for p in parts):
                bands = [base[p] for p in parts]
        out[s["name"]] = {"color": base[s["name"]], "bands": bands if len(bands) > 1 else []}
    return out


def blend(colors: list[str]) -> str:
    rgb = [_rgb(c) for c in colors]
    return _hex(tuple(sum(c[i] for c in rgb) / len(rgb) for i in range(3)))


def singer_scheme(tpl: dict, color: str, bands: list[str] | None = None):
    """Role scheme for one singer: template appearance with the singer's
    color as the sung (after) fill; with ``bands`` the sung fill is 拼色 (one band per colour)."""
    from krok_helper.subtitle_render.domain.models import SubtitleStyleScheme

    after = dict(tpl["after"])
    after["text"] = [shade(color, 0.12), shade(color, -0.08, 1.1)]
    if bands:
        after["text"] = {"split": [shade(c, 0.06) for c in bands]}
    if tpl.get("decoration") == "glow":
        after["shadow"] = color
    before = dict(tpl["before"])
    return SubtitleStyleScheme(karaoke_colors=karaoke_colors(before, after), fill_color=color)


def apply_overrides(style, overrides: dict | None):
    if not overrides:
        return style
    valid = {k: v for k, v in overrides.items() if hasattr(style, k)}
    return replace(style, **valid)


def web_style(tpl: dict, eff: dict, singers: list[dict], overrides: dict | None, *, font: str) -> dict:
    """Simplified description for the browser overlay preview."""
    def first(v):
        return v if isinstance(v, str) else v[0]

    def last(v):
        return v if isinstance(v, str) else v[-1]

    o = overrides or {}
    return {
        "font": font,
        "weight": o.get("font_weight", tpl.get("weight", 700)),
        "size": o.get("font_size_px", tpl.get("size", 96)),
        "spacing": o.get("letter_spacing_px", tpl.get("spacing", 0)),
        "before": {"top": first(tpl["before"]["text"]), "bottom": last(tpl["before"]["text"]),
                   "stroke": tpl["before"]["stroke"], "stroke2": tpl["before"]["stroke2"]},
        "after": {"top": first(tpl["after"]["text"]), "bottom": last(tpl["after"]["text"]),
                  "stroke": tpl["after"]["stroke"], "stroke2": tpl["after"]["stroke2"],
                  "glow": tpl["after"]["shadow"] if tpl.get("decoration") == "glow" else None},
        "stroke": o.get("stroke_width_px", tpl.get("stroke", 10)),
        "stroke2": o.get("stroke2_width_px", tpl.get("stroke2", 0)) if tpl.get("stroke2_on") else 0,
        "glow": tpl.get("glow", 0) if tpl.get("decoration") == "glow" else 0,
        "shadow": tpl.get("shadow_offset", 0) if tpl.get("decoration") != "glow" else 0,
        "entry": eff["fields"].get("entry_anim"),
        "exit": eff["fields"].get("exit_anim"),
        "singers": {s["id"]: {"name": s["name"], "color": s["color"],
                              "top": shade(s["color"], 0.12), "bottom": shade(s["color"], -0.08, 1.1),
                              "bands": [shade(c, 0.06) for c in s.get("bands") or []]}
                    for s in singers},
    }
