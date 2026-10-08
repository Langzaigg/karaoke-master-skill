# -*- coding: utf-8 -*-
"""读音 → 罗马字风格表音转写（对齐 token 预处理，2026-08）。

默认对齐模型在日文罗马字分布上微调：拼音/英文按原样透传与模型分布
不匹配，是纯中文/纯英文歌曲对齐精度低的主因。本模块按参考项目口径
转写（转写发生在主进程构建对齐请求时，worker 无需额外依赖）：

- 中文（FA-Kara 口径）：拼音 → 表音拼写（声母/韵母映射表 + 整体
  认读特例），如 zhong→jong、xiao→shyao、lü→ryu、zhi→jru；
- 韩文（FA-Kara 式声学口径）：谚文音节算术分解为初/中/终声 jamo 后
  查静态映射表，如 사랑해→sa/rang/he、강남→gang/nam、학교→hak/gyo；
- 英文（SUG 自有技术优先）：e2k.txt 词典（CMU 加工的英単語→片假名，
  ``EnglishRubyLookup``）查片假名 → 按拍切分（拗音/长音附前拍）→
  走与日文路径同一个罗马字转换器（take→テイク→te/i/ku）。词典
  未收录回退 pyphen 表面切分，再回退整词小写——pyphen 缺席时
  仍不阻断对齐。
"""

from __future__ import annotations

import re
import threading
from typing import Callable, List, Optional

# ── 中文：拼音 → 表音（FA-Kara PINYIN_TO_PHONETIC 移植）──

_PINYIN_INITIALS = {
    'b': 'b', 'p': 'p', 'm': 'm', 'f': 'f',
    'd': 'd', 't': 't', 'n': 'n', 'l': 'r',
    'g': 'g', 'k': 'k', 'h': 'h',
    'j': 'j', 'q': 'ch', 'x': 'sh',
    'zh': 'j', 'ch': 'ch', 'sh': 'sh', 'r': 'r',
    'z': 'z', 'c': 'ts', 's': 's',
    'y': 'y', 'w': 'w',
}

_PINYIN_FINALS = {
    'a': 'a', 'o': 'o', 'e': 'e', 'i': 'i', 'u': 'u', 'ü': 'yu',
    'ai': 'ai', 'ei': 'ei', 'ao': 'ao', 'ou': 'ou',
    'an': 'an', 'en': 'en', 'ang': 'ang', 'eng': 'eng', 'ong': 'ong',
    'ia': 'ya', 'ie': 'ye', 'iao': 'yao', 'iu': 'yu',
    'ian': 'yan', 'in': 'in', 'iang': 'yang', 'ing': 'ing', 'iong': 'yong',
    'ua': 'wa', 'uo': 'wo', 'uai': 'wai', 'ui': 'wei',
    'uan': 'wan', 'un': 'wen', 'uang': 'wang', 'ueng': 'weng',
    'üe': 'yue', 'üan': 'yuan', 'ün': 'yun',
    'er': 'a', 'io': 'yo',
}

_PINYIN_SPECIAL = {
    'zhi': 'jru', 'chi': 'chu', 'shi': 'shu', 'ri': 'ru',
    'zi': 'zu', 'ci': 'tsu', 'si': 'su',
    'yi': 'i', 'wu': 'u', 'yu': 'yu',
    'ye': 'ye', 'yue': 'yue', 'yuan': 'yuen',
    'yin': 'in', 'yun': 'yun', 'ying': 'ing',
}

# 声调符号 → 基字母（Style.TONE 形式 "lǜ"/"zhōng" 先归一化）
_TONE_MARKS = str.maketrans(
    "āáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜ",
    "aaaaeeeeiiiioooouuuuüüüü",
)


def pinyin_to_phonetic(pinyin: str) -> str:
    """拼音音节 → 表音拼写（FA-Kara convert_pinyin_to_phonetic 口径）。

    接受带声调符号/声调数字/v 代 ü 的输入；无法分解时原样返回小写。
    """
    text = pinyin.strip().translate(_TONE_MARKS)
    text = re.sub(r"[1-5]", "", text).replace("v", "ü").lower()
    if not text:
        return text
    if text in _PINYIN_SPECIAL:
        return _PINYIN_SPECIAL[text]

    initial = ""
    for cand in sorted(_PINYIN_INITIALS, key=len, reverse=True):
        if text.startswith(cand):
            initial = cand
            break
    final = text[len(initial):] if initial else text

    phonetic_initial = _PINYIN_INITIALS.get(initial, initial)
    phonetic_final = _PINYIN_FINALS.get(final, final)

    if not initial:
        # 零声母 i/u/ü 开头补 y/w
        if final.startswith("i"):
            phonetic_final = "y" + phonetic_final
        elif final.startswith("u"):
            phonetic_final = "w" + phonetic_final
        elif final.startswith("ü"):
            phonetic_final = (
                "yu" + phonetic_final[2:] if len(phonetic_final) > 2 else "yu"
            )
    if initial in ("j", "q", "x") and final.startswith("ü"):
        # j/q/x 后的 ü 写作 u
        phonetic_final = (
            "u" + phonetic_final[2:] if len(phonetic_final) > 2 else "u"
        )
    return phonetic_initial + phonetic_final


# ── 韩文：谚文音节 → 罗马字表音（jamo 算术分解 + 静态映射）──

# 音节块算术分解常数：U+AC00 + cho*588 + jung*28 + jong
_HANGUL_SYL_BASE = 0xAC00
_HANGUL_SYL_COUNT = 11172  # 19 * 21 * 28

# 初声（choseong，19 个）→ 罗马字；紧音（ㄲㄸㅃㅉㅆ）按平音同位处理：
# 单音节 token 内无从体现紧音的长闭塞段，声学上与平音分布一致
_HANGUL_CHO = [
    'g', 'g', 'n', 'd', 'd', 'r', 'm', 'b', 'b', 's', 's', '',
    'j', 'j', 'ch', 'k', 't', 'p', 'h',
]

# 中声（jungseong，21 个）→ 罗马字。音节块槽位顺序注意：ㅢ=19、
# ㅣ=20（预组音节的编码序与 Unicode jamo 块不同）。ㅓ→o、ㅡ→u 是
# 声学选择：对齐模型在日文罗马字分布上微调，词表无 eo/eu 组合
# （e+o 会被读成两个元音）；ㅐ/ㅔ 同归 e，ㅙ/ㅚ/ㅞ 同归 we（现代
# 首尔音 [we]）
_HANGUL_JUNG = [
    'a', 'e', 'ya', 'ye', 'o', 'e', 'yo', 'ye', 'o', 'wa', 'we', 'we',
    'yo', 'u', 'wo', 'we', 'wi', 'yu', 'u', 'ui', 'i',
]

# 终声（jongseong，28 个槽位，0=无）→ 罗马字。不除阻韵尾只保留首个
# 可听辅音痕迹：ㅅ/ㅆ/ㅈ/ㅊ/ㅌ 收尾实际为 [t̚]→t，ㄹ 系（含复合）→r
# （与英文 L→r 同因：模型分布无 l），ㅎ 收尾弱化丢弃
_HANGUL_JONG = [
    '', 'k', 'k', 'k', 'n', 'n', 'n', 't', 'r', 'k', 'm', 'r', 'r', 'r',
    'p', 'r', 'm', 'p', 'p', 't', 't', 'ng', 't', 't', 'k', 't', 'p', '',
]

# 颚化：ㅅ/ㅆ 后接 i/y 类中声时读 [ɕ] → sh（시→shi、셔→shyo）
# （在 _hangul_syllable_phonetic 内联判定）


def _hangul_syllable_phonetic(ch: str) -> str:
    """单个谚文音节块 → 罗马字表音拼写（非音节块原样返回）。"""
    code = ord(ch)
    offset = code - _HANGUL_SYL_BASE
    if not (0 <= offset < _HANGUL_SYL_COUNT):
        return ch
    cho, rem = divmod(offset, 588)
    jung, jong = divmod(rem, 28)
    initial = _HANGUL_CHO[cho]
    medial = _HANGUL_JUNG[jung]
    if initial == 's' and (medial.startswith('i') or medial.startswith('y')):
        initial = 'sh'
    return initial + medial + _HANGUL_JONG[jong]


def contains_hangul(text: str) -> bool:
    """文本是否含谚文音节块（alignment 侧按内容路由汉字韩音读音用）。"""
    return any(
        _HANGUL_SYL_BASE <= ord(c) <= _HANGUL_SYL_BASE + _HANGUL_SYL_COUNT - 1
        for c in text
    )


def hangul_to_phonetic(text: str) -> str:
    """谚文读音 → 罗马字表音拼写（对齐 token 口径，与拼音路径同构）。

    逐音节分解为初/中/终声 jamo 后查静态映射表（FA-Kara 式声学贴近
    转写，非严格 RR）；非谚文字符（用户手标拉丁读音等）原样小写透传。
    跨音节音变（连音/跨拍颚化）不做——与拼音的正字法转写口径一致。
    """
    if not text:
        return text
    if not contains_hangul(text):
        return text.lower()
    return "".join(
        _hangul_syllable_phonetic(c) if contains_hangul(c) else c.lower()
        for c in text
    )


# ── 英文：e2k 词典 → 片假名 → 按拍罗马字（SUG 自有数据）──

_LAZY_INIT_LOCK = threading.Lock()
"""惰性加载缓存的初始化锁（双线程同时触发加载只做一次；结果查询
依赖 GIL 保证读写安全，无需全程持锁）。"""

_E2K_LOOKUP_CACHE: Optional[Callable[[str], Optional[str]]] = None
"""惰性持有的 e2k 查询函数；测试可整体替换（封闭环境）。"""

_PYPhen_CACHE: Optional[object] = None
_ENGLISH_CACHE: dict = {}

# 拗音/合拗音的小假名与长音符：附到前一拍
_ATTACH_TO_PREV = set("ァィゥェォャュョヮー")


def _e2k_lookup(word: str) -> Optional[str]:
    """e2k.txt 词表查询（EnglishRubyLookup 单例，首次调用加载）。"""
    global _E2K_LOOKUP_CACHE
    if _E2K_LOOKUP_CACHE is None:
        with _LAZY_INIT_LOCK:
            if _E2K_LOOKUP_CACHE is None:
                try:
                    from strange_uta_game.backend.infrastructure.parsers.english_ruby import (
                        EnglishRubyLookup,
                    )

                    inst = EnglishRubyLookup.instance()
                    if inst.has():
                        _E2K_LOOKUP_CACHE = inst.lookup
                    else:
                        _E2K_LOOKUP_CACHE = lambda w: None
                except Exception:
                    _E2K_LOOKUP_CACHE = lambda w: None
    return _E2K_LOOKUP_CACHE(word)


def _katakana_mora(text: str) -> List[str]:
    """片假名切拍：拗音小假名与长音符附到前一拍。"""
    moras: List[str] = []
    for ch in text:
        if moras and ch in _ATTACH_TO_PREV:
            moras[-1] += ch
        else:
            moras.append(ch)
    return moras


def _pyphen_dic():
    global _PYPhen_CACHE
    if _PYPhen_CACHE is None:
        with _LAZY_INIT_LOCK:
            if _PYPhen_CACHE is None:
                try:
                    import pyphen

                    _PYPhen_CACHE = pyphen.Pyphen(lang="en_US")
                except Exception:
                    _PYPhen_CACHE = False
    return _PYPhen_CACHE or None


def english_word_syllables(word: str) -> List[str]:
    """英文词 → 每拍罗马字读音列表（e2k → pyphen → 整词小写逐级回退）。

    e2k 命中：片假名按拍切分后走与日文路径相同的罗马字转换器
    （take→テイク→["te","i","ku"]）。结果按词缓存。

    音素路径（``english_word_phoneme_syllables``）未命中时的回退口径，
    词内 token 仍由 provider 按比例切分。
    """
    word = word.strip()
    if not word:
        return []
    key = word.lower()
    if key in _ENGLISH_CACHE:
        return list(_ENGLISH_CACHE[key])

    result: List[str] = []
    kana = _e2k_lookup(key)
    if kana:
        try:
            from strange_uta_game.backend.application.ai_timing.alignment import (
                romanize_ruby_parts,
            )

            roms = romanize_ruby_parts(
                _katakana_mora(kana), for_alignment=True
            )
            result = [r for r in roms if r and r.strip()]
        except Exception:
            result = []
    if not result:
        dic = _pyphen_dic()
        if dic is not None:
            surface = [s.lower() for s in dic.inserted(key).split("-")]
            result = [s for s in surface if s]
    if not result:
        result = [key]

    _ENGLISH_CACHE[key] = list(result)
    return list(result)


# ── 英文：CMU 音素 → 音节罗马字（FA-Kara 口径，MIT）──
# 与 e2k（词→片假名→按拍）不同：读音直接来自 cmudict ARPAbet 音素，
# 使用与英文节奏点相同的发音分音节器，再逐音素映射成罗马字。
# 每个发音音节可独立对齐，不再按 Pyphen 拼写断字数合并。
# （e2k 按拍口径词内边界靠比例切分，实测不如音素口径准）。
# 音素映射表取自 https://github.com/moriwx/FA-Kara（MIT），未内嵌其代码。

_CMU_PHONEME_ROMAJI = {
    'AA': 'a', 'AE': 'a', 'AH': 'a', 'AO': 'o', 'AW': 'au', 'AY': 'ai',
    'B': 'b', 'CH': 'ch', 'D': 'd', 'DH': 'z', 'EH': 'e', 'ER': 'a',
    'EY': 'ei', 'F': 'f', 'G': 'g', 'HH': 'h', 'IH': 'i', 'IY': 'i',
    'JH': 'j', 'K': 'k', 'L': 'r', 'M': 'm', 'N': 'n', 'NG': 'ng',
    'OW': 'o', 'OY': 'oi', 'P': 'p', 'R': 'r', 'S': 's', 'SH': 'sh',
    'T': 't', 'TH': 's', 'UH': 'u', 'UW': 'u', 'V': 'v', 'W': 'w',
    'Y': 'y', 'Z': 'z', 'ZH': 'j',
}

_CMU_VOWELS = {
    'AA', 'AE', 'AH', 'AO', 'AW', 'AY', 'EH', 'ER', 'EY',
    'IH', 'IY', 'OW', 'OY', 'UH', 'UW',
}

_CMU_LOOKUP_CACHE: Optional[Callable[[str], Optional[List[str]]]] = None
"""惰性持有的 cmudict 查询函数（word_lower → 首选音素序列）；测试可整体替换。"""

_PHONEME_CACHE: dict = {}


def _cmu_lookup(word: str) -> Optional[List[str]]:
    """仅查 CMU；对齐输入可能是 ruby 罗马字，不能交给英文 G2P 猜读。"""
    global _CMU_LOOKUP_CACHE
    if _CMU_LOOKUP_CACHE is None:
        with _LAZY_INIT_LOCK:
            if _CMU_LOOKUP_CACHE is not None:
                return _CMU_LOOKUP_CACHE(word)
            from strange_uta_game.backend.infrastructure.parsers.english_syllables import (
                cmu_pronunciation_for_word,
            )

            def lookup(key):
                phones = cmu_pronunciation_for_word(key)
                return list(phones) if phones else None

            _CMU_LOOKUP_CACHE = lookup
    return _CMU_LOOKUP_CACHE(word)


def _phoneme_syllabify(phonemes: List[str]) -> List[List[str]]:
    """共享音素分音节：保留合法的节首辅音串。"""
    from strange_uta_game.backend.infrastructure.parsers.english_syllables import (
        syllabify_phonemes,
    )

    return syllabify_phonemes(phonemes)


def english_word_phoneme_syllables(word: str) -> Optional[List[str]]:
    """英文词 → CMU 发音音节罗马字；词典未收录返回 None。

    每个元音核保留一个音节，绝不按排版断字数合并或平均分配。
    命中结果逐音节独立对齐（alignment 侧不进 word_groups）。
    G2P 仅用于自动英文节奏点，避免重读片假名 ruby 的罗马字和日语罗马字。
    """
    word = word.strip()
    if not word:
        return None
    if word == "a":
        return ["a"]
    if word == "A":
        return ["ei"]
    key = word.lower()
    if key in _PHONEME_CACHE:
        return list(_PHONEME_CACHE[key]) if _PHONEME_CACHE[key] else None

    result: Optional[List[str]] = None
    phonemes = _cmu_lookup(key)
    if phonemes:
        from strange_uta_game.backend.infrastructure.parsers.english_syllables import (
            syllabify_word_phonemes,
        )

        groups = syllabify_word_phonemes(word, phonemes)
        roms = [
            "".join(
                _CMU_PHONEME_ROMAJI.get(ph.rstrip("012"), "")
                for ph in group
            )
            for group in groups
        ]
        roms = [r for r in roms if r]
        if roms:
            result = roms

    _PHONEME_CACHE[key] = list(result) if result else []
    return list(result) if result else None


# ── 数字 → 英文读音（FA-Kara number_to_english 移植）──

_NUM_ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven",
    "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
    "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
]
_NUM_TENS = [
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety",
]


def number_to_english(number_str: str) -> str:
    """阿拉伯数字 → 英文读法（整数与小数；解析失败返回空串）。"""
    try:
        if "." in number_str:
            num = float(number_str)
        else:
            num = int(number_str)
    except ValueError:
        return ""

    if isinstance(num, float):
        integer_part = int(num)
        decimal_part = round(num - integer_part, 3)
        words = (
            number_to_english(str(integer_part)) if integer_part > 0 else ""
        )
        decimal_str = f"{decimal_part:.3f}"[2:]
        decimal_words = " point"
        for digit in decimal_str:
            if digit == "0" and not decimal_words.endswith(" zero"):
                decimal_words += " zero"
            elif digit != "0":
                decimal_words += " " + _NUM_ONES[int(digit)]
        return (words + decimal_words).strip()

    if num < 0:
        return "minus " + number_to_english(str(abs(num)))
    if num < 20:
        return _NUM_ONES[num]
    if num < 100:
        return _NUM_TENS[num // 10] + (
            (" " + _NUM_ONES[num % 10]) if num % 10 != 0 else ""
        )
    if num < 1000:
        return _NUM_ONES[num // 100] + " hundred" + (
            (" and " + number_to_english(str(num % 100)))
            if num % 100 != 0
            else ""
        )
    for scale_value, scale_name in (
        (10**12, "trillion"),
        (10**9, "billion"),
        (10**6, "million"),
        (10**3, "thousand"),
    ):
        if num >= scale_value:
            return number_to_english(str(num // scale_value)) + " " + (
                scale_name
            ) + (
                (" " + number_to_english(str(num % scale_value)))
                if num % scale_value != 0
                else ""
            )
    return ""


def english_number_reading(number_str: str) -> str:
    """数字 → 英文读法 → 音素口径罗马字（单一 token 文本；失败返回空串）。

    数字单位通常是一个时间点（整段读唱），与 FA-Kara 的 surf=False
    同口径：全部音节读音拼成一个字符串。读音优先走 CMU 音素表，
    未收录回退 e2k 按拍口径。
    """
    words = number_to_english(number_str).split()
    if not words:
        return ""
    parts: List[str] = []
    for word in words:
        syllables = english_word_phoneme_syllables(word)
        if syllables is None:
            syllables = english_word_syllables(word)
        if syllables:
            parts.append("".join(syllables))
    return "".join(parts).strip()


__all__ = [
    "pinyin_to_phonetic",
    "hangul_to_phonetic",
    "contains_hangul",
    "english_word_syllables",
    "english_word_phoneme_syllables",
    "number_to_english",
    "english_number_reading",
]
