"""Tests for Unicode (UAX #14) line breaking + Knuth-Plass wrapping.

These pin down two new behaviours:

  - ``pdf_reflow.linebreak`` finds break opportunities per the Unicode
    Line Breaking Algorithm (the rule set ICU implements): after spaces
    and hyphens, between ideographs, around slashes / em-dashes, while
    never orphaning closing punctuation or splitting a number.

  - ``pdf_reflow.knuth_plass`` chooses break points so the paragraph is
    optimally balanced (minimum raggedness) instead of greedily ragged,
    and never lets a line overflow the column.

Run with:  uv run python -m unittest tests.test_linebreak
"""

from __future__ import annotations

import unittest

from pdf_reflow.linebreak import (
    Sep,
    line_break_opportunities,
    segments,
)
from pdf_reflow.knuth_plass import (
    Box,
    BreakParams,
    Glue,
    Penalty,
    add_final_break,
    break_lines,
)
from pdf_reflow.layout import FontMetrics, _multifont_width, _wrap_paragraph


def break_before(text: str) -> set:
    """Set of indices where a (any) break is permitted before text[i]."""
    return {i for i, _ in line_break_opportunities(text)}


class UAX14OpportunityTests(unittest.TestCase):
    def test_breaks_after_space(self):
        # "ab cd": only legal break is before 'c' (index 3).
        self.assertEqual(break_before("ab cd"), {3})

    def test_breaks_after_hyphen_not_before(self):
        opp = break_before("well-known")
        self.assertIn(5, opp)          # after the hyphen, before 'k'
        self.assertNotIn(4, opp)       # not before the hyphen

    def test_breaks_after_slash(self):
        opp = break_before("input/output")
        self.assertIn(6, opp)          # after '/', before 'o'
        self.assertNotIn(5, opp)       # not before '/'

    def test_em_dash_breaks_both_sides(self):
        # word—word : break before and after the em dash.
        boxes, seps = segments("word\u2014word")
        self.assertEqual(boxes, ["word", "\u2014", "word"])

    def test_no_break_before_closing_punctuation(self):
        # A full stop / comma must never start the next line.
        boxes, _ = segments("hello, world.")
        self.assertEqual(boxes, ["hello,", "world."])

    def test_no_break_before_closing_bracket(self):
        boxes, _ = segments("see (note)")
        # The ')' stays attached to 'note'.
        self.assertTrue(boxes[-1].endswith(")"))
        self.assertNotIn(")", "".join(boxes[:-1])[-1:] if boxes[:-1] else "")

    def test_nbsp_is_not_a_break(self):
        # Non-breaking space keeps the two tokens in one box.
        boxes, seps = segments("Fig.\u00a01")
        self.assertEqual(boxes, ["Fig.\u00a01"])
        self.assertEqual(seps, [])

    def test_number_is_not_split(self):
        boxes, _ = segments("pay 1,000.00 now")
        self.assertIn("1,000.00", boxes)

    def test_currency_and_percent_stay_with_number(self):
        boxes, _ = segments("about $1,500 and 10% more")
        self.assertIn("$1,500", boxes)
        self.assertIn("10%", boxes)


class UAX14CJKTests(unittest.TestCase):
    def test_breaks_between_ideographs(self):
        boxes, seps = segments("中文字符")
        self.assertEqual(boxes, ["中", "文", "字", "符"])
        self.assertTrue(all(not s.space for s in seps))

    def test_no_break_before_ideographic_period(self):
        # The ideographic full stop 。 must not start a line.
        boxes, _ = segments("中文。下一句")
        # Every box except possibly the first must not start with 。
        for b in boxes[1:]:
            self.assertFalse(b.startswith("。"))
        # 。 rides along with the preceding ideograph.
        self.assertTrue(any(b.endswith("。") for b in boxes))

    def test_no_break_after_opening_cjk_bracket(self):
        # 「 opens a quote; no break right after it.
        boxes, _ = segments("他说「你好」吗")
        self.assertFalse(any(b.startswith("」") for b in boxes))
        self.assertFalse(any(b.endswith("「") for b in boxes))

    def test_inline_latin_space_preserved(self):
        boxes, seps = segments("詩經 Shijing")
        self.assertEqual(boxes, ["詩", "經", "Shijing"])
        # 詩|經 flush; 經|Shijing separated by a (collapsed) space.
        self.assertFalse(seps[0].space)
        self.assertTrue(seps[1].space)


class KnuthPlassUnitTests(unittest.TestCase):
    def _para(self, widths, space=2.0, line_width=10.0):
        """Build an alternating box/glue stream for equal-width 'words'."""
        items = []
        for i, w in enumerate(widths):
            if i > 0:
                items.append(Glue(space, space * 3, 0.0))
            items.append(Box(w))
        return add_final_break(items)

    def test_never_overflows(self):
        # Ten words of width 3, glue 1, column 10 -> at most lines of
        # width <= 10. Verify no chosen line exceeds the column.
        widths = [3.0] * 10
        items = self._para(widths, space=1.0, line_width=10.0)
        breaks = break_lines(items, 10.0, BreakParams(default_stretch=3.0))
        # Reconstruct line widths.
        self._assert_lines_fit(items, breaks, 10.0)

    def test_balances_better_than_greedy(self):
        # A classic case where greedy strands a short last line. We check
        # Knuth-Plass minimises the sum of squared trailing slack.
        widths = [4.0, 4.0, 4.0, 4.0, 4.0, 4.0]
        line_width = 9.0
        items = self._para(widths, space=1.0, line_width=line_width)
        breaks = break_lines(items, line_width, BreakParams(default_stretch=3.0))
        kp_cost = self._ragged_cost(items, breaks, line_width)
        greedy_cost = self._greedy_cost(widths, 1.0, line_width)
        self.assertLessEqual(kp_cost, greedy_cost)

    def test_forced_break_is_honoured(self):
        items = [Box(2.0), Penalty(0.0, -1_000_000.0, False), Box(2.0)]
        items = add_final_break(items)
        breaks = break_lines(items, 100.0, BreakParams())
        # The forced penalty (index 1) must be among the breaks.
        self.assertIn(1, breaks)

    def _assert_lines_fit(self, items, breaks, line_width):
        # Walk items, summing widths between consecutive breaks.
        start = -1
        for b in breaks:
            w = sum(self._w(items[k]) for k in range(start + 1, b))
            self.assertLessEqual(w, line_width + 1e-6,
                                 f"line width {w} exceeds {line_width}")
            start = b

    @staticmethod
    def _w(it):
        if isinstance(it, Box):
            return it.width
        if isinstance(it, Glue):
            return it.width
        return 0.0

    def _ragged_cost(self, items, breaks, line_width):
        cost = 0.0
        start = -1
        last = breaks[-1] if breaks else 0
        for b in breaks:
            w = sum(self._w(items[k]) for k in range(start + 1, b))
            if b != last:   # last line slack is free
                cost += (line_width - w) ** 2
            start = b
        return cost

    @staticmethod
    def _greedy_cost(widths, space, line_width):
        cost = 0.0
        cur = 0.0
        lines = []
        for w in widths:
            add = (space + w) if cur > 0 else w
            if cur + add <= line_width:
                cur += add
            else:
                lines.append(cur)
                cur = w
        if cur > 0:
            lines.append(cur)
        for ln in lines[:-1]:
            cost += (line_width - ln) ** 2
        return cost


class WrapParagraphTests(unittest.TestCase):
    FONT = "times-roman"
    SIZE = 11.0

    def _fits(self, lines, max_width):
        for ln in lines:
            self.assertLessEqual(
                _multifont_width(ln, self.FONT, self.SIZE), max_width + 0.5,
                f"line overflows column: {ln!r}",
            )

    def test_no_line_overflows_column(self):
        text = ("Reflowing a dense academic paper onto a narrow mobile "
                "column requires careful, Unicode-aware line breaking.")
        for width in (80.0, 120.0, 200.0):
            lines = _wrap_paragraph(text, self.FONT, self.SIZE, width)
            self._fits(lines, width)

    def test_roundtrips_text_content(self):
        text = "The quick brown fox jumps over the lazy dog."
        lines = _wrap_paragraph(text, self.FONT, self.SIZE, 90.0)
        self.assertEqual(" ".join(lines), text)

    def test_hyphen_stays_at_line_end(self):
        # A break after an existing hyphen keeps the hyphen on the upper
        # line (never moves it down to the next).
        text = "a state-of-the-art mobile-first responsive typesetting engine"
        lines = _wrap_paragraph(text, self.FONT, self.SIZE, 70.0)
        self._fits(lines, 70.0)
        for ln in lines[1:]:
            self.assertFalse(ln.startswith("-"),
                             f"hyphen leaked to start of line: {ln!r}")

    def test_balanced_paragraph_has_even_right_edge(self):
        # Knuth-Plass should keep interior line widths close together.
        text = ("Knuth and Plass described an algorithm that breaks a "
                "paragraph into lines so the overall result is as smooth "
                "as possible across the whole block of text.")
        width = 140.0
        lines = _wrap_paragraph(text, self.FONT, self.SIZE, width)
        self.assertGreater(len(lines), 2)
        interior = lines[:-1]
        widths = [_multifont_width(ln, self.FONT, self.SIZE) for ln in interior]
        spread = max(widths) - min(widths)
        # Interior lines should fill a good fraction of the column and be
        # reasonably even (spread well under half the column width).
        self.assertGreater(min(widths), width * 0.6)
        self.assertLess(spread, width * 0.4)

    def test_oversize_token_is_force_broken(self):
        text = "x" + "y" * 200
        lines = _wrap_paragraph(text, self.FONT, self.SIZE, 60.0)
        self.assertGreater(len(lines), 1)
        self._fits(lines, 60.0)

    def test_empty_text(self):
        self.assertEqual(_wrap_paragraph("", self.FONT, self.SIZE, 100.0), [])


if __name__ == "__main__":
    unittest.main()
