"""Deterministic Layer-1 scorecard for a single fixture.

The reference (ground truth) for reflow is the *source itself*: reflow must
carry every word across in reading order while only changing geometry. We
therefore build the reference reading-order token stream straight from the
analyzer's ``FlowItem`` list (the pipeline's own notion of reading order)
and diff it against the tokens read back out of the reflowed PDF.
"""

from __future__ import annotations

import difflib
import re
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Tuple

import fitz

from pdf_reflow import reflow_pdf, ReflowConfig
from pdf_reflow.extract import extract_document
from pdf_reflow.analyze import analyze_document


# Text-bearing FlowItem kinds, in the order the pipeline reads them.
_TEXT_KINDS = {"heading", "body", "caption", "toc", "label", "code"}
_PUA_RE = re.compile("[\ue000-\uf8ff]")  # Unicode Private Use Area (math-font garbage)
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _norm(s: str) -> str:
    """NFKC + collapse whitespace + casefold, for order-insensitive match."""
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", " ", s).strip().casefold()


def _tokens(s: str) -> List[str]:
    return _WORD_RE.findall(_norm(s))


def reference_stream(items) -> Tuple[List[str], List[str]]:
    """(reading-order tokens, heading texts) from analyzed FlowItems."""
    tokens: List[str] = []
    headings: List[str] = []
    for it in items:
        if it.kind == "figure":
            continue
        if it.kind == "code" and it.code_lines:
            text = "\n".join(it.code_lines)
        else:
            text = it.text
        if it.kind == "heading" and _norm(text):
            headings.append(text)
        if it.kind in _TEXT_KINDS:
            tokens.extend(_tokens(text))
    return tokens, headings


def output_stream(doc: "fitz.Document") -> List[str]:
    """Reading-order tokens read back out of the reflowed PDF."""
    tokens: List[str] = []
    for page in doc:
        blocks = page.get_text("blocks")  # (x0,y0,x1,y1,text,no,type)
        text_blocks = [b for b in blocks if len(b) < 7 or b[6] == 0]
        # Single-column output => plain top-to-bottom, then left-to-right.
        text_blocks.sort(key=lambda b: (round(b[1], 1), round(b[0], 1)))
        for b in text_blocks:
            tokens.extend(_tokens(b[4]))
    return tokens


def word_diff(ref: List[str], out: List[str]) -> Dict[str, float]:
    """Bast/Korzen word-level taxonomy via a sequence alignment.

      W-  : reference words missing from output (dropped text)
      W+  : output words not in reference (spurious text, e.g. headers kept)
      W~  : words that changed (garbled / mis-mapped glyphs)
      retention = matched / len(ref)   -- the headline number
    """
    sm = difflib.SequenceMatcher(a=ref, b=out, autojunk=False)
    matched = w_minus = w_plus = w_tilde = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        la, lb = i2 - i1, j2 - j1
        if tag == "equal":
            matched += la
        elif tag == "delete":
            w_minus += la
        elif tag == "insert":
            w_plus += lb
        elif tag == "replace":
            common = min(la, lb)
            w_tilde += common
            w_minus += la - common
            w_plus += lb - common
    ref_n = max(1, len(ref))
    return {
        "ref_words": len(ref),
        "out_words": len(out),
        "matched": matched,
        "w_minus": w_minus,
        "w_plus": w_plus,
        "w_tilde": w_tilde,
        "retention": round(matched / ref_n, 4),
    }


def heading_retention(headings: List[str], out_doc: "fitz.Document") -> Dict[str, float]:
    """Fraction of source headings whose text survives in the output."""
    full = _norm(" ".join(page.get_text("text") for page in out_doc))
    kept = 0
    missing: List[str] = []
    for h in headings:
        hn = _norm(h)
        if hn and hn in full:
            kept += 1
        elif hn:
            missing.append(h)
    n = max(1, len(headings))
    return {
        "headings": len(headings),
        "headings_kept": kept,
        "heading_retention": round(kept / n, 4),
        "headings_missing": missing[:20],
    }


def render_quality(out_doc: "fitz.Document") -> Dict[str, float]:
    """Signals visible to a reader: text clipped by the page edge, private-use
    glyph garbage leaking into text, and (informational) very short widow
    lines. ``clipped_lines`` is the one that means "this looks broken"."""
    clipped = 0
    total_lines = 0
    widows = 0
    pua = 0
    for page in out_doc:
        right = page.rect.x1
        d = page.get_text("dict")
        for block in d.get("blocks", []):
            for line in block.get("lines", []):
                total_lines += 1
                x0, _, x1, _ = line["bbox"]
                if x1 > right - 1.0:
                    clipped += 1
                text = "".join(sp["text"] for sp in line["spans"])
                pua += len(_PUA_RE.findall(text))
                if 0 < len(text.strip()) <= 2 and len(line["spans"]) <= 1:
                    widows += 1
    return {
        "output_lines": total_lines,
        "clipped_lines": clipped,
        "widow_lines": widows,
        "pua_chars": pua,
    }


def figure_balance(items, out_doc: "fitz.Document") -> Dict[str, int]:
    """How many figures the analyzer wanted vs. images actually rendered."""
    wanted = sum(1 for it in items if it.kind == "figure")
    rendered = sum(len(page.get_images(full=False)) for page in out_doc)
    return {"figures_wanted": wanted, "images_rendered": rendered}


@dataclass
class FixtureScore:
    name: str
    source_pages: int = 0
    output_pages: int = 0
    seconds: float = 0.0
    metrics: Dict[str, object] = field(default_factory=dict)

    def flat(self) -> Dict[str, object]:
        """Numeric metrics used for baseline comparison (drops list fields)."""
        out: Dict[str, object] = {
            "source_pages": self.source_pages,
            "output_pages": self.output_pages,
        }
        for k, v in self.metrics.items():
            if isinstance(v, (int, float)):
                out[k] = v
        return out

    def to_json(self) -> Dict[str, object]:
        return asdict(self)


def score_fixture(pdf_path: str, out_path: str, cfg: ReflowConfig | None = None) -> FixtureScore:
    """Reflow ``pdf_path`` -> ``out_path`` and compute the full scorecard."""
    cfg = cfg or ReflowConfig()

    t0 = time.perf_counter()
    stats = reflow_pdf(pdf_path, out_path, cfg)
    seconds = time.perf_counter() - t0

    # Reference: analyze the source independently of the reflow call.
    src = fitz.open(pdf_path)
    try:
        items, _ = analyze_document(extract_document(src))
    finally:
        src.close()
    ref_tokens, headings = reference_stream(items)

    out_doc = fitz.open(out_path)
    try:
        metrics: Dict[str, object] = {}
        metrics.update(word_diff(ref_tokens, output_stream(out_doc)))
        metrics.update(heading_retention(headings, out_doc))
        metrics.update(render_quality(out_doc))
        metrics.update(figure_balance(items, out_doc))
        metrics["seconds"] = round(seconds, 3)
        return FixtureScore(
            name=pdf_path.rsplit("/", 1)[-1],
            source_pages=stats["source_pages"],
            output_pages=stats["output_pages"],
            seconds=round(seconds, 3),
            metrics=metrics,
        )
    finally:
        out_doc.close()
