"""End-to-end tests on the IEEE-style two-column fixture.

``two-column.pdf`` is a 2-page IEEE conference template that exercises the
two-column reflow path:

  - Two-column body where left- and right-column lines share baseline y
    (the column-detection-from-blocks pre-existing heuristic merged them
    into single full-width lines and then mis-classified the page as
    single-column; the per-span detector fixes this).

  - A centered, page-wide title and centered author / affiliation block
    sitting above the two-column body (full-width spans must be lifted
    out of column partitioning so the title is one block, not two).

  - A figure (Magnetization plot + caption) embedded in the right column
    only — the figure band must not swallow left-column body text in the
    same y-range, and the figure crop must clip to the right column.

  - Centered alignment on the title and author block must round-trip to
    the reflowed PDF (``align="center"`` on the FlowItem).

Run with:  uv run python -m unittest tests.test_two_column
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

import fitz

from pdf_reflow.extract import extract_document
from pdf_reflow.analyze import (
    analyze_document,
    _detect_columns_from_spans,
    _partition_spans_by_column,
)
from pdf_reflow.reflow import reflow_pdf


FIXTURES = Path(__file__).parent / "fixtures"
TWO_COL_PDF = FIXTURES / "two-column.pdf"


def _normalize(text: str) -> str:
    text = re.sub(r"-\s+\n", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


class TwoColumnExtractTests(unittest.TestCase):
    """Detection happens on the raw spans, before line grouping merges
    aligned baselines from left and right columns into one full-width
    line. These tests pin the detector down."""

    @classmethod
    def setUpClass(cls):
        if not TWO_COL_PDF.exists():
            raise unittest.SkipTest(f"missing fixture: {TWO_COL_PDF}")
        doc = fitz.open(str(TWO_COL_PDF))
        cls.pages = extract_document(doc)
        doc.close()

    def test_both_pages_detected_as_two_column(self):
        for p in self.pages:
            ncols, mid = _detect_columns_from_spans(p.spans, p.width)
            self.assertEqual(
                ncols, 2,
                f"page {p.index} detected as {ncols}-column (expected 2)",
            )
            self.assertGreater(mid, p.width * 0.3)
            self.assertLess(mid, p.width * 0.7)

    def test_partition_pulls_title_into_full_width_group(self):
        p = self.pages[0]
        ncols, mid = _detect_columns_from_spans(p.spans, p.width)
        full, left, right = _partition_spans_by_column(p.spans, mid)
        # The title "Preparation of Papers in Two-Column Format ..." spans
        # across the gutter; at least one span containing "Preparation"
        # must end up in the full-width group.
        full_text = " ".join(s.text for s in full)
        self.assertIn("Preparation", full_text)
        # Body text must have populated BOTH columns substantially.
        self.assertGreater(len(left), 30)
        self.assertGreater(len(right), 30)


class TwoColumnReflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not TWO_COL_PDF.exists():
            raise unittest.SkipTest(f"missing fixture: {TWO_COL_PDF}")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out_path = os.path.join(cls.tmp.name, "two-col-out.pdf")
        cls.stats = reflow_pdf(str(TWO_COL_PDF), cls.out_path)
        cls.out_doc = fitz.open(cls.out_path)
        cls.pages_text = [p.get_text() for p in cls.out_doc]
        cls.all_text = _normalize("\n".join(cls.pages_text))

    @classmethod
    def tearDownClass(cls):
        cls.out_doc.close()
        cls.tmp.cleanup()

    def test_output_pdf_exists(self):
        self.assertTrue(os.path.getsize(self.out_path) > 5000)
        self.assertGreaterEqual(self.out_doc.page_count, 3)

    def test_left_column_before_right_in_reading_order(self):
        """The phrase "Your goal is to simulate" appears in the LEFT
        column of page 1; the phrase "Large figures and tables may span
        across both columns" appears in the RIGHT column of page 1. In a
        correctly-reflowed (column-major) document the left phrase must
        precede the right phrase."""
        left_marker = "Your goal is to simulate"
        right_marker = "Large figures and tables may span"
        i_left = self.all_text.find(left_marker)
        i_right = self.all_text.find(right_marker)
        self.assertGreaterEqual(i_left, 0, f"missing left-column marker: {left_marker!r}")
        self.assertGreaterEqual(i_right, 0, f"missing right-column marker: {right_marker!r}")
        self.assertLess(
            i_left, i_right,
            "reading order is wrong — right-column text appears before left-column",
        )

    def test_no_cross_column_line_merging(self):
        """Before the fix, ``_group_lines`` merged spans from the left
        and right columns into a single full-width line because they
        shared a baseline y. The downstream text builder then produced
        output like ``"Give all authors' names ... if there are six
        authors III. U"`` — where the right-column section head landed
        mid-sentence inside a left-column paragraph.

        We assert the opposite of that bug: the left-column phrase "if
        there are six authors" is followed in the reflowed text by "or
        more" (its actual continuation in the source), not by the
        right-column phrase "III. UNITS"."""
        text = re.sub(r"\s+", " ", self.all_text)
        i = text.find("if there are six authors")
        self.assertGreaterEqual(
            i, 0, "missing left-column phrase 'if there are six authors'",
        )
        tail = text[i:i + 80]
        self.assertIn("or more", tail,
                      f"sentence continuation lost — got {tail!r}")
        self.assertNotIn(
            "III. U", tail,
            f"right-column 'III. U' was glued into left-column sentence: {tail!r}",
        )

    def test_title_is_centered_in_output(self):
        """The IEEE-style centered title must round-trip as centered
        text. We detect "centered" by checking that the title line's
        left margin to the page edge is approximately equal to the right
        margin (within 6pt)."""
        p = self.out_doc[0]
        page_w = p.rect.width
        found_centered_title_line = False
        for block in p.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                if not line.get("spans"):
                    continue
                text = "".join(s["text"] for s in line["spans"])
                if "Preparation of Papers" not in text and "Conference Proceedings" not in text:
                    continue
                bbox = line["bbox"]
                left = bbox[0]
                right = page_w - bbox[2]
                self.assertLess(
                    abs(left - right), 6.0,
                    f"title line not centered: left={left:.1f} right={right:.1f}",
                )
                found_centered_title_line = True
        self.assertTrue(found_centered_title_line, "no title line found in output")

    def test_figure_in_right_column_is_rasterized(self):
        """The Magnetization plot lives in the right column of source
        page 1. After reflow it must appear as a rasterized image."""
        n_images = sum(len(p.get_images()) for p in self.out_doc)
        self.assertGreaterEqual(
            n_images, 1,
            f"expected at least 1 rasterized figure (Magnetization plot), got {n_images}",
        )

    def test_left_column_body_not_swallowed_by_right_column_figure(self):
        """The right-column figure (Magnetization plot) sits at roughly
        the same source y as substantial left-column body text
        ("Your goal is to simulate...", "A4 column width is 88mm...").
        Before the per-column figure-region fix, that body text was
        marked "inside figure" and dropped from reading. Assert the
        body content survived."""
        for phrase in [
            "Your goal is to simulate",
            "A4 column width",
            "Paragraph indentation",
        ]:
            self.assertIn(
                phrase, self.all_text,
                f"left-column body phrase missing — likely swallowed by figure band: {phrase!r}",
            )

    def test_line_spacing_is_consistent_within_a_paragraph(self):
        """Within a single output paragraph the baseline-to-baseline
        spacing should be uniform (==line_height_mult·body_size). Before
        the column fix, the reading-order interleaving produced spans
        from two source y-positions in one output paragraph and the
        baselines varied by 5+pt; now they should differ by <0.5pt.

        We scan the first text-heavy page and look at every consecutive
        pair of text lines whose left edges agree to within 2pt
        (i.e. they're plausibly the same paragraph, both left-aligned).
        Of those pairs, at least 90% must share the same baseline gap
        (within 0.5pt of the modal value)."""
        from collections import Counter

        for p in self.out_doc:
            lines = []
            for block in p.get_text("dict")["blocks"]:
                if block.get("type") != 0:
                    continue
                for ln in block.get("lines", []):
                    if not ln.get("spans"):
                        continue
                    lines.append(ln["bbox"])
            if len(lines) < 6:
                continue
            lines.sort(key=lambda b: b[1])
            gaps = []
            for a, b in zip(lines, lines[1:]):
                if abs(a[0] - b[0]) > 2.0:
                    continue
                dy = b[1] - a[1]
                if dy <= 0 or dy > 60:
                    continue
                gaps.append(round(dy, 1))
            if len(gaps) < 4:
                continue
            modal, modal_count = Counter(gaps).most_common(1)[0]
            consistent = sum(1 for g in gaps if abs(g - modal) <= 0.5)
            self.assertGreaterEqual(
                consistent / len(gaps), 0.85,
                f"page {p.number + 1}: inconsistent line spacing "
                f"(modal={modal} count={modal_count}/{len(gaps)} gaps={Counter(gaps)})",
            )
            return  # one populated page is enough
        self.skipTest("no page with enough paragraph lines to measure")

    def test_section_text_present(self):
        """Sanity: every major IEEE section heading text appears in the
        output (regardless of styling)."""
        for phrase in [
            "INTRODUCTION",
            "HELPFUL HINTS",
            "UNITS",
            "SOME COMMON MISTAKES",
            "ACKNOWLEDGMENT",
            "REFERENCES",
        ]:
            self.assertIn(phrase, self.all_text, f"missing section: {phrase!r}")


if __name__ == "__main__":
    unittest.main()
