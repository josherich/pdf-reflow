"""Verification harness for the pdf_reflow pipeline.

A dependency-free (stdlib + PyMuPDF only) harness to iterate on and guard the
reflow quality. Three layers, following the document-parsing evaluation
literature:

  Layer 1 - deterministic scorecard (fast, runs in CI)
      For each fixture: reflow it, then compare the reflowed output against
      a *reference* derived from the source itself. Because reflow must
      preserve text and reading order while changing geometry, the source's
      own reading-order text IS the ground truth -- no hand annotation.
      Metrics mirror the Bast/Korzen PDF-extraction taxonomy (word +/-/~,
      paragraph counts) plus reflow-specific structural invariants
      (headings preserved, no private-use-glyph leakage, figures kept) and
      a render-quality signal (lines overflowing the column).

  Layer 2 - human visual feedback (``--serve`` / ``--feedback``)
      A local web tool where the user uploads *golden* images of how a
      reflowed page should look (saved to verify/golden/, diffed per page
      against the rendered output) and draws annotated boxes with notes on
      output pages (saved to verify/feedback/). ``--feedback`` exports both
      as one JSON document so a coding agent can act on the human's visual
      judgement.

  Layer 3 - HTML scorecard report
      A browsable per-fixture summary: the scorecard delta vs baseline and
      any headings that fell out of the output, so tuning a heuristic is:
      run -> read -> adjust -> rerun.

See ``tools/verify.py`` for the CLI entry point and ``docs/verify.md`` for
the design rationale.
"""

from .metrics import FixtureScore, score_fixture

__all__ = ["FixtureScore", "score_fixture"]
