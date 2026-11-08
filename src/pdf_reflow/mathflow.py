"""Recover complete formula objects before prose line/block heuristics run.

Body baselines anchor inline scripts and raised radicals. Display rows are
kept together only across adjacent mathematical rows, never across prose.
The objects retain source rectangles and a body baseline for native PDF
rendering; no attempt is made to reconstruct TeX from extracted Unicode.
"""
from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, field, replace
from itertools import count
from typing import Iterable

from .cjk_fonts import is_cjk_char
from .extract import MathFragment, PageContent, Span
from .mathfont import is_math_font

_FUNCTIONS = {"lim", "sin", "cos", "tan", "log", "exp", "max", "min", "softmax",
              "attention", "relu", "gelu", "sigmoid", "ln", "det", "sum", "prod",
              "clip", "old", "if", "else", "otherwise", "mod"}


def _baseline(s: Span) -> float:
    return s.origin[1] if s.origin else s.y1 - s.size * 0.22


def _extension(s: Span) -> bool:
    return any(p in s.font for p in ("CMEX", "LMEX"))


def _math(s: Span) -> bool:
    if s.text.strip() in {"•", "▪", "◦", "‣", "●", "○"}:
        return False
    return s.math_candidate or is_math_font(s.font) or any(0xE000 <= ord(c) <= 0xF8FF for c in s.text)


def _scripted_variables(spans: list[Span], body: float) -> dict[int, Span]:
    """Detect x_i / y^j even when the PDF uses ordinary italic fonts.

    Require both an italic single-letter base and a nearby smaller run
    on a shifted baseline; italic prose alone is not evidence of math.
    """
    ordered = sorted(spans, key=lambda s: s.x0)
    xs = [s.x0 for s in ordered]
    marked = set()
    for s in spans:
        if not (s.is_italic and s.size >= body * 0.85 and len(s.text.strip()) == 1
                and s.text.strip().isalpha()):
            continue
        lo = bisect_left(xs, s.x0 - s.size * 0.25)
        hi = bisect_right(xs, s.x1 + s.size * 0.8)
        scripts = [t for t in ordered[lo:hi] if t.size < s.size * 0.85
                   and len(t.text.strip()) <= 8
                   and 0.08 * s.size <= abs(_baseline(t) - _baseline(s)) <= 1.1 * s.size]
        if scripts:
            marked.add(id(s))
            marked.update(id(t) for t in scripts)
    return {id(s): replace(s, math_candidate=True) for s in spans if id(s) in marked}


def _prose(s: Span) -> bool:
    if _math(s):
        return False
    if any(is_cjk_char(c) and c.isalpha() for c in s.text):
        return True
    return any(w.lower() not in _FUNCTIONS and not (w.isupper() and len(w) <= 5)
               for w in re.findall(r"[A-Za-z]{2,}", s.text))


def _gap(a: Span, b: Span) -> float:
    return max(0.0, a.x0 - b.x1, b.x0 - a.x1)


@dataclass
class _Row:
    baseline: float
    spans: list[Span] = field(default_factory=list)

    @property
    def has_math(self) -> bool:
        return any(_math(s) for s in self.spans)

    @property
    def prose(self) -> bool:
        words = [s for s in self.spans if _prose(s)]
        if not words:
            return False
        if any(any(is_cjk_char(ch) and ch.isalpha() for ch in s.text) for s in words):
            return True
        # Cases/constraints may contain words such as "budget" or a phase
        # label. A few such words inside otherwise mathematical notation
        # do not turn the display into a prose paragraph.
        total = sum(len(s.text.strip()) for s in self.spans)
        return not self.has_math or sum(len(s.text.strip()) for s in words) > total * 0.35


def _rows(spans: list[Span], body: float) -> list[_Row]:
    # Do not let scripts or TeX extension-font origins create their own
    # baselines: an extension origin often sits near the top of its glyph.
    seeds = [s for s in spans if s.size >= body * 0.85
             and not _extension(s) and s.text.strip()
             and not any(ch in s.text for ch in "√∑∫∏")]
    rows: list[_Row] = []
    owners: dict[int, _Row] = {}
    for s in sorted(seeds, key=_baseline):
        baseline = _baseline(s)
        if not rows or abs(baseline - rows[-1].baseline) > body * 0.22:
            rows.append(_Row(baseline))
        rows[-1].spans.append(s)
        owners[id(s)] = rows[-1]
    if not rows:
        return [_Row(_baseline(s), [s]) for s in spans]

    pending = [s for s in spans if id(s) not in owners]
    # Large operators first, so their scripts can use them as anchors.
    pending.sort(key=lambda s: (s.size < body * 0.85, s.y0))
    for s in pending:
        if "√" in s.text:
            expected = _baseline(s) + s.size * 0.85
        elif _extension(s):
            expected = (s.y0 + s.y1) / 2 + body * 0.25
        else:
            expected = _baseline(s)
        nearest = min(rows, key=lambda r: abs(r.baseline - expected))
        if _extension(s) and any(0xF8EE <= ord(ch) <= 0xF8FF for ch in s.text):
            # Top pieces of stretched TeX brackets belong to the display
            # below them, not the preceding prose line.
            below = [r for r in rows if not r.prose and 0 <= r.baseline - s.y0 <= body * 3]
            if below:
                nearest = min(below, key=lambda r: r.baseline - s.y0)
        if s.size < body * 0.85:
            candidates = [(abs(r.baseline - expected) * 0.35 + _gap(s, a), r)
                          for r in rows for a in r.spans
                          if _math(a) and _gap(s, a) <= body * 0.8
                          and abs(r.baseline - expected) <= body * 1.8]
            if candidates:
                nearest = min(candidates, key=lambda pair: pair[0])[1]
        if abs(nearest.baseline - expected) > body * 1.8:
            nearest = _Row(expected)
            rows.append(nearest)
        nearest.spans.append(s)
    return sorted(rows, key=lambda r: r.baseline)


def _fragment(page: PageContent, spans: list[Span], row: _Row,
              body: float, display: bool) -> MathFragment:
    rects = []
    for s in spans:
        rect = s.bbox
        if "√" in s.text:
            # TeX's radical font box is above the ink; the painted radical
            # descends from its origin to the radicand's baseline.
            y = _baseline(s)
            rect = (s.x0, y - s.size * 0.12, s.x1, y + s.size * 1.05)
        elif _extension(s) and s.text.strip() in ("(", ")", "[", "]"):
            # Delimiter font boxes describe the top piece, not the full
            # stretched delimiter. Other formula components set its height.
            continue
        rects.append(rect)
    if not rects:
        rects = [s.bbox for s in spans]
    x0 = min(s.x0 for s in spans)
    x1 = max(s.x1 for s in spans)
    y0 = min(r[1] for r in rects)
    y1 = max(r[3] for r in rects)
    # Include rules wholly belonging to the expression (fraction/radical
    # bars), without pulling in long footnote rules or adjacent text rows.
    rules = []
    for d in page.drawings:
        a, b, c, e = d.bbox
        if x0 - 0.5 <= a <= c <= x1 + 0.5 and y0 - 0.5 <= b <= e <= y1 + 0.5:
            y0, y1 = min(y0, b), max(y1, e)
            if e - b <= body * 0.15:
                rules.append((d.bbox, d.width))
    pad = 0.2
    rect = (max(page.rect[0], x0 - pad), max(page.rect[1], y0 - pad),
            min(page.rect[2], x1 + pad), min(page.rect[3], y1 + pad))
    sizes = Counter()
    for s in row.spans:
        if _prose(s):
            sizes[s.size] += len(s.text)
    source_size = sizes.most_common(1)[0][0] if sizes else body
    return MathFragment(page.index, rect, row.baseline, source_size,
                        " ".join(s.text for s in sorted(spans, key=lambda s: (s.y0, s.x0))), display,
                        [(s.bbox, _baseline(s), s.size, s.color) for s in spans], rules)


def prepare_math(page: PageContent, body: float,
                 lanes: Iterable[list[Span]] | None = None) -> tuple[PageContent, dict[str, MathFragment]]:
    scripted = _scripted_variables(page.spans, body)
    if not scripted and not any(_math(s) for s in page.spans):
        return page, {}
    fragments: dict[str, MathFragment] = {}
    output: list[Span] = []
    serial = count()

    def atom(spans: list[Span], row: _Row, display: bool) -> Span:
        key = chr(0xF0000 + next(serial))
        fragment = _fragment(page, spans, row, body, display)
        fragments[key] = fragment
        # Synthetic bounds follow the prose baseline for inline objects;
        # their true ascent/descent is restored by layout. This prevents a
        # radical or subscript from seeding a separate figure band.
        bbox = fragment.rect if display else (fragment.rect[0], row.baseline - fragment.size * 0.8,
                                              fragment.rect[2], row.baseline + fragment.size * 0.2)
        context = next((s for s in row.spans if _prose(s)), spans[0])
        return Span(key, bbox, context.font if not _math(context) else "Times-Roman",
                    fragment.size, context.flags, context.color, (bbox[0], row.baseline), fragment)

    for lane in lanes if lanes is not None else [page.spans]:
        if scripted:
            lane = [scripted.get(id(s), s) for s in lane]
        rows = _rows(lane, body)
        i = 0
        while i < len(rows):
            row = rows[i]
            if not row.prose:
                group = [row]
                j = i + 1
                while (j < len(rows) and not rows[j].prose
                       and rows[j].baseline - group[-1].baseline < body * 2.0):
                    group.append(rows[j]); j += 1
                if any(r.has_math for r in group):
                    spans = [s for r in group for s in r.spans]
                    output.append(atom(spans, max(group, key=lambda r: len(r.spans)), True))
                else:
                    output.extend(s for r in group for s in r.spans)
                i = j
                continue
            ss = sorted(row.spans, key=lambda s: (s.x0, s.y0))
            k = 0
            while k < len(ss):
                if _prose(ss[k]):
                    output.append(ss[k]); k += 1; continue
                group = [ss[k]]
                j = k + 1
                while j < len(ss) and not _prose(ss[j]) and _gap(group[-1], ss[j]) <= body * 0.8:
                    group.append(ss[j]); j += 1
                if any(_math(s) for s in group):
                    output.append(atom(group, row, False))
                else:
                    output.extend(group)
                k = j
            i += 1
    return replace(page, spans=output), fragments
