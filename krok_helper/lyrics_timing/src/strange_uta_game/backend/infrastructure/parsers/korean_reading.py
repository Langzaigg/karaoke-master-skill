# -*- coding: utf-8 -*-
"""韩文字（谚文）注音读音转换器（2026-10）。

三种标注风格（设置 → 打轴自动检查 → 韩文注音风格）：

- 片假名（默认）：韩日媒体通行对译口径——初声×中声合并为单假名、
  词中平音清浊变化（대구→テグ）、紧音词中带ッ（오빠→オッパ）、
  韵尾取首个可听音（한국→ハングク）。逐音节转写，不建模跨音节
  音变（连音等）；唱法特殊可手改 ruby。
- 平假名：片假名结果逐字符转平假名（사랑→さらん）。
- 罗马音：标准 RR 罗马字（ㅓ→eo、ㅡ→eu、ㄹ 词首 r 词尾 l；
  사랑→sarang）。**显示标注口径**——AI 打轴的对齐转写走
  ``ai_timing/transcription.hangul_to_phonetic``（声学口径），
  两者刻意分离。注意：RR 官方流程含「按发音换写」预处理
  （종로→종노 式跨音节音变），本模块为逐音节口径（同官方
  letter-by-letter 可逆变体的定位），跨音节音变不体现，可手改。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from strange_uta_game.backend.infrastructure.parsers.ruby_analyzer import (
    KoreanHanjaAnalyzer,
    RubyAnalyzer,
    RubyResult,
    _apply_initial_sound_law,
    _load_hanja_table,
)

KOREAN_ANNOTATION_STYLES = ("katakana", "hiragana", "romaji")

_HANGUL_BASE = 0xAC00
_HANGUL_COUNT = 11172


def _is_hangul_syllable(ch: str) -> bool:
    return _HANGUL_BASE <= ord(ch) < _HANGUL_BASE + _HANGUL_COUNT


def contains_hangul_syllable(text: str) -> bool:
    return any(_is_hangul_syllable(c) for c in text)


# ── 片假名：初声（行）× 中声（21 列，按音节编码序）对译表 ──
# 中声编码序：ㅏ ㅐ ㅑ ㅒ ㅓ ㅔ ㅕ ㅖ ㅗ ㅘ ㅙ ㅚ ㅛ ㅜ ㅝ ㅞ ㅟ ㅠ ㅡ ㅢ ㅣ
# （注意 ㅢ=19、ㅣ=20——预组音节块的编码序与 Unicode jamo 块不同）

_KATA_ROWS: Dict[str, List[str]] = {
    # ㅇ（ア行）
    "a": ["ア", "エ", "ヤ", "エ", "オ", "エ", "ヨ", "エ", "オ", "ワ",
          "ウェ", "ウィ", "ヨ", "ウ", "ウォ", "ウェ", "ウィ", "ユ", "ウ", "ウィ", "イ"],
    # ㄱ/ㄲ/ㅋ（カ行）
    "ka": ["カ", "ケ", "キャ", "ケ", "コ", "ケ", "キョ", "ケ", "コ", "クァ",
           "クェ", "クィ", "キョ", "ク", "クォ", "クェ", "クィ", "キュ", "ク", "クィ", "キ"],
    # ㄴ（ナ行）
    "na": ["ナ", "ネ", "ニャ", "ネ", "ノ", "ネ", "ニョ", "ネ", "ノ", "ヌァ",
           "ヌェ", "ヌィ", "ニョ", "ヌ", "ヌォ", "ヌェ", "ヌィ", "ニュ", "ヌ", "ヌィ", "ニ"],
    # ㄷ/ㄸ/ㅌ（タ行）
    "ta": ["タ", "テ", "テャ", "テ", "ト", "テ", "テョ", "テ", "ト", "トァ",
           "トェ", "トィ", "テョ", "トゥ", "トォ", "トェ", "トィ", "テュ", "トゥ", "トィ", "チ"],
    # ㄹ（ラ行）
    "ra": ["ラ", "レ", "リャ", "レ", "ロ", "レ", "リョ", "レ", "ロ", "ルァ",
           "ルェ", "リェ", "リョ", "ル", "ルォ", "ルェ", "リィ", "リュ", "ル", "リィ", "リ"],
    # ㅁ（マ行）
    "ma": ["マ", "メ", "ミャ", "メ", "モ", "メ", "ミョ", "メ", "モ", "ムァ",
           "ムェ", "ムィ", "ミョ", "ム", "ムォ", "ムェ", "ムィ", "ミュ", "ム", "ムィ", "ミ"],
    # ㅂ/ㅃ（パ行；ㅍ 另立）
    "pa": ["パ", "ペ", "ピャ", "ペ", "ポ", "ペ", "ピョ", "ペ", "ポ", "プァ",
           "プェ", "プィ", "ピョ", "プ", "プォ", "プェ", "プィ", "ピュ", "プ", "プィ", "ピ"],
    # ㅍ（パ行无声化；ㅡ 列作 フ：프→フ）
    "pha": ["パ", "ペ", "ピャ", "ペ", "ポ", "ペ", "ピョ", "ペ", "ポ", "プァ",
            "プェ", "プィ", "ピョ", "プ", "プォ", "プェ", "プィ", "ピュ", "フ", "プィ", "ピ"],
    # ㅅ/ㅆ（サ行）
    "sa": ["サ", "セ", "シャ", "セ", "ソ", "セ", "ショ", "セ", "ソ", "スァ",
           "スェ", "スィ", "ショ", "ス", "スォ", "スェ", "スィ", "シュ", "ス", "スィ", "シ"],
    # ㅈ/ㅉ（チャ行，词中浊化为 ザ/ジャ 行）
    "ja": ["チャ", "チェ", "チャ", "チェ", "チョ", "チェ", "チョ", "チェ", "チョ", "チュァ",
           "チュェ", "チィ", "チョ", "チュ", "チュォ", "チュェ", "チィ", "チュ", "チュ", "チィ", "チ"],
    # ㅊ（チャ行，不浊化）
    "cha": ["チャ", "チェ", "チャ", "チェ", "チョ", "チェ", "チョ", "チェ", "チョ", "チュァ",
            "チュェ", "チィ", "チョ", "チュ", "チュォ", "チュェ", "チィ", "チュ", "チュ", "チィ", "チ"],
    # ㅋ（カ行，不浊化）
    "kha": ["カ", "ケ", "キャ", "ケ", "コ", "ケ", "キョ", "ケ", "コ", "クァ",
            "クェ", "クィ", "キョ", "ク", "クォ", "クェ", "クィ", "キュ", "ク", "クィ", "キ"],
    # ㅌ（タ行，不浊化）
    "tha": ["タ", "テ", "テャ", "テ", "ト", "テ", "テョ", "テ", "ト", "トァ",
            "トェ", "トィ", "テョ", "トゥ", "トォ", "トェ", "トィ", "テュ", "トゥ", "トィ", "チ"],
    # ㅎ（ハ行）
    "ha": ["ハ", "ヘ", "ヒャ", "ヘ", "ホ", "ヘ", "ヒョ", "ヘ", "ホ", "ファ",
           "フェ", "フィ", "ヒョ", "フ", "フォ", "フェ", "フィ", "フュ", "フ", "フィ", "ヒ"],
}

# 初声（cho 编码序 0-18）→ 行键；紧音/送气音分立（浊化行为不同）
_CHO_ROW = {
    0: "ka", 1: "ka", 2: "na", 3: "ta", 4: "ta", 5: "ra", 6: "ma",
    7: "pa", 8: "pa", 9: "sa", 10: "sa", 11: "a", 12: "ja",
    13: "ja", 14: "cha", 15: "kha", 16: "tha", 17: "pha", 18: "ha",
}

# 紧音初声（带 ッ，词首亦然：떡볶이→トッポギ 的通行口径）
_CHO_TENSE = {1, 4, 8, 10, 13}

# 词中清浊变化仅限平音 ㄱ/ㄷ/ㅂ/ㅈ 行（ㅅ/送气音依惯例不浊化）
_VOICED_ROWS = {"ka", "ta", "pa", "ja"}

# 首字符浊化映射（カ→ガ、タ→ダ、パ→バ、チ→ジ）
_VOICE_FIRST = {
    "カ": "ガ", "キ": "ギ", "ク": "グ", "ケ": "ゲ", "コ": "ゴ",
    "タ": "ダ", "チ": "ジ", "ツ": "ヅ", "テ": "デ", "ト": "ド",
    "パ": "バ", "ピ": "ビ", "プ": "ブ", "ペ": "ベ", "ポ": "ボ",
}

# 韵尾（jong 编码序 0-27，0=无）→ 片假名；不除阻韵尾取首个可听音，
# ㅎ 弱化丢弃
_KATA_JONG = [
    "", "ク", "ク", "ク", "ン", "ン", "ン", "ト", "ル", "ク", "ム",
    "ル", "ル", "ル", "プ", "ル", "ム", "プ", "プ", "ト", "ト",
    "ン", "ト", "ト", "ク", "ト", "プ", "",
]

# ── 平假名：片假名逐字符 -0x60 区移（ャュョィゥォ 小假名同区间）──


def _kata_to_hira_char(ch: str) -> str:
    code = ord(ch)
    if 0x30A1 <= code <= 0x30F6:
        return chr(code - 0x60)
    return ch


# ── 罗马音：标准 RR（显示口径）──

_RR_CHO = {
    0: "g", 1: "kk", 2: "n", 3: "d", 4: "tt", 5: "r", 6: "m",
    7: "b", 8: "pp", 9: "s", 10: "ss", 11: "", 12: "j",
    13: "jj", 14: "ch", 15: "k", 16: "t", 17: "p", 18: "h",
}

_RR_JUNG = [
    "a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa",
    "wae", "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "ui", "i",
]

_RR_JONG = [
    "", "k", "k", "ks", "n", "nj", "nh", "t", "l", "lk", "lm",
    "lb", "ls", "lt", "lp", "lh", "m", "p", "ps", "t", "t",
    "ng", "t", "t", "k", "t", "p", "",
]


def _syllable_parts(ch: str):
    """谚文音节 → (初声 idx, 中声 idx, 韵尾 idx)；非音节返回 None。"""
    offset = ord(ch) - _HANGUL_BASE
    if not (0 <= offset < _HANGUL_COUNT):
        return None
    cho, rem = divmod(offset, 588)
    jung, jong = divmod(rem, 28)
    return cho, jung, jong


def _syllable_katakana(ch: str, word_initial: bool) -> str:
    parts = _syllable_parts(ch)
    if parts is None:
        return ch
    cho, jung, jong = parts
    row = _CHO_ROW[cho]
    kana = _KATA_ROWS[row][jung]
    if cho in _CHO_TENSE:
        kana = "ッ" + kana
    elif not word_initial and row in _VOICED_ROWS:
        first = _VOICE_FIRST.get(kana[0])
        if first:
            kana = first + kana[1:]
    return kana + _KATA_JONG[jong]


def _syllable_romaji(ch: str) -> str:
    parts = _syllable_parts(ch)
    if parts is None:
        return ch
    cho, jung, jong = parts
    return _RR_CHO[cho] + _RR_JUNG[jung] + _RR_JONG[jong]


def _is_word_initial(text: str, idx: int) -> bool:
    """词首判据（复用 KoreanHanjaAnalyzer 的口径）。"""
    return KoreanHanjaAnalyzer._is_word_initial(text, idx)


def _hanja_hangul(text: str, idx: int) -> Optional[str]:
    """汉字位 → 韩音谚文（词首두음법칙）；非汉字/未收录返回 None。"""
    ch = text[idx]
    if not KoreanHanjaAnalyzer._is_hanja(ch):
        return None
    hit = _load_hanja_table().get(ch)
    if not hit:
        return None
    return (
        _apply_initial_sound_law(hit)
        if _is_word_initial(text, idx)
        else hit
    )


def korean_readings(text: str, style: str) -> Dict[int, str]:
    """整行文本 → {字符索引: 风格读音}（仅韩文字与韩音汉字，其余不进表）。

    词首语境按整行判定（紧音 ッ、词中浊化、汉字두음법칙）。
    """
    readings: Dict[int, str] = {}
    for i, ch in enumerate(text):
        if _is_hangul_syllable(ch):
            if style == "romaji":
                readings[i] = _syllable_romaji(ch)
            else:
                kana = _syllable_katakana(ch, _is_word_initial(text, i))
                if style == "hiragana":
                    kana = "".join(_kata_to_hira_char(c) for c in kana)
                readings[i] = kana
        else:
            hangul = _hanja_hangul(text, i)
            if hangul:
                if style == "romaji":
                    readings[i] = "".join(
                        _syllable_romaji(c) for c in hangul
                    )
                else:
                    kana = "".join(
                        _syllable_katakana(c, _is_word_initial(text, i))
                        for c in hangul
                    )
                    if style == "hiragana":
                        kana = "".join(_kata_to_hira_char(c) for c in kana)
                    readings[i] = kana
    return readings


def hangul_to_katakana(word: str) -> str:
    """整词（或整行）→ 片假名连写（非韩文字符原样保留）。"""
    readings = korean_readings(word, "katakana")
    return "".join(readings.get(i, ch) for i, ch in enumerate(word))


def hangul_to_hiragana(word: str) -> str:
    readings = korean_readings(word, "hiragana")
    return "".join(readings.get(i, ch) for i, ch in enumerate(word))


def hangul_to_romaji(word: str) -> str:
    readings = korean_readings(word, "romaji")
    return "".join(readings.get(i, ch) for i, ch in enumerate(word))


class KoreanReadingAnalyzer(RubyAnalyzer):
    """韩文注音分析器：韩文字/汉字 → 按风格生成片假名/平假名/罗马音读音。

    纯查表无外部依赖；拉丁字母/数字/假名等其他字符原样透传
    （与 PinyinAnalyzer 的透传口径一致，供中文式逐字注音路径使用）。
    """

    def __init__(self, style: str = "katakana"):
        if style not in KOREAN_ANNOTATION_STYLES:
            raise ValueError(f"未知的韩文注音风格: {style}")
        self._style = style

    @property
    def style(self) -> str:
        return self._style

    def analyze(self, text: str) -> List[RubyResult]:
        readings = korean_readings(text, self._style)
        return [
            RubyResult(
                text=ch,
                reading=readings.get(i, ch),
                start_idx=i,
                end_idx=i + 1,
            )
            for i, ch in enumerate(text)
        ]

    def get_reading(self, text: str) -> str:
        if not text:
            return ""
        return "".join(r.reading for r in self.analyze(text))


def create_korean_reading_analyzer(
    style: str = "katakana",
) -> KoreanReadingAnalyzer:
    """创建韩文注音分析器（style ∈ katakana / hiragana / romaji）。"""
    return KoreanReadingAnalyzer(style)


__all__ = [
    "KOREAN_ANNOTATION_STYLES",
    "KoreanReadingAnalyzer",
    "create_korean_reading_analyzer",
    "korean_readings",
    "hangul_to_katakana",
    "hangul_to_hiragana",
    "hangul_to_romaji",
    "contains_hangul_syllable",
]
