"""Verification harness for the pdf_reflow pipeline.

A dependency-free (stdlib + PyMuPDF only) harness to iterate on and guard
the reflow *rendering* quality. Three layers, following the document-parsing
evaluation literature:

  Layer 1 - deterministic scorecard (fast, runs in CI)
      For each fixture: reflow it, then compare the reflowed output against
      a *reference* derived from the source itself. Because reflow must
      preserve text and reading order while changing geometry, the source's
      own reading-order text IS the ground truth -- no hand annotation.
      Metrics mirror the Bast/Korzen PDF-extraction taxonomy (word +/-/~,
      paragraph counts) plus reflow-specific structural invariants
      (headings preserved, no private-use-glyph leakage, figures kept) and
      a render-quality signal (lines overflowing the column).

  Layer 2 - visual golden snapshots (SSIM, not pixel-diff)
      Rasterize each output page and compare to a committed golden PNG with
      structural similarity. SSIM absorbs anti-alias / font jitter that
      would swamp a raw pixel diff. Goldens are generated from the current
      output and human-blessed by committing them.

  Layer 3 - HTML iteration report
      Side-by-side source vs. reflowed pages with the scorecard delta on
      top, so tuning a heuristic is: run -> eyeball -> adjust -> rerun.

See ``tools/verify.py`` for the CLI entry point and ``docs/verify.md`` for
the design rationale.
"""

from .metrics import FixtureScore, score_fixture
from .imaging import rasterize_gray, ssim

__all__ = ["FixtureScore", "score_fixture", "rasterize_gray", "ssim"]
