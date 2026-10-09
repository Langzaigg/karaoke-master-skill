"""Singer parts (歌割り) from a subtitle file or a part-distribution source, aligned to the timed lyrics.

The singer of every character comes from the source; nothing is guessed from the audio. Sources:

* ``.ass`` / ``.ssa``: per Dialogue the Name (actor) field, else the Style; inline colour overrides
  (``{\\c&HBBGGRR&}`` / ``{\\1c…}``) split a line into runs keyed by that colour (``#rrggbb``).
* ``.lrc`` / ``.txt``: a singer marker at the start of a line (``名前：``, ``[名前]``, ``【名前】``,
  ``(名前)``) when the marker is a known key; unmarked lines keep the previous singer.
* ``.html`` / ``.htm``: text runs keyed by their CSS / ``<font>`` colour (colour-coded パート分け
  pages); text without a colour has the key ``none``.
* ``.json``: ``[{"singer": "...", "text": "..."}]`` runs, written by the agent from any other source.

Keys map to singers with ``KEY=歌手`` (several keys may share one singer, e.g. members singing
together → 和声; ``KEY=-`` drops a key such as translations or legends). A key that already is a
singer name maps to itself. The runs are matched to the timed characters in order (difflib), so
repeated choruses line up with their own occurrence; lines the source does not cover keep their
singer and are reported.
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

PALETTE = ["#FF8A1E", "#A04BFF", "#3FA9F5", "#FF5FA2", "#3FD48E", "#FFC21A"]
NO_KEY = "none"


# ------------------------------------------------------------------ reading
def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8-sig", "cp932", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _hex(value: str) -> str:
    v = value.strip().lower()
    m = re.match(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", v)
    if m:
        return "#" + "".join(f"{int(x):02x}" for x in m.groups())
    if re.fullmatch(r"#[0-9a-f]{3}", v):
        return "#" + "".join(c * 2 for c in v[1:])
    return v


def _runs_ass(text: str) -> list[tuple[str | None, str]]:
    runs: list[tuple[str | None, str]] = []
    fmt = None
    events = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            events = line.lower() == "[events]"
            continue
        if not events:
            continue
        low = line.lower()
        if low.startswith("format:"):
            fmt = [f.strip().lower() for f in line.split(":", 1)[1].split(",")]
            continue
        if not low.startswith("dialogue:") or not fmt:
            continue
        vals = line.split(":", 1)[1].split(",", len(fmt) - 1)
        if len(vals) < len(fmt):
            continue
        rec = dict(zip(fmt, vals))
        base = rec.get("name", "").strip() or rec.get("style", "").strip()
        key = base
        for m in re.finditer(r"\{([^}]*)\}|([^{]+)", rec.get("text", "")):
            if m.group(1) is not None:
                tags = m.group(1)
                if re.search(r"\\r(?![a-z])", tags):
                    key = base
                cols = re.findall(r"\\1?c&H([0-9a-fA-F]{6,8})&?", tags)
                if cols:
                    b = cols[-1][-6:]
                    key = "#" + (b[4:6] + b[2:4] + b[0:2]).lower()
            else:
                runs.append((key, re.sub(r"\\[Nnh]", " ", m.group(2))))
        runs.append((None, "\n"))
    return runs


def _runs_lrc(text: str, known: set[str]) -> list[tuple[str | None, str]]:
    runs: list[tuple[str | None, str]] = []
    key = None
    for raw in text.splitlines():
        line = re.sub(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]|<\d{1,3}:\d{2}(?:[.:]\d{1,3})?>", "", raw).strip()
        if re.fullmatch(r"\[[a-zA-Z]+:.*\]", line):
            continue
        m = re.match(r"^(?:[\[【(（]([^\]】)）]{1,24})[\]】)）]|([^\s:：]{1,24})[:：])\s*", line)
        if m and (m.group(1) or m.group(2)).strip() in known:
            key = (m.group(1) or m.group(2)).strip()
            line = line[m.end():]
        runs.append((key, line))
        runs.append((None, "\n"))
    return runs


def _runs_html(text: str) -> list[tuple[str | None, str]]:
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1\s*>", "", text)
    runs: list[tuple[str | None, str]] = []
    stack: list[str | None] = []
    for m in re.finditer(r"(?is)<(/?)([a-z0-9]+)\b([^>]*)>|<[^>]*>|([^<]+)", text):
        if m.group(4) is not None:
            runs.append((stack[-1] if stack else NO_KEY, _html.unescape(m.group(4))))
            continue
        tag = (m.group(2) or "").lower()
        if tag in ("span", "font"):
            if m.group(1):
                if stack:
                    stack.pop()
            elif not m.group(3).rstrip().endswith("/"):
                c = re.search(r"color\s*[:=]\s*[\"']?\s*(#[0-9a-fA-F]{3,6}\b|rgba?\([^)]*\))", m.group(3), re.I)
                stack.append(_hex(c.group(1)) if c else (stack[-1] if stack else NO_KEY))
        elif tag in ("br", "p", "div", "li", "tr", "td", "h1", "h2", "h3", "h4", "h5", "h6"):
            runs.append((None, "\n"))
    return runs


def _runs_json(text: str) -> list[tuple[str | None, str]]:
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("runs") or data.get("parts") or []
    return [(str(r.get("singer") or NO_KEY), str(r.get("text") or "")) for r in data]


def read_runs(path: Path, known: set[str]) -> list[tuple[str | None, str]]:
    ext = path.suffix.lower()
    text = read_text(path)
    if ext in (".ass", ".ssa"):
        return _runs_ass(text)
    if ext in (".html", ".htm"):
        return _runs_html(text)
    if ext == ".json":
        return _runs_json(text)
    return _runs_lrc(text, known)


# ------------------------------------------------------------------ matching
def _norm(ch: str) -> str:
    out = []
    for x in unicodedata.normalize("NFKC", ch).lower():
        if "ァ" <= x <= "ヶ":
            x = chr(ord(x) - 0x60)  # katakana → hiragana
        if x.isalnum():
            out.append(x)
    return "".join(out)


def singer_id(name: str) -> str:
    return "p" + hashlib.md5(name.encode("utf-8")).hexdigest()[:6]


def plan(view: dict, runs: list[tuple[str | None, str]], mapping: dict[str, str], names: set[str]) -> dict:
    """Per-character singer names for the timed lines + a report (no lyric text in it)."""
    keys: dict[str, int] = {}
    for k, t in runs:
        n = len(_norm(t))
        if k is not None and n:
            keys[k] = keys.get(k, 0) + n

    def who(k):
        if k in mapping:
            return None if mapping[k] in ("-", "") else mapping[k]
        return k if k in names else ...

    report_keys = {k: {"chars": n, "singer": (None if who(k) is ... else who(k) or "-")} for k, n in keys.items()}
    unmapped = sorted((k for k in keys if who(k) is ... and k != NO_KEY), key=lambda k: -keys[k])
    src = [(x, s) for k, t in runs if k is not None for s in [who(k)] if s not in (None, ...)
           for ch in t for x in _norm(ch)]
    tgt = [(x, li, ci) for li, line in enumerate(view["lines"]) for ci, ch in enumerate(line["chars"])
           for x in _norm(ch["c"])]
    sm = SequenceMatcher(None, [x for x, _ in src], [x for x, *_ in tgt], autojunk=False)
    assign: dict[tuple[int, int], str] = {}
    matched = [0] * len(view["lines"])
    total = [0] * len(view["lines"])
    for _x, li, _ci in tgt:
        total[li] += 1
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            _x, li, ci = tgt[b + k]
            assign[(li, ci)] = src[a + k][1]
            matched[li] += 1
    lines: list[list[str] | None] = []
    for li, line in enumerate(view["lines"]):
        vals = [assign.get((li, ci)) for ci in range(len(line["chars"]))]
        if not any(vals):
            lines.append(None)
            continue
        last = None
        for ci, v in enumerate(vals):  # unmatched characters / punctuation take the neighbouring singer
            last = vals[ci] = v or last
        nxt = None
        for ci in range(len(vals) - 1, -1, -1):
            nxt = vals[ci] = vals[ci] or nxt
        lines.append(vals)
    counted = sum(total) or 1
    per: dict[str, int] = {}
    for vals in lines:
        for v in vals or []:
            per[v] = per.get(v, 0) + 1
    return {
        "lines": lines,
        "keys": report_keys,
        "unmapped": unmapped,
        "coverage": round(sum(matched) / counted, 3),
        "singers": per,
        "uncovered": [li + 1 for li, v in enumerate(lines) if v is None],
        "weak": [li + 1 for li, v in enumerate(lines) if v is not None and total[li]
                 and matched[li] / total[li] < 0.5],
    }


def edit_ops(result: dict) -> list[dict]:
    ops = []
    for li, vals in enumerate(result["lines"]):
        if not vals:
            continue
        runs = []
        for ci, v in enumerate(vals):
            if runs and runs[-1][0] == v:
                runs[-1][2] = ci
            else:
                runs.append([v, ci, ci])
        if len(runs) == 1:
            ops.append({"op": "set_singer", "lines": [li], "singer": runs[0][0]})
        else:
            ops += [{"op": "set_singer", "lines": [li], "singer": v, "chars": [a, b]} for v, a, b in runs]
    return ops
