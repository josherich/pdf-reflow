"""Small independent accuracy oracles; expectations never come from analysis.

These complement corpus retention: a self-derived reference cannot detect
an analyzer losing a hyphen or a layout engine stranding a heading.
"""
from __future__ import annotations

from pdf_reflow.analyze import Block, FlowItem, Line, _merge_continuation_items
from pdf_reflow.extract import Span
from pdf_reflow.layout import DrawText, FontMetrics, LayoutConfig, layout


def compound_hyphen() -> bool:
    lines = [Line([Span(t, (20, y, 180, y + 11), "Times-Roman", 11, 4, 0)],
                  (20, y, 180, y + 11), 11) for t, y in
             [("Trained with Megatron-", 20), ("LM for research.", 34)]]
    return Block(lines, (20, 20, 180, 45), 0).text == "Trained with Megatron-LM for research."


def code_fits() -> bool:
    source = "    result = " + "very_long_identifier_" * 12
    cfg = LayoutConfig()
    pages, _ = layout([FlowItem("code", 0, (0, 0, 100, 10),
                               code_lines=[source, "    return result"])], cfg)
    ops = [op for p in pages for op in p.ops if isinstance(op, DrawText)]
    return ("".join(op.text for op in ops) == source + "    return result"
            and all(op.x + FontMetrics.width(op.font, op.text, op.size)
                    <= cfg.content_right + 1e-5 for op in ops))


def heading_stays_with_body() -> bool:
    cfg = LayoutConfig(page_height=180)
    items = [FlowItem("body", 0, (0, 0, 100, 10), text="\n".join(["lead"] * 7)),
             FlowItem("heading", 0, (0, 0, 100, 10), text="Section", size=14),
             FlowItem("body", 0, (0, 0, 100, 10), text="Following paragraph.")]
    pages, anchors = layout(items, cfg)
    heading_page = anchors[0].out_page
    return any(isinstance(op, DrawText) and "Following" in op.text for op in pages[heading_page].ops)


def continuation_stops_in_same_lane() -> bool:
    items = [FlowItem("body", page, (0, 0, 100, 10), text=text, size=11)
             for page, text in [(0, "A sentence continues"), (1, "on this page"),
                                (1, "another deliberately separate paragraph.")]]
    merged = _merge_continuation_items(items)
    return (len(merged) == 2 and merged[0].text == "A sentence continues on this page"
            and merged[1].text == "another deliberately separate paragraph.")


PROBES = {"compound_hyphen": compound_hyphen, "code_fits": code_fits,
          "heading_stays_with_body": heading_stays_with_body,
          "continuation_stops_in_same_lane": continuation_stops_in_same_lane}


def run_probes() -> dict[str, bool]:
    return {name: probe() for name, probe in PROBES.items()}


def _fixture_items(name: str, page: int):
    from pathlib import Path
    import fitz
    from pdf_reflow.extract import extract_document
    from pdf_reflow.analyze import analyze_document
    path = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / name
    with fitz.open(path) as doc:
        return analyze_document(extract_document(doc, page_indices=[page]))[0]


def attention_equation_keeps_definition() -> bool:
    items = _fixture_items("llm-cjk.pdf", 10)
    equations = [it for it in items if it.kind == "figure" and 155 < it.bbox[1] < 170]
    paragraphs = [it for it in items if "其中，" in it.text and "Query" in it.text
                  and "矩阵计算中需要进行掩码" in it.text]
    return bool(equations and paragraphs and (equations[0].source_rect or equations[0].bbox)[3] < 194)


def inline_radical_keeps_bar_and_radicand() -> bool:
    items = _fixture_items("llm-cjk.pdf", 10)
    fragments = [f for it in items for f in getattr(it, "inline_math", {}).values()]
    return any("√" in f.text and "d" in f.text
               and any(abs(rule[0][1] - 213.249) < 0.01 for rule in f.rules)
               and abs(f.baseline - 222.407) < 0.01 for f in fragments)


def aligned_equation_keeps_all_rows() -> bool:
    items = _fixture_items("mit_latex_sample.pdf", 1)
    return any(it.kind == "figure" and 450 < (it.source_rect or it.bbox)[1] < 460
               and (it.source_rect or it.bbox)[3] > 500 for it in items)


PROBES.update({"attention_equation_keeps_definition": attention_equation_keeps_definition,
               "inline_radical_keeps_bar_and_radicand": inline_radical_keeps_bar_and_radicand,
               "aligned_equation_keeps_all_rows": aligned_equation_keeps_all_rows})
