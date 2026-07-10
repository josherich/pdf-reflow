"""Layer 2: visual golden snapshots compared with SSIM.

A golden is just the last output a human looked at and blessed by committing
it. Lifecycle:

    generate (bootstrap)  ->  human approves once (commit PNGs)
        ->  auto-compare forever  ->  re-bless on purpose (--update-golden)

We render each output page to a low-DPI PNG. On a normal run we SSIM-compare
against the committed golden; below ``threshold`` is a visual regression and
we emit a side-by-side diff into the report. ``update`` overwrites the
goldens (the git diff of those PNGs is then the review artifact).

Low-DPI grayscale keeps the committed PNGs tiny and lets SSIM shrug off
anti-aliasing differences between MuPDF builds -- record the MuPDF version so
a mass failure has an obvious cause.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

import fitz

from .imaging import GRID, downsample, rasterize_gray, ssim

# Goldens are only ever fed to SSIM, which downsamples to a ~110-cell grid, so
# there's no point storing them large. 48 DPI keeps a phone page around
# 240px (well above the grid), stays visually inspectable, and keeps the
# committed PNGs small.
GOLDEN_DPI = 48


@dataclass
class PageResult:
    page: int
    score: float
    golden_png: Optional[str]
    actual_png: str
    ok: bool


@dataclass
class GoldenResult:
    name: str
    threshold: float
    pages: List[PageResult]
    bootstrapped: bool = False
    updated: bool = False

    @property
    def min_score(self) -> float:
        return min((p.score for p in self.pages), default=1.0)

    @property
    def ok(self) -> bool:
        return all(p.ok for p in self.pages)


def _stem(name: str) -> str:
    return os.path.splitext(os.path.basename(name))[0]


def check_golden(
    name: str,
    out_pdf: str,
    golden_dir: str,
    actual_dir: str,
    *,
    threshold: float = 0.97,
    update: bool = False,
    dpi: int = GOLDEN_DPI,
) -> GoldenResult:
    """SSIM-compare every output page against its golden PNG.

    When a golden is missing it is bootstrapped (written, scored 1.0). When
    ``update`` is set every golden is overwritten from the current output.
    """
    stem = _stem(name)
    os.makedirs(golden_dir, exist_ok=True)
    os.makedirs(actual_dir, exist_ok=True)

    doc = fitz.open(out_pdf)
    pages: List[PageResult] = []
    bootstrapped = False
    updated = False
    try:
        for i, page in enumerate(doc):
            pix = rasterize_gray(page, dpi=dpi)
            actual_png = os.path.join(actual_dir, f"{stem}_p{i:03d}.png")
            pix.save(actual_png)
            actual_grid = downsample(pix, grid=GRID)

            golden_png = os.path.join(golden_dir, f"{stem}_p{i:03d}.png")
            if update or not os.path.exists(golden_png):
                pix.save(golden_png)
                if update:
                    updated = True
                else:
                    bootstrapped = True
                pages.append(PageResult(i, 1.0, golden_png, actual_png, True))
                continue

            gpix = fitz.Pixmap(golden_png)
            if gpix.colorspace and gpix.colorspace.n != 1:
                gpix = fitz.Pixmap(fitz.csGRAY, gpix)
            golden_grid = downsample(gpix, grid=GRID)
            score = ssim(golden_grid, actual_grid)
            pages.append(
                PageResult(i, round(score, 4), golden_png, actual_png, score >= threshold)
            )
    finally:
        doc.close()

    # Goldens for pages that no longer exist (output got shorter) are stale.
    stem_prefix = f"{stem}_p"
    live = {os.path.basename(p.golden_png) for p in pages if p.golden_png}
    for fn in os.listdir(golden_dir):
        if fn.startswith(stem_prefix) and fn.endswith(".png") and fn not in live:
            # Missing page => a regression unless we're updating goldens.
            if update:
                os.remove(os.path.join(golden_dir, fn))
                updated = True
            else:
                pages.append(
                    PageResult(-1, 0.0, os.path.join(golden_dir, fn), "", False)
                )

    return GoldenResult(
        name=name,
        threshold=threshold,
        pages=pages,
        bootstrapped=bootstrapped,
        updated=updated,
    )
