"""English pronunciation syllables and their original spelling offsets.

CMUdict -> offline G2P -> unsplit word. A weighted grapheme/phoneme alignment
maps pronunciation boundaries to letters; spelling hyphenation never dictates
the number of syllables. All offsets refer to the unmodified input spelling.
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from .english_g2p import predict_pronunciation
from .english_ruby import normalize_apostrophes

VOWELS = frozenset(
    [
        "AA",
        "AE",
        "AH",
        "AO",
        "AW",
        "AY",
        "EH",
        "ER",
        "EY",
        "IH",
        "IY",
        "OW",
        "OY",
        "UH",
        "UW",
    ]
)
_SINGLE_ONSETS = frozenset(
    [
        "B",
        "CH",
        "D",
        "DH",
        "F",
        "G",
        "HH",
        "JH",
        "K",
        "L",
        "M",
        "N",
        "P",
        "R",
        "S",
        "SH",
        "T",
        "TH",
        "V",
        "W",
        "Y",
        "Z",
        "ZH",
    ]
)
_CLUSTER_ONSETS = frozenset(
    tuple(cluster.split())
    for cluster in (
        "P R",
        "B R",
        "T R",
        "D R",
        "K R",
        "G R",
        "F R",
        "TH R",
        "SH R",
        "P L",
        "B L",
        "K L",
        "G L",
        "F L",
        "S L",
        "K W",
        "G W",
        "S W",
        "T W",
        "D W",
        "TH W",
        "S P",
        "S T",
        "S K",
        "S M",
        "S N",
        "S F",
        "P Y",
        "B Y",
        "F Y",
        "V Y",
        "K Y",
        "G Y",
        "M Y",
        "HH Y",
        "S P R",
        "S T R",
        "S K R",
        "S P L",
        "S K L",
        "S K W",
        "S K Y",
    )
)


def _base(phone: str) -> str:
    return phone.rstrip("012")


def syllabify_phonemes(phones):
    """Partition ARPAbet by vowel nuclei and legal maximal medial onsets."""
    nuclei = [i for i, phone in enumerate(phones) if _base(phone) in VOWELS]
    if not nuclei:
        return [list(phones)] if phones else []
    starts = [0]
    for left, right in zip(nuclei, nuclei[1:]):
        boundary = right
        for candidate in range(left + 1, right):
            onset = tuple(_base(p) for p in phones[candidate:right])
            if onset in _CLUSTER_ONSETS or (
                len(onset) == 1 and onset[0] in _SINGLE_ONSETS
            ):
                boundary = candidate
                break
        starts.append(boundary)
    return [list(phones[a:b]) for a, b in zip(starts, starts[1:] + [len(phones)])]


@lru_cache(maxsize=1)
def _dictionary():
    from .e2k_engine import EnglishToKanaEngine

    path = EnglishToKanaEngine._resolve_cmudict_path()
    words = {}
    if path is None:
        return words
    try:
        with path.open(encoding="latin-1") as source:
            for line in source:
                parts = line.split()
                if len(parts) < 2 or not parts[0][0].isalpha():
                    continue
                word = parts[0].lower()
                if "(" in word:
                    continue  # deterministic primary pronunciation, like existing e2k
                phones = tuple(parts[1:])
                if any(_base(p) in VOWELS for p in phones):
                    words.setdefault(word, phones)
    except OSError:
        return {}
    return words


@lru_cache(maxsize=32768)
def pronunciation_for_word(word: str) -> Optional[tuple[str, ...]]:
    key = normalize_apostrophes(word).lower()
    return cmu_pronunciation_for_word(key) or predict_pronunciation(key)


def cmu_pronunciation_for_word(word: str) -> Optional[tuple[str, ...]]:
    """Dictionary-only lookup for callers whose Latin input may be romaji."""
    return _dictionary().get(normalize_apostrophes(word).lower())


def syllabify_word_phonemes(word, phones):
    """Keep evidenced compound/inflection boundaries before onset maximization.

    A spelling suffix alone is insufficient: its stem pronunciation must match
    the corresponding CMU phones. This keeps heart-ache, blind-ed and fall-ing
    without inventing a break in sing, thing or open.
    """
    key = normalize_apostrophes(word).lower()
    bases = tuple(_base(p) for p in phones)
    dictionary = _dictionary()
    if key.endswith("n't") and bases[-3:] == ("AH", "N", "T"):
        return syllabify_phonemes(phones[:-3]) + [list(phones[-3:])]
    for boundary in range(3, len(key) - 2):
        left = dictionary.get(key[:boundary])
        right = dictionary.get(key[boundary:])
        if left and right and tuple(_base(p) for p in left + right) == bases:
            split = len(left)
            return syllabify_word_phonemes(
                key[:boundary], phones[:split]
            ) + syllabify_word_phonemes(key[boundary:], phones[split:])
    for suffix, ending in (
        ("ing", ("IH", "NG")),
        ("ed", ("IH", "D")),
        ("ed", ("AH", "D")),
        ("en", ("AH", "N")),
        ("ly", ("L", "IY")),
        ("ness", ("N", "AH", "S")),
        ("less", ("L", "AH", "S")),
    ):
        if not key.endswith(suffix) or bases[-len(ending) :] != ending:
            continue
        stem = key[: -len(suffix)]
        prefix = bases[: -len(ending)]
        # A doubled consonant belongs across the displayed syllable boundary
        # (run-ning, hid-den). Treat these with ordinary onset maximization;
        # stripping the duplicate also invents stems such as hap + en.
        if len(stem) >= 2 and stem[-1] == stem[-2] and stem[-1] not in "aeiouy":
            continue
        candidates = [stem, stem + "e"]
        if stem.endswith("i"):
            candidates.append(stem[:-1] + "y")
        for candidate in candidates:
            known = dictionary.get(candidate)
            if known and tuple(_base(p) for p in known) == prefix:
                return syllabify_word_phonemes(candidate, phones[: -len(ending)]) + [
                    list(phones[-len(ending) :])
                ]
    return syllabify_phonemes(phones)


# Many-to-many spelling/phone correspondences. DP must consume every character
# and every phone. Silent letters are explicitly costed; unknown substitutions
# are not invented to make a desired syllable count fit.
_SPELLINGS = {
    "a": "AE AH AA AO EY EH",
    "e": "EH IY AH IH ER",
    "i": "IH AY IY AH",
    "o": "AA AO OW AH UH UW",
    "u": "AH UW UH IH Y+UW Y+UH Y+AH",
    "y": "IY IH AY Y AH",
    "b": "B",
    "c": "K S SH",
    "d": "D JH T",
    "f": "F",
    "g": "G JH ZH",
    "h": "HH",
    "j": "JH Y",
    "k": "K",
    "l": "L AH+L",
    "m": "M AH+M",
    "n": "N NG AH+N",
    "p": "P",
    "q": "K",
    "r": "R ER",
    "s": "S Z SH ZH",
    "t": "T SH CH",
    "v": "V",
    "w": "W",
    "x": "K+S G+Z Z",
    "z": "Z",
    "ai": "EY EH AE",
    "ay": "EY",
    "au": "AO AA",
    "aw": "AO",
    "ao": "AW",
    "ea": "IY EH EY",
    "ee": "IY",
    "ei": "IY EY AY",
    "ey": "IY EY",
    "ie": "IY AY",
    "oa": "OW",
    "oe": "OW UW AH",
    "oi": "OY",
    "oy": "OY",
    "oo": "UW UH",
    "ou": "AW AH UW OW UH",
    "ow": "AW OW",
    "ue": "UW",
    "ui": "UW IH",
    "eu": "Y+UW UW",
    "ew": "Y+UW UW",
    "uy": "AY",
    "ar": "AA+R ER AO+R",
    "er": "ER",
    "ir": "ER",
    "or": "AO+R ER",
    "ur": "ER",
    "ear": "ER IY+R EH+R AA+R",
    "air": "EH+R",
    "are": "EH+R",
    "eer": "IY+R",
    "ore": "AO+R",
    "our": "AO+R ER AW+R",
    "ch": "CH K SH",
    "sh": "SH",
    "th": "TH DH",
    "ph": "F",
    "wh": "W HH+W HH",
    "ng": "NG NG+G",
    "nk": "NG+K",
    "ck": "K",
    "qu": "K+W K",
    "gh": "F G",
    "tch": "CH",
    "dge": "JH",
    "dg": "JH",
    "sc": "S S+K",
    "sch": "S+K SH",
    "ti": "SH CH",
    "ci": "SH",
    "si": "ZH SH",
    "ssi": "SH",
    "sion": "ZH+AH+N SH+AH+N",
    "tion": "SH+AH+N",
    "cian": "SH+AH+N",
    "ough": "OW UW AO AW AH+F AO+F",
    "eigh": "EY",
    "igh": "AY",
    "wr": "R",
    "kn": "N",
    "gn": "N",
    "ps": "S",
    "mb": "M",
    "bb": "B",
    "cc": "K K+S",
    "dd": "D",
    "ff": "F",
    "gg": "G",
    "ll": "L",
    "mm": "M",
    "nn": "N",
    "pp": "P",
    "rr": "R",
    "ss": "S Z",
    "tt": "T",
    "zz": "Z",
    "es": "IH+Z AH+Z",
    "ed": "IH+D AH+D D T",
}
_RULES = {
    spelling: tuple(tuple(option.split("+")) for option in choices.split())
    for spelling, choices in _SPELLINGS.items()
}


def _letter_alignment(word, phones, *, allow_syllabic_consonants=False):
    """Map explicit vowel spellings first, then allow syllabic consonants.

    A vowel digraph followed by an invented consonant nucleus must not displace
    available vowel letters (po-em, sci-ence). The second pass still permits
    genuine syllabic consonants in words such as rhythm and bottle.
    """
    n, m = len(word), len(phones)
    bases = tuple(_base(p) for p in phones)
    best = {(0, 0): (0.0, ())}
    for i in range(n):
        for j in range(m + 1):
            previous = best.get((i, j))
            if previous is None:
                continue
            cost, spans = previous

            def update(
                end,
                phone_count,
                penalty,
                anchor=None,
                phone_spans=None,
                *,
                i=i,
                j=j,
                cost=cost,
                spans=spans,
            ):
                target = (end, j + phone_count)
                score = cost + penalty
                if score < best.get(target, (float("inf"),))[0]:
                    mapped = (
                        phone_spans
                        if phone_spans is not None
                        else ((i if anchor is None else anchor, end),) * phone_count
                    )
                    best[target] = (score, spans + mapped)

            for length in range(1, min(4, n - i) + 1):
                spelling = word[i : i + length]
                for variant, emitted in enumerate(_RULES.get(spelling, ())):
                    if bases[j : j + len(emitted)] != emitted:
                        continue
                    syllabic = length == 1 and len(emitted) > 1 and spelling in "lmn"
                    if syllabic and not allow_syllabic_consonants:
                        continue
                    penalty = 1.0 + variant * 0.025
                    # Doubled consonants share a sound; its onset belongs to the
                    # second letter when another syllable follows (hap-py).
                    anchor = (
                        i + 1
                        if length == 2
                        and spelling[0] == spelling[1]
                        and spelling[0] not in "aeiouy"
                        else i
                    )
                    if syllabic:
                        penalty += 1.0  # syllabic consonants, not ordinary l/m/n
                    phone_spans = None
                    if length == len(emitted) > 1 and all(
                        (phone,) in _RULES.get(letter, ())
                        for letter, phone in zip(spelling, emitted)
                    ):
                        phone_spans = tuple((i + k, i + k + 1) for k in range(length))
                    update(i + length, len(emitted), penalty, anchor, phone_spans)
            # Apostrophes never cost a sound. Silent e is common; other silent
            # letters are allowed only at a higher cost than an actual match.
            silent_cost = (
                0.05
                if word[i] in "'."
                else (0.5 if word[i] == "e" and i == n - 1 else 3.5)
            )
            update(i + 1, 0, silent_cost)
    result = best.get((n, m))
    if result is None or result[0] > max(n, m) * 1.8:
        if not allow_syllabic_consonants:
            return _letter_alignment(word, phones, allow_syllabic_consonants=True)
        return None
    return result[1]


@dataclass(frozen=True)
class EnglishSyllables:
    offsets: tuple[int, ...]
    phonemes: tuple[str, ...] = ()
    syllables: tuple[tuple[str, ...], ...] = ()
    source: str = "whole_word"


@lru_cache(maxsize=32768)
def analyze_english_word(word: str) -> EnglishSyllables:
    """Analyze one word, preserving case/apostrophes and original positions."""
    key = normalize_apostrophes(word).lower()
    if (
        not key
        or len(key) > 64
        or not all(c in "abcdefghijklmnopqrstuvwxyz'." for c in key)
    ):
        return EnglishSyllables((0,))
    phones = pronunciation_for_word(key)
    if not phones:
        return EnglishSyllables((0,))
    groups = syllabify_word_phonemes(key, phones)
    source = "cmudict" if key in _dictionary() else "g2p"
    if len(groups) == 1:
        return EnglishSyllables((0,), phones, tuple(map(tuple, groups)), source)
    spans = _letter_alignment(key, phones)
    if spans is None:
        return EnglishSyllables((0,), phones, tuple(map(tuple, groups)), "unaligned")
    offsets = [0]
    phone_index = 0
    for group in groups[:-1]:
        phone_index += len(group)
        offset = spans[phone_index][0]
        # A digraph may straddle a phonetic boundary (e.g. x -> K+S).
        # Keep its letters together and start on the following letter.
        if offset <= offsets[-1] or spans[phone_index] == spans[phone_index - 1]:
            offset = spans[phone_index][1]
        while offset < len(key) and key[offset] in "'.":
            offset += 1
        if offset >= len(key) or offset <= offsets[-1]:
            return EnglishSyllables(
                (0,), phones, tuple(map(tuple, groups)), "unaligned"
            )
        offsets.append(offset)
    return EnglishSyllables(tuple(offsets), phones, tuple(map(tuple, groups)), source)
