"""Independent accuracy probes and equivalence guards for faster PDF I/O."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from benchmark import compare_runs
from reflow_verify.probes import PROBES
from pdf_reflow.extract import Span, extract_page, normalize_text
from pdf_reflow.mathfont import is_math_font
from pdf_reflow.layout import DrawImage, DrawRule, DrawText, LaidOutPage, LayoutConfig
from pdf_reflow.render import render, _insert_text_args, _figure_scale

FIXTURES = Path(__file__).parent / "fixtures"


class AccuracyProbeTests(unittest.TestCase):
    def test_independent_accuracy_oracles(self):
        for name, probe in PROBES.items():
            with self.subTest(probe=name):
                self.assertTrue(probe())

    def test_multiline_heading_and_anchor_move_together(self):
        from pdf_reflow.analyze import FlowItem
        from pdf_reflow.layout import layout
        cfg = LayoutConfig(page_width=180, page_height=180)
        items = [FlowItem("body", 0, (0, 0, 100, 10), text="\n".join(["lead"] * 5)),
                 FlowItem("heading", 0, (0, 0, 100, 10),
                          text="A heading that wraps onto two lines", size=14),
                 FlowItem("body", 0, (0, 0, 100, 10), text="Following text.")]
        pages, anchors = layout(items, cfg)
        page = pages[anchors[0].out_page]
        self.assertEqual(anchors[0].out_page, 1)
        text = " ".join(op.text for op in page.ops if isinstance(op, DrawText))
        self.assertIn("A heading that wraps onto two lines Following text.", text)
        self.assertAlmostEqual(anchors[0].y, cfg.content_top)

    def test_oversized_heading_does_not_create_blank_first_page(self):
        from pdf_reflow.analyze import FlowItem
        from pdf_reflow.layout import layout
        pages, anchors = layout(
            [FlowItem("heading", 0, (0, 0, 100, 10),
                      text="An unusually long heading " * 25, size=14)],
            LayoutConfig(page_width=180, page_height=180))
        self.assertEqual(anchors[0].out_page, 0)
        self.assertTrue(all(page.ops for page in pages))


class ExtractionEquivalenceTests(unittest.TestCase):
    def test_compact_spans_match_legacy_glyph_union_across_corpus(self):
        for path in sorted(FIXTURES.glob("*.pdf")):
            with fitz.open(path) as doc:
                for page in doc:
                    with self.subTest(fixture=path.name, page=page.number):
                        expected = []
                        raw = page.get_text("rawdict", flags=fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES)
                        for block in raw["blocks"]:
                            for line in block.get("lines", []):
                                for span in line["spans"]:
                                    chars = span["chars"]
                                    text = normalize_text("".join(c["c"] for c in chars))
                                    if not text.strip() and not is_math_font(span["font"]):
                                        continue
                                    bbox = (min(c["bbox"][0] for c in chars),
                                            min(c["bbox"][1] for c in chars),
                                            max(c["bbox"][2] for c in chars),
                                            max(c["bbox"][3] for c in chars))
                                    expected.append(Span(text, bbox, span["font"], span["size"],
                                                         span["flags"], span["color"], tuple(span["origin"])))
                        actual = extract_page(page, page.number)
                        self.assertEqual(actual.spans, expected)
                        self.assertEqual([(d.bbox, d.kind) for d in actual.drawings],
                                         [(tuple(d["rect"]), d["type"]) for d in page.get_drawings()])


class RenderBatchEquivalenceTests(unittest.TestCase):
    def test_batching_preserves_pixels_text_and_graphics_order(self):
        cfg = LayoutConfig(page_width=180, page_height=180)
        with fitz.open() as src, fitz.open() as legacy:
            src_page = src.new_page(width=80, height=80)
            src_page.draw_rect((0, 0, 80, 80), color=None, fill=(0.8, 0.9, 1))
            ops = [DrawText(18, 30, "First line", "times-roman", 11),
                   DrawText(18, 45, "Behind image", "times-roman", 11),
                   DrawImage(25, 40, 40, 40, 0, (0, 0, 80, 80)),
                   DrawText(18, 55, "In front", "times-bold", 11),
                   DrawText(18, 95, "中文", "china-s", 11),
                   DrawRule(18, 100, 150),
                   DrawText(18, 120, "After rule", "helvetica", 11)]
            page = legacy.new_page(width=180, height=180)
            for op in ops:
                if isinstance(op, DrawText):
                    page.insert_text((op.x, op.y), op.text, fontsize=op.size,
                                     color=op.color, **_insert_text_args(op.font))
                elif isinstance(op, DrawImage):
                    scale = _figure_scale(op, cfg)
                    png = src[0].get_pixmap(matrix=fitz.Matrix(scale, scale),
                                           clip=fitz.Rect(op.source_rect), alpha=False).tobytes("png")
                    page.insert_image((op.x, op.y, op.x + op.w, op.y + op.h), stream=png)
                else:
                    page.draw_line((op.x0, op.y), (op.x1, op.y), color=(0.6, 0.6, 0.6), width=0.5)
            with render(src, [LaidOutPage(180, 180, ops)], cfg) as batched:
                self.assertEqual(batched[0].get_pixmap(matrix=fitz.Matrix(2, 2)).samples,
                                 legacy[0].get_pixmap(matrix=fitz.Matrix(2, 2)).samples)
                self.assertEqual(batched[0].get_text(), legacy[0].get_text())
                self.assertLess(len(batched[0].get_contents()), len(legacy[0].get_contents()))


class BenchmarkComparisonTests(unittest.TestCase):
    def setUp(self):
        self.before = {"schema": 1, "config": {}, "runtime": {}, "probes": {"case": False},
                       "fixtures": {"a.pdf": {"sha256": "abc", "median_seconds": 1,
                                               "quality": {"retention": 1, "clipped_lines": 0}}}}
        self.after = copy.deepcopy(self.before)
        self.after["probes"]["case"] = True

    def test_faster_with_fixed_probe_passes(self):
        self.after["fixtures"]["a.pdf"]["median_seconds"] = 0.5
        self.assertEqual(compare_runs(self.before, self.after), [])

    def test_quality_regression_cannot_be_traded_for_speed(self):
        self.after["fixtures"]["a.pdf"]["median_seconds"] = 0.5
        self.after["fixtures"]["a.pdf"]["quality"]["clipped_lines"] = 1
        self.assertTrue(any("quality" in s for s in compare_runs(self.before, self.after)))

    def test_slowdown_fails(self):
        self.after["fixtures"]["a.pdf"]["median_seconds"] = 1.3
        self.assertTrue(any("runtime regressed" in s for s in compare_runs(self.before, self.after)))

    def test_changed_corpus_or_runtime_fails(self):
        for key in ("runtime", "config"):
            after = copy.deepcopy(self.after)
            after[key] = {"changed": True}
            self.assertTrue(compare_runs(self.before, after))
        self.after["fixtures"]["a.pdf"]["sha256"] = "def"
        self.assertTrue(compare_runs(self.before, self.after))

    def test_changed_quality_scorer_requires_rescoring_both_algorithms(self):
        self.after["quality_schema"] = 2
        self.assertTrue(any("scoring schemas differ" in s
                            for s in compare_runs(self.before, self.after)))

    def test_failed_or_removed_probe_fails(self):
        self.after["probes"]["case"] = False
        self.assertTrue(compare_runs(self.before, self.after))
        self.after["probes"] = {}
        self.assertTrue(compare_runs(self.before, self.after))


if __name__ == "__main__":
    unittest.main()
