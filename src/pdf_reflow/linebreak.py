"""Unicode line breaking (UAX #14) — the algorithm ICU implements.

This is a pure-Python implementation of the Unicode Line Breaking
Algorithm (Unicode Standard Annex #14), the same rule set ICU's
``BreakIterator`` uses for its default (non-tailored) line break mode.
It finds the *legal* places a line may break — after spaces and hyphens,
between CJK ideographs, around slashes, em-dashes and the like — while
forbidding breaks that would orphan punctuation (e.g. before a closing
bracket or a full stop, after an opening bracket, inside a number).

Why reimplement it instead of binding ICU / PyICU?  The reflow pipeline
runs both natively and in the browser (Pyodide), and the whole text
engine is deliberately pure Python with no native text shaper.  A native
ICU dependency would not load in the WASM build, so we port the rules.

The public surface is small:

    segments(text) -> (boxes, seps)
        Split ``text`` at every legal break opportunity.  ``boxes`` are
        the unbreakable runs (with trailing collapsible spaces removed);
        ``seps[k]`` describes the boundary between ``boxes[k]`` and
        ``boxes[k+1]`` — whether a space was collapsed there, whether it
        is a hyphenation break, and whether it is a mandatory break.

    line_break_opportunities(text) -> list[(index, mandatory)]
        Lower-level: the raw break positions (break is allowed *before*
        ``text[index]``).

The line breaker only finds *existing* opportunities; it never inserts
soft hyphens (there is no hyphenation dictionary), so a break "after a
hyphen" only happens where the source already contains one.
"""

from __future__ import annotations

import unicodedata
from typing import Dict, List, Tuple

from .cjk_fonts import is_cjk_char as _is_cjk_char


# ---------------------------------------------------------------------------
# Line break classes (UAX #14, Table 1). We use the subset that actually
# influences breaking for the scripts this tool handles; the rarely-seen
# classes are folded into their resolved equivalents (see _lb_class):
#   AI, SA, SG, XX -> AL    CJ -> ID    H2/H3/JL/JV/JT -> ID
#   EB/EM -> ID             CM/ZWJ -> attach to base (LB9/LB10)
# ---------------------------------------------------------------------------

BK = "BK"   # mandatory break
CR = "CR"   # carriage return
LF = "LF"   # line feed
NL = "NL"   # next line
SP = "SP"   # space
ZW = "ZW"   # zero width space
WJ = "WJ"   # word joiner (no break either side)
GL = "GL"   # non-breaking glue (NBSP and friends)
CM = "CM"   # combining mark (attaches to base)
BA = "BA"   # break opportunity after
BB = "BB"   # break opportunity before
B2 = "B2"   # break opportunity before and after (em dash)
HY = "HY"   # hyphen-minus
CB = "CB"   # contingent break (inline object)
OP = "OP"   # open punctuation
CL = "CL"   # close punctuation
CP = "CP"   # close parenthesis
QU = "QU"   # quotation
NS = "NS"   # nonstarter (cannot begin a line)
EX = "EX"   # exclamation / interrogation
IS = "IS"   # infix numeric separator (, . : ;)
SY = "SY"   # symbol allowing break after (solidus)
IN = "IN"   # inseparable (ellipsis)
PR = "PR"   # prefix numeric ($, +)
PO = "PO"   # postfix numeric (%)
NU = "NU"   # numeric
AL = "AL"   # alphabetic / ordinary
ID = "ID"   # ideographic (CJK)
RI = "RI"   # regional indicator (flag emoji)


# Break actions.
PROHIBITED = 0
ALLOWED = 1
MANDATORY = 2


# ---------------------------------------------------------------------------
# Per-codepoint line break class.
# ---------------------------------------------------------------------------

# Explicit overrides for codepoints whose class is not derivable cleanly
# from the Unicode general category (or where the category is ambiguous
# for line breaking). Everything not listed here falls through to the
# category-based resolver in ``_lb_class``.
_EXPLICIT: Dict[int, str] = {}


def _seed_explicit() -> None:
    e = _EXPLICIT
    # Mandatory-break / control characters.
    e[0x000A] = LF
    e[0x000D] = CR
    e[0x0085] = NL
    e[0x000B] = BK
    e[0x000C] = BK
    e[0x2028] = BK
    e[0x2029] = BK
    e[0x0009] = BA          # tab
    # Spaces and glue.
    e[0x0020] = SP
    e[0x00A0] = GL          # no-break space
    e[0x2007] = GL          # figure space (kept with numbers)
    e[0x202F] = GL          # narrow no-break space
    e[0x2011] = GL          # non-breaking hyphen
    e[0x200B] = ZW          # zero width space
    e[0x200D] = CM          # zero width joiner -> attach to base
    e[0x2060] = WJ          # word joiner
    e[0xFEFF] = WJ          # BOM / zero width no-break space
    e[0x3000] = ID          # ideographic space
    for cp in range(0x2000, 0x200B):   # en/em/thin/etc. spaces
        e.setdefault(cp, BA)
    e[0x2007] = GL
    # ASCII punctuation.
    e[0x21] = EX            # !
    e[0x22] = QU            # "
    e[0x24] = PR            # $
    e[0x25] = PO            # %
    e[0x27] = QU            # '
    e[0x28] = OP            # (
    e[0x29] = CP            # )
    e[0x2B] = PR            # +
    e[0x2C] = IS            # ,
    e[0x2D] = HY            # - (hyphen-minus)
    e[0x2E] = IS            # .
    e[0x2F] = SY            # /
    e[0x3A] = IS            # :
    e[0x3B] = IS            # ;
    e[0x3F] = EX            # ?
    e[0x5B] = OP            # [
    e[0x5C] = PR            # backslash
    e[0x5D] = CP            # ]
    e[0x7B] = OP            # {
    e[0x7C] = BA            # |
    e[0x7D] = CP            # }
    for cp in range(0x30, 0x3A):
        e[cp] = NU          # 0-9
    # Latin-1 punctuation.
    e[0x00AD] = BA          # soft hyphen (a real break opportunity)
    e[0x00AB] = QU          # «
    e[0x00BB] = QU          # »
    # General punctuation dashes / quotes / ellipsis.
    e[0x2010] = BA          # hyphen
    e[0x2013] = BA          # en dash
    e[0x2014] = B2          # em dash (break both sides)
    e[0x2015] = B2          # horizontal bar
    e[0x2018] = QU
    e[0x2019] = QU
    e[0x201C] = QU
    e[0x201D] = QU
    e[0x2025] = IN          # two dot leader
    e[0x2026] = IN          # horizontal ellipsis
    e[0x2212] = PR          # minus sign
    # CJK opening punctuation.
    for cp in (0x3008, 0x300A, 0x300C, 0x300E, 0x3010, 0x3014, 0x3016,
               0x3018, 0x301A, 0x301D, 0xFF08, 0xFF3B, 0xFF5B, 0xFF5F):
        e[cp] = OP
    # CJK closing punctuation + ideographic comma/period.
    for cp in (0x3009, 0x300B, 0x300D, 0x300F, 0x3011, 0x3015, 0x3017,
               0x3019, 0x301B, 0x301E, 0x301F, 0xFF09, 0xFF3D, 0xFF5D,
               0xFF60, 0x3001, 0x3002, 0xFF0C, 0xFF0E, 0xFF61, 0xFF64):
        e[cp] = CL
    # CJK exclamation / question (fullwidth).
    e[0xFF01] = EX
    e[0xFF1F] = EX
    # CJK nonstarters: small kana, iteration / prolonged-sound marks,
    # fullwidth colon/semicolon, katakana middle dot.
    for cp in (0x3005, 0x303B, 0x301C, 0x30A0, 0x30FB, 0x30FC, 0xFF65,
               0xFF70, 0xFF1A, 0xFF1B, 0x2047, 0x2048, 0x2049):
        e[cp] = NS
    for cp in (0x3041, 0x3043, 0x3045, 0x3047, 0x3049, 0x3063, 0x3083,
               0x3085, 0x3087, 0x308E, 0x3095, 0x3096,
               0x30A1, 0x30A3, 0x30A5, 0x30A7, 0x30A9, 0x30C3, 0x30E3,
               0x30E5, 0x30E7, 0x30EE, 0x30F5, 0x30F6):
        e[cp] = NS


_seed_explicit()


_CLASS_CACHE: Dict[int, str] = {}


def _lb_class(cp: int) -> str:
    """Resolve the (folded) line break class of a codepoint."""
    cached = _CLASS_CACHE.get(cp)
    if cached is not None:
        return cached
    v = _EXPLICIT.get(cp)
    if v is None:
        ch = chr(cp)
        if _is_cjk_char(ch):
            # CJK punctuation handled in _EXPLICIT above; the rest is
            # ideographic (Han / Kana / Hangul / fullwidth forms).
            v = ID
        else:
            cat = unicodedata.category(ch)
            if cat == "Nd":
                v = NU
            elif cat in ("Lu", "Ll", "Lt", "Lm", "Lo"):
                v = AL
            elif cat in ("Mn", "Mc", "Me"):
                v = CM
            elif cat == "Zs":
                v = SP
            elif cat in ("Zl", "Zp"):
                v = BK
            elif cat == "Ps":
                v = OP
            elif cat == "Pe":
                v = CP
            elif cat in ("Pi", "Pf"):
                v = QU
            elif cat == "Pd":
                v = BA
            elif cat == "Sc":
                v = PR
            elif cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
                v = CM
            else:
                # Po, Sm, Sk, So, and everything else: ordinary.
                v = AL
    _CLASS_CACHE[cp] = v
    return v


def _classes(text: str) -> List[str]:
    """Resolve every character's class, applying LB9/LB10 so a combining
    mark inherits the class of its base (and a leading mark becomes AL)."""
    raw = [_lb_class(ord(c)) for c in text]
    out: List[str] = []
    for i, cl in enumerate(raw):
        if cl == CM:
            if i == 0:
                out.append(AL)
            else:
                base = out[i - 1]
                out.append(AL if base in (BK, CR, LF, NL, SP, ZW) else base)
        else:
            out.append(cl)
    return out


# LB25: a compact set of class pairs that must not break, keeping numeric
# expressions (1,000.00 · $5 · 10% · (123)) together.
_LB25 = frozenset({
    (CL, PO), (CP, PO), (CL, PR), (CP, PR),
    (NU, PO), (NU, PR),
    (PO, OP), (PR, OP),
    (PO, NU), (PR, NU),
    (HY, NU), (IS, NU), (NU, NU), (SY, NU),
    (NU, SY), (NU, IS), (NU, CL), (NU, CP),
})


def _decide(a: str, b: str, lnb: str) -> int:
    """Decide the break action between left class ``a`` and right class
    ``b``. ``lnb`` is the nearest non-space class at/left of ``a`` (so the
    "X SP* ÷/×" rules can see across a run of spaces). Rules are evaluated
    in UAX #14 priority order; the first match wins."""
    # LB4 / LB5: mandatory breaks.
    if a == BK:
        return MANDATORY
    if a == CR:
        return PROHIBITED if b == LF else MANDATORY
    if a == LF or a == NL:
        return MANDATORY
    # LB6: do not break before a hard break.
    if b in (BK, CR, LF, NL):
        return PROHIBITED
    # LB7: do not break before a space or zero-width space.
    if b == SP or b == ZW:
        return PROHIBITED
    # LB8: break after a zero-width space (even across spaces).
    if lnb == ZW:
        return ALLOWED
    # LB11: word joiner glues both sides.
    if b == WJ or a == WJ:
        return PROHIBITED
    # LB12 / LB12a: non-breaking glue.
    if a == GL:
        return PROHIBITED
    if b == GL and a not in (SP, BA, HY):
        return PROHIBITED
    # LB13: do not break before these, even after spaces.
    if b in (CL, CP, EX, IS, SY):
        return PROHIBITED
    # LB14: do not break after an opening bracket (across spaces).
    if lnb == OP:
        return PROHIBITED
    # LB15: QU SP* × OP.
    if lnb == QU and b == OP:
        return PROHIBITED
    # LB16: (CL|CP) SP* × NS.
    if lnb in (CL, CP) and b == NS:
        return PROHIBITED
    # LB17: B2 SP* × B2.
    if lnb == B2 and b == B2:
        return PROHIBITED
    # LB18: break after spaces.
    if a == SP:
        return ALLOWED
    # LB19: do not break before or after quotation marks.
    if b == QU or a == QU:
        return PROHIBITED
    # LB20: break around a contingent break.
    if b == CB or a == CB:
        return ALLOWED
    # LB21: do not break before BA / HY / NS; do not break after BB.
    if b in (BA, HY, NS):
        return PROHIBITED
    if a == BB:
        return PROHIBITED
    # LB22: do not break before an inseparable (ellipsis).
    if b == IN:
        return PROHIBITED
    # LB23: do not break between alphabetics and numbers.
    if a == AL and b == NU:
        return PROHIBITED
    if a == NU and b == AL:
        return PROHIBITED
    # LB23a: PR × ID ; ID × PO.
    if a == PR and b == ID:
        return PROHIBITED
    if a == ID and b == PO:
        return PROHIBITED
    # LB24: prefix/postfix glued to letters.
    if a in (PR, PO) and b == AL:
        return PROHIBITED
    if a == AL and b in (PR, PO):
        return PROHIBITED
    # LB25: numeric expressions.
    if (a, b) in _LB25:
        return PROHIBITED
    # LB28: do not break between two alphabetics.
    if a == AL and b == AL:
        return PROHIBITED
    # LB29: IS × AL ("a.m.").
    if a == IS and b == AL:
        return PROHIBITED
    # LB30: glue letters/numbers to brackets when no space intervenes.
    if a in (AL, NU) and b == OP:
        return PROHIBITED
    if a == CP and b in (AL, NU):
        return PROHIBITED
    # LB30a: keep regional-indicator (flag) pairs together.
    if a == RI and b == RI:
        return PROHIBITED
    # LB31: break everywhere else (e.g. between two ideographs).
    return ALLOWED


def _break_actions(cls: List[str]) -> List[int]:
    """Return the break action for each gap (between char j and j+1)."""
    n = len(cls)
    if n <= 1:
        return []
    acts: List[int] = []
    for j in range(n - 1):
        a = cls[j]
        b = cls[j + 1]
        if a == SP:
            k = j
            while k >= 0 and cls[k] == SP:
                k -= 1
            lnb = cls[k] if k >= 0 else SP
        else:
            lnb = a
        acts.append(_decide(a, b, lnb))
    return acts


def line_break_opportunities(text: str) -> List[Tuple[int, bool]]:
    """Positions where a line may break, as ``(index, mandatory)`` pairs.

    A break is permitted *before* ``text[index]`` (so ``index`` ranges
    over ``1 .. len(text) - 1``). ``mandatory`` is True for hard breaks
    (explicit newlines, line/paragraph separators)."""
    cls = _classes(text)
    acts = _break_actions(cls)
    out: List[Tuple[int, bool]] = []
    for j, act in enumerate(acts):
        if act != PROHIBITED:
            out.append((j + 1, act == MANDATORY))
    return out


# ---------------------------------------------------------------------------
# Segmentation helper used by the layout engine.
# ---------------------------------------------------------------------------


class Sep:
    """A boundary between two boxes (an allowed break opportunity).

    ``space`` is True when one or more collapsible spaces were folded away
    at this boundary (so the renderer re-inserts a single space when the
    boundary lands mid-line). ``hyphen`` marks a break right after a
    hyphen character. ``mandatory`` marks a hard line break.
    """

    __slots__ = ("space", "hyphen", "mandatory")

    def __init__(self, space: bool, hyphen: bool, mandatory: bool):
        self.space = space
        self.hyphen = hyphen
        self.mandatory = mandatory

    def __repr__(self) -> str:   # pragma: no cover - debug aid
        bits = []
        if self.space:
            bits.append("space")
        if self.hyphen:
            bits.append("hyphen")
        if self.mandatory:
            bits.append("mandatory")
        return f"Sep({', '.join(bits) or 'direct'})"


# Classes whose characters are dropped from a box when they sit at a
# boundary: ordinary spaces (collapsed to glue) and zero-width spaces.
def _strip_trailing(seg: str) -> Tuple[str, bool]:
    """Strip trailing collapsible spaces / zero-width spaces from ``seg``.

    Returns ``(stripped, had_space)`` where ``had_space`` is True iff at
    least one *visible* space (class SP) was removed (a zero-width space
    does not count as a rendered space)."""
    end = len(seg)
    had_space = False
    while end > 0:
        cl = _lb_class(ord(seg[end - 1]))
        if cl == SP:
            had_space = True
            end -= 1
        elif cl == ZW:
            end -= 1
        else:
            break
    return seg[:end], had_space


def segments(text: str) -> Tuple[List[str], List[Sep]]:
    """Split ``text`` into unbreakable boxes and the separators between
    them, using the UAX #14 break opportunities.

    ``boxes`` has length ``len(seps) + 1`` (alternating box / sep / box).
    Collapsible spaces at a boundary are removed from the boxes and
    recorded on the separator; non-breaking spaces (NBSP and friends)
    stay inside their box so they render verbatim.
    """
    if not text:
        return [], []
    cls = _classes(text)
    acts = _break_actions(cls)
    boxes: List[str] = []
    seps: List[Sep] = []
    start = 0
    n = len(text)
    for j in range(n - 1):
        if acts[j] == PROHIBITED:
            continue
        seg = text[start:j + 1]
        box, had_space = _strip_trailing(seg)
        hyphen = (
            not had_space
            and bool(box)
            and _lb_class(ord(box[-1])) == HY
        )
        boxes.append(box)
        seps.append(Sep(space=had_space, hyphen=hyphen,
                        mandatory=acts[j] == MANDATORY))
        start = j + 1
    last, _ = _strip_trailing(text[start:n])
    boxes.append(last)

    # Defensive: a box could be empty only for degenerate all-space input.
    # Merge any empty interior box into its neighbour so callers always
    # see non-empty boxes with one sep between each adjacent pair.
    if any(b == "" for b in boxes):
        boxes, seps = _drop_empty_boxes(boxes, seps)
    return boxes, seps


def _drop_empty_boxes(
    boxes: List[str], seps: List[Sep]
) -> Tuple[List[str], List[Sep]]:
    out_boxes: List[str] = []
    out_seps: List[Sep] = []
    for i, b in enumerate(boxes):
        if b == "":
            # Skip the box; also skip the separator that followed it (if
            # any) so the alternation stays consistent.
            if i < len(seps):
                # Carry a space flag forward onto the previous sep.
                if out_seps and seps[i].space:
                    out_seps[-1].space = True
            continue
        out_boxes.append(b)
        if i < len(seps):
            out_seps.append(seps[i])
    # Trim a trailing separator with no following box.
    while len(out_seps) >= len(out_boxes) and out_seps:
        out_seps.pop()
    if not out_boxes:
        return [""], []
    return out_boxes, out_seps
