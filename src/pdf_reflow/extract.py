"""Extract structured content from each PDF page.

Produces a list of `Span` and `Drawing` items per page, plus the page rect.
We rely on PyMuPDF only to parse the PDF bytestream — the structural
interpretation (lines, paragraphs, columns, figures) is all done in this
project.
"""

from __future__ import annotations

import concurrent.futures
import os
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import fitz


BBox = Tuple[float, float, float, float]


# Typography normalization: many PDFs (especially LaTeX output) use Unicode
# ligature codepoints and Computer Modern's "minus" / soft hyphen / smart
# quote characters that have no glyph in the base14 PDF fonts we render
# with. Map them down to plain ASCII at the extraction boundary so every
# downstream stage (line text, wrap width measurement, output rendering)
# sees the same canonical text.
_TEXT_NORMALIZE = str.maketrans({
    "ﬀ": "ff",   # ﬀ
    "ﬁ": "fi",   # ﬁ
    "ﬂ": "fl",   # ﬂ
    "ﬃ": "ffi",  # ﬃ
    "ﬄ": "ffl",  # ﬄ
    "ﬅ": "st",   # ﬅ
    "ﬆ": "st",   # ﬆ
    "‘": "'",    # left single quote
    "’": "'",    # right single quote / apostrophe
    "“": '"',    # left double quote
    "”": '"',    # right double quote
    "–": "-",    # en dash
    "—": "--",   # em dash
    "…": "...",  # horizontal ellipsis
    "­": "",     # soft hyphen (rendering hint, not real content)
    "−": "-",    # math minus (CMSY -) — fall back to ASCII hyphen
})


def normalize_text(text: str) -> str:
    """Replace ligature / smart-quote / soft-hyphen codepoints with the
    plain-ASCII equivalents that base14 PDF fonts can actually render."""
    return text.translate(_TEXT_NORMALIZE)


@dataclass
class Span:
    text: str
    bbox: BBox
    font: str
    size: float
    flags: int          # PyMuPDF span flags (bit 4 = bold, bit 1 = italic, bit 2 = serif)
    color: int

    @property
    def x0(self) -> float: return self.bbox[0]
    @property
    def y0(self) -> float: return self.bbox[1]
    @property
    def x1(self) -> float: return self.bbox[2]
    @property
    def y1(self) -> float: return self.bbox[3]
    @property
    def width(self) -> float: return self.x1 - self.x0
    @property
    def height(self) -> float: return self.y1 - self.y0
    @property
    def is_bold(self) -> bool:
        return bool(self.flags & 16) or "Bold" in self.font or "bold" in self.font
    @property
    def is_italic(self) -> bool:
        return bool(self.flags & 2) or "Italic" in self.font or "Oblique" in self.font


@dataclass
class Drawing:
    """A vector drawing primitive (line, rect, curve) reduced to its bbox."""
    bbox: BBox
    kind: str  # 'l','re','c', etc. from MuPDF's drawing dicts


@dataclass
class Image:
    bbox: BBox
    xref: int


@dataclass
class PageContent:
    index: int
    rect: BBox            # (0, 0, w, h)
    spans: List[Span] = field(default_factory=list)
    drawings: List[Drawing] = field(default_factory=list)
    images: List[Image] = field(default_factory=list)

    @property
    def width(self) -> float: return self.rect[2] - self.rect[0]
    @property
    def height(self) -> float: return self.rect[3] - self.rect[1]


def extract_page(page: fitz.Page, index: int) -> PageContent:
    rect = tuple(page.rect)  # type: ignore[arg-type]
    pc = PageContent(index=index, rect=rect)

    raw = page.get_text("rawdict", flags=fitz.TEXTFLAGS_RAWDICT)
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                # rawdict gives chars individually; join into a span text.
                chars = span.get("chars") or []
                if chars:
                    text = "".join(c.get("c", "") for c in chars)
                    # Use the char bbox union for accurate span bbox.
                    xs = [c["bbox"][0] for c in chars] + [c["bbox"][2] for c in chars]
                    ys = [c["bbox"][1] for c in chars] + [c["bbox"][3] for c in chars]
                    bbox = (min(xs), min(ys), max(xs), max(ys))
                else:
                    text = span.get("text", "")
                    bbox = tuple(span.get("bbox", (0, 0, 0, 0)))  # type: ignore[arg-type]
                text = normalize_text(text)
                if not text.strip():
                    continue
                pc.spans.append(
                    Span(
                        text=text,
                        bbox=bbox,
                        font=span.get("font", ""),
                        size=float(span.get("size", 0.0)),
                        flags=int(span.get("flags", 0)),
                        color=int(span.get("color", 0)),
                    )
                )

    for d in page.get_drawings():
        bbox = d.get("rect")
        if bbox is None:
            continue
        pc.drawings.append(Drawing(bbox=tuple(bbox), kind=d.get("type", "")))

    for img in page.get_image_info(hashes=False, xrefs=True):
        bbox = img.get("bbox")
        if bbox is None:
            continue
        pc.images.append(Image(bbox=tuple(bbox), xref=int(img.get("xref", 0))))

    return pc


def extract_document(doc: fitz.Document) -> List[PageContent]:
    return [extract_page(doc[i], i) for i in range(doc.page_count)]


# ---------------------------------------------------------------------------
# Parallel extraction.
#
# PyMuPDF is not thread-safe at the Document level: a single Document
# instance must not be touched concurrently from multiple threads, and the
# underlying SWIG calls into MuPDF generally hold the GIL anyway. We
# therefore parallelise across PROCESSES, with each worker opening its own
# fitz.Document from the same file path. This gives ~linear speed-up on
# extraction-bound documents (long technical reports where get_drawings /
# get_text("rawdict") dominate the runtime).
# ---------------------------------------------------------------------------


def _extract_page_worker(args: Tuple[str, int]) -> "PageContent":
    """Worker entry point for ProcessPoolExecutor.

    Must be importable at module top-level so multiprocessing's pickle
    boundary can find it. Each worker opens its own Document and closes
    it before returning — no shared state survives across pages.
    """
    src_path, idx = args
    doc = fitz.open(src_path)
    try:
        return extract_page(doc[idx], idx)
    finally:
        doc.close()


def extract_document_parallel(
    src_path: str,
    workers: Optional[int] = None,
    min_pages: int = 4,
) -> List[PageContent]:
    """Extract all pages of ``src_path`` in parallel.

    ``workers=None`` picks ``os.cpu_count()`` (capped at 8). For documents
    smaller than ``min_pages``, falls back to sequential extraction
    because process-pool startup (≈50–150 ms on Linux) dwarfs the saving.
    """
    if workers is None:
        workers = min(os.cpu_count() or 1, 8)

    doc = fitz.open(src_path)
    n = doc.page_count
    if workers <= 1 or n < min_pages:
        try:
            return [extract_page(doc[i], i) for i in range(n)]
        finally:
            doc.close()
    doc.close()

    workers = min(workers, n)
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(_extract_page_worker, [(src_path, i) for i in range(n)]))
