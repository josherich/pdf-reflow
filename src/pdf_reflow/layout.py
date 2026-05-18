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

from .analyze import FlowItem


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
    """

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
# Line breaking.
# ---------------------------------------------------------------------------


def _split_words(text: str) -> List[str]:
    """Word-tokenize while preserving spaces and breaks. We keep each word as
    a string; spaces are added back during measurement."""
    return text.split()


def _wrap_paragraph(
    text: str,
    font: str,
    size: float,
    max_width: float,
) -> List[str]:
    words = _split_words(text)
    if not words:
        return []
    lines: List[str] = []
    cur: List[str] = []
    cur_width = 0.0
    space_w = FontMetrics.width(font, " ", size)

    for w in words:
        ww = FontMetrics.width(font, w, size)
        if ww > max_width and not cur:
            # Single token wider than line: hard-break by chars.
            for chunk in _break_oversize_word(w, font, size, max_width):
                lines.append(chunk)
            cur_width = 0.0
            continue
        proposed = cur_width + (space_w if cur else 0.0) + ww
        if proposed <= max_width or not cur:
            cur.append(w)
            cur_width = proposed
        else:
            lines.append(" ".join(cur))
            cur = [w]
            cur_width = ww
    if cur:
        lines.append(" ".join(cur))
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
        elif it.kind == "caption":
            font = "times-italic" if it.italic else "times-roman"
            emit_paragraph(it.text, font, cfg.caption_size, lead_space=cfg.para_space, align=it.align)
        elif it.kind == "code":
            # Pre-formatted: each source line drawn at code_size. If a line is
            # wider than the column, scale the code_size down for that block
            # so it fits without horizontal scroll.
            lines = it.code_lines or [it.text]
            font = "courier"
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
