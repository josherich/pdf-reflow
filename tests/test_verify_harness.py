"""Tests for the verify harness itself (tools/reflow_verify).

The harness guards the pipeline, so its own scoring logic needs to be
trustworthy: SSIM must be 1.0 for identical images and fall for different
ones, and the word-diff taxonomy must count drops/additions/changes right.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from reflow_verify.imaging import GrayImage, ssim  # noqa: E402
from reflow_verify.metrics import word_diff  # noqa: E402
from reflow_verify.baseline import compare_fixture, has_gating_regression  # noqa: E402


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
