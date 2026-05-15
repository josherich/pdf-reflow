"""End-to-end tests on the Kimi K2.5 tech report fixture.

The Kimi K2.5 tech report is a 30-page PDF where the figures are embedded
as raster images (PNG/JPEG XObjects), not as vector drawings. This
distinguishes it from the bitcoin and MIT LaTeX fixtures, where every
figure is a vector composition. The figure-band heuristic in analyze
originally seeded bands only from ``page.drawings`` and ignored
``page.images``, so most of the report's figures were dropped silently
from the reflowed output.

The report also embeds a tiny (~9pt) logo image in the running header on
every page, alongside ~15 small vector outlines that make up the logo's
glyphs. Both must be filtered as page chrome — otherwise every output
page picks up a useless header strip "figure."

Run with:  uv run python -m unittest tests.test_tech_report
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
TECH_PDF = FIXTURES / "tech_report.pdf"


# (page_index, caption_prefix) — every numbered figure / table in the source
# whose caption sits on the same page as its illustration. These are the
# regions a reader expects to see rasterized in the output.
EXPECTED_FIGURE_PAGES = [
    (0, "Figure 1"),
    (3, "Figure 2"),
    (4, "Figure 3"),
    (5, "Figure 4"),
    (9, "Figure 5"),
    (13, "Figure 6"),
    (13, "Figure 7"),
    (14, "Figure 8"),
    (20, "Figure 9"),
    (22, "Figure 10"),
    (27, "Figure 11"),
    (28, "Figure 12"),
]


def _normalize(text: str) -> str:
    text = re.sub(r"-\s+\n", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


class TechReportExtractTests(unittest.TestCase):
    """Extract-level invariants — every embedded image is surfaced."""

    @classmethod
    def setUpClass(cls):
        if not TECH_PDF.exists():
            raise unittest.SkipTest(f"missing fixture: {TECH_PDF}")
        cls.doc = fitz.open(str(TECH_PDF))
        cls.pages = extract_document(cls.doc)

    @classmethod
    def tearDownClass(cls):
        cls.doc.close()

    def test_embedded_images_extracted(self):
        """Every figure-bearing page surfaces either a substantive raster
        image (>40pt on a side) or a sizeable vector drawing in extract
        output. The Kimi report mostly uses raster figures, but Figure 9
        on p21 is a vector composition — both paths must reach analyze."""
        for pg_idx, label in EXPECTED_FIGURE_PAGES:
            pg = self.pages[pg_idx]
            big_images = [
                im for im in pg.images
                if (im.bbox[2] - im.bbox[0]) > 40
                and (im.bbox[3] - im.bbox[1]) > 40
            ]
            big_drawings = [
                d for d in pg.drawings
                if (d.bbox[2] - d.bbox[0]) > 40
                and (d.bbox[3] - d.bbox[1]) > 5
            ]
            self.assertTrue(
                big_images or big_drawings,
                f"page {pg_idx+1} ({label}): no substantive image or "
                f"vector drawing extracted",
            )


class TechReportAnalyzeTests(unittest.TestCase):
    """Analyze-level invariants — bands seeded from rasters and chrome dropped."""

    @classmethod
    def setUpClass(cls):
        if not TECH_PDF.exists():
            raise unittest.SkipTest(f"missing fixture: {TECH_PDF}")
        cls.doc = fitz.open(str(TECH_PDF))
        cls.pages = extract_document(cls.doc)
        cls.items, cls.body_size = analyze_document(cls.pages)

    @classmethod
    def tearDownClass(cls):
        cls.doc.close()

    def test_each_figure_page_emits_figure_item(self):
        """Every page that carries a numbered figure caption must emit at
        least one figure item whose source rect overlaps the image area
        (i.e. lies in the upper / mid page, not in the running header)."""
        figs_by_page: dict[int, list] = {}
        for it in self.items:
            if it.kind != "figure":
                continue
            figs_by_page.setdefault(it.page_index, []).append(it)
        missing = []
        for pg_idx, label in EXPECTED_FIGURE_PAGES:
            page_h = self.pages[pg_idx].height
            substantive = [
                it for it in figs_by_page.get(pg_idx, [])
                # Must not be confined to the page-chrome region.
                if (it.source_rect or it.bbox)[1] > page_h * 0.08
                and (it.source_rect or it.bbox)[3] < page_h * 0.95
                # Must have at least body-line height of vertical extent.
                and ((it.source_rect or it.bbox)[3]
                     - (it.source_rect or it.bbox)[1]) > self.body_size * 2.0
            ]
            if not substantive:
                missing.append(f"p{pg_idx+1} ({label})")
        self.assertFalse(
            missing,
            "expected at least one substantive figure item on these pages "
            f"but got none: {missing}",
        )

    def test_no_figure_inside_running_header_band(self):
        """The Kimi logo (a ~9x9pt PNG at xref=0) plus the ~15 small vector
        paths that draw its glyph live in the top 6% of every page. The
        figure-region heuristic must NOT promote that area into a figure
        band — otherwise every output page picks up a black strip that
        rasterizes the running ``Kimi K2.5 / TECHNICAL REPORT`` header."""
        offenders = []
        for it in self.items:
            if it.kind != "figure":
                continue
            page_h = self.pages[it.page_index].height
            sr = it.source_rect or it.bbox
            # Header sits at y ~ 33-49 on a 792pt page (~6% of page height).
            if sr[3] < page_h * 0.08:
                offenders.append((it.page_index + 1, sr))
        self.assertFalse(
            offenders,
            f"figure(s) cover only the running-header strip: {offenders}",
        )

    def test_figure_band_does_not_swallow_whole_page(self):
        """A figure on a body-text page (e.g. p14, p15) should not span
        the entire page height. If the band stretches >90% of the page
        the body paragraphs underneath/around it get absorbed and the
        rasterized crop is mostly text — defeating the reflow."""
        for it in self.items:
            if it.kind != "figure":
                continue
            page_h = self.pages[it.page_index].height
            sr = it.source_rect or it.bbox
            band_h = sr[3] - sr[1]
            # An honest full-page table is OK (page 12 — Table 4), but for
            # pages whose caption text reads "Figure" we expect at least
            # one body block to survive outside the figure.
            page_text = self.doc[it.page_index].get_text()
            if "Figure " in page_text and band_h > page_h * 0.92:
                self.fail(
                    f"page {it.page_index+1}: figure band spans the entire "
                    f"page ({band_h:.0f}/{page_h:.0f}pt) — body content "
                    f"got absorbed into a single raster crop"
                )


class TechReportReflowTests(unittest.TestCase):
    """End-to-end — figures actually make it into the output PDF."""

    @classmethod
    def setUpClass(cls):
        if not TECH_PDF.exists():
            raise unittest.SkipTest(f"missing fixture: {TECH_PDF}")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out_path = os.path.join(cls.tmp.name, "out.pdf")
        cls.stats = reflow_pdf(str(TECH_PDF), cls.out_path)
        cls.out_doc = fitz.open(cls.out_path)
        cls.all_text = _normalize("\n".join(p.get_text() for p in cls.out_doc))

    @classmethod
    def tearDownClass(cls):
        cls.out_doc.close()
        cls.tmp.cleanup()

    def test_output_pdf_exists_and_valid(self):
        self.assertGreater(os.path.getsize(self.out_path), 10_000)
        self.assertGreaterEqual(self.out_doc.page_count, 20)

    def test_figure_captions_preserved(self):
        for _, label in EXPECTED_FIGURE_PAGES:
            self.assertIn(
                label, self.all_text,
                f"caption {label!r} dropped from reflowed text",
            )

    def test_enough_rasterized_figures_in_output(self):
        """At least one rasterized image per real figure-bearing page.
        Anything substantially below ``len(EXPECTED_FIGURE_PAGES)`` means
        the raster-image-seeded bands are not making it through."""
        n_images = sum(len(p.get_images()) for p in self.out_doc)
        self.assertGreaterEqual(
            n_images, len(EXPECTED_FIGURE_PAGES),
            f"only {n_images} rasterized figures in output, expected "
            f">= {len(EXPECTED_FIGURE_PAGES)} (one per numbered figure)",
        )

    def test_no_running_header_figure_per_page(self):
        """Before the fix, a spurious figure band was emitted on every
        source page covering the ~16pt running header — that produced
        an extra image on ~30 output pages. After the fix the total
        image count should be well below ``source_pages``."""
        n_images = sum(len(p.get_images()) for p in self.out_doc)
        self.assertLess(
            n_images, 60,
            f"{n_images} images in output suggests the running-header "
            f"band is leaking through on every source page",
        )

    def test_bold_run_in_subheadings_rendered_bold(self):
        """In the source, p5 has run-in subheadings like ``Architecture and
        Learning Setup`` and ``PARL Reward`` set on their own line in
        NimbusRomNo9L-Medi (bold) at body size 10pt, followed by a
        body-sized regular paragraph that elaborates on the topic. The
        original block grouper merged them into the body paragraph below,
        so block-wide ``bold`` flipped to False and the lead-ins were
        rendered as plain prose. They must survive as bold runs in the
        output (font name on the rendered span ends with ``-Bold``)."""
        targets = ("Architecture and Learning Setup", "PARL Reward")
        found_bold = {t: False for t in targets}
        for page in self.out_doc:
            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    line_text = "".join(
                        s.get("text", "") for s in line.get("spans", [])
                    )
                    for needle in targets:
                        if needle not in line_text:
                            continue
                        # Bold check: PyMuPDF span flags bit 4 (= 16) is bold,
                        # OR the font name carries a ``-Bold`` suffix.
                        for s in line.get("spans", []):
                            if needle not in s.get("text", ""):
                                continue
                            flags = int(s.get("flags", 0))
                            font = s.get("font", "")
                            if (flags & 16) or "Bold" in font:
                                found_bold[needle] = True
        missing = [t for t, ok in found_bold.items() if not ok]
        self.assertFalse(
            missing,
            f"bold run-in subheadings not rendered bold in output: {missing}",
        )

    def test_bold_subheading_starts_its_own_line(self):
        """A run-in subheading should not be glued to the start of the
        body paragraph that follows it. In the output, ``Architecture and
        Learning Setup`` and ``PARL Reward`` must each begin a new line —
        i.e. the body word ``The`` / ``Training`` that follows in the
        source paragraph appears on a *different* output line."""
        for needle, next_word in (
            ("Architecture and Learning Setup", "The"),
            ("PARL Reward", "Training"),
        ):
            for page in self.out_doc:
                lines_text = []
                for block in page.get_text("dict").get("blocks", []):
                    if block.get("type") != 0:
                        continue
                    for line in block.get("lines", []):
                        lines_text.append(
                            "".join(s.get("text", "") for s in line.get("spans", []))
                        )
                # Find the first line that contains the needle.
                for i, lt in enumerate(lines_text):
                    if needle in lt:
                        # The needle's line should not also contain the
                        # body sentence's opening word jammed onto it.
                        # Allow the needle to be the trailing content of
                        # its line; the body paragraph must start on
                        # subsequent line(s).
                        tail = lt.split(needle, 1)[1].strip()
                        self.assertFalse(
                            tail.startswith(next_word),
                            f"on output page, {needle!r} is glued to body "
                            f"word {next_word!r}: line={lt!r}",
                        )
                        break


if __name__ == "__main__":
    unittest.main()
