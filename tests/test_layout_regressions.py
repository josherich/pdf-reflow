"""Layout / reading-order regression tests across the fixture PDFs.

These tests pin down a family of bugs where the reflow scrambled layout
and line breaks:

  - Single-column pages were mis-detected as two-column whenever a
    figure's labels, a table, or a multi-column name list happened to
    leave a clear vertical channel near the page mid. The two-column
    reading-order path then interleaved the page's text nonsensically
    (e.g. bitcoin.pdf p2: the section-3 heading rendered before the
    section-2 body, and a stranded "ownership." line floated alone).

  - Paragraphs cut in two by a column or page boundary stayed two
    separate FlowItems, so the output rendered a paragraph break in
    the middle of a sentence — often splitting a hyphenated word
    ("informa-" / "tion") across the break.

  - Hanging-indent list items ('[44] Some reference…', '• Bullet…')
    wrap their continuation lines to the item's text column, a few
    points right of the marker. The paragraph-indent heuristic took
    that as an indented new-paragraph start and split every reference
    into one block per line.

Run with:  uv run python -m unittest tests.test_layout_regressions
"""

from __future__ import annotations

import unittest
from pathlib import Path

import fitz

from pdf_reflow.extract import extract_document
from pdf_reflow.analyze import (
    FlowItem,
    _detect_columns_from_spans,
    _items_continue,
    _join_continuation_text,
    _merge_continuation_items,
    analyze_document,
)


FIXTURES = Path(__file__).parent / "fixtures"

# Ground truth column count per page for every fixture. two-column.pdf
# is the only genuinely two-column document; everything else must be
# detected single-column on every page, including the pages that used
# to false-positive:
#   - bitcoin.pdf p2: the transaction diagram's box labels populate
#     both half-pages and outnumber the body-paragraph spans
#   - tech_report p12: a full-page benchmark table
#   - tech_report p20: a five-column contributor name list
#   - llm-cjk p6 / p14: pages mixing CJK prose with tables/figures
_EXPECTED_COLUMNS = {
    "two-column.pdf": [2, 2],
    "bitcoin.pdf": [1] * 9,
    "tech_report.pdf": [1] * 30,
    "mit_latex_sample.pdf": [1] * 4,
    "TABLE-OF-CONTENTS-SP2018.pdf": [1],
    "llm-cjk.pdf": [1] * 25,
    "old-chinese-a-new-construction.pdf": [1] * 7,
}


_ANALYSIS_CACHE: dict = {}


def _analyzed(name: str):
    """extract + analyze a fixture once per test run (no rendering)."""
    if name not in _ANALYSIS_CACHE:
        path = FIXTURES / name
        if not path.exists():
            raise unittest.SkipTest(f"missing fixture: {path}")
        doc = fitz.open(str(path))
        try:
            pages = extract_document(doc)
        finally:
            doc.close()
        items, body_size = analyze_document(pages)
        _ANALYSIS_CACHE[name] = (pages, items, body_size)
    return _ANALYSIS_CACHE[name]


class ColumnDetectionFixtureTests(unittest.TestCase):
    """Per-page column detection must match ground truth on every fixture."""

    def test_detected_columns_match_ground_truth(self):
        for name, expected in _EXPECTED_COLUMNS.items():
            pages, _, _ = _analyzed(name)
            got = [
                _detect_columns_from_spans(p.spans, p.width)[0]
                for p in pages
            ]
            self.assertEqual(
                got, expected,
                f"{name}: detected columns {got} != expected {expected}",
            )


class BitcoinReadingOrderTests(unittest.TestCase):
    """bitcoin.pdf p2 was mis-detected as two-column: the figure labels
    of the transaction diagram dominated the (span-count-weighted) body
    size estimate and split the page at a phantom gutter. Reading order
    then put '3. Timestamp Server' before the section-2 body and left a
    stranded 'ownership.' fragment."""

    @classmethod
    def setUpClass(cls):
        _, cls.items, _ = _analyzed("bitcoin.pdf")
        cls.texts = [it.text for it in cls.items if it.text]

    def _index_containing(self, phrase: str) -> int:
        for i, t in enumerate(self.texts):
            if phrase in t:
                return i
        self.fail(f"no item contains {phrase!r}")

    def test_section_2_body_precedes_section_3_heading(self):
        i_h2 = self._index_containing("2. Transactions")
        i_body = self._index_containing("We define an electronic coin")
        i_h3 = self._index_containing("3. Timestamp Server")
        self.assertLess(i_h2, i_body)
        self.assertLess(
            i_body, i_h3,
            "section-2 body must come before the section-3 heading",
        )

    def test_no_stranded_sentence_fragments(self):
        """The phantom column split tore single words off paragraph
        tails ('ownership.' floated as its own item)."""
        for it in self.items:
            if it.kind != "body":
                continue
            self.assertNotEqual(
                it.text.strip(), "ownership.",
                "stranded paragraph fragment — two-column split regressed",
            )

    def test_page_boundary_paragraph_is_fused(self):
        """'...creating the next block in the' (p2) continues with
        'chain, using the hash...' (p3); the merged stream must carry
        the sentence in one item."""
        self._index_containing("next block in the chain, using the hash")


class ContinuationMergeUnitTests(unittest.TestCase):
    """Unit-level behavior of the cross-boundary paragraph fuser."""

    @staticmethod
    def _body(text, page=0, column=0, **kw):
        return FlowItem(kind="body", page_index=page, bbox=(0, 0, 100, 10),
                        text=text, size=10.0, column=column, **kw)

    def test_lowercase_continuation_across_pages_merges(self):
        a = self._body("The framework adopts a decoupled", page=0)
        b = self._body("architecture for training.", page=1)
        self.assertTrue(_items_continue(a, b))
        merged = _merge_continuation_items([a, b])
        self.assertEqual(len(merged), 1)
        self.assertEqual(
            merged[0].text,
            "The framework adopts a decoupled architecture for training.",
        )

    def test_lowercase_continuation_across_columns_merges(self):
        a = self._body("split mid sentence and continues in", column=1)
        b = self._body("the next column.", column=2)
        self.assertTrue(_items_continue(a, b))

    def test_same_page_same_column_never_merges(self):
        """Blocks split within one column were split on purpose (e.g.
        the paragraph-indent rule); never re-fuse them."""
        a = self._body("ends mid sentence and", page=3, column=0)
        b = self._body("looks like a continuation", page=3, column=0)
        self.assertFalse(_items_continue(a, b))

    def test_terminal_punctuation_blocks_merge(self):
        a = self._body("A complete sentence.", page=0)
        b = self._body("lowercase but a new thought", page=1)
        self.assertFalse(_items_continue(a, b))

    def test_uppercase_start_blocks_merge(self):
        a = self._body("ends mid sentence and", page=0)
        b = self._body("Then a new paragraph", page=1)
        self.assertFalse(_items_continue(a, b))

    def test_centered_items_never_merge(self):
        a = self._body("Satoshi Nakamoto satoshin@gmx.com", page=0,
                       align="center")
        b = self._body("www.bitcoin.org", page=1, align="center")
        self.assertFalse(_items_continue(a, b))

    def test_non_body_kinds_never_merge(self):
        a = FlowItem(kind="heading", page_index=0, bbox=(0, 0, 1, 1),
                     text="Policy Optimization", size=10.0)
        b = self._body("the previous policy is optimized", page=1)
        self.assertFalse(_items_continue(a, b))

    def test_hyphenated_word_is_fused(self):
        self.assertEqual(
            _join_continuation_text("complex informa-", "tion synthesis."),
            "complex information synthesis.",
        )

    def test_compound_hyphen_before_uppercase_is_kept(self):
        self.assertEqual(
            _join_continuation_text("trained with Megatron-", "LM."),
            "trained with Megatron-LM.",
        )

    def test_cjk_continuation_joins_flush(self):
        a = self._body("工作机制极其复杂，给", page=0)
        b = self._body("对其能力的研究带来了很大困难。", page=1)
        self.assertTrue(_items_continue(a, b))
        merged = _merge_continuation_items([a, b])
        self.assertEqual(
            merged[0].text, "工作机制极其复杂，给对其能力的研究带来了很大困难。",
        )

    def test_cjk_sentence_end_blocks_merge(self):
        a = self._body("第一段到此结束。", page=0)
        b = self._body("第二段开始了", page=1)
        self.assertFalse(_items_continue(a, b))


class ContinuationMergeFixtureTests(unittest.TestCase):
    """End-to-end: paragraphs that the source cut at page bottoms must
    read as one item after analysis."""

    def test_tech_report_hyphenated_bullet_is_whole(self):
        _, items, _ = _analyzed("tech_report.pdf")
        texts = [it.text for it in items if it.text]
        self.assertTrue(
            any("complex information synthesis" in t for t in texts),
            "hyphenated page-boundary split 'informa-/tion' not fused",
        )

    def test_mit_latex_page_boundary_paragraph_is_whole(self):
        _, items, _ = _analyzed("mit_latex_sample.pdf")
        texts = [it.text for it in items if it.text]
        self.assertTrue(
            any("In fact, if you look at the top" in t for t in texts),
            "page-boundary continuation 'In fact, / if you look' not fused",
        )


class HangingIndentListTests(unittest.TestCase):
    """Hanging-indent items ([44] references, bullets) must keep their
    wrapped continuation lines inside one block instead of splitting at
    every line because the continuation x0 sits right of the marker."""

    @classmethod
    def setUpClass(cls):
        _, cls.items, _ = _analyzed("tech_report.pdf")
        cls.texts = [it.text for it in cls.items if it.text]

    def test_reference_item_keeps_continuation_lines(self):
        """Reference [3] wraps onto a second line ('amazon.com/s3/...');
        both lines must be in one item."""
        ref3 = [t for t in self.texts if t.startswith("[3] ")]
        self.assertTrue(ref3, "reference [3] missing")
        self.assertIn(
            "amazon.com/s3/", ref3[0],
            "continuation line split off the hanging-indent reference",
        )

    def test_each_reference_starts_its_own_item(self):
        starts = {
            t.split()[0] for t in self.texts if t.startswith("[")
        }
        for i in (1, 2, 3, 4, 5):
            self.assertIn(
                f"[{i}]", starts,
                f"reference [{i}] does not begin its own item",
            )

    def test_bullet_items_start_their_own_item(self):
        bullets = [t for t in self.texts if t.startswith("•")]
        self.assertGreater(
            len(bullets), 10,
            "bulleted list items were fused into surrounding paragraphs",
        )

    def test_bullet_item_keeps_continuation_lines(self):
        target = [
            t for t in self.texts
            if t.startswith("•") and "BrowseComp" in t[:14]
        ]
        self.assertTrue(target, "'• BrowseComp:' bullet missing")
        self.assertIn(
            "information synthesis", target[0],
            "bullet continuation lines split into separate items",
        )


class TwoColumnStillDetectedTests(unittest.TestCase):
    """Guard: the stricter detector must not regress the genuine
    two-column fixture — both pages stay two-column with a sane gutter
    and the analysis still orders left-column before right-column."""

    def test_reading_order_left_then_right(self):
        _, items, _ = _analyzed("two-column.pdf")
        texts = [it.text for it in items if it.text]

        def idx(phrase):
            for i, t in enumerate(texts):
                if phrase in t:
                    return i
            self.fail(f"no item contains {phrase!r}")

        # p1: left-column body before right-column body.
        self.assertLess(
            idx("Your goal is to simulate"),
            idx("Large figures and tables may span"),
        )
        # The full-width title precedes everything column-bound.
        self.assertLess(
            idx("Preparation of Papers"),
            idx("Your goal is to simulate"),
        )


if __name__ == "__main__":
    unittest.main()
