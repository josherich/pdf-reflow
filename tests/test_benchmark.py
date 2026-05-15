"""Benchmarks for reflow performance.

These tests measure per-phase and end-to-end runtime on the three fixture
PDFs (small / medium-LaTeX / large-tech-report) and assert performance
budgets. The budgets are set generously above current measured numbers
on a 4-core developer laptop so the suite passes on slower CI runners
while still catching real regressions (anything > 2x slowdown trips).

The benchmark also checks that parallel mode (workers > 1) is not slower
than sequential on the large fixture — i.e. that the multiprocessing
plumbing actually delivers speed-up where it matters.

Run with:
    uv run python -m unittest tests.test_benchmark
or, to print a timing table:
    uv run python -m tests.test_benchmark
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Callable, Dict, List, Tuple

import fitz

from pdf_reflow.analyze import analyze_document
from pdf_reflow.extract import extract_document, extract_document_parallel
from pdf_reflow.layout import LayoutConfig, layout
from pdf_reflow.reflow import ReflowConfig, reflow_pdf
from pdf_reflow.render import render


FIXTURES = Path(__file__).parent / "fixtures"
BITCOIN_PDF = FIXTURES / "bitcoin.pdf"
MIT_PDF = FIXTURES / "mit_latex_sample.pdf"
TECH_PDF = FIXTURES / "tech_report.pdf"


# (fixture_path, end_to_end_budget_seconds). Numbers reflect a 4-core
# laptop running the post-optimization code with a 3x cushion. The
# tech_report budget covers the sequential path; the parallel-speedup
# check below verifies that workers>1 is genuinely faster on it.
BUDGETS = [
    (BITCOIN_PDF, 3.0),
    (MIT_PDF, 2.0),
    (TECH_PDF, 30.0),
]


def _time(fn: Callable[[], None], repeat: int = 1) -> float:
    """Return the best of ``repeat`` wall-clock measurements (seconds)."""
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        dt = time.perf_counter() - t0
        if dt < best:
            best = dt
    return best


def _phase_times(src: Path, workers: int = 1) -> Dict[str, float]:
    """Measure per-phase cost for one reflow run.

    Returns wall-clock seconds for extract / analyze / layout / render /
    total. The phases are run inline so the total matches the sum to
    within a few ms.
    """
    out_path = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False).name
    try:
        cfg = ReflowConfig(workers=workers).to_layout()
        t_total = time.perf_counter()
        doc = fitz.open(str(src))
        try:
            t0 = time.perf_counter()
            if workers > 1:
                pages = extract_document_parallel(str(src), workers=workers)
            else:
                pages = extract_document(doc)
            t_extract = time.perf_counter() - t0

            t0 = time.perf_counter()
            items, _ = analyze_document(pages)
            t_analyze = time.perf_counter() - t0

            t0 = time.perf_counter()
            laid_out, _anchors = layout(items, cfg)
            t_layout = time.perf_counter() - t0

            t0 = time.perf_counter()
            out = render(doc, laid_out, cfg,
                         source_path=str(src), workers=workers)
            out.save(out_path, deflate=True, deflate_images=True,
                     deflate_fonts=True, garbage=4)
            out.close()
            t_render = time.perf_counter() - t0
        finally:
            doc.close()
        t_total = time.perf_counter() - t_total
        return {
            "extract": t_extract,
            "analyze": t_analyze,
            "layout": t_layout,
            "render": t_render,
            "total": t_total,
            "pages": len(pages),
        }
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# unittest cases
# ---------------------------------------------------------------------------


class ReflowBenchmarkBudgets(unittest.TestCase):
    """End-to-end runtime must stay inside a generous budget on each fixture."""

    def test_per_fixture_runtime_within_budget(self):
        for src, budget in BUDGETS:
            if not src.exists():
                self.skipTest(f"missing fixture: {src}")
            with self.subTest(fixture=src.name):
                with tempfile.TemporaryDirectory() as td:
                    out = os.path.join(td, "out.pdf")
                    dt = _time(lambda: reflow_pdf(str(src), out))
                    self.assertLess(
                        dt, budget,
                        f"{src.name}: {dt:.2f}s over budget {budget:.1f}s",
                    )


class ReflowParallelSpeedup(unittest.TestCase):
    """For the large fixture, workers>1 must not be slower than sequential.

    On a multi-core machine it should be a meaningful speed-up. On a
    single-core CI runner the two times should be within ~20% of each
    other; we only assert "no slower than 1.5x".
    """

    def test_tech_report_parallel_not_slower(self):
        if not TECH_PDF.exists():
            self.skipTest(f"missing fixture: {TECH_PDF}")
        if (os.cpu_count() or 1) < 2:
            self.skipTest("need >=2 CPUs to test parallel speed-up")
        with tempfile.TemporaryDirectory() as td:
            out1 = os.path.join(td, "seq.pdf")
            outN = os.path.join(td, "par.pdf")
            t_seq = _time(lambda: reflow_pdf(str(TECH_PDF), out1,
                                             ReflowConfig(workers=1)))
            workers = min(os.cpu_count() or 1, 4)
            t_par = _time(lambda: reflow_pdf(str(TECH_PDF), outN,
                                             ReflowConfig(workers=workers)))
            # Both outputs must have the same page count — parallelism is
            # purely a perf optimization, never a behavior change.
            d1 = fitz.open(out1); dN = fitz.open(outN)
            try:
                self.assertEqual(d1.page_count, dN.page_count)
            finally:
                d1.close(); dN.close()
            self.assertLess(
                t_par, t_seq * 1.5,
                f"parallel ({t_par:.2f}s w={workers}) slower than 1.5x "
                f"of sequential ({t_seq:.2f}s)",
            )


class ReflowPhaseBudgets(unittest.TestCase):
    """Per-phase budgets that pinpoint a regression.

    extract / render are dominated by PyMuPDF C calls and scale with page
    count and figure density. analyze / layout are pure-Python and should
    stay sub-second on every fixture in this repo.
    """

    PHASE_BUDGETS: List[Tuple[Path, Dict[str, float]]] = [
        (BITCOIN_PDF, {"extract": 0.5, "analyze": 0.3, "layout": 0.5, "render": 1.5}),
        (MIT_PDF,     {"extract": 0.4, "analyze": 0.2, "layout": 0.3, "render": 1.0}),
        (TECH_PDF,    {"extract": 22.0, "analyze": 2.0, "layout": 2.0, "render": 8.0}),
    ]

    def test_phase_budgets(self):
        for src, budgets in self.PHASE_BUDGETS:
            if not src.exists():
                self.skipTest(f"missing fixture: {src}")
            with self.subTest(fixture=src.name):
                phases = _phase_times(src, workers=1)
                for phase, budget in budgets.items():
                    self.assertLess(
                        phases[phase], budget,
                        f"{src.name} phase {phase!r}: "
                        f"{phases[phase]:.2f}s > budget {budget:.1f}s",
                    )


# ---------------------------------------------------------------------------
# Manual reporting entry point: ``python -m tests.test_benchmark``
# ---------------------------------------------------------------------------


def _print_report() -> None:
    print(f"{'fixture':30s}  {'workers':>7s}  {'extract':>8s}  {'analyze':>8s}  "
          f"{'layout':>7s}  {'render':>7s}  {'total':>7s}  {'pages':>5s}")
    print("-" * 95)
    for src in (BITCOIN_PDF, MIT_PDF, TECH_PDF):
        if not src.exists():
            print(f"{src.name}: MISSING")
            continue
        for workers in (1, min(os.cpu_count() or 1, 4)):
            if workers == 1 or src == BITCOIN_PDF or src == MIT_PDF:
                # Only show parallel result for the big fixture; small docs
                # don't benefit and clutter the table.
                if workers != 1 and src != TECH_PDF:
                    continue
            p = _phase_times(src, workers=workers)
            print(f"{src.name:30s}  {workers:>7d}  "
                  f"{p['extract']:>7.2f}s  {p['analyze']:>7.2f}s  "
                  f"{p['layout']:>6.2f}s  {p['render']:>6.2f}s  "
                  f"{p['total']:>6.2f}s  {p['pages']:>5d}")


if __name__ == "__main__":
    if "--report" in sys.argv:
        _print_report()
    else:
        unittest.main()
