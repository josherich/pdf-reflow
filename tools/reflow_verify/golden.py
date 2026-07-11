"""Layer 2: pagination-invariant visual golden compared with SSIM.

A reflow tool re-paginates on almost every meaningful change (a spacing tweak
pushes a paragraph from the bottom of one page to the top of the next). Page-
by-page image comparison would then misalign every page after the shift and
flag a cascade of false regressions -- on exactly the changes this harness
exists to help you make.

So instead of comparing page N to golden page N, we reconstruct the *continuous
reflowed column*: crop each output page to its content (dropping page margins
and the partial-page whitespace at each break) and stack the crops into one
tall strip. Where the page breaks fall no longer matters -- the strip is the
same whether a line sits at the bottom of p5 or the top of p6 -- so SSIM only
drops when the rendering *actually* changes.

A golden is the last strip a human looked at and blessed by committing it.
Lifecycle unchanged: bootstrap -> approve once -> auto-compare -> re-bless on
purpose (--update-golden). One small PNG per fixture.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

import fitz

from .imaging import (
    GrayImage,
    density_map,
    downsample_to_width,
    from_pixmap,
    ssim,
    to_pixmap,
    vstack,
)

# Rasterize page-content clips at this DPI, downsample to STRIP_W columns, then
# reduce the stitched strip to a DENSITY_W-wide ink-density map for comparison.
CLIP_DPI = 48
STRIP_W = 64
DENSITY_W = 32


@dataclass
class GoldenResult:
    name: str
    threshold: float
    score: float
    golden_png: Optional[str]
    actual_png: str
    source_pages: int = 0
    bootstrapped: bool = False
    updated: bool = False

    @property
    def ok(self) -> bool:
        return self.score >= self.threshold

    # Back-compat alias: the strip yields a single score per fixture.
    @property
    def min_score(self) -> float:
        return self.score


def _stem(name: str) -> str:
    return os.path.splitext(os.path.basename(name))[0]


def _page_content_rect(page: "fitz.Page") -> Optional["fitz.Rect"]:
    """Union of the page's text + image block bboxes (its inked content).

    Horizontal extent is the full content column; vertical extent is just the
    inked band, so top/bottom page margins and a partial last page don't
    contribute whitespace to the stitched strip.
    """
    rect = None
    d = page.get_text("dict")
    for block in d.get("blocks", []):
        b = fitz.Rect(block["bbox"])
        rect = b if rect is None else (rect | b)
    return rect


def build_strip(out_pdf: str, clip_dpi: int = CLIP_DPI, strip_w: int = STRIP_W) -> GrayImage:
    """Stitch an output PDF's margin-cropped page content into one gray strip."""
    doc = fitz.open(out_pdf)
    try:
        # Global horizontal content band, so every page crop shares a width.
        gx0, gx1 = None, None
        rects: List[Optional[fitz.Rect]] = []
        for page in doc:
            r = _page_content_rect(page)
            rects.append(r)
            if r is not None:
                gx0 = r.x0 if gx0 is None else min(gx0, r.x0)
                gx1 = r.x1 if gx1 is None else max(gx1, r.x1)

        bands: List[GrayImage] = []
        for page, r in zip(doc, rects):
            if r is None or gx0 is None:
                continue
            clip = fitz.Rect(gx0, r.y0, gx1, r.y1)
            if clip.is_empty or clip.width <= 0 or clip.height <= 0:
                continue
            pix = page.get_pixmap(dpi=clip_dpi, colorspace=fitz.csGRAY,
                                  alpha=False, clip=clip)
            if pix.width == 0 or pix.height == 0:
                continue
            bands.append(downsample_to_width(pix, strip_w))
        return vstack(bands)
    finally:
        doc.close()


def check_golden(
    name: str,
    out_pdf: str,
    golden_dir: str,
    actual_dir: str,
    *,
    threshold: float = 0.97,
    update: bool = False,
) -> GoldenResult:
    """Build the output's content strip and SSIM-compare it to the golden strip.

    Missing golden => bootstrap (write it, score 1.0). ``update`` => overwrite.
    """
    stem = _stem(name)
    os.makedirs(golden_dir, exist_ok=True)
    os.makedirs(actual_dir, exist_ok=True)

    strip = density_map(build_strip(out_pdf), width=DENSITY_W)
    actual_png = os.path.join(actual_dir, f"{stem}.png")
    to_pixmap(strip).save(actual_png)

    golden_png = os.path.join(golden_dir, f"{stem}.png")
    doc = fitz.open(out_pdf)
    n_pages = doc.page_count
    doc.close()

    if update or not os.path.exists(golden_png):
        to_pixmap(strip).save(golden_png)
        return GoldenResult(
            name=name, threshold=threshold, score=1.0,
            golden_png=golden_png, actual_png=actual_png, source_pages=n_pages,
            bootstrapped=not update, updated=update,
        )

    golden = from_pixmap(fitz.Pixmap(golden_png))
    score = ssim(golden, strip)
    return GoldenResult(
        name=name, threshold=threshold, score=round(score, 4),
        golden_png=golden_png, actual_png=actual_png, source_pages=n_pages,
    )
