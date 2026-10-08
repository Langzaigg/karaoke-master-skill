"""Offline English OOV pronunciation (g2p-en's Apache-2.0 GRU weights).

The NumPy inference equations are adapted from Kyubyong Park and Jongseok
Kim's g2p-en (https://github.com/Kyubyong/g2p). Only word prediction is used:
no NLTK import, POS tagging, downloads, subprocesses or user-data writes.
See config/g2p_en_LICENSE.txt and docs/english_syllables.md for attribution.
"""

from functools import lru_cache
from pathlib import Path
from typing import Optional

_PHONES = [
    "<pad>",
    "<unk>",
    "<s>",
    "</s>",
    "AA0",
    "AA1",
    "AA2",
    "AE0",
    "AE1",
    "AE2",
    "AH0",
    "AH1",
    "AH2",
    "AO0",
    "AO1",
    "AO2",
    "AW0",
    "AW1",
    "AW2",
    "AY0",
    "AY1",
    "AY2",
    "B",
    "CH",
    "D",
    "DH",
    "EH0",
    "EH1",
    "EH2",
    "ER0",
    "ER1",
    "ER2",
    "EY0",
    "EY1",
    "EY2",
    "F",
    "G",
    "HH",
    "IH0",
    "IH1",
    "IH2",
    "IY0",
    "IY1",
    "IY2",
    "JH",
    "K",
    "L",
    "M",
    "N",
    "NG",
    "OW0",
    "OW1",
    "OW2",
    "OY0",
    "OY1",
    "OY2",
    "P",
    "R",
    "S",
    "SH",
    "T",
    "TH",
    "UH0",
    "UH1",
    "UH2",
    "UW",
    "UW0",
    "UW1",
    "UW2",
    "V",
    "W",
    "Y",
    "Z",
    "ZH",
]


def _model_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "g2p_en_checkpoint20.npz"


@lru_cache(maxsize=1)
def _weights():
    """Load the GRU weights once; missing/corrupt resources degrade to None.

    A truncated or damaged npz raises zipfile.BadZipFile, which derives
    directly from Exception and therefore never matched the historical
    (ImportError, OSError, ValueError) tuple. Catching broadly here also
    lets lru_cache remember the failure, so a broken asset is read once,
    not once per OOV word.
    """
    import numpy as np

    try:
        with np.load(_model_path(), allow_pickle=False) as archive:
            return {key: archive[key] for key in archive.files}
    except Exception:
        return None


def predict_pronunciation(word: str) -> Optional[tuple[str, ...]]:
    """Predict a plain word; unavailable resources/invalid output return None."""
    if not (word.isascii() and word.isalpha()) or not 2 <= len(word) <= 48:
        return None
    # A sung extension such as heyyyyyyyy is not a new multi-syllable word.
    import re

    if re.search(r"(.)\1{3}", word):
        return None
    import numpy as np

    weights = _weights()
    if weights is None:
        return None

    def step(x, hidden, prefix):
        incoming = x @ weights[prefix + "_w_ih"].T + weights[prefix + "_b_ih"]
        recurrent = hidden @ weights[prefix + "_w_hh"].T + weights[prefix + "_b_hh"]
        ir, iz, candidate = np.split(incoming, 3)
        hr, hz, hc = np.split(recurrent, 3)
        reset = 1.0 / (1.0 + np.exp(-np.clip(ir + hr, -60, 60)))
        update = 1.0 / (1.0 + np.exp(-np.clip(iz + hz, -60, 60)))
        proposal = np.tanh(candidate + reset * hc)
        return (1.0 - update) * proposal + update * hidden

    try:
        hidden = np.zeros(weights["enc_w_hh"].shape[1], dtype=np.float32)
        for char_id in [ord(char) - ord("a") + 3 for char in word.lower()] + [2]:
            hidden = step(weights["enc_emb"][char_id], hidden, "enc")
        previous = 2  # <s>
        result = []
        for _ in range(96):
            hidden = step(weights["dec_emb"][previous], hidden, "dec")
            previous = int((hidden @ weights["fc_w"].T + weights["fc_b"]).argmax())
            if previous == 3:  # </s>; never use truncated predictions
                return tuple(result) if any(p[-1:] in "012" for p in result) else None
            if previous < 4:
                return None
            result.append(_PHONES[previous])
    except Exception:
        # Malformed weights (missing entries / unexpected shapes) degrade the
        # same way as a missing model; this path must never raise into
        # lyric import or auto-checkpoint analysis.
        return None
    return None
