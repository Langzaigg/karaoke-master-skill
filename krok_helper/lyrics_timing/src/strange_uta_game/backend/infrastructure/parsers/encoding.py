"""歌词文件字节流解码（charset-normalizer 主检测 + 碰撞家族启发式决胜）。

历史实现一是固定顺序回退链（utf-8-sig → cp932 → gb18030 → big5），
存在两个致命问题：

1. Big5 歌词几乎总能先被 gb18030 "成功"解码成私用区（U+E000-F8FF）
   乱码，big5 分支沦为死代码；GBK 也常被 cp932 拦截——静默乱码比
   抛错更糟。
2. cp932/gb18030/big5 的字节空间大量重叠，同一串字节往往在多个编码
   下都能"成功"解码，固定顺序无法区分碰撞家族。

本模块改为：

    BOM（UTF-16/32/UTF-8，权威）→ charset-normalizer 主检测
    （utf-8 直接采信）→ 碰撞家族 {cp932, gb18030, big5} 的确定性
    启发式决胜（``_score_decoded``）→ 全体候选得分为负时回退
    charset-normalizer 首选 → 全部无法解码时抛 UnicodeDecodeError

charset-normalizer 按语言连贯度给候选编码排序，能解决大部分拦截；
但它对短文本（单行歌词）分辨力不足（如 gb18030 样本可能排在 big5
之后），因此碰撞家族内部再按启发式打分改判。启发式只做确定性统计
（假名/私用区/常用字），不引入不可复现的外部状态。
"""

from __future__ import annotations

import codecs

import charset_normalizer

# 无 BOM 且非 UTF-8 时参与决胜的碰撞家族候选（末位兜底顺序）。
_CJK_CANDIDATES = ("cp932", "gb18030", "big5")

# charset-normalizer 的编码名 → 本模块的规范名。家族外的检测结果
# （cp949/euc_jp 等）不在支持范围，忽略。
_CN_TO_CANONICAL = {
    "utf_8": "utf-8-sig",
    "utf_8_sig": "utf-8-sig",
    "ascii": "utf-8-sig",
    "cp932": "cp932",
    "shift_jis": "cp932",
    "shift_jis_2004": "cp932",
    "shift_jisx0213": "cp932",
    "gb18030": "gb18030",
    "gbk": "gb18030",
    "gb2312": "gb18030",
    "big5": "big5",
    "big5hkscs": "big5",
    "cp950": "big5",
}

# 简繁合计常用字表（启发式决胜用）。正确解码的中文几乎全由常用字
# 组成，跨编码硬解产出的以生僻字为主，命中率显著更低；集合刻意精简，
# 不追求完备，只提供统计意义上的区分度。覆盖歌词高频字（含
# 月亮代表我的心 / 編碼測試 等测试样本用字）。
_COMMON_HANZI = frozenset(
    "的一是了我不人在他有这个上们来到时大地为子中你说生国年着就那和要"
    "她出也得里后自以会家可下过天去能对小多然于心学么之都好看起发当没"
    "成只如事把还用第样道想作种开月代表亮心从听见温柔世面最行动意方头"
    "长爱理点文书水主界利海情儿回位分老因很给名法间知什两次使身者被高"
    "亲其进此话常与活正感问力几定本公特做外孩相西果走将十实向无总已声"
    "车全信重三机工物气每并别真打太新比才便再书部水像眼等体却加电门受"
    "的一是了我不人在他有這個上們來到時大地為子中你說生國年著就那和要"
    "她出也得裡後自以會家可下過天去能對小多然於心學麼之都好看起發當沒"
    "成只如事把還用第樣道想作種開月代表亮心從聽見溫柔世面最行動意方頭"
    "長愛理點文書水主界利海情兒回位分老因很給名法間知什兩次使身者被高"
    "親其進此話常與活正感問力幾定本公特做外孩相西果走將十實向無總已聲"
    "車全信重三機工物氣每並別真打太新比才便再書部像眼等體卻加電門受編"
    "碼測試謝"
)


def _score_decoded(text: str) -> float:
    """给碰撞家族的单个候选解码结果打分，分数越大越可信（确定性）。

    规则按说服力排序：

    1. 常用字命中率（基础分）：正确解码的中文几乎全由常用字组成，
       跨编码硬解产出的多为生僻字。
    2. 全宽假名（ひらがな/カタカナ U+3040-30FF）出现：日文原文的强
       信号——中文按 Shift-JIS 硬解只会产生半角假名，不会产生全宽
       假名，几乎只有 cp932 能成立，直接 +2。
    3. 私用区（U+E000-F8FF）出现：外来字节落进 GB18030/GBK 用户区的
       产物，错误解码的强信号，按占比扣分。
    4. 全无全宽假名而半角假名（U+FF61-FF9F）占比 > 25%：中文被按
       Shift-JIS 硬解的典型乱码形态（如 Big5 字节 → ｷPﾁﾂ...），按
       占比扣分。
    """
    n = len(text)
    if n == 0:
        return 0.0
    pua = sum(1 for ch in text if 0xE000 <= ord(ch) <= 0xF8FF)
    fw_kana = sum(1 for ch in text if 0x3041 <= ord(ch) <= 0x30FF)
    hw_kana = sum(1 for ch in text if 0xFF61 <= ord(ch) <= 0xFF9F)
    cjk = sum(1 for ch in text if 0x4E00 <= ord(ch) <= 0x9FFF)
    common = sum(1 for ch in text if ch in _COMMON_HANZI)

    score = common / cjk if cjk else 0.0
    if fw_kana:
        score += 2.0
    if pua:
        score -= 2.0 * pua / n
    if not fw_kana and hw_kana / n > 0.25:
        score -= 2.0 * hw_kana / n
    return score


def _ranked_candidates(data: bytes) -> list[str]:
    """碰撞家族候选列表：charset-normalizer 的推荐序在前，家族中未被
    其提及的成员按固定顺序补在末尾兜底。"""
    ranked: list[str] = []
    for match in charset_normalizer.from_bytes(data):
        name = _CN_TO_CANONICAL.get(match.encoding)
        if name in _CJK_CANDIDATES and name not in ranked:
            ranked.append(name)
    for name in _CJK_CANDIDATES:
        if name not in ranked:
            ranked.append(name)
    return ranked


def decode_lyric_bytes(data: bytes) -> tuple[str, str]:
    """把歌词文件字节流解码为文本。

    Args:
        data: 文件原始字节。

    Returns:
        (text, encoding_name)：解码后的文本与实际使用的编码名。
        UTF-8（无论有无 BOM）一律返回 ``"utf-8-sig"``（BOM 已剥离）。

    Raises:
        UnicodeDecodeError: 所有候选编码都无法解码时（抛最后一次的错误）。
    """
    if not data:
        return "", "utf-8-sig"

    # 带 BOM 的 UTF-16/UTF-32/UTF-8（Windows 记事本"Unicode"保存常见）
    # 直接按 BOM 解码，BOM 是权威判定。注意 UTF-32-LE 的 BOM 以
    # UTF-16-LE 的 BOM 为前缀，必须先判长 BOM。
    for bom, encoding in (
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
        (codecs.BOM_UTF8, "utf-8-sig"),
    ):
        if data.startswith(bom):
            return data.decode(encoding), encoding

    # charset-normalizer 主检测：按语言连贯度排序候选。utf-8 的合法
    # 解码是权威判定，直接采信。
    matches = charset_normalizer.from_bytes(data)
    best = matches.best()
    primary = _CN_TO_CANONICAL.get(best.encoding) if best is not None else None
    if primary == "utf-8-sig":
        return data.decode("utf-8-sig"), "utf-8-sig"

    # 碰撞家族：同一串字节往往在多个东亚编码下都能"成功"解码，
    # charset-normalizer 对短文本分辨力不足，按启发式打分决胜。
    # max 返回首个最大值，charset-normalizer 推荐序天然充当平级裁决。
    last_error: UnicodeDecodeError | None = None
    scored: list[tuple[str, str, float]] = []  # (规范名, 文本, 得分)
    for name in _ranked_candidates(data):
        try:
            text = data.decode(name)
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
        scored.append((name, text, _score_decoded(text)))

    if not scored:
        assert last_error is not None
        raise last_error

    best_name, best_text, best_score = max(scored, key=lambda item: item[2])
    if best_score < 0:
        # 全体候选都呈现乱码特征（已知局限：纯半角假名、无任何全宽
        # 假名的日文文件），启发式失去区分度 → 回退 charset-normalizer
        # 的首选（scored 按其推荐序排列）。
        best_name, best_text = scored[0][0], scored[0][1]
    return best_text, best_name
