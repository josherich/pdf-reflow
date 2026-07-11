"""Tests for the verify harness itself (tools/reflow_verify).

The harness guards the pipeline, so its own scoring logic needs to be
trustworthy: SSIM must be 1.0 for identical images and fall for different
ones, and the word-diff taxonomy must count drops/additions/changes right.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from reflow_verify.imaging import GrayImage, ssim  # noqa: E402
from reflow_verify.metrics import word_diff, _wordchars  # noqa: E402
from reflow_verify.baseline import compare_fixture, has_gating_regression  # noqa: E402
from reflow_verify.imaging import density_map  # noqa: E402
from reflow_verify.golden import build_strip  # noqa: E402
from pdf_reflow import reflow_pdf, ReflowConfig  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _dstrip(pdf):
    return density_map(build_strip(pdf))


def _img(rows):
    h = len(rows)
    w = len(rows[0])
    px = [float(v) for r in rows for v in r]
    return GrayImage(w, h, px)


class SsimTests(unittest.TestCase):
    def test_identical_is_one(self):
        a = _img([[i * 7 % 256 for i in range(16)] for _ in range(16)])
        self.assertAlmostEqual(ssim(a, a), 1.0, places=6)

    def test_blank_pages_match(self):
        a = _img([[255] * 16 for _ in range(16)])
        self.assertAlmostEqual(ssim(a, a), 1.0, places=6)

    def test_inverted_is_low(self):
        a = _img([[0 if (x + y) % 2 else 255 for x in range(16)] for y in range(16)])
        b = _img([[255 if (x + y) % 2 else 0 for x in range(16)] for y in range(16)])
        self.assertLess(ssim(a, b), 0.5)

    def test_mismatched_grids_align(self):
        a = _img([[100] * 20 for _ in range(10)])
        b = _img([[100] * 19 for _ in range(10)])  # off-by-one column
        self.assertGreater(ssim(a, b), 0.99)


class StripTests(unittest.TestCase):
    """Layer 2 compares the stitched ink-density map of the whole reflowed
    column, not page N vs golden page N. The guarantees the gate relies on:

      1. Deterministic: reflowing the same source at the same config twice
         yields an identical density map (SSIM 1.0) -> no false failures on
         unrelated code changes.
      2. Pagination-invariant enough: moving the page breaks (a different page
         *height*, same content and column width) barely perturbs the map,
         and always far less than a genuine re-layout.
      3. Sensitive: changing the column *width* re-wraps every line and
         clearly lowers the score.
    """

    def _reflow(self, d, name, **cfg):
        p = os.path.join(d, name)
        reflow_pdf(os.path.join(FIXTURES, "bitcoin.pdf"), p, ReflowConfig(**cfg))
        return p

    def test_deterministic_identical_map(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._reflow(d, "a.pdf", page_width=360, page_height=600)
            b = self._reflow(d, "b.pdf", page_width=360, page_height=600)
            self.assertGreaterEqual(ssim(_dstrip(a), _dstrip(b)), 0.999)

    def test_pagination_shift_beats_relayout(self):
        with tempfile.TemporaryDirectory() as d:
            base = self._reflow(d, "base.pdf", page_width=360, page_height=600)
            taller = self._reflow(d, "tall.pdf", page_width=360, page_height=740)
            narrower = self._reflow(d, "narrow.pdf", page_width=300, page_height=600)
            import fitz
            self.assertNotEqual(fitz.open(base).page_count,
                                fitz.open(taller).page_count)  # breaks moved
            paginate = ssim(_dstrip(base), _dstrip(taller))
            relayout = ssim(_dstrip(base), _dstrip(narrower))
            # A pure pagination shift disturbs the density map far less than a
            # real re-wrap, and a real re-wrap is clearly caught.
            self.assertGreater(paginate, relayout + 0.1)
            self.assertLess(relayout, 0.95)


class WordDiffTests(unittest.TestCase):
    def test_perfect_retention(self):
        ref = ["the", "quick", "brown", "fox"]
        d = word_diff(ref, list(ref))
        self.assertEqual(d["retention"], 1.0)
        self.assertEqual((d["w_minus"], d["w_plus"], d["w_tilde"]), (0, 0, 0))

    def test_dropped_word(self):
        d = word_diff(["a", "b", "c"], ["a", "c"])
        self.assertEqual(d["w_minus"], 1)
        self.assertEqual(d["w_plus"], 0)

    def test_spurious_word(self):
        d = word_diff(["a", "c"], ["a", "b", "c"])
        self.assertEqual(d["w_plus"], 1)
        self.assertEqual(d["w_minus"], 0)

    def test_changed_word(self):
        d = word_diff(["a", "b", "c"], ["a", "x", "c"])
        self.assertEqual(d["w_tilde"], 1)


class WordCharMatchTests(unittest.TestCase):
    """Heading matching must survive re-extraction quirks: differing CJK/Latin
    spacing and list-bullet glyphs would otherwise flag present headings as
    missing (the headings_missing bug)."""

    def test_cjk_latin_spacing_ignored(self):
        heading = "LLM 的语言和思考能力"
        output = "标题 LLM的语言和思考能力 正文"  # no space between LLM and 的
        self.assertIn(_wordchars(heading), _wordchars(output))

    def test_leading_bullet_ignored(self):
        heading = "• 可以用 Next Token Prediction (NTP)"
        output = "前文 可以用Next Token Prediction NTP 后文"  # bullet/paren dropped
        self.assertIn(_wordchars(heading), _wordchars(output))

    def test_genuinely_absent_heading_still_missing(self):
        self.assertNotIn(_wordchars("完全不同的标题"),
                         _wordchars("something entirely unrelated"))


class BaselineTests(unittest.TestCase):
    def test_retention_drop_gates(self):
        deltas = compare_fixture({"retention": 1.0}, {"retention": 0.90})
        self.assertTrue(has_gating_regression(deltas))

    def test_tiny_retention_wobble_ok(self):
        deltas = compare_fixture({"retention": 1.0}, {"retention": 0.995})
        self.assertFalse(has_gating_regression(deltas))

    def test_new_clipped_line_gates(self):
        deltas = compare_fixture({"clipped_lines": 0}, {"clipped_lines": 1})
        self.assertTrue(has_gating_regression(deltas))

    def test_improvement_is_not_regression(self):
        deltas = compare_fixture({"w_minus": 10}, {"w_minus": 0})
        self.assertFalse(has_gating_regression(deltas))


if __name__ == "__main__":
    unittest.main()
