"""Group raw spans into lines and blocks, then classify them.

Outputs a stream of `FlowItem`s in reading order, ready for the layout
engine. Figures (vector diagrams) become single image-rasterization items
that capture the original visual content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from collections import Counter
from typing import List, Optional, Tuple

from .extract import PageContent, Span


BBox = Tuple[float, float, float, float]


@dataclass
class Line:
    spans: List[Span]
    bbox: BBox
    size: float           # representative size

    @property
    def text(self) -> str:
        # spans are not yet space-separated. Join with spacing inferred from x-gaps.
        if not self.spans:
            return ""
        parts: List[str] = []
        prev: Optional[Span] = None
        for s in self.spans:
            t = s.text
            if prev is not None:
                gap = s.x0 - prev.x1
                # If span ends without a space and there's a real gap, add one.
                # 0.18·size catches the tight inter-word kerning that LaTeX
                # produces when small-caps or math fonts split a word into
                # several spans (3pt gap at body size 12 → 0.25·size).
                if gap > 0.18 * s.size and not parts[-1].endswith(" ") and not t.startswith(" "):
                    parts.append(" ")
            parts.append(t)
            prev = s
        return "".join(parts)


@dataclass
class Block:
    lines: List[Line]
    bbox: BBox
    page_index: int
    kind: str = "body"     # 'heading' | 'body' | 'caption' | 'label' | 'figure'
    size: float = 0.0
    bold: bool = False
    italic: bool = False
    column: int = 0        # -1 = full-width (spans both columns), 0 = left/single, 1 = right
    align: str = "left"    # 'left' | 'center'

    @property
    def text(self) -> str:
        # Join lines with single spaces, treating common hyphenations.
        out: List[str] = []
        for ln in self.lines:
            t = ln.text.rstrip()
            if out and out[-1].endswith("-") and len(out[-1]) > 1 and out[-1][-2].isalpha():
                out[-1] = out[-1][:-1] + t.lstrip()
            else:
                if out:
                    out.append(" ")
                out.append(t.lstrip() if out else t)
        return "".join(out).strip()


@dataclass
class FlowItem:
    """A single piece of mobile-bound content: text block, figure raster, or pagebreak."""
    kind: str                      # 'heading'|'body'|'caption'|'figure'|'label'|'code'
    page_index: int
    bbox: BBox                     # original bbox (for figures: y-band on source page)
    text: str = ""
    size: float = 0.0
    bold: bool = False
    italic: bool = False
    source_rect: Optional[BBox] = None  # for figures: rect to rasterize on the source page
    monospace: bool = False             # for 'code' blocks
    code_lines: List[str] = field(default_factory=list)
    align: str = "left"                 # 'left' | 'center' — preserves centered headings/captions
    indent: float = 0.0                 # for 'toc': source-pt indent of the entry relative to the block column


# Font-name prefixes that identify math-only glyph fonts. LaTeX's
# Computer Modern math fonts (CMMI, CMSY, CMEX) and AMS extensions
# (MSAM, MSBM) emit math symbols at real Unicode codepoints (π, ∞, ℵ, …)
# rather than the Private Use Area, so a PUA-only check misses them.
# Latin Modern, RSFS (script), Euler and Stix Math variants follow the
# same naming convention.
_MATH_FONT_PREFIXES = (
    "CMMI", "CMSY", "CMEX",
    "MSAM", "MSBM",
    "LMMI", "LMSY", "LMEX",
    "RSFS",
    "EUSM", "EUSB", "EUEX", "EUFM", "EURB", "EURM",
    "STIXMath", "STIX-Math",
)


def _math_font(name: str) -> bool:
    """Return True if ``name`` is a font that exists solely to render math
    glyphs (its presence is a strong signal that the host block is an
    equation, not body text)."""
    if not name:
        return False
    if "Math" in name and "MathJax" not in name:
        return True
    # PyMuPDF font names can have a 6-char "+ABCDEF" subset prefix and a
    # weight/style suffix after a hyphen; strip both before matching.
    head = name
    if "+" in head:
        head = head.split("+", 1)[1]
    head = head.split("-", 1)[0]
    return any(head.startswith(p) for p in _MATH_FONT_PREFIXES)


def _line_math_score(line: "Line") -> float:
    """Fraction of non-whitespace chars on the line that come from a math
    font. Used to detect display-math lines that share their baseline with
    body text size."""
    math = 0
    total = 0
    for s in line.spans:
        n = sum(1 for c in s.text if not c.isspace())
        total += n
        if _math_font(s.font):
            math += n
    return math / total if total else 0.0


def _block_math_score(block: "Block") -> float:
    math = 0
    total = 0
    for ln in block.lines:
        for s in ln.spans:
            n = sum(1 for c in s.text if not c.isspace())
            total += n
            if _math_font(s.font):
                math += n
    return math / total if total else 0.0


def _block_has_math_font(block: "Block") -> bool:
    for ln in block.lines:
        for s in ln.spans:
            if _math_font(s.font):
                return True
    return False


def _split_runin_subheading_line(line: "Line") -> List["Line"]:
    """If ``line`` begins with one or more bold spans followed by a run of
    regular-weight spans on the same baseline, split it into two lines —
    the bold prefix (a LaTeX ``\\paragraph{...}`` style run-in subheading)
    and the body continuation. Otherwise return ``[line]`` unchanged.

    LaTeX templates typeset run-in subheadings inline with the first body
    sentence: the baseline is shared, so ``_group_lines`` correctly puts
    the bold and regular spans on a single line. The line builder /
    classifier downstream is char-count-weighted, so the bold prefix is
    drowned out by the body and the lead-in renders as plain prose. We
    pre-emptively split here when the bold prefix is short, ends without
    sentence-final punctuation, and is followed by a substantial body
    fragment — the same shape as a ``\\paragraph`` macro's output."""
    spans = line.spans
    if len(spans) < 2:
        return [line]
    # Walk from the left while spans are bold (and not math).
    cut = 0
    for s in spans:
        if s.is_bold and not _math_font(s.font):
            cut += 1
        else:
            break
    if cut == 0 or cut == len(spans):
        return [line]
    head_spans = spans[:cut]
    tail_spans = spans[cut:]
    head_text = "".join(s.text for s in head_spans).strip()
    tail_text = "".join(s.text for s in tail_spans).strip()
    # Run-in subheadings are short and don't terminate a sentence.
    if not head_text or len(head_text) > 80 or head_text.endswith((".", "?", "!")):
        return [line]
    # The body continuation must itself be substantial — a single trailing
    # symbol like a footnote mark shouldn't trigger a split.
    if len(tail_text) < 8:
        return [line]
    # Both halves keep the original baseline y; bboxes are the union of
    # their constituent spans.
    def _bbox(ss):
        x0 = min(s.x0 for s in ss); y0 = min(s.y0 for s in ss)
        x1 = max(s.x1 for s in ss); y1 = max(s.y1 for s in ss)
        return (x0, y0, x1, y1)
    head_size = Counter(round(s.size, 1) for s in head_spans).most_common(1)[0][0]
    tail_size = Counter(round(s.size, 1) for s in tail_spans).most_common(1)[0][0]
    return [
        Line(spans=head_spans, bbox=_bbox(head_spans), size=head_size),
        Line(spans=tail_spans, bbox=_bbox(tail_spans), size=tail_size),
    ]


# Numbered reference list items: '[1] G. Eason ...', '[12] ...'. Each new
# item must start its own block so wrapped continuation lines stay with
# the right item and the rendered output reads as a vertical list.
_LIST_ITEM_RE = re.compile(r"^\s*\[\d+\]\s")


def _line_starts_list_item(line: "Line") -> bool:
    return bool(_LIST_ITEM_RE.match(line.text))


# Table-of-contents entry: any line that ends with a dot leader (".....")
# followed by a page reference (arabic or roman numeral). Each TOC entry
# must occupy its own block so the page numbers don't get wrapped into
# the next entry's title.
_TOC_LINE_RE = re.compile(
    r"\.\s*(?:\.\s*){2,}\s*(?:\d+|[ivxlcdmIVXLCDM]+)\s*$"
)


def _line_is_toc_entry(line: "Line") -> bool:
    return bool(_TOC_LINE_RE.search(line.text))


def _parse_toc_entry(text: str) -> Optional[Tuple[str, str]]:
    """Split a TOC line ``"Chapter 1 ........ 12"`` into (title, page).
    Returns None if the line doesn't look like a TOC entry."""
    m = re.match(
        r"^\s*(.+?)\s*\.\s*(?:\.\s*){2,}\s*(\d+|[ivxlcdmIVXLCDM]+)\s*$",
        text,
    )
    if not m:
        return None
    title = m.group(1).strip()
    page = m.group(2).strip()
    if not title:
        return None
    return title, page


def _line_italic_score(line: "Line") -> float:
    """Fraction of non-whitespace chars on the line whose span is italic."""
    italic = 0
    total = 0
    for s in line.spans:
        n = sum(1 for c in s.text if not c.isspace())
        total += n
        if s.is_italic:
            italic += n
    return italic / total if total else 0.0


def _line_is_smallcaps_section_head(line: "Line", body_size: float) -> bool:
    """Detect an IEEE-style section heading rendered as small caps at
    body size: ``I. INTRODUCTION``, ``ACKNOWLEDGMENT``, ``REFERENCES``.

    Heuristics:
      - mostly uppercase Latin letters (≥85% of alpha chars)
      - short single line (< 80 chars)
      - approx body size (small-caps spans are tagged at ~80% size for
        the secondary glyphs but the line's dominant size is body)
      - has at least three letters (rules out single-letter labels)
    """
    text = line.text.strip()
    if not text or len(text) > 80:
        return False
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 3:
        return False
    if sum(1 for c in letters if c.isupper()) / len(letters) < 0.85:
        return False
    if line.size > body_size + 1.5 or line.size < body_size - 4.0:
        return False
    return True


def _line_is_italic_subheading(line: "Line", body_size: float) -> bool:
    """Detect an IEEE-style sub-section heading rendered as a short
    italic line at body size: ``A. Full-Sized Camera-Ready (CR) Copy``,
    ``B. References``, ``A. Figures and Tables``.

    Required: ≥80% italic glyphs, short single line, ≈ body size, and a
    letter-period prefix (``A.``, ``B.``, ``C.``…) — the prefix
    constraint keeps run-of-mill italicized words in body prose from
    being promoted to a heading.
    """
    text = line.text.strip()
    if not text or len(text) > 80:
        return False
    if line.size > body_size + 1.5 or line.size < body_size - 1.5:
        return False
    if _line_italic_score(line) < 0.8:
        return False
    return bool(re.match(r"^[A-Z]\.\s+[A-Z]", text))


def _line_is_minor_heading(line: "Line", body_size: float) -> bool:
    return (
        _line_is_smallcaps_section_head(line, body_size)
        or _line_is_italic_subheading(line, body_size)
    )


def _block_all_caps(block: "Block") -> bool:
    text = block.text.strip()
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 3:
        return False
    return sum(1 for c in letters if c.isupper()) / len(letters) >= 0.85


def _block_italic_score(block: "Block") -> float:
    italic = 0
    total = 0
    for ln in block.lines:
        for s in ln.spans:
            n = sum(1 for c in s.text if not c.isspace())
            total += n
            if s.is_italic:
                italic += n
    return italic / total if total else 0.0


def _line_bold_score(line: "Line") -> float:
    """Fraction of non-whitespace chars on the line whose span is bold.
    Used to detect run-in subheadings — short fully-bold lines at body
    size that LaTeX templates use to label a paragraph (``Architecture
    and Learning Setup``) before the regular-weight body sentence
    starts. They share the body size + line height with the paragraph
    below, so size + gap alone fuses them; tracking bold flips lets us
    keep them as a distinct block."""
    bold = 0
    total = 0
    for s in line.spans:
        n = sum(1 for c in s.text if not c.isspace())
        total += n
        if s.is_bold:
            bold += n
    return bold / total if total else 0.0


def _group_lines(spans: List[Span]) -> List[Line]:
    """Group spans into lines by baseline-y proximity."""
    if not spans:
        return []
    # Sort by (y_mid, x0)
    spans = sorted(spans, key=lambda s: (round((s.y0 + s.y1) / 2, 1), s.x0))
    lines: List[List[Span]] = []
    current: List[Span] = []
    cur_y_mid: Optional[float] = None
    for s in spans:
        y_mid = (s.y0 + s.y1) / 2
        tol = max(1.5, 0.5 * s.size)
        if cur_y_mid is None or abs(y_mid - cur_y_mid) <= tol:
            current.append(s)
            cur_y_mid = (
                y_mid if cur_y_mid is None else (cur_y_mid * (len(current) - 1) + y_mid) / len(current)
            )
        else:
            lines.append(current)
            current = [s]
            cur_y_mid = y_mid
    if current:
        lines.append(current)

    out: List[Line] = []
    for grp in lines:
        grp = sorted(grp, key=lambda s: s.x0)
        x0 = min(s.x0 for s in grp); y0 = min(s.y0 for s in grp)
        x1 = max(s.x1 for s in grp); y1 = max(s.y1 for s in grp)
        size = Counter(round(s.size, 1) for s in grp).most_common(1)[0][0]
        out.append(Line(spans=grp, bbox=(x0, y0, x1, y1), size=size))
    return out


def _group_blocks(lines: List[Line], page_index: int, body_size: float = 10.0) -> List[Block]:
    """Group adjacent lines with similar style into blocks."""
    if not lines:
        return []
    lines = sorted(lines, key=lambda ln: (ln.bbox[1], ln.bbox[0]))
    blocks: List[Block] = []
    current: List[Line] = []

    def flush():
        if not current:
            return
        x0 = min(ln.bbox[0] for ln in current); y0 = min(ln.bbox[1] for ln in current)
        x1 = max(ln.bbox[2] for ln in current); y1 = max(ln.bbox[3] for ln in current)
        sizes = Counter()
        bolds = 0; italics = 0; nspans = 0
        for ln in current:
            for s in ln.spans:
                sizes[round(s.size, 1)] += len(s.text)
                if s.is_bold: bolds += len(s.text)
                if s.is_italic: italics += len(s.text)
                nspans += len(s.text)
        size = sizes.most_common(1)[0][0]
        bold = bolds * 2 >= nspans
        italic = italics * 2 >= nspans
        blocks.append(Block(
            lines=list(current),
            bbox=(x0, y0, x1, y1),
            page_index=page_index,
            size=size, bold=bold, italic=italic,
        ))

    for ln in lines:
        if not current:
            current.append(ln); continue
        prev = current[-1]
        prev_size = prev.size
        gap = ln.bbox[1] - prev.bbox[3]
        x_overlap = min(ln.bbox[2], prev.bbox[2]) - max(ln.bbox[0], prev.bbox[0])
        same_size = abs(ln.size - prev_size) <= 0.6
        # Display math is set at the same baseline size as surrounding
        # body text by LaTeX, so size + gap alone would glue an equation
        # line onto its lead-in sentence. Break the block when the line's
        # math-font fraction changes substantially.
        prev_math = _line_math_score(prev) >= 0.35
        cur_math = _line_math_score(ln) >= 0.35
        # A fully-bold line followed by a regular-weight line (or vice
        # versa) is a run-in subheading boundary, not a paragraph
        # continuation — keep them as distinct blocks so the bold tag
        # survives classification.
        prev_bold = _line_bold_score(prev) >= 0.8
        cur_bold = _line_bold_score(ln) >= 0.8
        # Minor headings (IEEE small-caps section heads like
        # "I. INTRODUCTION", italic sub-section heads like
        # "A. Full-Sized Camera-Ready (CR) Copy") render at body size
        # without bold weight — size + gap alone glues them to the body
        # paragraph below. Force a block break on either side of such a
        # line so the heading survives as its own block.
        prev_minor_head = _line_is_minor_heading(prev, body_size)
        cur_minor_head = _line_is_minor_heading(ln, body_size)
        # Numbered reference list items always start a new block.
        cur_list_item = _line_starts_list_item(ln)
        # Table-of-contents entries always start a new block — each entry
        # (title + dot leader + page number) must stay on its own line so
        # the page number doesn't get word-wrapped into the next entry.
        prev_toc = _line_is_toc_entry(prev)
        cur_toc = _line_is_toc_entry(ln)
        # First-line indent: when a line is meaningfully indented
        # compared to the block's column-left edge (the minimum x0 of
        # all lines already in the current block), it's the indented
        # first line of a new paragraph — e.g. IEEE format indents
        # paragraph starts ~9pt past the column edge, and the same
        # indent appears on numbered list items like ``1) US letter
        # margins``. Without this rule, all paragraphs inside a section
        # fuse into one body block and the list items disappear.
        min_x0 = min(l.bbox[0] for l in current)
        cur_para_indent = (
            ln.bbox[0] - min_x0 > 4.0
            and len(current) >= 1
            # Only break for plausible body-text indents, not centered
            # display equations or random horizontally shifted lines.
            and not cur_math
        )
        # Heuristic: tight gap (< 0.9 * size) and same approximate font size and some x-overlap => same block.
        if (
            same_size and gap <= max(1.2 * prev_size, 6.0) and x_overlap > -4.0
            and prev_math == cur_math
            and prev_bold == cur_bold
            and not prev_minor_head
            and not cur_minor_head
            and not cur_list_item
            and not cur_para_indent
            and not prev_toc
            and not cur_toc
        ):
            current.append(ln)
        else:
            flush()
            current = [ln]
    flush()
    return blocks


def _detect_columns_from_spans(spans: List[Span], page_width: float) -> Tuple[int, float]:
    """Detect column count from raw spans BEFORE line grouping.

    Returns ``(ncols, mid_x)``. Two-column layouts share a horizontal
    baseline across columns, so ``_group_lines`` would merge spans from
    both columns into a single full-width "line" — running column
    detection on the resulting blocks then sees only one column. We
    therefore detect from per-span x-distribution: if a substantial body
    population sits on each side of the page mid AND the gap between the
    rightmost left-side edge and the leftmost right-side edge is wide
    enough to be a gutter, declare two columns.
    """
    if not spans:
        return 1, page_width / 2
    sizes = Counter(round(s.size, 1) for s in spans)
    body_size = sizes.most_common(1)[0][0]
    body = [s for s in spans if abs(s.size - body_size) < 0.5]
    if len(body) < 20:
        return 1, page_width / 2
    mid = page_width / 2
    # Spans wholly inside one half-page (don't straddle the mid).
    left = [s for s in body if s.x1 <= mid + 4]
    right = [s for s in body if s.x0 >= mid - 4]
    if len(left) < 8 or len(right) < 8:
        return 1, page_width / 2
    left_right_edge = max(s.x1 for s in left)
    right_left_edge = min(s.x0 for s in right)
    if right_left_edge - left_right_edge < 6:
        return 1, page_width / 2
    # Reject when a substantial number of body spans CROSS the candidate
    # gutter — a true two-column layout has very few such spans (just
    # full-width titles or page-spanning figure captions). Single-column
    # documents with frequent inline font switches (italic titles,
    # inline CJK glosses) produce many narrow fragments on each side
    # AND many wide single spans that span the whole content width;
    # the second population is the tell.
    straddling = sum(1 for s in body if s.x0 < mid - 4 and s.x1 > mid + 4)
    if straddling > 0.25 * len(body):
        return 1, page_width / 2
    # Refine mid to sit in the gutter.
    mid = (left_right_edge + right_left_edge) / 2
    return 2, mid


def _partition_spans_by_column(
    spans: List[Span], mid: float
) -> Tuple[List[Span], List[Span], List[Span]]:
    """Partition spans into (full_width, left, right) groups.

    A span counts as full-width when it straddles the gutter — typical
    of titles, abstracts, page-spanning figure captions, and footers.
    Tolerance is small (4pt) so an italic word that brushes the gutter
    isn't promoted to full-width.
    """
    full: List[Span] = []
    left: List[Span] = []
    right: List[Span] = []
    for s in spans:
        if s.x0 < mid - 4 and s.x1 > mid + 4:
            full.append(s)
        elif s.x1 <= mid + 4:
            left.append(s)
        elif s.x0 >= mid - 4:
            right.append(s)
        else:
            # Tie-break by center.
            if (s.x0 + s.x1) / 2 < mid:
                left.append(s)
            else:
                right.append(s)
    return full, left, right


def _detect_columns(blocks: List[Block], page_width: float) -> int:
    """Return the inferred number of columns. We support 1 or 2 columns."""
    if not blocks:
        return 1
    # Use x-centers of body-sized blocks only to avoid being skewed by labels.
    sizes = Counter(b.size for b in blocks if not b.bold)
    if not sizes:
        return 1
    body_size = sizes.most_common(1)[0][0]
    body = [b for b in blocks if abs(b.size - body_size) < 0.5]
    if len(body) < 4:
        return 1
    centers = sorted((b.bbox[0] + b.bbox[2]) / 2 for b in body)
    # Try splitting at the page mid and see if both halves are populated.
    mid = page_width / 2
    left = [c for c in centers if c < mid]
    right = [c for c in centers if c >= mid]
    if left and right and min(len(left), len(right)) >= max(2, len(body) // 5):
        # Verify that left-column rightmost edge < right-column leftmost edge.
        left_right_edge = max(b.bbox[2] for b in body if (b.bbox[0] + b.bbox[2]) / 2 < mid)
        right_left_edge = min(b.bbox[0] for b in body if (b.bbox[0] + b.bbox[2]) / 2 >= mid)
        if right_left_edge - left_right_edge > 6:
            return 2
    return 1


def _assign_columns(blocks: List[Block], page_width: float, ncols: int) -> None:
    if ncols <= 1:
        for b in blocks:
            b.column = 0
        return
    mid = page_width / 2
    for b in blocks:
        cx = (b.bbox[0] + b.bbox[2]) / 2
        b.column = 0 if cx < mid else 1


def _is_monospace_block(b: Block) -> bool:
    mono_chars = 0
    total = 0
    for ln in b.lines:
        for s in ln.spans:
            n = len(s.text)
            total += n
            if "Courier" in s.font or "Mono" in s.font or "Consolas" in s.font:
                mono_chars += n
    return total > 0 and mono_chars * 2 >= total


def _has_private_use_chars(b: Block) -> bool:
    for ln in b.lines:
        for s in ln.spans:
            for ch in s.text:
                if 0xE000 <= ord(ch) <= 0xF8FF:
                    return True
    return False


def _classify_blocks(blocks: List[Block], body_size: float) -> None:
    """Tag each block kind."""
    for b in blocks:
        t = b.text.strip()
        # All-numeric short tokens or page-number-looking lines: drop (mark as label).
        if len(t) <= 4 and t.replace(".", "").replace(",", "").isdigit():
            b.kind = "label"
            continue
        if _is_monospace_block(b):
            b.kind = "code"
            continue
        if _has_private_use_chars(b):
            b.kind = "equation"
            continue
        # Computer Modern / AMS / Latin Modern math fonts use real Unicode
        # codepoints, not the PUA. Tag a block as equation when math-font
        # content dominates OR when it's a tiny fragment that uses any
        # math font (typical of display-equation subscripts like "i=1").
        math_score = _block_math_score(b)
        if math_score >= 0.35 or (math_score > 0 and len(t) <= 12 and _block_has_math_font(b)):
            b.kind = "equation"
            continue
        if (
            len(b.lines) == 1
            and _line_is_toc_entry(b.lines[0])
        ):
            # Single-line block ending in dot-leader + page number: a
            # table-of-contents entry. Rendered specially in layout (no
            # word-wrap; right-aligned page number).
            b.kind = "toc"
            continue
        if b.size >= body_size + 1.0 and b.bold:
            b.kind = "heading"
        elif b.size >= body_size + 2.5:
            b.kind = "heading"
        elif (
            b.bold
            and abs(b.size - body_size) < 1.0
            and len(b.lines) <= 2
            and 1 < len(t) < 80
            and not t.endswith((".", "?", "!"))
        ):
            # Body-size, fully-bold, short, non-sentence-ending block:
            # this is a run-in subheading (LaTeX ``\paragraph{...}`` or a
            # ``\textbf{...}`` lead-in tag). Promote to heading so the
            # output gets bold weight + a small space above, instead of
            # being typeset as plain prose because no character-count
            # majority of bold survives merge with the body paragraph.
            b.kind = "heading"
        elif (
            abs(b.size - body_size) < 1.5
            and len(b.lines) == 1
            and 2 < len(t) < 80
            and _block_all_caps(b)
        ):
            # IEEE-style small-caps section heading: ``I. INTRODUCTION``,
            # ``ACKNOWLEDGMENT``, ``REFERENCES``. Rendered at body size
            # without bold weight, so size + bold checks both miss it;
            # we use the all-caps signal instead.
            b.kind = "heading"
        elif (
            abs(b.size - body_size) < 1.5
            and len(b.lines) == 1
            and 2 < len(t) < 80
            and _block_italic_score(b) >= 0.8
            and re.match(r"^[A-Z]\.\s+[A-Z]", t)
        ):
            # IEEE-style italic sub-section heading: ``A. Full-Sized
            # Camera-Ready (CR) Copy``, ``B. References``. The
            # letter-period prefix keeps stray italic words in body
            # prose from being promoted.
            b.kind = "heading"
        elif b.size <= body_size - 1.0:
            # Small text. Captions tend to be near a figure (handled later).
            b.kind = "caption"
        else:
            b.kind = "body"


def _is_meaningful_drawing(bbox: BBox) -> bool:
    """Reject drawings that are too thin / too short to be part of an
    actual figure — fraction bars inside an equation, footnote hairlines
    above a "Date:" line, and underline strokes are all narrow horizontal
    rules with zero height. They should never seed a figure band on their
    own; if they belong to a real region, the surrounding equation block
    or wide drawing will seed it."""
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    if w < 0.5 and h < 0.5:
        return False
    # Short horizontal hairline (fraction bar / footnote rule / underline).
    if h < 1.0 and w < 80.0:
        return False
    return True


def _in_page_chrome_band(bbox: BBox, page_height: float) -> bool:
    """A bbox sits entirely inside the top ~7% / bottom ~12% of the page,
    where running headers, page numbers, footnote rules, and tiny logos
    live. Drawings or raster images confined to this band must never seed
    a figure region — otherwise a brand logo (e.g. the ~9pt Kimi badge in
    the Kimi K2.5 tech report header) and its glyph outlines produce one
    bogus "figure" strip per source page."""
    return bbox[3] < page_height * 0.07 or bbox[1] > page_height * 0.88


def _is_figure_image(bbox: BBox, page_width: float, page_height: float) -> bool:
    """A raster image is treated as a figure seed when it's bigger than
    the typical inline-logo / bullet-icon size. Threshold is generous
    (≥40pt on each side AND ≥1500pt² area) so we still drop the 9-10pt
    page-header logos that academic templates inject, while picking up
    things like 50pt-tall result-card mosaics."""
    if _in_page_chrome_band(bbox, page_height):
        return False
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    if w < 40.0 or h < 40.0:
        return False
    if w * h < 1500.0:
        return False
    # An image that fills nearly the whole page width AND nearly the whole
    # page height is almost certainly a scanned/whole-page background, not
    # a figure embedded in body prose — but we still want it as a figure,
    # so don't filter on size alone. The page-chrome check above already
    # excludes header/footer placement.
    _ = page_width
    return True


def _is_page_chrome(b: Block, page_height: float, body_size: float) -> bool:
    """Running header / running footer / page-number / footnote-date line.

    These live in the page margin (top ~15% or bottom ~12%) at sub-body
    text size. They must be filtered out before figure-band absorption so
    a stray footnote rule near a display equation doesn't bridge the
    whole footer into a "figure."""
    if b.kind == "heading":
        return False
    if b.size > body_size - 0.5:
        return False
    if b.bbox[3] < page_height * 0.15:
        return True
    if b.bbox[1] > page_height * 0.88:
        return True
    return False


def _body_block_in_gap(blocks: List[Block], y_lo: float, y_hi: float) -> bool:
    """Is there a wide body block sitting in the vertical gap (y_lo, y_hi)?
    If yes, two figure bands on either side must NOT be merged into one —
    a body paragraph is a hard reading-order separator."""
    if y_hi - y_lo <= 0:
        return False
    for b in blocks:
        if b.kind != "body":
            continue
        # Body block must sit predominantly INSIDE the gap.
        by0, by1 = b.bbox[1], b.bbox[3]
        if by0 < y_lo - 1 or by1 > y_hi + 1:
            continue
        if (b.bbox[2] - b.bbox[0]) < 80.0:
            continue  # too narrow to be a real paragraph
        return True
    return False


def _classify_alignment(
    blocks: List[Block],
    col_extent: Tuple[float, float],
) -> None:
    """Mark each block as centered or left-aligned within its column.

    Detection rules (tight on purpose so left-aligned body paragraphs
    don't get false-positive flagged centered, since their final line is
    often short and could look symmetric):

    - Multi-line blocks: all lines (allow one short tail) must have a
      meaningful left indent (>5% column width) AND be symmetric about
      the column center. Line x0 must NOT be near-constant (which would
      indicate left alignment with ragged right).
    - Single-line blocks: must have both indents above 8% of column
      width AND be narrower than 80% of the column.
    """
    col_left, col_right = col_extent
    col_width = col_right - col_left
    if col_width <= 0:
        return
    tol = max(6.0, 0.06 * col_width)
    min_indent = max(6.0, 0.05 * col_width)

    def line_centered(ln: Line) -> bool:
        lw = ln.bbox[2] - ln.bbox[0]
        if lw <= 0 or lw > col_width * 0.9:
            return False
        left_gap = ln.bbox[0] - col_left
        right_gap = col_right - ln.bbox[2]
        return abs(left_gap - right_gap) <= tol and left_gap >= min_indent

    for b in blocks:
        if not b.lines:
            continue
        if len(b.lines) == 1:
            ln = b.lines[0]
            lw = ln.bbox[2] - ln.bbox[0]
            left_gap = ln.bbox[0] - col_left
            right_gap = col_right - ln.bbox[2]
            if (
                lw < col_width * 0.8
                and left_gap >= max(10.0, 0.08 * col_width)
                and right_gap >= max(10.0, 0.08 * col_width)
                and abs(left_gap - right_gap) <= tol
            ):
                b.align = "center"
            continue
        # Multi-line: distinguish centered vs left-aligned-with-ragged-tail.
        # Require all but at most one line to be individually centered.
        # An additional left-aligned signal — near-constant x0 with varying
        # x1 — would rule out marking centered.
        x0s = [ln.bbox[0] for ln in b.lines]
        x1s = [ln.bbox[2] for ln in b.lines]
        x0_range = max(x0s) - min(x0s)
        x1_range = max(x1s) - min(x1s)
        if x0_range < tol and x1_range >= tol:
            continue  # classic left-aligned with ragged right
        centered = sum(1 for ln in b.lines if line_centered(ln))
        if centered >= len(b.lines) - 1 and centered >= 2:
            b.align = "center"


def _figure_regions_in_extent(
    page: PageContent,
    blocks: List[Block],
    body_size: float,
    x_extent: Tuple[float, float],
) -> List[Tuple[float, float]]:
    """Like ``_figure_regions`` but limited to drawings/images/equations
    whose horizontal center falls inside ``x_extent``. Used per-column on
    two-column pages so a figure in the right column doesn't seed a
    page-wide y-band that swallows the corresponding left-column body
    text."""
    x_lo, x_hi = x_extent

    def in_extent(bbox: BBox) -> bool:
        cx = (bbox[0] + bbox[2]) / 2
        return x_lo - 2 <= cx <= x_hi + 2

    dboxes: List[BBox] = []
    for d in page.drawings:
        if not _is_meaningful_drawing(d.bbox):
            continue
        if _in_page_chrome_band(d.bbox, page.height):
            continue
        if not in_extent(d.bbox):
            continue
        dboxes.append(d.bbox)
    for im in page.images:
        if _is_figure_image(im.bbox, page.width, page.height) and in_extent(im.bbox):
            dboxes.append(im.bbox)
    for b in blocks:
        if b.kind == "equation" and in_extent(b.bbox):
            dboxes.append(b.bbox)
    if not dboxes:
        return []

    dboxes_sorted = sorted(dboxes, key=lambda b: b[1])
    bands: List[List[float]] = []
    merge_gap = 2.0 * body_size
    for (x0, y0, x1, y1) in dboxes_sorted:
        if (
            bands
            and y0 - bands[-1][1] < merge_gap
            and not _body_block_in_gap(blocks, bands[-1][1], y0)
        ):
            bands[-1][1] = max(bands[-1][1], y1)
            bands[-1][0] = min(bands[-1][0], y0)
        else:
            bands.append([y0, y1])

    for b in blocks:
        if _is_page_chrome(b, page.height, body_size):
            continue
        if not in_extent(b.bbox):
            continue
        bw = b.bbox[2] - b.bbox[0]
        is_narrow_fragment = bw < 40.0 and len(b.text.strip()) <= 6
        if b.kind in ("body", "heading", "toc") and not (b.kind == "body" and is_narrow_fragment):
            continue
        by0, by1 = b.bbox[1], b.bbox[3]
        for band in bands:
            if (by0 < band[1] + body_size * 1.0 and by1 > band[0] - body_size * 1.5):
                band[0] = min(band[0], by0)
                band[1] = max(band[1], by1)
                break

    bands.sort(key=lambda b: b[0])
    merged: List[List[float]] = []
    for band in bands:
        if (
            merged
            and band[0] - merged[-1][1] < merge_gap
            and not _body_block_in_gap(blocks, merged[-1][1], band[0])
        ):
            merged[-1][1] = max(merged[-1][1], band[1])
        else:
            merged.append([band[0], band[1]])

    return [(b[0], b[1]) for b in merged if (b[1] - b[0]) > body_size * 1.2]


def _figure_regions(page: PageContent, blocks: List[Block], body_size: float) -> List[Tuple[float, float]]:
    """Return (y0, y1) bands of the source page that are figure regions.

    Heuristic: rows of drawings, plus any caption/label/short text whose
    vertical extent overlaps the drawing band, define a single figure
    region. Adjacent drawing bands are merged when close vertically.
    """
    # Seed bands from:
    #   - vector drawings (ignoring hairline rules and chrome-band paths)
    #   - embedded raster images (PNG/JPEG XObjects) that look like figures
    #     rather than inline logos
    #   - equation blocks (these become rasterized figures too, and any
    #     neighboring single-letter math fragments fall into the same band)
    # Chrome-confined paths/images are dropped here so a 9pt logo plus its
    # glyph outlines in the running header doesn't promote the header
    # strip into a figure region on every page.
    dboxes: List[BBox] = []
    for d in page.drawings:
        if not _is_meaningful_drawing(d.bbox):
            continue
        if _in_page_chrome_band(d.bbox, page.height):
            continue
        dboxes.append(d.bbox)
    for im in page.images:
        if _is_figure_image(im.bbox, page.width, page.height):
            dboxes.append(im.bbox)
    for b in blocks:
        if b.kind == "equation":
            dboxes.append(b.bbox)
    if not dboxes:
        return []

    # Build initial bands: sort by y0 and merge when vertical gap is small.
    dboxes_sorted = sorted(dboxes, key=lambda b: b[1])
    bands: List[List[float]] = []  # mutable [y0, y1]
    merge_gap = 2.0 * body_size
    for (x0, y0, x1, y1) in dboxes_sorted:
        if (
            bands
            and y0 - bands[-1][1] < merge_gap
            and not _body_block_in_gap(blocks, bands[-1][1], y0)
        ):
            bands[-1][1] = max(bands[-1][1], y1)
            bands[-1][0] = min(bands[-1][0], y0)
        else:
            bands.append([y0, y1])

    # Pull in any small (label/caption) blocks that fall inside or just below a band.
    # Also absorb narrow, short body fragments (single-letter math symbols floating
    # near a figure) — these are virtually always part of the figure.
    for b in blocks:
        if _is_page_chrome(b, page.height, body_size):
            continue
        bw = b.bbox[2] - b.bbox[0]
        is_narrow_fragment = bw < 40.0 and len(b.text.strip()) <= 6
        if b.kind in ("body", "heading", "toc") and not (b.kind == "body" and is_narrow_fragment):
            continue
        by0, by1 = b.bbox[1], b.bbox[3]
        for band in bands:
            # Downward extension is tighter than upward: we don't want a
            # band of display equations to swallow the next body paragraph
            # below it (e.g. the "Date: ..." footnote line on the LaTeX
            # sample sits 13pt below the equations).
            if (by0 < band[1] + body_size * 1.0 and by1 > band[0] - body_size * 1.5):
                band[0] = min(band[0], by0)
                band[1] = max(band[1], by1)
                break

    # Re-merge after expansion.
    bands.sort(key=lambda b: b[0])
    merged: List[List[float]] = []
    for band in bands:
        if (
            merged
            and band[0] - merged[-1][1] < merge_gap
            and not _body_block_in_gap(blocks, merged[-1][1], band[0])
        ):
            merged[-1][1] = max(merged[-1][1], band[1])
        else:
            merged.append([band[0], band[1]])

    # Filter trivially small bands (likely a stray rule), require height > body_size.
    return [(b[0], b[1]) for b in merged if (b[1] - b[0]) > body_size * 1.2]


def _lines_to_blocks(spans: List[Span], page_index: int, body_size: float) -> List[Block]:
    """Group spans → lines → blocks, applying the run-in subheading split."""
    lines = _group_lines(spans)
    split_lines: List[Line] = []
    for ln in lines:
        split_lines.extend(_split_runin_subheading_line(ln))
    return _group_blocks(split_lines, page_index, body_size)


def _analyze_two_column(
    page: PageContent, body_size: float, mid: float
) -> List[FlowItem]:
    """Two-column page: partition spans by column, build blocks
    independently per column, and emit reading order as
    [full-width header → left column → right column → full-width footer].
    """
    full_spans, left_spans, right_spans = _partition_spans_by_column(page.spans, mid)
    left_blocks = _lines_to_blocks(left_spans, page.index, body_size)
    right_blocks = _lines_to_blocks(right_spans, page.index, body_size)
    full_blocks = _lines_to_blocks(full_spans, page.index, body_size)
    for b in left_blocks:
        b.column = 0
    for b in right_blocks:
        b.column = 1
    for b in full_blocks:
        b.column = -1

    all_blocks = full_blocks + left_blocks + right_blocks
    _classify_blocks(all_blocks, body_size)

    # Column extents for alignment + figure region scoping.
    page_margin = 16.0
    left_ext = (page_margin, mid - 2)
    right_ext = (mid + 2, page.width - page_margin)
    full_ext = (page_margin, page.width - page_margin)
    _classify_alignment(left_blocks, left_ext)
    _classify_alignment(right_blocks, right_ext)
    _classify_alignment(full_blocks, full_ext)

    # Figure regions PER COLUMN so a right-column figure doesn't suppress
    # left-column body text in the same y-band.
    left_bands = _figure_regions_in_extent(page, left_blocks, body_size, left_ext)
    right_bands = _figure_regions_in_extent(page, right_blocks, body_size, right_ext)
    # Full-width figures (those that straddle the gutter — drawings whose
    # center sits within ±10pt of the gutter and whose width > half page)
    full_bands: List[Tuple[float, float]] = []
    page_wide_dboxes: List[BBox] = []
    for d in page.drawings:
        if not _is_meaningful_drawing(d.bbox):
            continue
        if _in_page_chrome_band(d.bbox, page.height):
            continue
        if d.bbox[0] < mid - 6 and d.bbox[2] > mid + 6 and (d.bbox[2] - d.bbox[0]) > page.width * 0.45:
            page_wide_dboxes.append(d.bbox)
    for im in page.images:
        if not _is_figure_image(im.bbox, page.width, page.height):
            continue
        if im.bbox[0] < mid - 6 and im.bbox[2] > mid + 6 and (im.bbox[2] - im.bbox[0]) > page.width * 0.45:
            page_wide_dboxes.append(im.bbox)
    if page_wide_dboxes:
        page_wide_dboxes.sort(key=lambda b: b[1])
        for (x0, y0, x1, y1) in page_wide_dboxes:
            if full_bands and y0 - full_bands[-1][1] < 2.0 * body_size:
                full_bands[-1] = (full_bands[-1][0], max(full_bands[-1][1], y1))
            else:
                full_bands.append((y0, y1))

    # Determine the two-column "band" — the y-range where both columns
    # have substantial body content. Full-width blocks above this band
    # come first; full-width blocks below come last; everything else is
    # interleaved column-major.
    left_body_ys = [b.bbox[1] for b in left_blocks if b.kind in ("body", "heading", "caption")]
    right_body_ys = [b.bbox[1] for b in right_blocks if b.kind in ("body", "heading", "caption")]
    if left_body_ys and right_body_ys:
        band_start = min(min(left_body_ys), min(right_body_ys))
        band_end = max(
            max(b.bbox[3] for b in left_blocks),
            max(b.bbox[3] for b in right_blocks),
        )
    else:
        band_start = float("inf")
        band_end = float("-inf")

    items_with_key: List[Tuple[int, float, int, FlowItem]] = []

    def emit_block_items(blocks: List[Block], fig_bands: List[Tuple[float, float]],
                         x_ext: Tuple[float, float], sort_col: int) -> None:
        x_lo, x_hi = x_ext
        inside_band: List[bool] = []
        for b in blocks:
            cy = (b.bbox[1] + b.bbox[3]) / 2
            in_fig = False
            for (y0, y1) in fig_bands:
                if y0 <= cy <= y1:
                    in_fig = True
                    break
            inside_band.append(in_fig)
        for b, inside in zip(blocks, inside_band):
            if inside or b.kind == "label":
                continue
            if _is_page_chrome(b, page.height, body_size):
                continue
            if b.kind == "equation":
                if (b.bbox[3] - b.bbox[1]) < body_size or b.size < body_size - 1.0:
                    continue
                src_rect = (max(x_lo, b.bbox[0] - 4), b.bbox[1] - 2,
                            min(x_hi, b.bbox[2] + 4), b.bbox[3] + 2)
                items_with_key.append((sort_col, b.bbox[1], 0, FlowItem(
                    kind="figure", page_index=page.index,
                    bbox=b.bbox, source_rect=src_rect)))
                continue
            if b.kind == "code":
                code_lines = [ln.text.rstrip() for ln in b.lines]
                items_with_key.append((sort_col, b.bbox[1], 0, FlowItem(
                    kind="code", page_index=page.index, bbox=b.bbox,
                    text="\n".join(code_lines), size=b.size, bold=b.bold,
                    italic=b.italic, monospace=True, code_lines=code_lines,
                    align=b.align)))
                continue
            items_with_key.append((sort_col, b.bbox[1], 0, FlowItem(
                kind=b.kind, page_index=page.index, bbox=b.bbox,
                text=b.text, size=b.size, bold=b.bold, italic=b.italic,
                align=b.align)))
        # Figures cropped to the column.
        for (y0, y1) in fig_bands:
            x_extents: List[Tuple[float, float]] = []
            for d in page.drawings:
                dx0, dy0, dx1, dy1 = d.bbox
                if dy1 < y0 or dy0 > y1:
                    continue
                if _in_page_chrome_band(d.bbox, page.height):
                    continue
                cx = (dx0 + dx1) / 2
                if not (x_lo - 2 <= cx <= x_hi + 2):
                    continue
                x_extents.append((max(dx0, x_lo), min(dx1, x_hi)))
            for im in page.images:
                if not _is_figure_image(im.bbox, page.width, page.height):
                    continue
                ix0, iy0, ix1, iy1 = im.bbox
                if iy1 < y0 or iy0 > y1:
                    continue
                cx = (ix0 + ix1) / 2
                if not (x_lo - 2 <= cx <= x_hi + 2):
                    continue
                x_extents.append((max(ix0, x_lo), min(ix1, x_hi)))
            for b in blocks:
                bx0, by0, bx1, by1 = b.bbox
                if by1 < y0 or by0 > y1:
                    continue
                cx = (bx0 + bx1) / 2
                if not (x_lo - 2 <= cx <= x_hi + 2):
                    continue
                x_extents.append((bx0, bx1))
            if x_extents:
                fx0 = max(x_lo, min(e[0] for e in x_extents) - 3.0)
                fx1 = min(x_hi, max(e[1] for e in x_extents) + 3.0)
            else:
                fx0, fx1 = x_lo, x_hi
            src_rect = (fx0, y0 - 2, fx1, y1 + 2)
            items_with_key.append((sort_col, y0, 0, FlowItem(
                kind="figure", page_index=page.index,
                bbox=(fx0, y0, fx1, y1), source_rect=src_rect)))

    # Full-width header items: any full-width block whose center y is
    # above the two-column band start.
    for b in full_blocks:
        if b.kind == "label":
            continue
        if _is_page_chrome(b, page.height, body_size):
            continue
        cy = (b.bbox[1] + b.bbox[3]) / 2
        # Skip if it sits inside a full-width figure band (rasterized below).
        in_full_fig = any(y0 <= cy <= y1 for (y0, y1) in full_bands)
        if in_full_fig:
            continue
        sort_col = 0 if cy < band_start else 3
        items_with_key.append((sort_col, b.bbox[1], 0, FlowItem(
            kind=b.kind, page_index=page.index, bbox=b.bbox,
            text=b.text, size=b.size, bold=b.bold, italic=b.italic,
            align=b.align)))

    # Full-width figure bands.
    for (y0, y1) in full_bands:
        sort_col = 0 if (y0 + y1) / 2 < band_start else 3
        src_rect = (page_margin, y0 - 2, page.width - page_margin, y1 + 2)
        items_with_key.append((sort_col, y0, 0, FlowItem(
            kind="figure", page_index=page.index,
            bbox=(page_margin, y0, page.width - page_margin, y1),
            source_rect=src_rect)))

    emit_block_items(left_blocks, left_bands, left_ext, sort_col=1)
    emit_block_items(right_blocks, right_bands, right_ext, sort_col=2)

    items_with_key.sort(key=lambda t: (t[0], t[1], t[2]))
    return [it for *_, it in items_with_key]


def analyze_page(page: PageContent, body_size: float) -> List[FlowItem]:
    ncols, mid = _detect_columns_from_spans(page.spans, page.width)
    if ncols >= 2:
        return _analyze_two_column(page, body_size, mid)

    lines = _group_lines(page.spans)
    # Split LaTeX-style run-in subheadings ("Architecture and Learning Setup
    # The PARL framework adopts a decoupled ...") into a bold-only line
    # plus a body-only line so the bold-aware block grouping below keeps
    # them distinct.
    split_lines: List[Line] = []
    for ln in lines:
        split_lines.extend(_split_runin_subheading_line(ln))
    blocks = _group_blocks(split_lines, page.index, body_size)

    ncols = _detect_columns(blocks, page.width)
    _assign_columns(blocks, page.width, ncols)
    _classify_blocks(blocks, body_size)
    page_margin = 16.0
    _classify_alignment(blocks, (page_margin, page.width - page_margin))

    fig_bands = _figure_regions(page, blocks, body_size)

    # Mark blocks that fall inside a figure band as 'inside_figure', except
    # body paragraphs that clearly extend across the band (a body paragraph
    # near a figure is normally NOT inside the figure).
    inside_band: List[bool] = []
    for b in blocks:
        cy = (b.bbox[1] + b.bbox[3]) / 2
        in_fig = False
        for (y0, y1) in fig_bands:
            if y0 <= cy <= y1:
                # Body paragraph that's wider than typical labels stays as body.
                width = b.bbox[2] - b.bbox[0]
                if b.kind == "body" and width > page.width * 0.55 and len(b.text) > 80:
                    in_fig = False
                elif b.kind == "toc":
                    in_fig = False
                else:
                    in_fig = True
                break
        inside_band.append(in_fig)

    # Compute reading order: column-major top→bottom; figures get a
    # synthetic position equal to their band's top y.
    items_with_y: List[Tuple[int, float, FlowItem]] = []

    # Add text blocks not inside figures.
    page_margin = 16.0
    toc_min_x0 = min(
        (b.bbox[0] for b in blocks if b.kind == "toc"),
        default=0.0,
    )
    for b, inside in zip(blocks, inside_band):
        if inside:
            continue
        if b.kind == "label":
            continue
        # Drop running headers/footers: small text living in the top 10% /
        # bottom 12% of the page (page numbers, "Date:", repeating header
        # like "2 X. BURPS, P. GURPS" in academic papers).
        if _is_page_chrome(b, page.height, body_size):
            continue
        # Equations: rasterize the area instead of trying to typeset.
        if b.kind == "equation":
            # Sub-baseline fragments (just the limits of an inline ∫ or ∑,
            # like "i=1" sitting under a body-text math expression) make
            # terrible standalone images: the source crop is a few-px-tall
            # sliver that gets blown up to fill the column. We can't
            # render them well in isolation and the parent expression is
            # in the body line above, so drop them.
            if (b.bbox[3] - b.bbox[1]) < body_size or b.size < body_size - 1.0:
                continue
            src_rect = (max(page_margin, b.bbox[0] - 4), b.bbox[1] - 2,
                        min(page.width - page_margin, b.bbox[2] + 4), b.bbox[3] + 2)
            item = FlowItem(
                kind="figure",
                page_index=page.index,
                bbox=b.bbox,
                source_rect=src_rect,
            )
            items_with_y.append((b.column, b.bbox[1], item))
            continue
        # Code: keep one string per source line.
        if b.kind == "code":
            code_lines = [ln.text.rstrip() for ln in b.lines]
            item = FlowItem(
                kind="code",
                page_index=page.index,
                bbox=b.bbox,
                text="\n".join(code_lines),
                size=b.size,
                bold=b.bold,
                italic=b.italic,
                monospace=True,
                code_lines=code_lines,
                align=b.align,
            )
            items_with_y.append((b.column, b.bbox[1], item))
            continue
        indent = max(0.0, b.bbox[0] - toc_min_x0) if b.kind == "toc" else 0.0
        item = FlowItem(
            kind=b.kind,
            page_index=page.index,
            bbox=b.bbox,
            text=b.text,
            size=b.size,
            bold=b.bold,
            italic=b.italic,
            align=b.align,
            indent=indent,
        )
        items_with_y.append((b.column, b.bbox[1], item))

    # Add figures, cropped horizontally to the actual content bbox in the band.
    for (y0, y1) in fig_bands:
        x_extents: List[Tuple[float, float]] = []
        for d in page.drawings:
            dx0, dy0, dx1, dy1 = d.bbox
            if dy1 < y0 or dy0 > y1:
                continue
            if _in_page_chrome_band(d.bbox, page.height):
                continue
            x_extents.append((dx0, dx1))
        for im in page.images:
            if not _is_figure_image(im.bbox, page.width, page.height):
                continue
            ix0, iy0, ix1, iy1 = im.bbox
            if iy1 < y0 or iy0 > y1:
                continue
            x_extents.append((ix0, ix1))
        for b in blocks:
            bx0, by0, bx1, by1 = b.bbox
            if by1 < y0 or by0 > y1:
                continue
            # Body paragraphs that aren't absorbed into the figure are skipped.
            inside = (b.kind != "body") or (
                (bx1 - bx0) < page.width * 0.55 and len(b.text) <= 80
            )
            if inside:
                x_extents.append((bx0, bx1))
        if x_extents:
            fx0 = min(e[0] for e in x_extents) - 3.0
            fx1 = max(e[1] for e in x_extents) + 3.0
        else:
            fx0, fx1 = page_margin, page.width - page_margin
        fx0 = max(0.0, fx0)
        fx1 = min(page.width, fx1)
        src_rect = (fx0, y0 - 2, fx1, y1 + 2)
        item = FlowItem(
            kind="figure",
            page_index=page.index,
            bbox=(fx0, y0, fx1, y1),
            source_rect=src_rect,
        )
        # Figures always occupy column 0 in reading order at their top y.
        items_with_y.append((0, y0, item))

    items_with_y.sort(key=lambda t: (t[0], t[1]))
    return [it for _, _, it in items_with_y]


def body_font_size(pages: List[PageContent]) -> float:
    """Determine the most common font size across all spans (the body size)."""
    counter: Counter = Counter()
    for p in pages:
        for s in p.spans:
            counter[round(s.size, 1)] += max(1, len(s.text))
    if not counter:
        return 10.0
    return counter.most_common(1)[0][0]


def analyze_document(pages: List[PageContent]) -> Tuple[List[FlowItem], float]:
    body_size = body_font_size(pages)
    all_items: List[FlowItem] = []
    for p in pages:
        all_items.extend(analyze_page(p, body_size))
    return all_items, body_size
