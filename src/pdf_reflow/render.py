"""Render laid-out pages as a new PDF using PyMuPDF.

The source document is used both to rasterize figure regions (clipped from
the original page at a high DPI for crisp output) and to read fonts. The
output uses base14 PDF fonts so the result is small and works on any reader.
"""

from __future__ import annotations

import concurrent.futures
import os
from typing import Dict, List, Optional, Tuple

import fitz

from .cjk_fonts import font_entry_for_fontname
from .layout import DrawImage, DrawRule, DrawText, LaidOutPage, LayoutConfig


_FONT_ALIASES = {
    "times-roman": "tiro",   # Times-Roman base14
    "times-bold": "tibo",
    "times-italic": "tiit",
    "times-bolditalic": "tibi",
    "helvetica": "helv",
    "helvetica-bold": "hebo",
    "helvetica-italic": "heit",         # Helvetica-Oblique
    "helvetica-bolditalic": "hebi",     # Helvetica-BoldOblique
    "courier": "cour",
    # PyMuPDF accepts these short names directly for its bundled CJK CID
    # fonts; pass them through unchanged.
    "china-s": "china-s",
    "china-t": "china-t",
    "japan": "japan",
    "korea": "korea",
}


def _insert_text_args(name: str) -> dict:
    """Build the keyword arguments needed by ``Page.insert_text`` for ``name``.

    For base14 + bundled CID fonts we just remap the short name. For a
    system CJK font we also forward the ``fontfile`` path so PyMuPDF
    embeds the right glyphs in the output PDF.
    """
    entry = font_entry_for_fontname(name)
    if entry is not None and entry.fontfile is not None:
        return {"fontname": entry.fontname, "fontfile": entry.fontfile}
    return {"fontname": _FONT_ALIASES.get(name, name)}


def _alias(name: str) -> str:
    """Back-compat shim for callers that just want the base14 short name."""
    return _FONT_ALIASES.get(name, name)


def _figure_scale(op: DrawImage, cfg: LayoutConfig) -> float:
    """Adaptive DPI: rasterize only as many pixels as the output rect
    actually needs, capped at cfg.figure_dpi. Matches the inline logic
    that was previously embedded in ``render``."""
    clip_w = fitz.Rect(op.source_rect).width
    if clip_w > 0:
        scale = min(op.w * cfg.figure_dpi / (72.0 * clip_w), cfg.figure_dpi / 72.0)
    else:
        scale = cfg.figure_dpi / 72.0
    return max(scale, 1.0)


def _figure_key(op: DrawImage, scale: float) -> Tuple:
    return (op.source_page, tuple(round(v, 1) for v in op.source_rect), round(scale, 2))


def _rasterize_one(src_doc: fitz.Document, page_index: int, rect, scale: float) -> bytes:
    src = src_doc[page_index]
    pix = src.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=fitz.Rect(rect), alpha=False)
    return pix.tobytes("png")


def _rasterize_worker(args):
    """Module-level worker for ProcessPoolExecutor: rasterize one figure
    region. Each call re-opens the source PDF so workers don't share a
    Document — PyMuPDF is not safe across pickle/process boundaries."""
    src_path, page_index, rect, scale = args
    doc = fitz.open(src_path)
    try:
        return _rasterize_one(doc, page_index, rect, scale)
    finally:
        doc.close()


def _collect_figure_tasks(
    pages: List[LaidOutPage], cfg: LayoutConfig
) -> Dict[Tuple, Tuple[int, Tuple[float, float, float, float], float]]:
    """Walk every DrawImage op once and build a de-duplicated map of
    figure rasterization tasks keyed by ``_figure_key``. Multiple ops can
    share a key (same source rect + scale) — we rasterize once."""
    tasks: Dict[Tuple, Tuple] = {}
    for lop in pages:
        for op in lop.ops:
            if isinstance(op, DrawImage):
                scale = _figure_scale(op, cfg)
                key = _figure_key(op, scale)
                if key not in tasks:
                    tasks[key] = (op.source_page, tuple(op.source_rect), scale)
    return tasks


def _prerasterize_figures(
    src_path: Optional[str],
    src_doc: fitz.Document,
    pages: List[LaidOutPage],
    cfg: LayoutConfig,
    workers: int,
) -> Dict[Tuple, bytes]:
    """Rasterize every figure clip up front, optionally in parallel.

    Sequential rasterization stays on the existing source Document.
    Parallel rasterization needs a path so workers can each open their
    own Document; if ``src_path`` is None we transparently fall back to
    sequential (e.g. the source came from a stream).
    """
    tasks = _collect_figure_tasks(pages, cfg)
    if not tasks:
        return {}
    if workers <= 1 or src_path is None or len(tasks) < 4:
        return {
            k: _rasterize_one(src_doc, p, r, s)
            for k, (p, r, s) in tasks.items()
        }
    keys = list(tasks.keys())
    args = [(src_path, *tasks[k]) for k in keys]
    workers = min(workers, len(tasks))
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as ex:
        blobs = list(ex.map(_rasterize_worker, args))
    return dict(zip(keys, blobs))


def render(
    source_doc: fitz.Document,
    pages: List[LaidOutPage],
    cfg: LayoutConfig,
    *,
    source_path: Optional[str] = None,
    workers: int = 1,
) -> fitz.Document:
    figure_cache = _prerasterize_figures(source_path, source_doc, pages, cfg, workers)

    out = fitz.open()
    for lop in pages:
        page = out.new_page(width=lop.width, height=lop.height)
        # Track system CJK fonts already registered on this page so we
        # only embed each fontfile once per page — repeated insert_text
        # calls with fontfile= would otherwise add duplicate font
        # resources to the page's resource dict.
        page_cjk_fonts: set = set()
        for op in lop.ops:
            if isinstance(op, DrawText):
                args = _insert_text_args(op.font)
                fontfile = args.pop("fontfile", None)
                fontname = args["fontname"]
                if fontfile is not None and fontname not in page_cjk_fonts:
                    page.insert_font(fontname=fontname, fontfile=fontfile)
                    page_cjk_fonts.add(fontname)
                page.insert_text(
                    point=(op.x, op.y),
                    text=op.text,
                    fontsize=op.size,
                    color=op.color,
                    **args,
                )
            elif isinstance(op, DrawImage):
                scale = _figure_scale(op, cfg)
                key = _figure_key(op, scale)
                png = figure_cache.get(key)
                if png is None:
                    png = _rasterize_one(source_doc, op.source_page, op.source_rect, scale)
                    figure_cache[key] = png
                page.insert_image(
                    rect=fitz.Rect(op.x, op.y, op.x + op.w, op.y + op.h),
                    stream=png,
                    keep_proportion=True,
                )
            elif isinstance(op, DrawRule):
                page.draw_line(
                    p1=(op.x0, op.y),
                    p2=(op.x1, op.y),
                    color=(0.6, 0.6, 0.6),
                    width=0.5,
                )
    return out
