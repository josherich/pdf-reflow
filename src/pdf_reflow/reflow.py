"""High-level entry: reflow a PDF file into a mobile-sized PDF."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

import fitz

from .extract import extract_document, extract_document_parallel
from .analyze import analyze_document
from .layout import HeadingAnchor, LayoutConfig, layout
from .render import render


@dataclass
class ReflowConfig:
    page_width: float = 360.0       # iPhone-friendly target width in PDF points
    page_height: float = 600.0
    body_size: float = 11.0
    figure_dpi: float = 150.0       # 150 DPI is sharp on mobile; use 220+ for print quality
    # Parallelism: 1 = sequential, >1 = use N worker processes for page
    # extraction and figure rasterization. ``None`` picks os.cpu_count()
    # (capped at 8). Small docs always run sequentially to avoid pool
    # startup overhead.
    workers: Optional[int] = 1

    def to_layout(self) -> LayoutConfig:
        return LayoutConfig(
            page_width=self.page_width,
            page_height=self.page_height,
            body_size=self.body_size,
            figure_dpi=self.figure_dpi,
        )


def _sanitize_toc(toc: list) -> list:
    """Ensure PyMuPDF's set_toc hierarchy constraints are met.

    Rules: first entry must be level 1; no entry may jump more than one
    level deeper than its predecessor. Levels are clamped in a single pass.
    """
    out = []
    prev_level = 0
    for entry in toc:
        level = entry[0]
        if not out:
            level = 1
        else:
            level = min(level, prev_level + 1)
            level = max(level, 1)
        out.append([level] + list(entry[1:]))
        prev_level = level
    return out


def _norm_title(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _apply_toc(
    src_doc: fitz.Document,
    out_doc: fitz.Document,
    anchors: List[HeadingAnchor],
) -> None:
    """Set a PDF outline on *out_doc* that mirrors the source TOC.

    If the source has no formal outline, one is synthesised from the
    headings detected during layout so PDF readers still show bookmarks.
    """
    src_toc = src_doc.get_toc(simple=False)  # [[level, title, page, dest], ...]

    def best_anchor(title: str) -> Optional[HeadingAnchor]:
        norm = _norm_title(title)
        best: Optional[HeadingAnchor] = None
        best_score = 0
        for a in anchors:
            a_norm = _norm_title(a.text)
            if norm == a_norm or norm in a_norm or a_norm in norm:
                score = min(len(norm), len(a_norm))
                if score > best_score:
                    best, best_score = a, score
        return best

    toc_out: list = []
    if src_toc:
        for entry in src_toc:
            level, title = entry[0], entry[1]
            a = best_anchor(title)
            if a:
                # PyMuPDF set_toc format: [level, title, page_1based, y_float]
                toc_out.append([level, title, a.out_page + 1, a.y])
    if not toc_out:
        # Synthesise from detected headings when source has no formal outline.
        for a in anchors:
            toc_out.append([a.level, a.text, a.out_page + 1, a.y])
    if toc_out:
        out_doc.set_toc(_sanitize_toc(toc_out))


def reflow_pdf(src_path: str, dst_path: str, cfg: Optional[ReflowConfig] = None) -> dict:
    """Reflow ``src_path`` and write a mobile PDF to ``dst_path``.

    Returns a dict of stats useful for testing and CLI display.
    """
    cfg = cfg or ReflowConfig()
    layout_cfg = cfg.to_layout()
    workers = cfg.workers if cfg.workers is not None else 1

    doc = fitz.open(src_path)
    try:
        if workers and workers > 1:
            pages = extract_document_parallel(src_path, workers=workers)
        else:
            pages = extract_document(doc)
        items, body_size = analyze_document(pages)
        # Adapt the layout body size to the source: if source body is much
        # larger than the default, scale to keep visual cadence.
        laid_out, anchors = layout(items, layout_cfg)
        out = render(doc, laid_out, layout_cfg,
                     source_path=src_path, workers=workers or 1)
        _apply_toc(doc, out, anchors)
        out.save(dst_path, deflate=True, deflate_images=True, deflate_fonts=True, garbage=4)
        out_pages = out.page_count
        out.close()
    finally:
        doc.close()

    return {
        "input": src_path,
        "output": dst_path,
        "source_pages": len(pages),
        "source_body_size": body_size,
        "output_pages": out_pages,
        "items": len(items),
    }
