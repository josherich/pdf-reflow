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
    figure_max_upscale: float = 2.0      # never blow a tiny equation past 2x
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

    CJK fonts are special-cased: PyMuPDF's ``insert_text`` with a CJK
    CID font draws every char (including Latin and spaces) at the
    fullwidth advance (1 em = fontsize), but ``Font.text_length`` for
    the same font reports proportional widths that don't match what
    gets drawn. We therefore measure CJK fonts as ``len(text) * size``
    so wrap boundaries line up with what actually renders.
    """

    _CJK_FONTS = frozenset({"china-s", "china-t", "japan", "korea",
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
        if font_name in cls._CJK_FONTS:
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


def _is_cjk_char(c: str) -> bool:
    if not c:
        return False
    o = ord(c)
    return (
        0x4E00 <= o <= 0x9FFF   # CJK Unified Ideographs
        or 0x3400 <= o <= 0x4DBF   # CJK Ext A
        or 0x20000 <= o <= 0x2A6DF # CJK Ext B
        or 0xF900 <= o <= 0xFAFF   # CJK Compatibility Ideographs
        or 0x3040 <= o <= 0x309F   # Hiragana
        or 0x30A0 <= o <= 0x30FF   # Katakana
        or 0xAC00 <= o <= 0xD7AF   # Hangul Syllables
        or 0x1100 <= o <= 0x11FF   # Hangul Jamo
        or 0x3130 <= o <= 0x318F   # Hangul Compatibility Jamo
        or 0x3000 <= o <= 0x303F   # CJK Symbols & Punctuation
        or 0xFF00 <= o <= 0xFFEF   # Halfwidth / Fullwidth Forms
    )


def _cjk_font_for_char(c: str) -> Optional[str]:
    """Pick the built-in CJK font that covers ``c``.

    Returns None when ``c`` is not a CJK codepoint — Latin text should keep
    its requested base14 font.
    """
    if not c:
        return None
    o = ord(c)
    if 0x3040 <= o <= 0x30FF:
        return "japan"
    if 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
        return "korea"
    if _is_cjk_char(c):
        # Han / CJK punctuation / halfwidth-fullwidth: china-s covers the
        # broadest character set so we use it as the Han default.
        return "china-s"
    return None


def _dominant_cjk_font(text: str) -> Optional[str]:
    """Pick a single CJK font for a run of mixed text.

    Used when we must commit to one fontname for an entire line draw call.
    Hiragana/Katakana presence wins (only ``japan`` covers them); else
    Hangul wins (only ``korea`` covers it); else any Han glyph falls back
    to ``china-s``. Returns None if the text contains no CJK at all.
    """
    has_kana = False
    has_hangul = False
    has_han = False
    for c in text:
        font = _cjk_font_for_char(c)
        if font == "japan":
            has_kana = True
        elif font == "korea":
            has_hangul = True
        elif font == "china-s":
            has_han = True
    if has_kana:
        return "japan"
    if has_hangul:
        return "korea"
    if has_han:
        return "china-s"
    return None


def _pick_font(base_font: str, text: str) -> str:
    """Promote ``base_font`` to a CJK font when ``text`` needs CJK glyphs.

    Bold / italic styling is dropped for CJK runs because PyMuPDF's
    built-in CJK fonts only ship a regular weight — keeping the base14
    style would silently drop every CJK glyph. The trade-off is no faux
    bold on a CJK heading; the alternative is no visible text at all.
    """
    cjk = _dominant_cjk_font(text)
    return cjk if cjk is not None else base_font


# ---------------------------------------------------------------------------
# Line breaking.
# ---------------------------------------------------------------------------


def _tokenize_for_wrap(text: str) -> List[str]:
    """Tokenize ``text`` into wrap-atomic units.

    Latin words stay grouped (no breaks inside a word). Each CJK char is
    its own token because CJK lines wrap at any character boundary —
    there are no inter-character spaces to use as break opportunities.
    Whitespace is dropped: ``_wrap_paragraph`` reintroduces a single
    separator space only between two Latin tokens.
    """
    tokens: List[str] = []
    buf: List[str] = []

    def flush_buf():
        if not buf:
            return
        # The Latin buffer may itself contain runs of whitespace from the
        # source (e.g. tab-indented prose); ``split`` normalises them.
        tokens.extend("".join(buf).split())
        buf.clear()

    for ch in text:
        if _is_cjk_char(ch):
            flush_buf()
            tokens.append(ch)
        elif ch.isspace():
            flush_buf()
        else:
            buf.append(ch)
    flush_buf()
    return tokens


def _wrap_paragraph(
    text: str,
    font: str,
    size: float,
    max_width: float,
) -> List[str]:
    tokens = _tokenize_for_wrap(text)
    if not tokens:
        return []
    lines: List[str] = []
    cur: List[str] = []      # alternating tokens + " " separators as added
    cur_width = 0.0
    space_w = FontMetrics.width(font, " ", size)

    def needs_sep(prev_token: str, next_token: str) -> bool:
        # No space across a CJK / non-CJK boundary, and no space inside
        # a CJK run. Only Latin-to-Latin gets a separator.
        if _is_cjk_char(prev_token[-1]) or _is_cjk_char(next_token[0]):
            return False
        return True

    for tok in tokens:
        tw = FontMetrics.width(font, tok, size)
        sep = needs_sep(cur[-1], tok) if cur else False
        sep_w = space_w if sep else 0.0
        if tw > max_width and not cur:
            for chunk in _break_oversize_word(tok, font, size, max_width):
                lines.append(chunk)
            cur_width = 0.0
            continue
        proposed = cur_width + sep_w + tw
        if proposed <= max_width or not cur:
            if sep:
                cur.append(" ")
            cur.append(tok)
            cur_width = proposed
        else:
            lines.append("".join(cur))
            cur = [tok]
            cur_width = tw
    if cur:
        lines.append("".join(cur))
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
            wpx = FontMetrics.width(font, word[start:start + mid], size)
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
        if x is not None:
            cx = x
        elif align == "center":
            w = FontMetrics.width(font, text, size)
            cx = self.cfg.content_left + max(0.0, (self.cfg.content_width - w) / 2)
        else:
            cx = self.cfg.content_left
        self.current.ops.append(DrawText(x=cx, y=baseline, text=text, font=font, size=size))
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
            self.current.ops.append(DrawText(
                x=left, y=baseline, text=line_text, font=font, size=size,
            ))
            if i == len(title_lines) - 1:
                title_w = FontMetrics.width(font, line_text, size)
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
        # Promote to a CJK font when the paragraph contains CJK characters
        # — base14 fonts would drop every CJK glyph silently. Width
        # measurement and rendering must share the same fontname or wrap
        # boundaries won't match what's drawn.
        font = _pick_font(font, text)
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
            if level == 3 and it.italic and not it.bold:
                font = "times-italic"
            elif it.italic and it.bold:
                font = "times-bolditalic"
            else:
                font = "times-bold"
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
            font = "times-italic" if it.italic else ("times-bold" if it.bold else "times-roman")
            emit_paragraph(it.text, font, body, lead_space=cfg.para_space, align=it.align)
        elif it.kind == "toc":
            parsed = _parse_toc_entry(it.text)
            if parsed is None:
                emit_paragraph(it.text, "times-roman", body, lead_space=cfg.para_space,
                               align=it.align)
                continue
            title, page_label = parsed
            # Scale source-pt indent to a milder mobile indent, capped so
            # deeply nested entries still leave room for the title text.
            scaled_indent = min(it.indent * 0.5, cw * 0.3)
            pb.emit_toc_entry(
                title=title,
                page=page_label,
                font=_pick_font("times-roman", title),
                size=body,
                indent=scaled_indent,
            )
        elif it.kind == "caption":
            font = "times-italic" if it.italic else "times-roman"
            emit_paragraph(it.text, font, cfg.caption_size, lead_space=cfg.para_space, align=it.align)
        elif it.kind == "code":
            # Pre-formatted: each source line drawn at code_size. If a line is
            # wider than the column, scale the code_size down for that block
            # so it fits without horizontal scroll.
            lines = it.code_lines or [it.text]
            # Courier has no CJK glyphs; if any line contains CJK, fall
            # back to a CJK font for the whole block (monospaced spacing
            # is lost but the text is at least visible).
            font = _pick_font("courier", "".join(lines))
            size = cfg.code_size
            longest = max((FontMetrics.width(font, ln, size) for ln in lines), default=0.0)
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
            # and get centered in the column instead.
            scale = min(cw / sw, cfg.figure_max_upscale)
            w = sw * scale
            h = sh * scale
            pb.add_space(cfg.para_space)
            pb.emit_image(it.page_index, src, w, h)
        else:
            # Unknown kind: fall back to body rendering.
            emit_paragraph(it.text, "times-roman", body, lead_space=cfg.para_space)

    return pb.pages, anchors
