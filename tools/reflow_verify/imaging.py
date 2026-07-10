"""Rasterization + structural-similarity (SSIM), pure Python + PyMuPDF.

We deliberately avoid numpy/scikit-image: the project prides itself on a
minimal dependency footprint, and the harness must run in the same venv as
the library with nothing extra installed.

SSIM is computed on a downsampled grayscale grid. Downsampling to a fixed
grid does three useful things at once:
  * makes the cost independent of page DPI / size (fast, deterministic),
  * absorbs sub-pixel anti-aliasing jitter (the reason to prefer SSIM over
    an exact pixel diff in the first place),
  * lets us compare pages whose pixel dimensions differ (e.g. after a
    layout change) by mapping both onto the same grid.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

import fitz

# Fixed comparison grid: the longer page dimension is scaled to GRID cells,
# the shorter kept proportional. ~110 keeps a phone-aspect page around
# 66x110 = ~7k cells -- plenty of structure, milliseconds to score.
GRID = 110

# SSIM stabilisation constants for an 8-bit (0..255) dynamic range, the
# canonical Wang et al. 2004 values (K1=0.01, K2=0.03).
_C1 = (0.01 * 255) ** 2
_C2 = (0.03 * 255) ** 2


@dataclass
class GrayImage:
    """A downsampled grayscale image as a flat row-major float list."""
    w: int
    h: int
    px: List[float]  # length w*h, values 0..255


def rasterize_gray(page: "fitz.Page", dpi: int = 96) -> "fitz.Pixmap":
    """Render a page to an 8-bit grayscale pixmap (no alpha)."""
    return page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, alpha=False)


def downsample(pix: "fitz.Pixmap", grid: int = GRID) -> GrayImage:
    """Box-average a grayscale pixmap down onto a fixed grid.

    Preserves aspect ratio: the longer side becomes ``grid`` cells.
    """
    sw, sh = pix.width, pix.height
    samples = pix.samples  # bytes, length sw*sh for 1-channel gray
    if sw >= sh:
        ow = grid
        oh = max(1, round(grid * sh / sw))
    else:
        oh = grid
        ow = max(1, round(grid * sw / sh))

    out = [0.0] * (ow * oh)
    # Map each output cell to a source rectangle and average it.
    for oy in range(oh):
        y0 = oy * sh // oh
        y1 = max(y0 + 1, (oy + 1) * sh // oh)
        for ox in range(ow):
            x0 = ox * sw // ow
            x1 = max(x0 + 1, (ox + 1) * sw // ow)
            total = 0
            count = 0
            for yy in range(y0, y1):
                base = yy * sw
                row = samples[base + x0: base + x1]
                total += sum(row)
                count += (x1 - x0)
            out[oy * ow + ox] = total / count if count else 0.0
    return GrayImage(ow, oh, out)


def _resize_to(img: GrayImage, w: int, h: int) -> GrayImage:
    """Nearest-neighbour resize of an already-small grid (for aligning two
    images whose grids differ by a row/column after independent rounding)."""
    if img.w == w and img.h == h:
        return img
    out = [0.0] * (w * h)
    for y in range(h):
        sy = y * img.h // h
        for x in range(w):
            sx = x * img.w // w
            out[y * w + x] = img.px[sy * img.w + sx]
    return GrayImage(w, h, out)


def ssim(a: GrayImage, b: GrayImage, window: int = 8) -> float:
    """Mean structural similarity over non-overlapping windows.

    Returns a value in roughly [-1, 1]; 1.0 == identical. Two blank pages
    score 1.0 (both windows are flat and equal).
    """
    if (a.w, a.h) != (b.w, b.h):
        # Align onto the smaller grid so both cover the same content.
        w = min(a.w, b.w)
        h = min(a.h, b.h)
        a = _resize_to(a, w, h)
        b = _resize_to(b, w, h)

    w, h = a.w, a.h
    scores: List[float] = []
    for wy in range(0, h, window):
        for wx in range(0, w, window):
            xs: List[float] = []
            ys: List[float] = []
            for yy in range(wy, min(wy + window, h)):
                row = yy * w
                for xx in range(wx, min(wx + window, w)):
                    xs.append(a.px[row + xx])
                    ys.append(b.px[row + xx])
            n = len(xs)
            if n < 2:
                continue
            mx = sum(xs) / n
            my = sum(ys) / n
            vx = sum((v - mx) ** 2 for v in xs) / (n - 1)
            vy = sum((v - my) ** 2 for v in ys) / (n - 1)
            cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / (n - 1)
            s = ((2 * mx * my + _C1) * (2 * cov + _C2)) / (
                (mx * mx + my * my + _C1) * (vx + vy + _C2)
            )
            scores.append(s)
    if not scores:
        return 1.0
    return sum(scores) / len(scores)


def page_grid(page: "fitz.Page", dpi: int = 96, grid: int = GRID) -> GrayImage:
    """Convenience: rasterize + downsample in one call."""
    return downsample(rasterize_gray(page, dpi=dpi), grid=grid)
