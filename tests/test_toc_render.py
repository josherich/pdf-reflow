"""TOC fixture: each TOC entry must render on its own line with the
title left, page number right, dot leader between — no wrapped/merged
entries.

Fixture: tests/fixtures/TABLE-OF-CONTENTS-SP2018.pdf — a single page
with an "EXAMPLE OF TABLE OF CONTENTS" header, then 18 TOC entries
(roman + arabic page numbers, three indent levels) followed by a
bullet-point notes block.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

import fitz

from pdf_reflow.analyze import (
    _line_is_toc_entry,
    _parse_toc_entry,
    analyze_document,
)
from pdf_reflow.extract import extract_document
from pdf_reflow.reflow import ReflowConfig, reflow_pdf


FIXTURES = Path(__file__).parent / "fixtures"
TOC_PDF = FIXTURES / "TABLE-OF-CONTENTS-SP2018.pdf"

EXPECTED_ENTRIES = [
    ("ABSTRACT", "ii"),
    ("ACKNOWLEDGEMENTS", "iii"),
    ("DEDICATION", "iv"),
    ("LIST OF TABLES", "vii"),
    ("LIST OF FIGURES", "viii"),
    ("Chapter 1 - Title of Chapter 1", "1"),
    ("Chapter 2 - Title of Chapter 2", "5"),
    ("Subheading 1 of Chapter 2", "7"),
    ("Subheading 2 of Chapter 2", "8"),
    ("Chapter 3 - Title of Chapter 3", "11"),
    ("Subheading 1 of Chapter 3", "12"),
    ("Subheading 1 of Subheading 1 of Chapter 3", "12"),
    ("Subheading 2 of Subheading 1 of Chapter 3", "14"),
    ("Subheading 2 of Chapter 3", "15"),
    ("Subheading 3 of Chapter 3", "16"),
    ("Subheading 4 of Chapter 3", "18"),
    ("Chapter 4 - Title of Chapter 4", "20"),
    ("Chapter 5 - Title of Chapter 5", "28"),
]


class TocParseUnitTests(unittest.TestCase):
    """Unit tests for the TOC line detector and parser."""

    def test_line_is_toc_entry_basic(self):
        self.assertTrue(_line_is_toc_entry(_FakeLine("ABSTRACT .................. ii")))
        self.assertTrue(_line_is_toc_entry(_FakeLine("Chapter 1 ........ 1")))
        self.assertTrue(_line_is_toc_entry(_FakeLine("Some title ...... 123")))

    def test_line_is_toc_entry_negative(self):
        # No dot leader.
        self.assertFalse(_line_is_toc_entry(_FakeLine("Just a sentence.")))
        # Ends with period but no page number.
        self.assertFalse(_line_is_toc_entry(_FakeLine("End of line ....")))
        # Plain prose with one period inside.
        self.assertFalse(_line_is_toc_entry(_FakeLine("See section 2. It explains x.")))

    def test_parse_toc_entry_splits_title_and_page(self):
        self.assertEqual(
            _parse_toc_entry("Chapter 2 - Title of Chapter 2 ........... 5"),
            ("Chapter 2 - Title of Chapter 2", "5"),
        )
        self.assertEqual(
            _parse_toc_entry("ABSTRACT ......... ii"),
            ("ABSTRACT", "ii"),
        )

    def test_parse_toc_entry_returns_none_for_nonmatch(self):
        self.assertIsNone(_parse_toc_entry("Plain body text without leader"))


class _FakeLine:
    """Tiny stand-in for analyze.Line — only ``.text`` is read by the
    TOC detector, so we don't need real spans."""
    def __init__(self, text: str):
        self.text = text


class TocAnalysisTests(unittest.TestCase):
    """Per-block classification: every TOC entry on the fixture page is
    isolated as a single-line ``kind='toc'`` FlowItem."""

    @classmethod
    def setUpClass(cls):
        if not TOC_PDF.exists():
            raise unittest.SkipTest(f"missing fixture: {TOC_PDF}")
        doc = fitz.open(str(TOC_PDF))
        try:
            cls.pages = extract_document(doc)
            cls.items, cls.body_size = analyze_document(cls.pages)
        finally:
            doc.close()

    def test_each_expected_entry_is_its_own_toc_item(self):
        toc_items = [it for it in self.items if it.kind == "toc"]
        self.assertEqual(
            len(toc_items), len(EXPECTED_ENTRIES),
            f"expected {len(EXPECTED_ENTRIES)} TOC items, got {len(toc_items)}",
        )
        for it, (title, page) in zip(toc_items, EXPECTED_ENTRIES):
            parsed = _parse_toc_entry(it.text)
            self.assertIsNotNone(parsed, f"unparseable TOC item: {it.text!r}")
            self.assertEqual(parsed[0], title)
            self.assertEqual(parsed[1], page)

    def test_nested_entries_carry_nonzero_indent(self):
        toc_items = [it for it in self.items if it.kind == "toc"]
        by_title = {_parse_toc_entry(it.text)[0]: it for it in toc_items}
        # Top-level chapters: indent 0.
        self.assertAlmostEqual(by_title["ABSTRACT"].indent, 0.0, places=1)
        self.assertAlmostEqual(
            by_title["Chapter 1 - Title of Chapter 1"].indent, 0.0, places=1,
        )
        # One-level sub-entry: indent > 0.
        self.assertGreater(by_title["Subheading 1 of Chapter 2"].indent, 20.0)
        # Two-level sub-sub-entry: more indented than its parent.
        self.assertGreater(
            by_title["Subheading 1 of Subheading 1 of Chapter 3"].indent,
            by_title["Subheading 1 of Chapter 3"].indent,
        )


class TocRenderTests(unittest.TestCase):
    """End-to-end: reflow the TOC fixture and verify the rendered text
    layout — entries on their own lines, page numbers right-aligned at
    the column edge."""

    @classmethod
    def setUpClass(cls):
        if not TOC_PDF.exists():
            raise unittest.SkipTest(f"missing fixture: {TOC_PDF}")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out_path = os.path.join(cls.tmp.name, "out.pdf")
        cls.stats = reflow_pdf(str(TOC_PDF), cls.out_path, ReflowConfig())
        cls.out_doc = fitz.open(cls.out_path)
        cls.all_text = "\n".join(p.get_text() for p in cls.out_doc)

    @classmethod
    def tearDownClass(cls):
        cls.out_doc.close()
        cls.tmp.cleanup()

    def test_each_entry_has_own_line_with_title_and_page(self):
        """Each expected TOC entry appears on a single rendered line that
        starts with the title and ends with the page number."""
        out_lines = self.all_text.splitlines()
        # Strip trailing whitespace.
        out_lines = [ln.rstrip() for ln in out_lines]
        for title, page in EXPECTED_ENTRIES:
            # Match a line that starts with whitespace + title, has the
            # dot leader, and ends with the page number.
            pattern = re.compile(
                rf"^\s*{re.escape(title)}\s*\.{{3,}}\s*{re.escape(page)}\s*$"
            )
            self.assertTrue(
                any(pattern.match(ln) for ln in out_lines),
                f"no rendered line matches entry: {title!r} → {page!r}\n"
                f"first 20 rendered lines: {out_lines[:20]}",
            )

    def test_page_numbers_align_at_right_margin(self):
        """Every TOC entry's page number renders flush against the
        right content margin (within a small tolerance)."""
        page = self.out_doc[0]
        right_xs: list[float] = []
        page_w = page.rect.width
        page_tokens = {p for _, p in EXPECTED_ENTRIES}
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    txt = span.get("text", "").strip()
                    if txt in page_tokens:
                        right_xs.append(span["bbox"][2])
        self.assertGreaterEqual(len(right_xs), 10,
                                "did not find enough page-number spans")
        # Margin to the right page edge should be small and consistent.
        gaps = [page_w - x for x in right_xs]
        self.assertLess(max(gaps), 28.0,
                        f"some page numbers not right-aligned: gaps={gaps}")
        self.assertLess(max(gaps) - min(gaps), 6.0,
                        f"page-number right edges vary too much: {gaps}")

    def test_no_two_entries_merged_on_one_rendered_line(self):
        """A rendered line must not contain two TOC tail patterns
        (``...... <page>``) — the regression we're guarding against was
        'Chapter 2 ... 5 Subheading 1 ... 7' rendered on one line."""
        tail_re = re.compile(r"\.{3,}\s*(?:\d+|[ivxlcdmIVXLCDM]+)")
        for ln in self.all_text.splitlines():
            tails = tail_re.findall(ln)
            self.assertLessEqual(
                len(tails), 1,
                f"two TOC tails on one line: {ln!r} (matched {tails})",
            )

    def test_dot_leader_present_between_title_and_page(self):
        """Every TOC line keeps at least three consecutive dots between
        the title and the page number — otherwise the layout looks like
        loose body prose rather than a TOC."""
        for title, page in EXPECTED_ENTRIES:
            pattern = re.compile(
                rf"{re.escape(title)}.*?\.{{3,}}.*?{re.escape(page)}\s*$",
                re.MULTILINE,
            )
            self.assertRegex(
                self.all_text, pattern,
                f"missing dot leader between {title!r} and {page!r}",
            )

    def test_entries_appear_in_source_order(self):
        """Reading-order preservation: the TOC entries appear top-to-bottom
        in the output text in the same order as in the source."""
        positions = []
        for title, _ in EXPECTED_ENTRIES:
            idx = self.all_text.find(title)
            self.assertGreaterEqual(idx, 0, f"{title!r} missing from output")
            positions.append(idx)
        self.assertEqual(
            positions, sorted(positions),
            "TOC entries are not in source order in the output",
        )

    def test_output_is_single_page(self):
        """An 18-entry TOC fits on one mobile page at default body size."""
        self.assertEqual(self.out_doc.page_count, 1)


if __name__ == "__main__":
    unittest.main()
