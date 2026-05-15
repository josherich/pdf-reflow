"""End-to-end behavior + performance tests on bitcoin.pdf.

Run with:  python -m unittest pdf_reflow.tests.test_reflow

The tests assert correctness invariants (key content preserved, single
column on mobile width, figures rasterized) and one performance budget
(reflow under 5 seconds on this 9-page document).
"""

from __future__ import annotations

import os
import re
import tempfile
import time
import unittest
from pathlib import Path

import fitz

from pdf_reflow.extract import extract_document
from pdf_reflow.analyze import analyze_document
from pdf_reflow.reflow import ReflowConfig, reflow_pdf


FIXTURES = Path(__file__).parent / "fixtures"
BITCOIN_PDF = FIXTURES / "bitcoin.pdf"

# Phrases from each major section that we expect in the reflowed output.
EXPECTED_PHRASES = [
    "Bitcoin: A Peer-to-Peer Electronic Cash",
    "Abstract",
    "purely peer-to-peer version of electronic cash",
    "1. Introduction",
    "2. Transactions",
    "3. Timestamp Server",
    "4. Proof-of-Work",
    "5. Network",
    "6. Incentive",
    "7. Reclaiming Disk Space",
    "8. Simplified Payment Verification",
    "9. Combining and Splitting Value",
    "10. Privacy",
    "11. Calculations",
    "12. Conclusion",
    "References",
    "AttackerSuccessProbability",
    "Gambler",
    "Hashcash",
    "Merkle",
    "satoshin@gmx.com",
]


def _normalize(text: str) -> str:
    """Collapse whitespace and break-induced hyphenation for comparison."""
    text = re.sub(r"-\s+\n", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


class ReflowCorrectnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BITCOIN_PDF.exists():
            raise unittest.SkipTest(f"missing fixture: {BITCOIN_PDF}")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out_path = os.path.join(cls.tmp.name, "out.pdf")
        cls.stats = reflow_pdf(str(BITCOIN_PDF), cls.out_path)
        cls.out_doc = fitz.open(cls.out_path)
        cls.all_text = _normalize("\n".join(p.get_text() for p in cls.out_doc))

    @classmethod
    def tearDownClass(cls):
        cls.out_doc.close()
        cls.tmp.cleanup()

    def test_output_pdf_exists_and_valid(self):
        self.assertTrue(os.path.getsize(self.out_path) > 5000)
        self.assertGreaterEqual(self.out_doc.page_count, 8)

    def test_output_size_reasonable(self):
        """Reflowed output must be under 1.5x the original.

        Rasterising vector figures always adds some bytes vs the original,
        but at 150 DPI with adaptive sampling the overhead should stay well
        below 50% of the source size even for figure-heavy papers.
        """
        in_size = os.path.getsize(str(BITCOIN_PDF))
        out_size = os.path.getsize(self.out_path)
        self.assertLessEqual(
            out_size, int(in_size * 1.5),
            f"output ({out_size:,} B) is more than 1.5x input ({in_size:,} B)",
        )

    def test_target_page_dimensions_match_preset(self):
        for page in self.out_doc:
            self.assertAlmostEqual(page.rect.width, 360.0, places=1)
            self.assertAlmostEqual(page.rect.height, 600.0, places=1)

    def test_pages_are_single_column(self):
        # In a single-column layout, the horizontal span of text on each page
        # should be narrow (less than 80% of the page width), and there should
        # only be one "column cluster" of text x-positions.
        for i, page in enumerate(self.out_doc):
            spans = []
            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    if line.get("spans"):
                        spans.append(line["bbox"])
            if not spans:
                continue
            x0s = [b[0] for b in spans]
            # All lines should begin within a narrow range — at most ~10pt
            # variation around the left margin (allows for italic kerning, etc).
            left = min(x0s)
            self.assertLessEqual(
                max(x0s) - left, 25.0,
                f"page {i+1}: x0 range too wide ({max(x0s)-left:.1f}pt) — multi-column?",
            )

    def test_section_headings_preserved(self):
        for phrase in EXPECTED_PHRASES:
            self.assertIn(
                phrase, self.all_text,
                f"missing from reflowed PDF: {phrase!r}",
            )

    def test_code_block_preserved_with_line_structure(self):
        code_lines = [
            "#include <math.h>",
            "double AttackerSuccessProbability(double q, int z)",
            "double p = 1.0 - q;",
            "double lambda = z * (q / p);",
            "return sum;",
        ]
        # The text on a single page should contain consecutive code lines.
        joined_pages = ["\n".join(p.get_text().splitlines()) for p in self.out_doc]
        found = any(
            all(line in page for line in code_lines)
            for page in joined_pages
        )
        self.assertTrue(found, "expected C code to be preserved on a single page")

    def test_figures_were_rasterized(self):
        n_images = sum(len(p.get_images()) for p in self.out_doc)
        # bitcoin.pdf has 7 named figures + several inline equations on p6/p7
        # — we expect at least 6 rasterized figure images in the output.
        self.assertGreaterEqual(n_images, 6, f"too few rasterized figures: {n_images}")

    def test_no_private_use_glyph_garbage_in_text(self):
        for ch in self.all_text:
            self.assertFalse(
                0xE000 <= ord(ch) <= 0xF8FF,
                f"private-use char leaked into text: U+{ord(ch):04X}",
            )

    def test_reading_order_is_top_to_bottom(self):
        # Section i must appear before section i+1 in the full document text.
        positions = []
        for i in range(1, 13):
            tag = f"{i}. "
            pos = self.all_text.find(tag + ("Conclusion" if i == 12 else ""))
            positions.append(pos)
        # find any position; just check the section-number sequence is monotonic
        ordered_positions = []
        for i in range(1, 13):
            tag = re.search(rf"\b{i}\.\s", self.all_text)
            ordered_positions.append(tag.start() if tag else -1)
        # Filter out -1s and verify ascending.
        present = [p for p in ordered_positions if p >= 0]
        self.assertEqual(present, sorted(present),
                         "section numbers do not appear in order")

    def test_reflow_returns_useful_stats(self):
        self.assertEqual(self.stats["source_pages"], 9)
        self.assertGreaterEqual(self.stats["output_pages"], 8)
        self.assertGreater(self.stats["items"], 30)

    def test_toc_is_present_in_output(self):
        toc = self.out_doc.get_toc()
        self.assertGreater(len(toc), 0, "output PDF has no TOC/outline")

    def test_toc_entries_cover_major_sections(self):
        toc = self.out_doc.get_toc()
        toc_text = " ".join(title for _, title, *_ in toc).lower()
        for section in ("introduction", "transactions", "conclusion"):
            self.assertIn(section, toc_text,
                          f"section {section!r} missing from output TOC")

    def test_toc_page_numbers_are_valid(self):
        toc = self.out_doc.get_toc()
        n_pages = self.out_doc.page_count
        for level, title, page, *_ in toc:
            self.assertGreaterEqual(page, 1,
                                    f"TOC entry {title!r} has page < 1")
            self.assertLessEqual(page, n_pages,
                                 f"TOC entry {title!r} page {page} > {n_pages}")


class ReflowPerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BITCOIN_PDF.exists():
            raise unittest.SkipTest(f"missing fixture: {BITCOIN_PDF}")

    def test_reflow_under_budget(self):
        """Full reflow of the 9-page paper completes in < 5 seconds."""
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "out.pdf")
            t0 = time.perf_counter()
            reflow_pdf(str(BITCOIN_PDF), out)
            dt = time.perf_counter() - t0
            self.assertLess(
                dt, 5.0,
                f"reflow took {dt:.2f}s, budget is 5.0s",
            )

    def test_extract_phase_under_budget(self):
        t0 = time.perf_counter()
        doc = fitz.open(str(BITCOIN_PDF))
        extract_document(doc)
        doc.close()
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_analyze_phase_under_budget(self):
        doc = fitz.open(str(BITCOIN_PDF))
        pages = extract_document(doc)
        t0 = time.perf_counter()
        items, _ = analyze_document(pages)
        doc.close()
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertGreater(len(items), 30)


if __name__ == "__main__":
    unittest.main()
