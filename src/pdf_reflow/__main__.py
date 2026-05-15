"""CLI: python -m pdf_reflow <input.pdf> <output.pdf>"""

from __future__ import annotations

import argparse
import sys
import time

from .reflow import ReflowConfig, reflow_pdf


# Common mobile page presets in PDF points (1 pt = 1/72 inch).
PRESETS = {
    # iPhone 17 / 15 logical size at 72dpi-equivalent (we use a slightly
    # narrower body to give comfortable reading margins).
    "iphone17": (360.0, 640.0),
    "iphone-mini": (320.0, 568.0),
    "ipad-mini": (480.0, 640.0),
    "kindle": (380.0, 540.0),
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pdf_reflow",
                                 description="Reflow a PDF for mobile single-column reading.")
    ap.add_argument("input", help="Input PDF path")
    ap.add_argument("output", help="Output PDF path")
    ap.add_argument("--preset", choices=sorted(PRESETS), default="iphone17",
                    help="Target screen preset")
    ap.add_argument("--page-width", type=float, default=None)
    ap.add_argument("--page-height", type=float, default=None)
    ap.add_argument("--body-size", type=float, default=11.0,
                    help="Body font size in points")
    ap.add_argument("--figure-dpi", type=float, default=150.0,
                    help="Rasterization DPI for diagrams/equations (default 150; use 220+ for print)")
    ap.add_argument("--workers", type=int, default=1,
                    help="Number of worker processes for page extraction "
                         "and figure rasterization. 1=sequential (default), "
                         "0=auto (= min(cpu_count, 8)). Parallelism only kicks "
                         "in for documents with >= 4 pages / >= 4 figures.")
    args = ap.parse_args(argv)

    w, h = PRESETS[args.preset]
    if args.page_width: w = args.page_width
    if args.page_height: h = args.page_height

    workers = None if args.workers == 0 else args.workers
    cfg = ReflowConfig(
        page_width=w,
        page_height=h,
        body_size=args.body_size,
        figure_dpi=args.figure_dpi,
        workers=workers,
    )
    t0 = time.perf_counter()
    stats = reflow_pdf(args.input, args.output, cfg)
    dt = time.perf_counter() - t0
    print(
        f"reflowed {args.input!r}: {stats['source_pages']} -> "
        f"{stats['output_pages']} pages, "
        f"{stats['items']} content items, body={stats['source_body_size']}pt, "
        f"{dt:.2f}s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
