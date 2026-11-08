"""Copy only a formula's original glyph outlines, excluding neighboring ink.

A rectangular page crop is insufficient for inline sums/integrals: their
limits can share the vertical extent of adjacent prose rows. MuPDF's SVG
export exposes each painted glyph with its original font path and matrix.
Selecting those glyphs by source baseline and x range preserves typography
without copying the unrelated characters inside a rectangular crop.
"""
from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET

import fitz

from .extract import MathFragment

_SVG = "http://www.w3.org/2000/svg"
_HREF = "{http://www.w3.org/1999/xlink}href"
ET.register_namespace("", _SVG)
ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")


class FormulaRenderer:
    def __init__(self, source: fitz.Document):
        self.source = source
        self.pages: dict = {}
        self.fragments: dict = {}

    def close(self) -> None:
        for doc in self.fragments.values():
            doc.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _page_svg(self, index: int) -> str:
        page = self.source[index]
        if (hasattr(fitz, "PDF_REDACT_TEXT_NONE")
                and any(image[2] * image[3] > 100_000 for image in page.get_images())):
            # SVG export otherwise PNG-encodes every unrelated photograph
            # on the page. Work on a private one-page copy, keeping all
            # source text and dropping graphics; formula rules are copied
            # separately from their original geometry below.
            with fitz.open() as text_only:
                text_only.insert_pdf(self.source, from_page=index, to_page=index)
                clean = text_only[0]
                clean.add_redact_annot(clean.rect, fill=None)
                clean.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE,
                                       graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                                       text=fitz.PDF_REDACT_TEXT_NONE)
                return clean.get_svg_image(text_as_path=True)
        return page.get_svg_image(text_as_path=True)

    def document(self, fragment: MathFragment) -> fitz.Document:
        key = (fragment.source_page, fragment.rect, tuple(fragment.glyph_regions), tuple(fragment.rules))
        if key in self.fragments:
            return self.fragments[key]
        if fragment.source_page not in self.pages:
            root = ET.fromstring(self._page_svg(fragment.source_page))
            defs = {el.attrib["id"]: el for el in root.iter()
                    if el.get("id", "").startswith("font_")}
            uses = []
            for el in root.iter(f"{{{_SVG}}}use"):
                if "data-text" not in el.attrib:
                    continue
                values = re.findall(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?", el.get("transform", ""))
                if len(values) == 6:
                    uses.append((el, tuple(map(float, values))))
            self.pages[fragment.source_page] = defs, uses
        defs, uses = self.pages[fragment.source_page]
        x0, y0, x1, y1 = fragment.rect
        root = ET.Element(f"{{{_SVG}}}svg", {
            "width": str(x1 - x0), "height": str(y1 - y0),
            "viewBox": f"{x0} {y0} {x1 - x0} {y1 - y0}", "version": "1.1",
        })
        definitions = ET.SubElement(root, f"{{{_SVG}}}defs")
        seen = set()
        for el, matrix in uses:
            sx, _, _, sy, x, y = matrix
            for bbox, baseline, size, color in fragment.glyph_regions:
                if (abs(y - baseline) < 0.05 and bbox[0] - 0.05 <= x < bbox[2] - 0.01
                        and abs(abs(sx) - size) < 0.05 and abs(abs(sy) - size) < 0.05):
                    name = el.get(_HREF, "").removeprefix("#")
                    if name in defs:
                        if name not in seen:
                            definitions.append(copy.deepcopy(defs[name]))
                            seen.add(name)
                        glyph = copy.deepcopy(el)
                        glyph.set("fill", f"#{color:06x}")
                        root.append(glyph)
                    break
        if not seen:
            raise ValueError("formula has no matching source glyph outlines")
        for bbox, width in fragment.rules:
            a, b, c, d = bbox
            if d - b > 0:
                ET.SubElement(root, f"{{{_SVG}}}rect", {
                    "x": str(a), "y": str(b), "width": str(c - a), "height": str(d - b),
                    "fill": "black",
                })
            else:
                ET.SubElement(root, f"{{{_SVG}}}path", {
                    "d": f"M {a} {b} L {c} {d}", "stroke": "black",
                    "stroke-width": str(max(width, 0.2)), "fill": "none",
                })
        with fitz.open(stream=ET.tostring(root), filetype="svg") as svg:
            doc = fitz.open(stream=svg.convert_to_pdf(), filetype="pdf")
        self.fragments[key] = doc
        return doc
