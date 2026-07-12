"""Layout engine: lay out the reading-order FlowItems on mobile pages.

Produces a list of `Page` objects, each containing positioned `Draw` ops.
The render module turns those into PDF content.

This is a single-column greedy word-wrap. Width is measured using actual
font metrics from PyMuPDF's base14 fonts (so no third-party text-shaper
is needed) — this keeps the algorithm pure and predictable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import fitz

from .analyze import FlowItem, _parse_toc_entry
from .cjk_fonts import (
    SCRIPT_HAN,
    SCRIPT_JAPAN,
    SCRIPT_KOREA,
    SCRIPT_LATIN_SANS,
    SCRIPT_LATIN_SERIF,
    STORE as _CJK_STORE,
    font_entry_for_fontname,
    fontname_for_script,
    is_cjk_char as _is_cjk_char,
)
from .knuth_plass import (
    INF as _KP_INF,
    Box as _KPBox,
    BreakParams as _KPParams,
    Glue as _KPGlue,
    Penalty as _KPPenalty,
    add_final_break as _kp_add_final_break,
    break_lines as _kp_break_lines,
)
from .linebreak import Sep as _Sep, segments as _uax14_segments


# ---------------------------------------------------------------------------
# Output draw operations.
# ---------------------------------------------------------------------------


@dataclass
class HeadingAnchor:
    """Records where a heading landed in the reflowed output."""
    text: str
    level: int      # 1 = title/h1, 2 = section/h2, 3 = run-in subheading
    out_page: int   # 0-indexed output page
    y: float        # top-y on output page


@dataclass
class DrawText:
    x: float
    y: float            # baseline y
    text: str
    font: str           # one of: 'times-roman', 'times-bold', 'times-italic', 'courier'
    size: float
    color: Tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass
class DrawImage:
    x: float
    y: float            # top-left
    w: float
    h: float
    source_page: int    # index into original document
    source_rect: Tuple[float, float, float, float]


@dataclass
class DrawRule:
    """A horizontal hairline (used between sections, etc)."""
    x0: float
    y: float
    x1: float


Op = object  # DrawText | DrawImage | DrawRule


@dataclass
class LaidOutPage:
    width: float
    height: float
    ops: List[Op] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Configuration.
# ---------------------------------------------------------------------------


@dataclass
class LayoutConfig:
    page_width: float = 360.0
    page_height: float = 600.0
    margin_left: float = 18.0
    margin_right: float = 18.0
    margin_top: float = 22.0
    margin_bottom: float = 22.0
    body_size: float = 11.0
    heading_scale_h1: float = 1.5
    heading_scale_h2: float = 1.25
    caption_size: float = 9.5
    code_size: float = 8.5
    line_height_mult: float = 1.25
    para_space: float = 5.0          # space between body paragraphs
    heading_space_above: float = 9.0
    heading_space_below: float = 3.0
    figure_max_height_frac: float = 0.7  # max image height as fraction of content height
    figure_max_upscale: float = 2.0      # how far a real figure may be enlarged to fill the column
    equation_max_upscale: float = 1.0    # display-math rasters render at authored size (never enlarged)
    figure_dpi: float = 150.0            # rasterization DPI (150 is sharp on mobile; 220+ for print)

    @property
    def content_left(self) -> float: return self.margin_left
    @property
    def content_right(self) -> float: return self.page_width - self.margin_right
    @property
    def content_width(self) -> float: return self.content_right - self.content_left
    @property
    def content_top(self) -> float: return self.margin_top
    @property
    def content_bottom(self) -> float: return self.page_height - self.margin_bottom
    @property
    def content_height(self) -> float: return self.content_bottom - self.content_top


# ---------------------------------------------------------------------------
# Font metrics. PyMuPDF ships base14 metrics; we use them directly.
# ---------------------------------------------------------------------------


class FontMetrics:
    """Measure text widths with PyMuPDF base14 fonts.

    Each call to ``Font.text_length`` is a SWIG round-trip into MuPDF and
    dominates layout time (≈70 µs/call × thousands of words). We avoid the
    crossing by pre-computing per-character advance widths at unit font
    size for every glyph we have already seen, then computing line widths
    as a Python-side sum. For base14 fonts this is exact — MuPDF reports
    the same per-glyph advances and there is no kerning table to apply.

    CJK fonts come in two flavors:
      * Bundled PyMuPDF CID fonts (``china-s`` / ``japan`` / ``korea``)
        — ``insert_text`` draws every glyph at fullwidth (1 em) regardless
        of the real advance, so width must be ``len(text) * size``.
      * System TTF/OTF fonts (Noto Sans CJK, PingFang, Microsoft YaHei,
        …) — glyph widths are proportional and ``Font.text_length`` is
        accurate. Loaded via ``fitz.Font(fontfile=path)`` so we route
        widths through the actual font.
    The right strategy is picked from the CJK font store at measure time.
    """

    _BUNDLED_CJK = frozenset({"china-s", "china-t", "japan", "korea",
                              "china-ss", "china-ts", "japan-s", "korea-s"})

    _cache: dict = {}
    # (font_name) -> {chr: advance_at_size_1.0}
    _char_table: dict = {}
    # ASCII range is pre-warmed lazily on first use of each font.
    _warmed: set = set()

    @classmethod
    def font(cls, name: str) -> fitz.Font:
        f = cls._cache.get(name)
        if f is None:
            entry = font_entry_for_fontname(name)
            if entry is not None and entry.fontfile is not None:
                # System CJK font — load by file path.
                f = fitz.Font(fontfile=entry.fontfile)
            else:
                f = fitz.Font(name)
            cls._cache[name] = f
        return f

    @classmethod
    def _char_widths(cls, name: str) -> dict:
        t = cls._char_table.get(name)
        if t is None:
            t = {}
            cls._char_table[name] = t
        if name not in cls._warmed:
            font = cls.font(name)
            # Warm the printable-ASCII range in one batch: a single
            # text_length call returns the total, but we want per-char
            # entries, so call once per char. This happens at most once
            # per font for the whole process.
            for i in range(32, 127):
                c = chr(i)
                t[c] = font.text_length(c, fontsize=1.0)
            cls._warmed.add(name)
        return t

    @classmethod
    def width(cls, font_name: str, text: str, size: float) -> float:
        if not text:
            return 0.0
        # Bundled CJK CID fonts: PyMuPDF's insert_text draws every glyph
        # at fullwidth regardless of the real advance, so width must
        # match — otherwise wrap boundaries don't line up with rendering.
        if font_name in cls._BUNDLED_CJK:
            return len(text) * size
        t = cls._char_widths(font_name)
        total = 0.0
        font = None
        for c in text:
            w = t.get(c)
            if w is None:
                if font is None:
                    font = cls.font(font_name)
                w = font.text_length(c, fontsize=1.0)
                t[c] = w
            total += w
        return total * size


# ---------------------------------------------------------------------------
# CJK support.
#
# Base14 PDF fonts (Times / Courier / Helvetica) carry no CJK glyphs, so
# inserting CJK text with them silently drops every codepoint. PyMuPDF
# bundles four CID fonts that cover the major CJK scripts and accept
# directly as fontnames to ``Page.insert_text``:
#
#   china-s  Simplified Chinese (also covers most CJK Unified Ideographs)
#   china-t  Traditional Chinese
#   japan    Japanese (hiragana / katakana, kanji via Han)
#   korea    Korean (hangul + Han)
#
# Each font handles its own script plus the shared Han block — but
# china-s has no hangul and korea has no kana, so we route each character
# to the right font based on its Unicode block. CJK doesn't use
# whitespace between characters, so wrapping breaks per character inside
# a CJK run (Latin words still break on whitespace as usual).
# ---------------------------------------------------------------------------


def _cjk_script_for_char(c: str) -> Optional[str]:
    """Pick the CJK script that owns ``c`` (or None for non-CJK)."""
    if not c:
        return None
    o = ord(c)
    if 0x3040 <= o <= 0x30FF:
        return SCRIPT_JAPAN
    if 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
        return SCRIPT_KOREA
    if _is_cjk_char(c):
        # Han / CJK punctuation / halfwidth-fullwidth → Chinese by default.
        return SCRIPT_HAN
    return None


def _cjk_font_for_char(c: str) -> Optional[str]:
    """Return the *concrete fontname* used to render ``c``.

    None when ``c`` is not CJK — the caller keeps its requested base14
    font. Concrete fontname comes from the CJK font store, so it may be
    a bundled name (``china-s``) or a system-resolved name (e.g.
    ``NotoSansCJK-SC``).
    """
    script = _cjk_script_for_char(c)
    if script is None:
        return None
    return fontname_for_script(script)


def _dominant_cjk_font(text: str) -> Optional[str]:
    """Pick a single concrete CJK fontname for a run of mixed text.

    Used when we must commit to one fontname for an entire line draw call.
    Kana presence wins (only the Japanese font reliably covers them);
    else Hangul wins (only the Korean font reliably covers it); else any
    Han glyph falls back to the Han font. Returns None if the text
    contains no CJK at all.
    """
    has_kana = False
    has_hangul = False
    has_han = False
    for c in text:
        script = _cjk_script_for_char(c)
        if script == SCRIPT_JAPAN:
            has_kana = True
        elif script == SCRIPT_KOREA:
            has_hangul = True
        elif script == SCRIPT_HAN:
            has_han = True
    if has_kana:
        return fontname_for_script(SCRIPT_JAPAN)
    if has_hangul:
        return fontname_for_script(SCRIPT_KOREA)
    if has_han:
        return fontname_for_script(SCRIPT_HAN)
    return None


# ---------------------------------------------------------------------------
# Font family + glyph-coverage promotion.
# ---------------------------------------------------------------------------


def _base_font_for(family: str, bold: bool, italic: bool) -> str:
    """Pick the base14 font name that matches the source's serif/sans
    style and the requested weight / slant.

    ``family`` is taken from the source block's char-weighted majority
    of serif vs sans-serif spans, so a sans-serif heading inside a
    serif paper renders in a sans-serif face, and vice versa.
    """
    if family == "sans":
        if bold and italic:
            return "helvetica-bolditalic"
        if bold:
            return "helvetica-bold"
        if italic:
            return "helvetica-italic"
        return "helvetica"
    if bold and italic:
        return "times-bolditalic"
    if bold:
        return "times-bold"
    if italic:
        return "times-italic"
    return "times-roman"


# Base14 PDF fonts (Times / Helvetica / Courier as exposed by PyMuPDF's
# short names) use the WinAnsi encoding when drawn via ``insert_text``.
# Even though MuPDF's ``Font.has_glyph`` may report a glyph for, say,
# U+012B (ī), the encoding can't actually address it — insert_text
# substitutes the bullet glyph. We therefore explicitly enumerate the
# codepoints that round-trip through base14 fonts; everything else
# needs a system fallback regardless of what has_glyph claims.
_BASE14_FONTS = frozenset({
    "times-roman", "times-bold", "times-italic", "times-bolditalic",
    "helvetica", "helvetica-bold", "helvetica-italic", "helvetica-bolditalic",
    "courier", "courier-bold", "courier-italic", "courier-bolditalic",
})

# WinAnsi codepoints: printable ASCII + Latin-1 supplement + a fixed
# set of typographic / European extras (smart quotes, dashes, OE/oe,
# S-caron / Z-caron / Y-diaeresis, modifier circumflex/tilde, dagger,
# euro, ellipsis, bullet, trademark, per-mille).
_WINANSI_CODEPOINTS = (
    set(range(0x0020, 0x007F))      # printable ASCII
    | set(range(0x00A0, 0x0100))    # Latin-1 supplement
    | {
        0x20AC, 0x201A, 0x0192, 0x201E, 0x2026, 0x2020, 0x2021, 0x02C6,
        0x2030, 0x0160, 0x2039, 0x0152, 0x017D, 0x2018, 0x2019, 0x201C,
        0x201D, 0x2022, 0x2013, 0x2014, 0x02DC, 0x2122, 0x0161, 0x203A,
        0x0153, 0x017E, 0x0178,
    }
)


class GlyphCoverage:
    """Cache per-(fontname, codepoint) glyph presence checks.

    For base14 fonts we use the WinAnsi whitelist above (because
    ``Font.has_glyph`` is unreliable for those — the glyph may exist
    in the font but be unaddressable through the encoding insert_text
    uses). For system / CJK fonts we trust ``has_glyph``.
    """

    _cache: dict = {}

    @classmethod
    def has(cls, font_name: str, codepoint: int) -> bool:
        if font_name in _BASE14_FONTS:
            return codepoint in _WINANSI_CODEPOINTS
        key = (font_name, codepoint)
        v = cls._cache.get(key)
        if v is None:
            font = FontMetrics.font(font_name)
            v = bool(font.has_glyph(codepoint))
            cls._cache[key] = v
        return v


def _text_fully_covered(font_name: str, text: str) -> bool:
    """Return True iff every non-whitespace char in ``text`` is
    addressable in ``font_name``."""
    for c in text:
        if c.isspace():
            continue
        if not GlyphCoverage.has(font_name, ord(c)):
            return False
    return True


def _family_of(font_name: str) -> str:
    """Recover the serif/sans family from a base14 fontname so the
    Latin-extended fallback can match it."""
    if font_name.startswith("helvetica"):
        return "sans"
    return "serif"


def _latin_fallback_for(base_font: str) -> Optional[str]:
    """Pick the Latin-extended fallback fontname matching ``base_font``'s
    family. Returns None when the resolver has no system font (i.e. the
    fallback collapses back to the base font and would be a no-op).
    """
    script = (SCRIPT_LATIN_SANS if _family_of(base_font) == "sans"
              else SCRIPT_LATIN_SERIF)
    fb = fontname_for_script(script)
    return None if fb == base_font else fb


def _font_for_char(c: str, base_font: str) -> str:
    """Pick the best font to render a single char given the paragraph's
    base font.

    Priority:
      1. CJK chars → the appropriate CJK font (china-s / japan / korea
         or whichever system font the resolver chose for that script).
      2. Chars covered by ``base_font`` (incl. the WinAnsi fast-path
         that all base14 fonts share) → ``base_font``.
      3. Else, a system Latin-extended fallback matching the family
         (Liberation Serif / Sans, DejaVu, Free, Noto). When none is
         installed we degrade to ``base_font`` and the glyph drops at
         render time — the same best-effort fallback as before.
    """
    if c.isspace():
        return base_font
    # CJK chars always go to their script's font, regardless of base.
    cjk = _cjk_font_for_char(c)
    if cjk is not None:
        return cjk
    o = ord(c)
    if GlyphCoverage.has(base_font, o):
        return base_font
    fb = _latin_fallback_for(base_font)
    if fb is not None and GlyphCoverage.has(fb, o):
        return fb
    return base_font


def _split_into_font_runs(text: str, base_font: str) -> List[Tuple[str, str]]:
    """Walk ``text`` and group consecutive chars that need the same font.

    Returns a list of ``(fontname, substring)`` pairs ready to feed to
    consecutive ``insert_text`` calls. Empty input returns an empty list.
    """
    runs: List[Tuple[str, str]] = []
    cur_font: Optional[str] = None
    cur: List[str] = []
    for c in text:
        f = _font_for_char(c, base_font)
        if cur_font is None or f == cur_font:
            cur.append(c)
            cur_font = f
        else:
            runs.append((cur_font, "".join(cur)))
            cur = [c]
            cur_font = f
    if cur and cur_font is not None:
        runs.append((cur_font, "".join(cur)))
    return runs


def _multifont_width(text: str, base_font: str, size: float) -> float:
    """Width of ``text`` if rendered with per-char font fallback off
    ``base_font``. Used by the wrap engine so line boundaries match
    what ``emit_text_line`` actually draws."""
    if not text:
        return 0.0
    total = 0.0
    for font, run in _split_into_font_runs(text, base_font):
        total += FontMetrics.width(font, run, size)
    return total


# Kept for backwards compatibility with callers that want the old
# "single fontname per paragraph" decision (e.g. the TOC dot-leader
# code path where the leader must be drawn in one font). Equivalent to
# the previous CJK-only promotion.
def _pick_font(base_font: str, text: str) -> str:
    cjk = _dominant_cjk_font(text)
    return cjk if cjk is not None else base_font


# ---------------------------------------------------------------------------
# Line breaking.
# ---------------------------------------------------------------------------


def _tokenize_for_wrap(text: str) -> List[Tuple[str, bool]]:
    """Tokenize ``text`` into ``(token, had_leading_whitespace)`` pairs.

    Latin words stay grouped (no breaks inside a word). Each CJK char is
    its own token because CJK lines wrap at any character boundary —
    there are no inter-character spaces to use as break opportunities.
    Whitespace in the source is collapsed and lifted into the boolean
    flag, so the wrapper can faithfully reproduce ``詩經 Shījīng``
    (Han + space + transcription) but elide gaps inside a pure CJK run.
    """
    tokens: List[Tuple[str, bool]] = []
    buf: List[str] = []
    pending_ws = False

    def flush_buf():
        nonlocal pending_ws
        if not buf:
            return
        # Defensive: split on any whitespace that may have slipped into
        # the Latin buffer (shouldn't happen — we drain on whitespace).
        words = "".join(buf).split()
        for i, w in enumerate(words):
            tokens.append((w, pending_ws if i == 0 else True))
        buf.clear()
        pending_ws = False

    for ch in text:
        if _is_cjk_char(ch):
            flush_buf()
            tokens.append((ch, pending_ws))
            pending_ws = False
        elif ch.isspace():
            flush_buf()
            pending_ws = True
        else:
            buf.append(ch)
    flush_buf()
    return tokens


# Penalty (in Knuth-Plass demerit units) added when a line breaks right
# after a hyphen, so the optimiser only hyphenates when it visibly
# improves the paragraph and avoids stacks of hyphenated lines.
_HYPHEN_PENALTY = 50.0


def _wrap_paragraph(
    text: str,
    font: str,
    size: float,
    max_width: float,
) -> List[str]:
    """Wrap ``text`` to ``max_width`` with Unicode-aware, optimal breaks.

    Break opportunities come from the Unicode line breaking algorithm
    (UAX #14, what ICU implements): after spaces and hyphens, between
    ideographs, around slashes / em-dashes, while never orphaning closing
    punctuation. The chosen break points are then selected by the
    Knuth-Plass total-fit algorithm so the right margin is as even as
    possible rather than greedily ragged.

    Width is measured with per-char font fallback off ``font`` (the
    paragraph's *base* font), so wrap boundaries line up exactly with what
    ``emit_text_line`` draws. ``font`` covers ASCII; CJK chars and
    Latin-extended glyphs are measured against their resolved fallback.
    """
    if not text:
        return []
    boxes, seps = _uax14_segments(text)
    if not boxes or (len(boxes) == 1 and boxes[0] == ""):
        return []
    # Single unbreakable run: emit as-is, or force-break if it overflows.
    if len(seps) == 0:
        only = boxes[0]
        if _multifont_width(only, font, size) <= max_width:
            return [only]
        return _break_oversize_word(only, font, size, max_width)

    space_w = FontMetrics.width(font, " ", size)
    stretch = max(space_w * 3.0, 1.0)

    # Build the Knuth-Plass item stream (Box / Glue / Penalty) alongside a
    # parallel ``pieces`` list used to rebuild line text from the chosen
    # breakpoints. The two lists are index-aligned.
    pieces: List[Tuple[str, object]] = []   # ('box', text) | ('sep', Sep)
    items: List[object] = []

    def add_box(t: str) -> None:
        w = _multifont_width(t, font, size)
        if w > max_width and len(t) > 1:
            # No legal break inside this run but it overflows the column;
            # fall back to character chunking (an "emergency" break) and
            # let the optimiser break between the chunks.
            chunks = _break_oversize_word(t, font, size, max_width)
            for ci, chunk in enumerate(chunks):
                if ci > 0:
                    pieces.append(("sep", _Sep(space=False, hyphen=False,
                                               mandatory=False)))
                    items.append(_KPPenalty(0.0, 0.0, False))
                pieces.append(("box", chunk))
                items.append(_KPBox(_multifont_width(chunk, font, size)))
        else:
            pieces.append(("box", t))
            items.append(_KPBox(w))

    add_box(boxes[0])
    for i, sep in enumerate(seps):
        pieces.append(("sep", sep))
        if sep.mandatory:
            items.append(_KPPenalty(0.0, -_KP_INF, False))
        elif sep.space:
            items.append(_KPGlue(space_w, stretch, 0.0))
        else:
            pen = _HYPHEN_PENALTY if sep.hyphen else 0.0
            items.append(_KPPenalty(0.0, pen, sep.hyphen))
        add_box(boxes[i + 1])

    params = _KPParams(default_stretch=stretch)
    chosen = set(_kp_break_lines(_kp_add_final_break(items), max_width, params))

    lines: List[str] = []
    cur = ""
    started = False
    for idx, (kind, payload) in enumerate(pieces):
        if kind == "box":
            if not started:
                cur = payload
                started = True
            else:
                prev_sep = pieces[idx - 1][1]
                cur += (" " + payload) if prev_sep.space else payload
        elif idx in chosen:
            lines.append(cur)
            cur = ""
            started = False
    if started:
        lines.append(cur)
    return lines


def _break_oversize_word(word: str, font: str, size: float, max_width: float) -> List[str]:
    """Force-break a word that is wider than the line by chunking characters."""
    chunks: List[str] = []
    start = 0
    while start < len(word):
        # Binary search for the largest prefix that fits.
        lo, hi = 1, len(word) - start
        best = 1
        while lo <= hi:
            mid = (lo + hi) // 2
            wpx = _multifont_width(word[start:start + mid], font, size)
            if wpx <= max_width:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        chunks.append(word[start:start + best])
        start += best
    return chunks


# ---------------------------------------------------------------------------
# Layout state machine.
# ---------------------------------------------------------------------------


class _PageBuilder:
    """Accumulates ops on the current page; opens a new page on overflow."""

    def __init__(self, cfg: LayoutConfig):
        self.cfg = cfg
        self.pages: List[LaidOutPage] = []
        self._open_page()

    def _open_page(self) -> None:
        self.pages.append(LaidOutPage(width=self.cfg.page_width, height=self.cfg.page_height))
        self.y = self.cfg.content_top

    @property
    def current(self) -> LaidOutPage:
        return self.pages[-1]

    def remaining(self) -> float:
        return self.cfg.content_bottom - self.y

    def ensure_space(self, h: float) -> None:
        if h > self.remaining():
            self._open_page()

    def add_space(self, dy: float) -> None:
        self.y += dy

    def emit_text_line(self, text: str, font: str, size: float, x: Optional[float] = None,
                       align: str = "left") -> None:
        line_h = size * self.cfg.line_height_mult
        # Reserve descender room: baseline is at y + size.
        if line_h > self.remaining():
            self._open_page()
        baseline = self.y + size
        # Split into per-font runs so chars missing from the base font
        # (IPA, modifier letters, CJK) get drawn in their fallback —
        # the same coverage logic that the wrap engine measured with,
        # so x-offsets line up exactly.
        runs = _split_into_font_runs(text, font)
        line_w = sum(FontMetrics.width(rf, rt, size) for rf, rt in runs)
        if x is not None:
            cx = x
        elif align == "center":
            cx = self.cfg.content_left + max(0.0, (self.cfg.content_width - line_w) / 2)
        else:
            cx = self.cfg.content_left
        for rf, rt in runs:
            self.current.ops.append(
                DrawText(x=cx, y=baseline, text=rt, font=rf, size=size))
            cx += FontMetrics.width(rf, rt, size)
        self.y += line_h

    def emit_toc_entry(
        self,
        title: str,
        page: str,
        font: str,
        size: float,
        indent: float = 0.0,
    ) -> None:
        """Render a single table-of-contents entry: ``title .... page``.

        The title is left-aligned (optionally indented for nested entries),
        the page number is right-aligned at the column edge, and a dot
        leader fills the gap. If the title doesn't fit on one line it is
        wrapped onto subsequent lines and the page number / leader sit on
        the last line."""
        cfg = self.cfg
        left = cfg.content_left + indent
        right = cfg.content_right
        avail = right - left
        if avail <= 0:
            avail = cfg.content_width
            left = cfg.content_left

        space_w = FontMetrics.width(font, " ", size)
        dot_w = FontMetrics.width(font, ".", size)
        page_w = FontMetrics.width(font, page, size)

        # How wide a title can be on one line before we need the leader to
        # disappear / wrap. Reserve room for the page number plus a single
        # space, and at least 3 dots so the leader stays recognizable.
        min_leader_w = 3 * dot_w + 2 * space_w
        title_budget = avail - page_w - min_leader_w

        title_lines = _wrap_paragraph(title, font, size, max(title_budget, avail * 0.4))
        if not title_lines:
            return

        line_h = size * cfg.line_height_mult
        total_h = line_h * len(title_lines)
        if total_h > self.remaining():
            self._open_page()

        for i, line_text in enumerate(title_lines):
            if line_h > self.remaining():
                self._open_page()
            baseline = self.y + size
            # Title may contain CJK / IPA chars that need fallback fonts;
            # render per-run so the right glyphs reach the output.
            cx = left
            title_runs = _split_into_font_runs(line_text, font)
            for rf, rt in title_runs:
                self.current.ops.append(DrawText(
                    x=cx, y=baseline, text=rt, font=rf, size=size,
                ))
                cx += FontMetrics.width(rf, rt, size)
            if i == len(title_lines) - 1:
                title_w = cx - left
                gap = avail - title_w - page_w - 2 * space_w
                if gap >= dot_w:
                    n_dots = max(1, int(gap / dot_w))
                    dots = "." * n_dots
                    dots_x = left + title_w + space_w
                    self.current.ops.append(DrawText(
                        x=dots_x, y=baseline, text=dots, font=font, size=size,
                    ))
                self.current.ops.append(DrawText(
                    x=right - page_w, y=baseline, text=page,
                    font=font, size=size,
                ))
            self.y += line_h

    def emit_image(self, src_page: int, src_rect, w: float, h: float, center: bool = True) -> None:
        if h > self.cfg.content_height * self.cfg.figure_max_height_frac:
            # Cap the figure's height.
            scale = (self.cfg.content_height * self.cfg.figure_max_height_frac) / h
            w *= scale
            h *= scale
        if h > self.remaining():
            self._open_page()
        x = self.cfg.content_left
        if center and w < self.cfg.content_width:
            x = self.cfg.content_left + (self.cfg.content_width - w) / 2
        self.current.ops.append(DrawImage(x=x, y=self.y, w=w, h=h,
                                          source_page=src_page, source_rect=src_rect))
        self.y += h


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def layout(
    items: List[FlowItem],
    cfg: Optional[LayoutConfig] = None,
) -> Tuple[List[LaidOutPage], List[HeadingAnchor]]:
    cfg = cfg or LayoutConfig()
    pb = _PageBuilder(cfg)
    body = cfg.body_size
    cw = cfg.content_width
    anchors: List[HeadingAnchor] = []

    def emit_paragraph(text: str, font: str, size: float, lead_space: float = 0.0,
                       align: str = "left"):
        text = text.strip()
        if not text:
            return
        # ``font`` is the requested *base* font; per-char fallback inside
        # _wrap_paragraph / emit_text_line routes CJK chars to the
        # appropriate CJK font and any remaining missing glyphs (IPA,
        # modifier letters, …) to a system Latin-extended font.
        if lead_space:
            pb.add_space(lead_space)
        for line in _wrap_paragraph(text, font, size, cw):
            pb.emit_text_line(line, font, size, align=align)

    for it in items:
        if it.kind == "heading":
            # Detect title vs section heading vs run-in subheading by
            # comparing the source font size to the inferred body size.
            if it.size >= body + 2.5:
                size = body * cfg.heading_scale_h1
                space_above = cfg.heading_space_above
                level = 1
            elif it.size >= body + 1.0:
                size = body * cfg.heading_scale_h2
                space_above = cfg.heading_space_above
                level = 2
            else:
                # Body-size run-in subheading (LaTeX \paragraph{...} or
                # IEEE-style ``A. Full-Sized Camera-Ready (CR) Copy``).
                # Keep the body size to preserve cadence; use a smaller
                # lead so it reads as a paragraph label, not a new
                # section break.
                size = body
                space_above = cfg.para_space
                level = 3
            # Level 3 headings keep their italic flag — IEEE-style
            # sub-section heads render as italic, not bold. Level 1/2
            # always render as bold; bold-italic when both flags set.
            # Family comes from the source block so a sans-serif
            # heading inside an otherwise serif paper stays sans-serif.
            if level == 3 and it.italic and not it.bold:
                font = _base_font_for(it.family, bold=False, italic=True)
            elif it.italic and it.bold:
                font = _base_font_for(it.family, bold=True, italic=True)
            else:
                font = _base_font_for(it.family, bold=True, italic=False)
            pb.add_space(space_above)
            # Ensure room for at least one heading line before recording the
            # anchor so the anchor page/y reflects any page-break that occurs.
            pb.ensure_space(size * cfg.line_height_mult)
            anchors.append(HeadingAnchor(
                text=it.text,
                level=level,
                out_page=len(pb.pages) - 1,
                y=pb.y,
            ))
            emit_paragraph(it.text, font, size, align=it.align)
            pb.add_space(cfg.heading_space_below)
        elif it.kind == "body":
            font = _base_font_for(it.family, bold=it.bold, italic=it.italic)
            emit_paragraph(it.text, font, body, lead_space=cfg.para_space, align=it.align)
        elif it.kind == "toc":
            parsed = _parse_toc_entry(it.text)
            base = _base_font_for(it.family, bold=False, italic=False)
            if parsed is None:
                emit_paragraph(it.text, base, body, lead_space=cfg.para_space,
                               align=it.align)
                continue
            title, page_label = parsed
            # Scale source-pt indent to a milder mobile indent, capped so
            # deeply nested entries still leave room for the title text.
            scaled_indent = min(it.indent * 0.5, cw * 0.3)
            pb.emit_toc_entry(
                title=title,
                page=page_label,
                font=base,
                size=body,
                indent=scaled_indent,
            )
        elif it.kind == "caption":
            font = _base_font_for(it.family, bold=False, italic=it.italic)
            emit_paragraph(it.text, font, cfg.caption_size, lead_space=cfg.para_space, align=it.align)
        elif it.kind == "code":
            # Pre-formatted: each source line drawn at code_size. If a line is
            # wider than the column, scale the code_size down for that block
            # so it fits without horizontal scroll.
            lines = it.code_lines or [it.text]
            # Courier base; per-char fallback handles any CJK/IPA chars
            # in code blocks (monospaced spacing is lost on fallback
            # chars but the text is at least visible).
            font = "courier"
            size = cfg.code_size
            longest = max((_multifont_width(ln, font, size) for ln in lines), default=0.0)
            if longest > cw:
                size = max(6.0, size * cw / longest)
            block_height = cfg.para_space + len(lines) * size * cfg.line_height_mult
            # Keep small code blocks together on one page.
            if block_height <= cfg.content_height * 0.85 and block_height > pb.remaining():
                pb._open_page()
            pb.add_space(cfg.para_space)
            for ln in lines:
                pb.emit_text_line(ln, font, size)
        elif it.kind == "figure":
            src = it.source_rect or it.bbox
            sx0, sy0, sx1, sy1 = src
            sw = max(1.0, sx1 - sx0)
            sh = max(1.0, sy1 - sy0)
            # Fit-to-column would stretch a 60pt-wide equation crop 5x,
            # filling 150pt+ of vertical space with two glyphs. Cap the
            # upscale so small math fragments stay near a readable size
            # and get centered in the column instead. Display-math rasters
            # (no vector art) use a tighter cap so their glyphs keep the
            # authored size and don't tower over the body text; only true
            # figures are enlarged to fill the column.
            max_upscale = (cfg.equation_max_upscale if it.is_equation
                           else cfg.figure_max_upscale)
            scale = min(cw / sw, max_upscale)
            w = sw * scale
            h = sh * scale
            pb.add_space(cfg.para_space)
            pb.emit_image(it.page_index, src, w, h)
        else:
            # Unknown kind: fall back to body rendering.
            emit_paragraph(it.text, _base_font_for(it.family, False, False),
                           body, lead_space=cfg.para_space)

    return pb.pages, anchors
