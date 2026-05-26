"""CJK reflow tests.

Base14 PDF fonts (Times / Courier / Helvetica) have no CJK glyphs, so
prior to CJK support every Chinese / Japanese / Korean codepoint was
silently dropped by ``insert_text``. These tests synthesize a small
mixed-script PDF, reflow it, and verify the CJK characters survive the
round-trip. They also reflow a real-world sinology paper with inline
Chinese embedded in English prose to exercise mixed-script wrapping.

The synthetic fixture is generated at runtime with PyMuPDF's built-in
CJK fonts so most tests don't depend on a checked-in binary PDF.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fitz

from pdf_reflow import cjk_fonts
from pdf_reflow.cjk_fonts import (
    SCRIPT_HAN,
    SCRIPT_JAPAN,
    SCRIPT_KOREA,
    CJKFontStore,
)
from pdf_reflow.layout import (
    _cjk_script_for_char,
    _dominant_cjk_font,
    _is_cjk_char,
    _tokenize_for_wrap,
    _wrap_paragraph,
)
from pdf_reflow.reflow import ReflowConfig, reflow_pdf


FIXTURES = Path(__file__).parent / "fixtures"
OLD_CHINESE_PDF = FIXTURES / "old-chinese-a-new-construction.pdf"


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
    """Tests for the script-detection helpers in layout.py.

    These cover *script* identity (Han / Japanese / Korean), which is
    system-independent. Concrete fontname resolution is tested
    separately so the script tests don't break depending on which CJK
    fonts happen to be installed on the test host.
    """

    def test_is_cjk_char_covers_major_blocks(self):
        for c in "中文测试":
            self.assertTrue(_is_cjk_char(c), c)
        for c in "ひらがなカタカナ":
            self.assertTrue(_is_cjk_char(c), c)
        self.assertTrue(_is_cjk_char("한"))
        for c in "Hello, world!":
            self.assertFalse(_is_cjk_char(c), c)

    def test_cjk_script_routes_per_codepoint(self):
        self.assertEqual(_cjk_script_for_char("中"), SCRIPT_HAN)
        self.assertEqual(_cjk_script_for_char("ひ"), SCRIPT_JAPAN)
        self.assertEqual(_cjk_script_for_char("カ"), SCRIPT_JAPAN)
        self.assertEqual(_cjk_script_for_char("한"), SCRIPT_KOREA)
        self.assertIsNone(_cjk_script_for_char("A"))

    def test_dominant_cjk_font_prefers_specific_scripts(self):
        # _dominant_cjk_font returns a *concrete fontname* (which depends
        # on what system fonts are installed). Pin the resolver to the
        # bundled CID fonts so the asserts stay deterministic.
        bundled = CJKFontStore(platform="zos")  # any non-real platform → bundled
        with mock.patch.object(cjk_fonts, "STORE", bundled):
            # Kana presence wins even when Han is also present.
            self.assertEqual(_dominant_cjk_font("日本語ひらがな"), "japan")
            # Hangul presence wins over plain Han.
            self.assertEqual(_dominant_cjk_font("한자 漢字"), "korea")
            # Pure Han falls back to Simplified Chinese.
            self.assertEqual(_dominant_cjk_font("纯中文"), "china-s")
        # No CJK at all → None (caller keeps its base14 font), independent
        # of the resolver.
        self.assertIsNone(_dominant_cjk_font("Latin only"))

    def test_tokenizer_splits_cjk_per_char_and_records_source_spaces(self):
        # Each token is (text, had_leading_whitespace). "Hello" starts
        # the string → no leading ws. The first CJK char has a leading
        # space ("Hello SPC 世"); the next CJK char does not (CJK chars
        # run flush against each other in the source). "abc" was
        # preceded by a space in the source.
        tokens = _tokenize_for_wrap("Hello 世界 abc")
        self.assertEqual(
            tokens,
            [("Hello", False), ("世", True), ("界", False), ("abc", True)],
        )

    def test_tokenizer_preserves_inline_cjk_to_latin_space(self):
        # 詩經 Shījīng — the space between the gloss and the
        # transcription must surface so the wrapper can re-emit it.
        tokens = _tokenize_for_wrap("the 詩經 Shījīng better")
        self.assertEqual(tokens[0], ("the", False))
        # Find the (詩, _) entry and its successors.
        idx = next(i for i, (t, _) in enumerate(tokens) if t == "詩")
        # 詩 is preceded by a space (it follows "the "); 經 follows
        # 詩 with no space; Shījīng follows 經 with a space.
        self.assertEqual(tokens[idx], ("詩", True))
        self.assertEqual(tokens[idx + 1], ("經", False))
        self.assertEqual(tokens[idx + 2], ("Shījīng", True))

    def test_wrap_breaks_inside_cjk_run(self):
        # A long CJK run with a narrow line should break inside the run,
        # not overflow as a single oversize "word".
        text = "中" * 30
        lines = _wrap_paragraph(text, "china-s", 12.0, max_width=60.0)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(len(ln) < 30 for ln in lines))

    def test_wrap_joins_inline_cjk_and_latin_without_extra_spaces(self):
        # "鄭 zhèng" should stay glued: no Latin↔CJK separator inserted.
        # The wrap is wide enough to fit on one line.
        line, = _wrap_paragraph("rhymes of the 詩經 Shījīng better",
                                "china-s", 11.0, max_width=600.0)
        # Order preserved; CJK chars and surrounding Latin words present.
        self.assertIn("詩經", line)
        self.assertIn("Shījīng", line)
        # Latin↔Latin separators kept ("rhymes of the").
        self.assertIn("rhymes of the", line)


class CJKFontStoreTests(unittest.TestCase):
    """Verify the OS-aware CJK font resolver."""

    def test_falls_back_to_bundled_when_no_system_font(self):
        store = CJKFontStore(platform="zos")  # unknown platform → no system candidates
        for script, bundled in [(SCRIPT_HAN, "china-s"),
                                (SCRIPT_JAPAN, "japan"),
                                (SCRIPT_KOREA, "korea")]:
            entry = store.resolve(script)
            self.assertEqual(entry.fontname, bundled)
            self.assertIsNone(entry.fontfile)
            self.assertTrue(entry.fullwidth)
            self.assertFalse(entry.is_system)

    def test_picks_system_font_when_present(self):
        with tempfile.NamedTemporaryFile(suffix=".ttf", delete=False) as f:
            fake = f.name
        try:
            store = CJKFontStore(
                platform="linux",
                extra_candidates={SCRIPT_HAN: [("FakeHan", fake)]},
            )
            entry = store.resolve(SCRIPT_HAN)
            self.assertEqual(entry.fontname, "FakeHan")
            self.assertEqual(entry.fontfile, fake)
            self.assertFalse(entry.fullwidth)
            self.assertTrue(entry.is_system)
        finally:
            os.unlink(fake)

    def test_missing_extra_skips_to_next_candidate(self):
        # First override doesn't exist on disk → resolver skips it and
        # falls through to whatever the platform default resolves to
        # (bundled on "zos").
        store = CJKFontStore(
            platform="zos",
            extra_candidates={SCRIPT_HAN: [("Ghost", "/nonexistent/path.ttf")]},
        )
        entry = store.resolve(SCRIPT_HAN)
        self.assertEqual(entry.fontname, "china-s")
        self.assertIsNone(entry.fontfile)

    def test_resolve_is_cached(self):
        store = CJKFontStore(platform="zos")
        a = store.resolve(SCRIPT_HAN)
        b = store.resolve(SCRIPT_HAN)
        self.assertIs(a, b)

    def test_by_fontname_reverse_lookup(self):
        store = CJKFontStore(platform="zos")
        self.assertEqual(store.by_fontname("china-s").fontname, "china-s")
        self.assertIsNone(store.by_fontname("times-roman"))

    def test_system_font_actually_renders_through_pipeline(self):
        # Smoke-test the full system-font path: force the Japanese
        # resolver to a real system TTF, then verify that:
        #   1. the font is embedded in the output PDF (system-font path
        #      registers via Page.insert_font with fontfile=)
        #   2. the Japanese text round-trips correctly through extract
        #      → analyze → layout → render with that system font
        jp_ttf = "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf"
        if not os.path.exists(jp_ttf):
            self.skipTest(f"no Japanese system TTF at {jp_ttf}")
        sys_store = CJKFontStore(
            platform="zos",   # disables platform defaults
            extra_candidates={SCRIPT_JAPAN: [("TestIPAGothic", jp_ttf)]},
        )
        # Sanity: the resolver should now report a system font binding.
        self.assertTrue(sys_store.resolve(SCRIPT_JAPAN).is_system)

        with mock.patch.object(cjk_fonts, "STORE", sys_store), \
                tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "in.pdf")
            out = os.path.join(td, "out.pdf")
            # Synthesize a Japanese source — Hiragana + Kanji.
            sd = fitz.open()
            sd.new_page(width=400, height=200).insert_text(
                (40, 60), "システムフォントの試験", fontname="japan", fontsize=14,
            )
            sd.save(src); sd.close()
            reflow_pdf(src, out, ReflowConfig())
            od = fitz.open(out)
            txt = "\n".join(p.get_text() for p in od)
            # Japanese text survives the system-font round-trip.
            self.assertIn("システム", txt)
            self.assertIn("試験", txt)
            # The output PDF embeds the system TTF — PyMuPDF reports
            # the font's intrinsic name (e.g. ``IPAGothic Regular``)
            # rather than our internal alias, so look for that.
            embedded = [f[3] for p_idx in range(od.page_count)
                        for f in od.get_page_fonts(p_idx)]
            self.assertTrue(
                any("IPAGothic" in name for name in embedded),
                f"expected system TTF embedded in output; got {embedded}",
            )
            # And the fallback bundled CJK font name (``japan``) must NOT
            # appear — confirming the system path took priority.
            self.assertFalse(
                any(name in ("japan", "ja", "STSong-Light") for name in embedded),
                f"bundled CJK font leaked into output: {embedded}",
            )
            od.close()


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


class OldChineseFixtureTests(unittest.TestCase):
    """End-to-end reflow of a real sinology paper with inline CJK.

    The fixture is an academic review whose English prose contains
    inline Chinese citations such as ``the rhymes of the 詩經 Shījīng``
    and ``the word 胳 kak``. This exercises:
      * mixed-script paragraphs (Latin + CJK on the same line)
      * the per-paragraph font promotion (Latin font → CJK font when
        any CJK is present)
      * char-level wrapping survival of inline glosses
      * page-bottom flow that wraps the surrounding English correctly
    """

    @classmethod
    def setUpClass(cls):
        if not OLD_CHINESE_PDF.exists():
            raise unittest.SkipTest(f"missing fixture: {OLD_CHINESE_PDF}")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = os.path.join(cls.tmp.name, "out.pdf")
        cls.stats = reflow_pdf(str(OLD_CHINESE_PDF), cls.out)
        cls.out_doc = fitz.open(cls.out)
        # Whole-document text used for substring assertions.
        cls.text = "\n".join(p.get_text() for p in cls.out_doc)
        # A flattened form with whitespace collapsed for fuzzy matches
        # — inline CJK occasionally falls on a wrap boundary so the
        # bridging Latin word may sit on the next line.
        cls.flat = " ".join(cls.text.split())

    @classmethod
    def tearDownClass(cls):
        cls.out_doc.close()
        cls.tmp.cleanup()

    def test_output_pdf_valid(self):
        self.assertGreaterEqual(self.out_doc.page_count, 5)
        self.assertGreater(os.path.getsize(self.out), 5000)

    def test_inline_chinese_glosses_preserved(self):
        # Each of these inline CJK fragments is embedded mid-sentence in
        # English body prose in the source paper; they must survive
        # reflow. (Fragments that live exclusively inside footnotes are
        # excluded — the analyzer occasionally folds footnotes into
        # adjacent figure bands; that's an analyzer issue tracked
        # separately, not a CJK rendering issue.)
        for cjk_fragment in ("詩經", "胳", "曉", "影", "以"):
            self.assertIn(
                cjk_fragment, self.text,
                f"CJK fragment {cjk_fragment!r} dropped from reflowed output",
            )

    def test_inline_cjk_keeps_neighbouring_latin(self):
        # The pinyin / transcription word adjacent to each CJK gloss
        # must also survive — verifies the mixed-script wrapper didn't
        # drop tokens at the script boundary. Source-side whitespace
        # between CJK and Latin is preserved as a single space.
        for pair in ["詩經 Shījīng", "胳 kak"]:
            self.assertIn(
                pair, self.flat,
                f"mixed-script fragment {pair!r} not found in flattened text",
            )

    def test_english_prose_around_cjk_intact(self):
        # The English sentence framing the 詩經 citation should still
        # read naturally after reflow. Check a recognisable phrase
        # that brackets the CJK.
        self.assertIn("rhymes of the", self.flat)
        self.assertIn("Shījīng", self.flat)

    def test_no_garbage_box_chars(self):
        for ch in self.text:
            o = ord(ch)
            self.assertNotEqual(o, 0xFFFD,
                                "U+FFFD replacement char leaked into output")
            self.assertFalse(0xE000 <= o <= 0xF8FF,
                             f"private-use char in output: U+{o:04X}")

    def test_reflow_stats(self):
        self.assertEqual(self.stats["source_pages"], 7)
        self.assertGreater(self.stats["items"], 20)


if __name__ == "__main__":
    unittest.main()
