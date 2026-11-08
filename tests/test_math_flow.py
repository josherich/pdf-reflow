"""Formula regressions checked against original page coordinates and glyphs."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import fitz

from pdf_reflow.analyze import analyze_document
from pdf_reflow.extract import extract_document
from pdf_reflow.layout import DrawMath, DrawText, LayoutConfig, layout
from pdf_reflow.math_render import FormulaRenderer
from pdf_reflow.render import render

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from reflow_verify.metrics import _tokens, math_preservation

FIXTURES = Path(__file__).parent / "fixtures"


class CJKPageElevenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = fitz.open(FIXTURES / "llm-cjk.pdf")
        cls.pages = extract_document(cls.doc)
        cls.items, _ = analyze_document(cls.pages)
        cls.page_items = [it for it in cls.items if it.page_index == 10]

    @classmethod
    def tearDownClass(cls):
        cls.doc.close()

    def test_equation_seven_does_not_swallow_definition_paragraph(self):
        # The original equation occupies y=162..189. The next paragraph
        # begins at y=197; the old crop extended to y=215 and swallowed it.
        equation = next(it for it in self.page_items if it.kind == "figure"
                        and 155 < it.bbox[1] < 170)
        self.assertLess(equation.source_rect[3], 194)
        self.assertLessEqual(equation.source_rect[1], 163)
        self.assertGreaterEqual(equation.source_rect[2], 477)  # equation number (7)
        definition = next(it for it in self.page_items if "其中，" in it.text)
        self.assertIn("Query", definition.text)
        self.assertIn("Key", definition.text)
        self.assertIn("Value", definition.text)
        self.assertIn("矩阵计算中需要进行掩码", definition.text)
        self.assertLess(self.page_items.index(equation), self.page_items.index(definition))

    def test_inline_radical_keeps_radicand_bar_and_body_baseline(self):
        definition = next(it for it in self.page_items if "其中，" in it.text)
        fragment = next(f for f in definition.inline_math.values() if "√" in f.text)
        self.assertIn("d", fragment.text)
        self.assertAlmostEqual(fragment.baseline, 222.407, places=2)
        # Independent coordinates of the source's radical rule.
        self.assertTrue(any(abs(r[0][1] - 213.249) < 0.01 for r in fragment.rules))
        cfg = LayoutConfig()
        pages, _ = layout([definition], cfg)
        op = next(op for p in pages for op in p.ops
                  if isinstance(op, DrawMath) and op.fragment is fragment)
        self.assertLess(op.x + op.w, cfg.content_right + 0.01)
        self.assertGreater(op.h, 10)
        # It stays on the same output baseline as neighboring prose.
        baseline = op.y + (fragment.baseline - fragment.rect[1]) * (op.w / (fragment.rect[2] - fragment.rect[0]))
        self.assertTrue(any(isinstance(t, DrawText) and abs(t.y - baseline) < 0.01
                            for p in pages for t in p.ops))

    def test_all_math_on_source_page_eleven_is_accounted_for(self):
        with fitz.open() as source:
            source.insert_pdf(self.doc, from_page=10, to_page=10)
            # Analyze the isolated page as well: page-range previews must
            # preserve the same math even without surrounding pages.
            items, _ = analyze_document(extract_document(source))
            metrics = math_preservation(source, items)
            self.assertGreater(metrics["math_spans"], 10)
            self.assertEqual(metrics["math_span_retention"], 1)


class LatexFormulaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = fitz.open(FIXTURES / "mit_latex_sample.pdf")
        cls.pages = extract_document(cls.doc)
        cls.items, _ = analyze_document(cls.pages)

    @classmethod
    def tearDownClass(cls):
        cls.doc.close()

    def test_space_encoded_integral_is_not_discarded(self):
        self.assertTrue(any(s.font == "CMEX10" and not s.text.strip()
                            for s in self.pages[0].spans))

    def test_inline_limits_stay_with_operators(self):
        paragraph = next(it for it in self.items if "sums and integrals" in it.text)
        fragment = next(f for f in paragraph.inline_math.values() if f.rect[0] > 330)
        # Source bounds cover both operators and all upper/lower limits,
        # rather than the old behavior of deleting the i=1 and 0 fragments.
        self.assertLess(fragment.rect[1], 469)
        self.assertGreater(fragment.rect[3], 487)
        self.assertGreater(fragment.rect[2], 485)
        with FormulaRenderer(self.doc) as renderer:
            isolated = renderer.document(fragment)
            pix = isolated[0].get_pixmap(matrix=fitz.Matrix(3, 3), alpha=False)
            self.assertGreater(sum(v < 180 for v in pix.samples), 200)
            # Glyph isolation excludes the neighboring prose, unlike a
            # rectangular source clip. No font resources are needed.
            self.assertFalse(isolated[0].get_fonts())
            self.assertGreater(len(isolated[0].get_drawings()), 12)

    def test_aligned_equation_keeps_first_row_with_remaining_rows(self):
        # The x1+2x2+3x3=7 row used to be rendered as flat prose,
        # losing all three subscripts and the alignment with y=mx+c.
        figures = [it for it in self.items if it.page_index == 1 and it.kind == "figure"]
        aligned = next(it for it in figures if 450 < it.source_rect[1] < 460)
        self.assertGreater(aligned.source_rect[3], 500)
        self.assertFalse(any(it.kind == "body" and "x1 + 2x2" in it.text for it in self.items))

    def test_formula_tokens_never_reach_text_draw_operations(self):
        pages, _ = layout(self.items)
        self.assertTrue(any(isinstance(op, DrawMath) for p in pages for op in p.ops))
        for p in pages:
            for op in p.ops:
                if isinstance(op, DrawText):
                    self.assertFalse(any(0xF0000 <= ord(ch) <= 0xFFFFD for ch in op.text))

    def test_inline_math_scales_and_wraps_on_narrow_pages(self):
        paragraph = next(it for it in self.items if "sums and integrals" in it.text)
        cfg = LayoutConfig(page_width=150)
        pages, _ = layout([paragraph], cfg)
        for page in pages:
            for op in page.ops:
                if isinstance(op, DrawMath):
                    self.assertLessEqual(op.x + op.w, cfg.content_right + 0.01)
                    self.assertGreaterEqual(op.y, cfg.content_top)
                    self.assertLessEqual(op.y + op.h, cfg.content_bottom + 0.01)
        with render(self.doc, pages, cfg) as out:
            self.assertEqual(out.page_count, len(pages))


class TokenScoringTests(unittest.TestCase):
    def test_cjk_tokenization_is_invariant_under_line_wrapping(self):
        self.assertEqual(_tokens("查询矩阵与键矩阵"), _tokens("查询矩阵\n与键矩阵"))
        self.assertEqual(_tokens("用softmax计算"), ["用", "softmax", "计", "算"])


class OrdinaryFontMathTests(unittest.TestCase):
    def test_text_only_svg_keeps_formula_pixels_and_source_images(self):
        from pdf_reflow.extract import MathFragment
        with fitz.open() as source:
            page = source.new_page(width=300, height=300)
            page.insert_text((30, 50), "x", fontname="tiit", fontsize=12)
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 500, 500), False)
            pix.clear_with(200)
            page.insert_image((20, 80, 280, 280), pixmap=pix)
            span = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)["blocks"][0]["lines"][0]["spans"][0]
            fragment = MathFragment(0, tuple(span["bbox"]), span["origin"][1], 12, "x",
                                    glyph_regions=[(tuple(span["bbox"]), span["origin"][1], 12, 0)])
            with FormulaRenderer(source) as fast, FormulaRenderer(source) as reference:
                reference._page_svg = lambda index: source[index].get_svg_image(text_as_path=True)
                self.assertNotIn("data:image", fast._page_svg(0))
                actual = fast.document(fragment)[0].get_pixmap(matrix=fitz.Matrix(3, 3)).samples
                expected = reference.document(fragment)[0].get_pixmap(matrix=fitz.Matrix(3, 3)).samples
                self.assertEqual(actual, expected)
                self.assertGreater(sum(v < 180 for v in actual), 20)
                self.assertEqual(len(source[0].get_images()), 1)

    def test_math_heading_bookmark_has_no_internal_object_markers(self):
        from pdf_reflow.analyze import FlowItem
        from pdf_reflow.extract import MathFragment
        key = "\U000f0000"
        fragment = MathFragment(0, (10, 10, 25, 25), 22, 12, "x_i")
        _, anchors = layout([FlowItem("heading", 0, (0, 0, 100, 20),
                                     text="Using " + key, size=14,
                                     inline_math={key: fragment})])
        self.assertEqual(anchors[0].text, "Using x_i")

    def test_italic_variable_keeps_both_scripts_in_regular_fonts(self):
        # The technical report uses ordinary Nimbus Roman italic for y,
        # with superscript j and subscript 0:i in smaller Roman faces.
        with fitz.open(FIXTURES / "tech_report.pdf") as doc:
            items, _ = analyze_document(extract_document(doc, page_indices=[7]))
        definition = next(it for it in items if it.text.startswith("Here"))
        fragment = next(f for f in definition.inline_math.values()
                        if "0:" in f.text and 220 < f.rect[0] < 240)
        self.assertIn("j", fragment.text)
        self.assertIn("y", fragment.text)
        self.assertGreaterEqual(len({round(r[1], 1) for r in fragment.glyph_regions}), 3)
        self.assertFalse(any(it.kind == "caption" and it.text.strip() == "0:i" for it in items))


if __name__ == "__main__":
    unittest.main()
