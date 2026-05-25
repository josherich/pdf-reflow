"""CJK reflow tests.

Base14 PDF fonts (Times / Courier / Helvetica) have no CJK glyphs, so
prior to CJK support every Chinese / Japanese / Korean codepoint was
silently dropped by ``insert_text``. These tests synthesize a small
mixed-script PDF, reflow it, and verify the CJK characters survive the
round-trip.

The source fixture is generated at runtime with PyMuPDF's built-in CJK
fonts so the tests don't depend on a checked-in binary PDF.
"""

from __future__ import annotations

import os
import tempfile
import unittest

import fitz

from pdf_reflow.layout import (
    _cjk_font_for_char,
    _dominant_cjk_font,
    _is_cjk_char,
    _tokenize_for_wrap,
    _wrap_paragraph,
)
from pdf_reflow.reflow import ReflowConfig, reflow_pdf


def _make_cjk_source(path: str) -> None:
    """Synthesize a small PDF with mixed CJK + Latin content.

    PyMuPDF's ``insert_text`` does not wrap, so we pre-split body text
    into short source lines that fit on the page — otherwise the source
    itself gets truncated at the page edge and the reflow pipeline
    never sees the full content.
    """
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    # Title (heading-sized) in Simplified Chinese.
    page.insert_text((50, 80), "中文标题示例", fontname="china-s", fontsize=20)
    # Body paragraph: Simplified Chinese with embedded Latin, broken
    # into source lines that fit horizontally.
    body_lines = [
        "这是一段用于测试 PDF 重新排版的中文段落，",
        "其中混合了 English words 与若干符号，",
        "例如 GPT-4 与 2024 年的数据。",
        "正文应能字符级换行并保留所有字形。",
    ]
    y = 130
    for line in body_lines:
        page.insert_text((50, y), line, fontname="china-s", fontsize=11)
        y += 16
    # Japanese paragraph (Hiragana + Kanji).
    page.insert_text((50, 250), "これはテストです。", fontname="japan", fontsize=11)
    page.insert_text((50, 266), "日本語の文章も正しく表示できます。", fontname="japan", fontsize=11)
    # Korean paragraph.
    page.insert_text((50, 300), "한국어 문장도", fontname="korea", fontsize=11)
    page.insert_text((50, 316), "정확히 렌더링됩니다.", fontname="korea", fontsize=11)
    doc.save(path)
    doc.close()


class CJKHelperTests(unittest.TestCase):
    def test_is_cjk_char_covers_major_blocks(self):
        for c in "中文测试":
            self.assertTrue(_is_cjk_char(c), c)
        for c in "ひらがなカタカナ":
            self.assertTrue(_is_cjk_char(c), c)
        self.assertTrue(_is_cjk_char("한"))
        for c in "Hello, world!":
            self.assertFalse(_is_cjk_char(c), c)

    def test_cjk_font_routes_per_script(self):
        self.assertEqual(_cjk_font_for_char("中"), "china-s")
        self.assertEqual(_cjk_font_for_char("ひ"), "japan")
        self.assertEqual(_cjk_font_for_char("カ"), "japan")
        self.assertEqual(_cjk_font_for_char("한"), "korea")
        self.assertIsNone(_cjk_font_for_char("A"))

    def test_dominant_cjk_font_prefers_specific_scripts(self):
        # Kana presence wins even when Han is also present.
        self.assertEqual(_dominant_cjk_font("日本語ひらがな"), "japan")
        # Hangul presence wins over plain Han.
        self.assertEqual(_dominant_cjk_font("한자 漢字"), "korea")
        # Pure Han falls back to Simplified Chinese.
        self.assertEqual(_dominant_cjk_font("纯中文"), "china-s")
        # No CJK at all → None (caller keeps its base14 font).
        self.assertIsNone(_dominant_cjk_font("Latin only"))

    def test_tokenizer_splits_cjk_per_char(self):
        tokens = _tokenize_for_wrap("Hello 世界 abc")
        self.assertEqual(tokens, ["Hello", "世", "界", "abc"])

    def test_wrap_breaks_inside_cjk_run(self):
        # A long CJK run with a narrow line should break inside the run,
        # not overflow as a single oversize "word".
        text = "中" * 30
        lines = _wrap_paragraph(text, "china-s", 12.0, max_width=60.0)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(len(ln) < 30 for ln in lines))


class CJKReflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.src = os.path.join(cls.tmp.name, "src.pdf")
        cls.out = os.path.join(cls.tmp.name, "out.pdf")
        _make_cjk_source(cls.src)
        reflow_pdf(cls.src, cls.out, ReflowConfig())
        cls.out_doc = fitz.open(cls.out)
        cls.text = "".join(p.get_text() for p in cls.out_doc)

    @classmethod
    def tearDownClass(cls):
        cls.out_doc.close()
        cls.tmp.cleanup()

    def test_chinese_title_survives(self):
        self.assertIn("中文标题示例", self.text)

    def test_chinese_body_survives(self):
        for fragment in ("English words", "GPT-4", "中文段落"):
            self.assertIn(fragment, self.text)

    def test_japanese_survives(self):
        self.assertIn("これはテストです", self.text)
        self.assertIn("日本語", self.text)

    def test_korean_survives(self):
        self.assertIn("한국어", self.text)
        self.assertIn("렌더링", self.text)

    def test_no_garbage_chars(self):
        for ch in self.text:
            o = ord(ch)
            self.assertNotEqual(o, 0xFFFD, "replacement char in output")
            self.assertFalse(0xE000 <= o <= 0xF8FF,
                             f"private-use char in output: U+{o:04X}")


if __name__ == "__main__":
    unittest.main()
