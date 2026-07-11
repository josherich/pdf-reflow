"""Tests for the Layer-2 visual feedback tools (tools/reflow_verify).

Covers the pieces agents and CI depend on: page rendering, the golden image
diff, the annotation store, the agent-facing feedback summary, and the web
tool's HTTP endpoints (upload golden, save annotations, serve images).
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import fitz  # noqa: E402

from reflow_verify import visual  # noqa: E402
from reflow_verify import webtool  # noqa: E402


def _make_pdf(path: str, texts) -> None:
    doc = fitz.open()
    for t in texts:
        page = doc.new_page(width=200, height=280)
        page.insert_text((20, 40), t, fontsize=14)
    doc.save(path)
    doc.close()


class RenderAndDiffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_render_pages_and_cache(self):
        pdf = os.path.join(self.tmp, "a.pdf")
        _make_pdf(pdf, ["one", "two"])
        pages_dir = os.path.join(self.tmp, "pages")
        names = visual.render_pdf_pages(pdf, pages_dir)
        self.assertEqual(names, ["page-001.png", "page-002.png"])
        for n in names:
            self.assertTrue(os.path.exists(os.path.join(pages_dir, n)))
        # Second call hits the mtime cache and returns the same list.
        self.assertEqual(visual.render_pdf_pages(pdf, pages_dir), names)

    def test_identical_images_diff_zero(self):
        pdf = os.path.join(self.tmp, "a.pdf")
        _make_pdf(pdf, ["hello world"])
        names = visual.render_pdf_pages(pdf, self.tmp)
        img = os.path.join(self.tmp, names[0])
        self.assertEqual(visual.image_diff_ratio(img, img), 0.0)

    def test_different_content_diffs(self):
        a, b = os.path.join(self.tmp, "a.pdf"), os.path.join(self.tmp, "b.pdf")
        _make_pdf(a, ["hello world"])
        _make_pdf(b, ["a completely different page\nwith more text"])
        na = visual.render_pdf_pages(a, os.path.join(self.tmp, "pa"))
        nb = visual.render_pdf_pages(b, os.path.join(self.tmp, "pb"))
        ratio = visual.image_diff_ratio(os.path.join(self.tmp, "pa", na[0]),
                                        os.path.join(self.tmp, "pb", nb[0]))
        self.assertGreater(ratio, 0.0)

    def test_size_mismatch_penalized(self):
        """A golden with a different aspect ratio must not diff to 0."""
        tall, short = os.path.join(self.tmp, "t.pdf"), os.path.join(self.tmp, "s.pdf")
        doc = fitz.open(); doc.new_page(width=200, height=400); doc.save(tall); doc.close()
        doc = fitz.open(); doc.new_page(width=200, height=200); doc.save(short); doc.close()
        nt = visual.render_pdf_pages(tall, os.path.join(self.tmp, "pt"))
        ns = visual.render_pdf_pages(short, os.path.join(self.tmp, "ps"))
        ratio = visual.image_diff_ratio(os.path.join(self.tmp, "pt", nt[0]),
                                        os.path.join(self.tmp, "ps", ns[0]))
        self.assertGreater(ratio, 0.4)  # half the tall page has no counterpart

    def test_golden_compare_matches_generated(self):
        pdf = os.path.join(self.tmp, "fix.pdf")
        _make_pdf(pdf, ["page one text"])
        pages_dir = os.path.join(self.tmp, "pages")
        names = visual.render_pdf_pages(pdf, pages_dir)
        golden_dir = os.path.join(self.tmp, "golden")
        os.makedirs(golden_dir)
        shutil.copy(os.path.join(pages_dir, names[0]),
                    os.path.join(golden_dir, "page-001.png"))
        ratios = visual.golden_compare(pdf, pages_dir, golden_dir)
        self.assertEqual(ratios, {1: 0.0})


class AnnotationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_roundtrip_and_validation(self):
        anns = [
            {"page": 1, "rect": [0.1, 0.2, 0.5, 0.4], "note": "heading merged"},
            {"page": 0, "rect": [0, 0, 1, 1], "note": "bad page, dropped"},
            {"page": 2, "rect": [0.1, 0.2], "note": "bad rect, dropped"},
            "not a dict",
        ]
        saved = visual.save_annotations(self.tmp, "fix", anns)
        self.assertEqual(len(saved["annotations"]), 1)
        a = saved["annotations"][0]
        self.assertEqual(a["status"], "open")
        self.assertTrue(a["id"])
        loaded = visual.load_annotations(self.tmp, "fix")
        self.assertEqual(loaded["annotations"], saved["annotations"])
        self.assertEqual(visual.open_annotation_count(self.tmp, "fix"), 1)

    def test_missing_file_is_empty(self):
        data = visual.load_annotations(self.tmp, "nope")
        self.assertEqual(data["annotations"], [])
        self.assertEqual(visual.open_annotation_count(self.tmp, "nope"), 0)


class FeedbackSummaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.out = os.path.join(self.tmp, "out")
        self.pages = os.path.join(self.tmp, "pages")
        self.golden = os.path.join(self.tmp, "golden")
        self.feedback = os.path.join(self.tmp, "feedback")
        for d in (self.out, self.golden, self.feedback):
            os.makedirs(d)
        _make_pdf(os.path.join(self.out, "fix.pdf"), ["page one"])

    def test_summary_merges_golden_and_annotations(self):
        names = visual.render_pdf_pages(os.path.join(self.out, "fix.pdf"),
                                        os.path.join(self.pages, "fix"))
        g_dir = os.path.join(self.golden, "fix")
        os.makedirs(g_dir)
        shutil.copy(os.path.join(self.pages, "fix", names[0]),
                    os.path.join(g_dir, "page-001.png"))
        visual.save_annotations(self.feedback, "fix",
                                [{"page": 1, "rect": [0.25, 0.5, 0.75, 0.75],
                                  "note": "figure clipped"}])

        s = visual.feedback_summary(self.out, self.pages, self.golden, self.feedback)
        self.assertEqual(len(s["fixtures"]), 1)
        fx = s["fixtures"][0]
        self.assertEqual(fx["fixture"], "fix.pdf")
        self.assertEqual(fx["golden"]["mean_diff_ratio"], 0.0)
        self.assertEqual(fx["open_annotations"], 1)
        ann = fx["annotations"][0]
        # Page is 200x280pt; normalized rect converts to PDF points.
        self.assertEqual(ann["rect_pdf"], [50.0, 140.0, 150.0, 210.0])
        self.assertEqual(ann["note"], "figure clipped")

    def test_fixture_without_feedback_excluded(self):
        s = visual.feedback_summary(self.out, self.pages, self.golden, self.feedback)
        self.assertEqual(s["fixtures"], [])


class WebToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.out = os.path.join(cls.tmp, "out")
        cls.pages = os.path.join(cls.tmp, "pages")
        cls.golden = os.path.join(cls.tmp, "golden")
        cls.feedback = os.path.join(cls.tmp, "feedback")
        os.makedirs(cls.out)
        _make_pdf(os.path.join(cls.out, "fix.pdf"), ["served page"])
        cls.httpd = webtool.make_server(cls.tmp, cls.out, cls.pages,
                                        cls.golden, cls.feedback, port=0)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.tmp)

    def _request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_index_lists_fixture(self):
        status, body = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"fix.pdf", body)

    def test_compare_and_annotate_pages_render(self):
        for view in ("/compare/fix", "/annotate/fix"):
            status, body = self._request("GET", view)
            self.assertEqual(status, 200, view)
            self.assertIn(b"page-001.png", body)
        status, body = self._request("GET", "/img/pages/fix/page-001.png")
        self.assertEqual(status, 200)
        self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n")

    def test_upload_then_serve_and_delete_golden(self):
        with open(os.path.join(self.pages, "fix", "page-001.png"), "rb") as f:
            png = f.read()
        status, _ = self._request("POST", "/upload/fix/1?name=shot.png", body=png)
        self.assertEqual(status, 200)
        path = os.path.join(self.golden, "fix", "page-001.png")
        self.assertTrue(os.path.exists(path))
        status, body = self._request("GET", "/img/golden/fix/page-001.png")
        self.assertEqual(status, 200)
        self.assertEqual(body, png)
        # Identical golden shows up as diff 0 in the agent summary.
        status, body = self._request("GET", "/api/feedback")
        self.assertEqual(status, 200)
        summary = json.loads(body)
        self.assertEqual(summary["fixtures"][0]["golden"]["mean_diff_ratio"], 0.0)
        status, _ = self._request("POST", "/golden/fix/1/delete")
        self.assertEqual(status, 200)
        self.assertFalse(os.path.exists(path))

    def test_upload_rejects_unknown_extension(self):
        status, _ = self._request("POST", "/upload/fix/1?name=evil.svg", body=b"x")
        self.assertEqual(status, 400)

    def test_annotations_roundtrip_over_http(self):
        payload = json.dumps({"annotations": [
            {"page": 1, "rect": [0.1, 0.1, 0.3, 0.2], "note": "clipped line"}
        ]})
        status, body = self._request("POST", "/annotations/fix", body=payload,
                                     headers={"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        saved = json.loads(body)
        self.assertEqual(saved["annotations"][0]["note"], "clipped line")
        status, body = self._request("GET", "/annotations/fix")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["annotations"], saved["annotations"])

    def test_unknown_fixture_and_traversal_404(self):
        for path in ("/compare/nope", "/annotations/nope",
                     "/img/pages/fix/..%2F..%2Fetc", "/img/pages/../fix/x.png"):
            status, _ = self._request("GET", path)
            self.assertEqual(status, 404, path)


if __name__ == "__main__":
    unittest.main()
