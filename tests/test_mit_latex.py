"""End-to-end tests on the MIT 18.821 LaTeX sample PDF.

This sample is a pdfLaTeX document with Computer Modern (CMR/CMMI/CMSY/CMEX)
fonts and an embedded vector figure. It exercises:

  - LaTeX ligatures (``ﬁ``/``ﬂ``) that don't exist in base14 Times — must
    be normalized to ``fi``/``fl`` for the output font, not rendered as a
    missing-glyph fallback (which historically came out as middle-dots).

  - Computer Modern math fonts (CMMI / CMSY / CMEX / MSBM / MSAM) — math
    glyphs here use real Unicode codepoints (π, ℵ, ∞, −), NOT the Private
    Use Area. Equation detection that only looks at PUA misses them, so
    display equations leak into prose as gibberish.

  - Inline word fusion when LaTeX shifts the baseline between adjacent
    spans ("LaTeX" small caps inside a body line) — the inter-span gap is
    smaller than the original 0.3·size threshold, so words merge.

  - A vector figure (an ODE phase-plane plot) on page 2 must be
    rasterized into the output PDF.

  - Page-edge footnote hairlines (a short horizontal rule above the
    "Date: …" line on page 1) must not be promoted to figure bands and
    bridge into the running footer.

Run with:  uv run python -m unittest tests.test_mit_latex
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

import fitz

from pdf_reflow.extract import extract_document
from pdf_reflow.analyze import analyze_document
from pdf_reflow.reflow import reflow_pdf


FIXTURES = Path(__file__).parent / "fixtures"
MIT_PDF = FIXTURES / "mit_latex_sample.pdf"


def _normalize(text: str) -> str:
    text = re.sub(r"-\s+\n", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


class MITLatexReflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not MIT_PDF.exists():
            raise unittest.SkipTest(f"missing fixture: {MIT_PDF}")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out_path = os.path.join(cls.tmp.name, "out.pdf")
        cls.stats = reflow_pdf(str(MIT_PDF), cls.out_path)
        cls.out_doc = fitz.open(cls.out_path)
        cls.all_text = _normalize("\n".join(p.get_text() for p in cls.out_doc))

    @classmethod
    def tearDownClass(cls):
        cls.out_doc.close()
        cls.tmp.cleanup()

    # -- structural sanity ----------------------------------------------------

    def test_output_pdf_exists_and_valid(self):
        self.assertTrue(os.path.getsize(self.out_path) > 5000)
        self.assertGreaterEqual(self.out_doc.page_count, 2)

    def test_target_page_dimensions_match_preset(self):
        for page in self.out_doc:
            self.assertAlmostEqual(page.rect.width, 360.0, places=1)
            self.assertAlmostEqual(page.rect.height, 600.0, places=1)

    # -- LaTeX text rendering -------------------------------------------------

    def test_no_ligature_artifacts_in_text(self):
        """``ﬁ`` / ``ﬂ`` ligatures should be normalized, not rendered as
        the middle-dot fallback that base14 Times produces for missing
        glyphs."""
        # Key words from the PDF that contain ﬁ / ﬂ ligatures in the
        # source ("defined", "figure", "file", "first", "floating",
        # "Unfinished"). After fix they should appear with regular ASCII
        # ``fi`` / ``fl`` so search works.
        # Note: "first" only appears inside the page-2 figure caption,
        # which is rasterized as part of the figure band, so we don't
        # check it here. These five words all live in body prose.
        for needle in ("defined", "figure", "file", "files", "floating"):
            self.assertIn(
                needle, self.all_text,
                f"missing word with ligature: {needle!r} — the ﬁ/ﬂ ligature "
                f"was likely dropped or rendered as a missing-glyph fallback.",
            )
        # No raw ligature codepoints should remain.
        for ch in ("ﬁ", "ﬂ", "ﬀ", "ﬃ", "ﬄ"):
            self.assertNotIn(
                ch, self.all_text,
                f"raw ligature codepoint U+{ord(ch):04X} leaked into text",
            )

    def test_no_word_fusion_across_spans(self):
        """A line like ``pre-defined either in LaTeX or in the AMS package``
        comes from PyMuPDF as ~15 spans with small gaps; the line builder
        must add spaces between them."""
        # ``pre-defined either in LaTeX or in`` should appear with spaces.
        self.assertRegex(
            self.all_text,
            r"pre-defined\s+either\s+in\s+L",
            msg="adjacent spans got fused without spaces (line-building bug)",
        )
        self.assertRegex(
            self.all_text,
            r"in\s+the\s+AMS\s+package",
            msg="words inside math-mixed body line lost their inter-word spaces",
        )

    def test_no_private_use_glyph_garbage_in_text(self):
        for ch in self.all_text:
            self.assertFalse(
                0xE000 <= ord(ch) <= 0xF8FF,
                f"private-use char leaked into text: U+{ord(ch):04X}",
            )

    # -- figure & equation rasterization --------------------------------------

    def test_ode_phase_plane_figure_was_rasterized(self):
        """The vector figure on page 2 ("My first .pdf figure", an ODE
        phase-plane plot) is the only big drawing in the source and MUST
        appear as a rasterized image somewhere in the output."""
        n_images = sum(len(p.get_images()) for p in self.out_doc)
        self.assertGreaterEqual(
            n_images, 1,
            f"no figure rasterized at all ({n_images} images)",
        )
        # Caption text must be preserved adjacent to the figure.
        self.assertIn("Figure 1", self.all_text)

    def test_display_equations_are_rasterized(self):
        """The PDF has at least three display equations on page 1 (an
        integral, a series, and a more complex fraction) and one numbered
        display equation on page 2 (``lim_{n→∞} Σ 1/k² = π²/6``).

        Detection used to rely solely on Unicode Private Use Area
        characters, which Computer Modern math fonts never use. The fix
        adds math-font-name detection (CMMI / CMSY / CMEX / MSBM / …) so
        these display equations seed figure bands and get rasterized."""
        n_images = sum(len(p.get_images()) for p in self.out_doc)
        # ODE plot (1) + numbered display equation on page 2 (1) +
        # at least 2 distinct display-equation rasters from page 1.
        self.assertGreaterEqual(
            n_images, 4,
            f"only {n_images} images in output — display equations were "
            f"probably not detected and got rendered as garbled prose.",
        )
        # The mangled inline rendering of the page-1 display equation
        # used to leak fragments like ``Xi=−`` or ``1−1 + 1−·· ·= .`` into
        # body text. After fix the equation block should be a figure, so
        # those exact mangled sequences should not appear in the output
        # text stream.
        self.assertNotRegex(
            self.all_text, r"Xi=[\-−]",
            msg="display equation 'Σ Xi=−...' got rendered as inline text "
                "instead of rasterized as a figure",
        )

    def test_running_header_dropped(self):
        """``2 X. BURPS, P. GURPS`` is the running header on page 2 (and
        ``THE 18.821 REPORT 3`` on page 3). They must be filtered out as
        page chrome, not absorbed into figure bands or echoed as
        body text."""
        # Page numbers / running-header pair appears in the source as
        # ``2 X. BURPS, P. GURPS`` (centered, smaller than body). It
        # should NOT appear in the reflowed output.
        self.assertNotIn(
            "2 X. BURPS, P. GURPS", self.all_text,
            "running header 'N X. BURPS, P. GURPS' leaked into output",
        )

    def test_no_extreme_image_upscale(self):
        """A small equation source rect (e.g. ``y = mx + c = 4x − 9.``
        at ~60×30pt on the source page) used to be stretched to the full
        column width — a 5x blow-up that filled 150pt+ of vertical space
        with two glyphs. layout caps the figure upscale at 2x so small
        equations stay legible without dominating the page."""
        from pdf_reflow.extract import extract_document as _ext
        from pdf_reflow.analyze import analyze_document as _ana
        doc = fitz.open(str(MIT_PDF))
        try:
            items, _ = _ana(_ext(doc))
        finally:
            doc.close()
        fig_source_widths = [
            it.source_rect[2] - it.source_rect[0]
            for it in items
            if it.kind == "figure" and it.source_rect is not None
        ]
        out_widths: list = []
        for page in self.out_doc:
            for img_xref in (img[0] for img in page.get_images()):
                for r in page.get_image_rects(img_xref):
                    out_widths.append(r.width)
        self.assertEqual(
            len(fig_source_widths), len(out_widths),
            f"figure-item count {len(fig_source_widths)} != "
            f"output-image count {len(out_widths)}",
        )
        for sw, ow in zip(fig_source_widths, out_widths):
            scale = ow / sw if sw > 0 else 1.0
            self.assertLessEqual(
                scale, 2.5,
                f"figure scaled {scale:.2f}x (src_w={sw:.0f} → "
                f"out_w={ow:.0f}) — small equation got blown up",
            )

    def test_page_one_footer_not_a_figure(self):
        """Page 1's footnote rule (a single short horizontal hairline above
        ``Date: February 10, 2013.``) used to merge with the display-
        equation fraction bars, producing a huge bogus figure band that
        captured the page footer (date + page number) as an image and
        dropped them from the text stream."""
        # Indirect check: the date and ``ocw.mit.edu`` references should
        # appear as TEXT, not be locked inside a rasterized footer strip.
        self.assertIn("ocw.mit.edu", self.all_text)
        # Indirect check on the analyze pipeline: no figure should have
        # its source rect entirely inside the source-page footer region
        # (bottom 15% of the source page).
        from pdf_reflow.extract import extract_document as _ext
        from pdf_reflow.analyze import analyze_document as _ana
        doc = fitz.open(str(MIT_PDF))
        try:
            pages = _ext(doc)
            items, _ = _ana(pages)
        finally:
            doc.close()
        bad = []
        for it in items:
            if it.kind != "figure":
                continue
            page_h = pages[it.page_index].height
            sr = it.source_rect or it.bbox
            # The date / page-number band on page 1 sits at y/page_h > 0.88.
            # A figure source rect that extends into that band has absorbed
            # the page chrome and is by definition spurious.
            if sr[3] > page_h * 0.90:
                bad.append((it.page_index, sr, page_h))
        self.assertFalse(
            bad,
            f"figure(s) extend into the page footer region: {bad}",
        )


class MITLatexAnalyzeTests(unittest.TestCase):
    """Unit-level checks on the analyze pipeline for the MIT sample."""

    @classmethod
    def setUpClass(cls):
        if not MIT_PDF.exists():
            raise unittest.SkipTest(f"missing fixture: {MIT_PDF}")
        cls.doc = fitz.open(str(MIT_PDF))
        cls.pages = extract_document(cls.doc)
        cls.items, cls.body_size = analyze_document(cls.pages)

    @classmethod
    def tearDownClass(cls):
        cls.doc.close()

    def test_body_size_inferred(self):
        # Source uses 12pt Computer Modern Roman for body.
        self.assertAlmostEqual(self.body_size, 12.0, places=0)

    def test_at_least_one_figure_item_per_equation_rich_page(self):
        """Page 1 has display equations; page 2 has a figure AND a
        numbered display equation. Both pages should emit ``figure``
        items after analysis."""
        figs_by_page = {}
        for it in self.items:
            if it.kind == "figure":
                figs_by_page.setdefault(it.page_index, 0)
                figs_by_page[it.page_index] += 1
        # Page 1's three display equations sit close together vertically
        # and are merged into a single figure band — that's correct, the
        # output figure shows all three stacked.
        self.assertGreaterEqual(
            figs_by_page.get(0, 0), 1,
            f"page 1 should emit ≥1 figure item for its display "
            f"equation cluster, got {figs_by_page.get(0, 0)}",
        )
        # Page 2 has the ODE phase-plane plot, a numbered display
        # equation `lim Σ 1/k² = π²/6`, AND an aligned-equation block
        # `y = mx + c = 4x − 9.` — three separate figures, each
        # separated by intervening body prose.
        self.assertGreaterEqual(
            figs_by_page.get(1, 0), 3,
            f"page 2 should emit ≥3 figure items (ODE plot + numbered "
            f"display equation + aligned equation), got {figs_by_page.get(1, 0)}",
        )

    def test_no_subbaseline_fragment_figure(self):
        """An ``equation`` block consisting only of subscript / superscript
        bounds (e.g. ``i=1 0`` at size 8 sitting under an inline ``∫``
        and ``∑`` in the body line above) is a sub-baseline fragment with
        no usable standalone visual. Rasterizing it produces a few-px-tall
        sliver that gets scaled up to fill the column. analyze must drop
        these rather than emit them as figure items."""
        for it in self.items:
            if it.kind != "figure":
                continue
            sr = it.source_rect or it.bbox
            h = sr[3] - sr[1]
            w = sr[2] - sr[0]
            self.assertGreaterEqual(
                h, self.body_size,
                f"figure source rect is sub-baseline thin (h={h:.1f}, "
                f"w={w:.1f}, body={self.body_size:.1f}): {sr}",
            )

    def test_ode_figure_does_not_swallow_following_equation(self):
        """The ODE plot (drawings end at y≈342) and the lim-Σ display
        equation (y≈363) are separated by the body line ``If you want a
        number for an equation, do it like this:``. Their figure bands
        must NOT be merged across that body paragraph — otherwise the
        single rasterization spans almost the whole page and dwarfs both
        figures."""
        page2_figs = [it for it in self.items if it.kind == "figure" and it.page_index == 1]
        # Find a figure whose source rect covers the ODE plot's y range.
        ode_figs = [
            it for it in page2_figs
            if (it.source_rect or it.bbox)[1] < 200
        ]
        self.assertTrue(ode_figs, "no ODE-plot figure on page 2")
        for it in ode_figs:
            sr = it.source_rect or it.bbox
            self.assertLess(
                sr[3], 360,  # ODE drawings end at ~342; allow small margin
                f"ODE plot figure source rect bridged into the lim-Σ "
                f"display equation below the figure caption: {sr}",
            )


if __name__ == "__main__":
    unittest.main()
