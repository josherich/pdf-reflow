"""Layer 2: visual feedback -- golden-image compare and human annotations.

Two feedback channels for reflow quality that the numeric scorecard can't
capture, both fed by a human through the web tool (``tools/verify.py
--serve``) and both readable by a coding agent (``tools/verify.py
--feedback``):

* **Golden compare** -- the user uploads "golden" page images (how a
  reflowed page *should* look) which are saved under
  ``verify/golden/<stem>/page-NNN.<ext>``. The harness renders the actual
  output pages to ``verify/pages/<stem>/`` and reports a pixel diff ratio
  per page (0.0 = identical, 1.0 = every pixel differs).

* **Annotations** -- the user draws boxes on rendered output pages and
  attaches a text note ("heading merged into body", "figure clipped", ...).
  Stored as JSON in ``verify/feedback/<stem>.json`` with rects in
  normalized page coordinates (resolution independent).

``feedback_summary`` merges both into one JSON document for agents, with
annotation rects also converted to PDF points of the output document so an
agent can relate a note back to the geometry it concerns.
"""

from __future__ import annotations

import glob
import json
import os
import time
import uuid
from typing import Dict, List, Optional

import fitz

PAGE_DPI = 144          # rendering resolution for the viewable page images
DIFF_WIDTH = 400        # both images are scaled to this width before diffing
DIFF_THRESHOLD = 32     # per-channel delta below this is "same" (antialiasing)

GOLDEN_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def stem_of(fixture_name: str) -> str:
    """`bitcoin.pdf` -> `bitcoin` (directory / file stem for a fixture)."""
    return os.path.splitext(os.path.basename(fixture_name))[0]


def page_image_name(page: int, ext: str = ".png") -> str:
    return f"page-{page:03d}{ext}"


# ---------------------------------------------------------------------------
# Rendering output PDFs to page images
# ---------------------------------------------------------------------------

def render_pdf_pages(pdf_path: str, pages_dir: str) -> List[str]:
    """Render every page of ``pdf_path`` to PNGs in ``pages_dir``.

    Skips work when the renders are already up to date with the PDF.
    Returns the sorted list of page image filenames.
    """
    meta_path = os.path.join(pages_dir, ".meta.json")
    mtime = os.path.getmtime(pdf_path)
    if os.path.exists(meta_path):
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            if meta.get("mtime") == mtime and all(
                os.path.exists(os.path.join(pages_dir, page_image_name(i + 1)))
                for i in range(int(meta.get("pages", 0)))
            ):
                return [page_image_name(i + 1) for i in range(int(meta["pages"]))]
        except (ValueError, OSError):
            pass

    os.makedirs(pages_dir, exist_ok=True)
    for old in glob.glob(os.path.join(pages_dir, "page-*.png")):
        os.remove(old)

    doc = fitz.open(pdf_path)
    try:
        zoom = PAGE_DPI / 72.0
        names = []
        for i, page in enumerate(doc):
            name = page_image_name(i + 1)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            pix.save(os.path.join(pages_dir, name))
            names.append(name)
    finally:
        doc.close()

    with open(meta_path, "w") as f:
        json.dump({"mtime": mtime, "pages": len(names)}, f)
    return names


# ---------------------------------------------------------------------------
# Golden image diff
# ---------------------------------------------------------------------------

def _pixmap_scaled(path: str, target_w: int) -> "fitz.Pixmap":
    """Open an image (or 1-page doc) and render it ~``target_w`` wide, RGB."""
    doc = fitz.open(path)
    try:
        page = doc[0]
        zoom = target_w / max(1.0, page.rect.width)
        return page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    finally:
        doc.close()


def image_diff_ratio(path_a: str, path_b: str,
                     width: int = DIFF_WIDTH,
                     threshold: int = DIFF_THRESHOLD) -> float:
    """Fraction of pixels that differ between two images, in [0, 1].

    Both images are scaled to ``width`` before comparison so resolution
    doesn't matter; a per-channel delta <= ``threshold`` counts as equal so
    antialiasing noise doesn't. Area outside the common overlap (differing
    aspect ratios) counts as different -- a golden with an extra half page
    of content should not diff to 0.
    """
    pa = _pixmap_scaled(path_a, width)
    pb = _pixmap_scaled(path_b, width)
    w, h = min(pa.width, pb.width), min(pa.height, pb.height)
    sa, sb = pa.samples, pb.samples
    na, nb = pa.n, pb.n
    diff = 0
    for y in range(h):
        ra, rb = y * pa.stride, y * pb.stride
        for x in range(w):
            ia, ib = ra + na * x, rb + nb * x
            if (abs(sa[ia] - sb[ib]) > threshold
                    or abs(sa[ia + 1] - sb[ib + 1]) > threshold
                    or abs(sa[ia + 2] - sb[ib + 2]) > threshold):
                diff += 1
    total = max(pa.width * pa.height, pb.width * pb.height, 1)
    return round((diff + (total - w * h)) / total, 4)


def golden_images(golden_dir: str) -> Dict[int, str]:
    """Map page number -> golden image filename found in ``golden_dir``."""
    out: Dict[int, str] = {}
    if not os.path.isdir(golden_dir):
        return out
    for name in sorted(os.listdir(golden_dir)):
        base, ext = os.path.splitext(name)
        if ext.lower() in GOLDEN_EXTS and base.startswith("page-"):
            try:
                out[int(base[len("page-"):])] = name
            except ValueError:
                continue
    return out


def golden_compare(out_pdf: str, pages_dir: str, golden_dir: str) -> Dict[int, float]:
    """Per-page pixel diff ratio for every page that has a golden image."""
    goldens = golden_images(golden_dir)
    if not goldens:
        return {}
    names = render_pdf_pages(out_pdf, pages_dir)
    ratios: Dict[int, float] = {}
    for page, gname in goldens.items():
        if 1 <= page <= len(names):
            ratios[page] = image_diff_ratio(
                os.path.join(golden_dir, gname),
                os.path.join(pages_dir, names[page - 1]),
            )
    return ratios


# ---------------------------------------------------------------------------
# Annotation store (verify/feedback/<stem>.json)
# ---------------------------------------------------------------------------

def feedback_path(feedback_dir: str, stem: str) -> str:
    return os.path.join(feedback_dir, f"{stem}.json")


def load_annotations(feedback_dir: str, stem: str) -> Dict[str, object]:
    path = feedback_path(feedback_dir, stem)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("annotations"), list):
                return data
        except (ValueError, OSError):
            pass
    return {"fixture": f"{stem}.pdf", "annotations": []}


def save_annotations(feedback_dir: str, stem: str,
                     annotations: List[Dict[str, object]]) -> Dict[str, object]:
    """Validate + persist the full annotation list for a fixture."""
    clean: List[Dict[str, object]] = []
    for a in annotations:
        if not isinstance(a, dict):
            continue
        rect = a.get("rect")
        page = a.get("page")
        if (not isinstance(page, int) or page < 1
                or not isinstance(rect, list) or len(rect) != 4
                or not all(isinstance(v, (int, float)) for v in rect)):
            continue
        clean.append({
            "id": str(a.get("id") or uuid.uuid4().hex[:12]),
            "page": page,
            "rect": [round(max(0.0, min(1.0, float(v))), 4) for v in rect],
            "note": str(a.get("note", "")).strip(),
            "status": "resolved" if a.get("status") == "resolved" else "open",
            "created": str(a.get("created") or _now()),
        })
    data = {"fixture": f"{stem}.pdf", "updated": _now(), "annotations": clean}
    os.makedirs(feedback_dir, exist_ok=True)
    with open(feedback_path(feedback_dir, stem), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return data


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ---------------------------------------------------------------------------
# Agent-facing summary
# ---------------------------------------------------------------------------

def _page_sizes(pdf_path: str) -> List[fitz.Rect]:
    doc = fitz.open(pdf_path)
    try:
        return [page.rect for page in doc]
    finally:
        doc.close()


def feedback_summary(out_dir: str, pages_dir: str, golden_dir: str,
                     feedback_dir: str,
                     stems: Optional[List[str]] = None) -> Dict[str, object]:
    """One JSON document with all human visual feedback, for coding agents.

    Includes only fixtures that actually carry a signal (a golden image or
    an annotation). Annotation rects are given both normalized (fraction of
    the page) and in PDF points of the reflowed output document.
    """
    if stems is None:
        stems = sorted(
            {stem_of(p) for p in glob.glob(os.path.join(out_dir, "*.pdf"))}
            | {stem_of(p) for p in glob.glob(os.path.join(golden_dir, "*")) if os.path.isdir(p)}
            | {stem_of(p) for p in glob.glob(os.path.join(feedback_dir, "*.json"))}
        )

    fixtures: List[Dict[str, object]] = []
    for stem in stems:
        out_pdf = os.path.join(out_dir, f"{stem}.pdf")
        g_dir = os.path.join(golden_dir, stem)
        entry: Dict[str, object] = {"fixture": f"{stem}.pdf"}

        goldens = golden_images(g_dir)
        if goldens and os.path.exists(out_pdf):
            ratios = golden_compare(out_pdf, os.path.join(pages_dir, stem), g_dir)
            pages = [
                {
                    "page": p,
                    "golden_image": os.path.join(g_dir, goldens[p]),
                    "generated_image": os.path.join(pages_dir, stem, page_image_name(p)),
                    "diff_ratio": ratios.get(p),
                }
                for p in sorted(goldens)
            ]
            valid = [r for r in ratios.values() if r is not None]
            entry["golden"] = {
                "pages": pages,
                "mean_diff_ratio": round(sum(valid) / len(valid), 4) if valid else None,
            }

        data = load_annotations(feedback_dir, stem)
        anns = data.get("annotations") or []
        if anns:
            sizes = _page_sizes(out_pdf) if os.path.exists(out_pdf) else []
            enriched = []
            for a in anns:
                item = dict(a)
                item["rect_norm"] = item.pop("rect")
                p = item.get("page", 0)
                if 1 <= p <= len(sizes):
                    r = sizes[p - 1]
                    x0, y0, x1, y1 = item["rect_norm"]
                    item["rect_pdf"] = [round(x0 * r.width, 1), round(y0 * r.height, 1),
                                        round(x1 * r.width, 1), round(y1 * r.height, 1)]
                    item["page_image"] = os.path.join(pages_dir, stem, page_image_name(p))
                enriched.append(item)
            entry["annotations"] = enriched
            entry["open_annotations"] = sum(1 for a in anns if a.get("status") == "open")

        if "golden" in entry or "annotations" in entry:
            fixtures.append(entry)

    return {
        "generated_at": _now(),
        "hint": (
            "Human visual feedback on reflow output. golden.*.diff_ratio is the "
            "fraction of pixels differing from the user-uploaded golden page image "
            "(0 = matches the desired look). annotations are user-drawn boxes with "
            "notes on rendered output pages; fix 'open' ones, using rect_pdf (PDF "
            "points in verify/out/<fixture>) to locate the region, and view the "
            "referenced images for context. Re-run `python tools/verify.py "
            "--feedback` after changes to see updated diff ratios."
        ),
        "fixtures": fixtures,
    }


def open_annotation_count(feedback_dir: str, stem: str) -> int:
    anns = load_annotations(feedback_dir, stem).get("annotations") or []
    return sum(1 for a in anns if a.get("status") == "open")
