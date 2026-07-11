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


def downsample_to_width(pix: "fitz.Pixmap", cell_w: int) -> GrayImage:
    """Box-average a grayscale pixmap to a fixed *width*, height proportional.

    Used to bring every page-content clip onto a common column width before
    stacking them into one tall strip. Unlike ``downsample`` (which fixes the
    longer side) this fixes the width, so a tall strip keeps its full height.
    """
    sw, sh = pix.width, pix.height
    samples = pix.samples
    ow = max(1, cell_w)
    oh = max(1, round(cell_w * sh / sw))
    out = [0.0] * (ow * oh)
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
                total += sum(samples[base + x0: base + x1])
                count += (x1 - x0)
            out[oy * ow + ox] = total / count if count else 0.0
    return GrayImage(ow, oh, out)


def vstack(imgs: List[GrayImage]) -> GrayImage:
    """Stack same-width grayscale images into one tall column."""
    imgs = [im for im in imgs if im.h > 0]
    if not imgs:
        return GrayImage(1, 1, [255.0])
    w = imgs[0].w
    px: List[float] = []
    h = 0
    for im in imgs:
        if im.w != w:  # defensive; strips are built at a common width
            im = _resize_to(im, w, im.h)
        px.extend(im.px)
        h += im.h
    return GrayImage(w, h, px)


def density_map(img: GrayImage, width: int = 32, max_h: int = 200) -> GrayImage:
    """Reduce a tall strip to a coarse ink-density map.

    Rendered text at strip resolution is effectively high-frequency noise:
    two independent renderings of the *same* content barely correlate
    pixel-for-pixel, so SSIM on the raw strip is unusable (~0.5 even when
    identical). Averaging the strip into coarse cells turns text into a smooth
    "ink per region" density, which IS stable: identical content maps to an
    identical density map (SSIM 1.0), a pure pagination shift barely moves it,
    and a genuine re-layout (different column width, changed spacing) clearly
    changes it. Height is capped so long documents stay cheap to score.
    """
    cw = max(1, width)
    ch = min(max_h, max(1, round(cw * img.h / img.w)))
    out = [0.0] * (cw * ch)
    for oy in range(ch):
        y0 = oy * img.h // ch
        y1 = max(y0 + 1, (oy + 1) * img.h // ch)
        for ox in range(cw):
            x0 = ox * img.w // cw
            x1 = max(x0 + 1, (ox + 1) * img.w // cw)
            total = 0.0
            count = 0
            for yy in range(y0, y1):
                base = yy * img.w
                total += sum(img.px[base + x0: base + x1])
                count += (x1 - x0)
            out[oy * cw + ox] = total / count if count else 0.0
    return GrayImage(cw, ch, out)


def to_pixmap(img: GrayImage) -> "fitz.Pixmap":
    """Pack a GrayImage into an 8-bit grayscale Pixmap (for saving as PNG)."""
    data = bytes(max(0, min(255, int(round(v)))) for v in img.px)
    return fitz.Pixmap(fitz.csGRAY, img.w, img.h, data, 0)


def from_pixmap(pix: "fitz.Pixmap") -> GrayImage:
    """Read a (grayscale) Pixmap into a GrayImage at native resolution."""
    if pix.colorspace is None or pix.colorspace.n != 1 or pix.alpha:
        pix = fitz.Pixmap(fitz.csGRAY, pix)
    return GrayImage(pix.width, pix.height, [float(b) for b in pix.samples])


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
