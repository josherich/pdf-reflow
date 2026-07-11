"""Tests for the verify harness itself (tools/reflow_verify).

The harness guards the pipeline, so its own scoring logic needs to be
trustworthy: the word-diff taxonomy must count drops/additions/changes right,
heading matching must survive re-extraction quirks, and the baseline gate must
fire only on genuine adverse moves.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from reflow_verify.metrics import word_diff, _wordchars  # noqa: E402
from reflow_verify.baseline import compare_fixture, has_gating_regression  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


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
