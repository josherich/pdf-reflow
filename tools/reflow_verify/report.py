"""Layer 3: the HTML scorecard report.

A browsable summary: a card per fixture, and a detail page with the scorecard
delta vs baseline and any headings that fell out of the output. No page images
-- this layer is the numbers, made easy to read and diff at a glance. Pure
string templating; no templating engine, no JS.
"""

from __future__ import annotations

import html
from typing import Dict, List

from .baseline import MetricDelta
from .metrics import FixtureScore

_CSS = """
:root{--bg:#0b0e14;--panel:#131826;--ink:#e6e9ef;--dim:#9aa4b8;--rule:#232b3d;
--good:#9ece6a;--bad:#f7768e;--warn:#e0af68;--accent:#7aa2f7}
@media(prefers-color-scheme:light){:root{--bg:#f7f8fb;--panel:#fff;--ink:#1a1f2e;
--dim:#5a6478;--rule:#e2e7ef}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:24px 18px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px;
border-bottom:1px solid var(--rule);padding-bottom:6px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.muted{color:var(--dim)}
table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13.5px}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--rule)}
th{color:var(--dim);font-weight:600}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600}
.ok{color:var(--good)}.fail{color:var(--bad)}.warnc{color:var(--warn)}
.pill.ok{background:rgba(158,206,106,.15)}.pill.fail{background:rgba(247,118,142,.15)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--rule);border-radius:10px;padding:14px}
.card h3{margin:0 0 6px;font-size:15px}
code{font-family:ui-monospace,Menlo,monospace;font-size:12.5px}
ul{margin:6px 0 0;padding-left:20px}li{margin:2px 0}
h3{font-size:14px;margin:16px 0 6px}
.chips{display:flex;flex-wrap:wrap;gap:5px}
.chip{display:inline-block;padding:1px 7px;border-radius:6px;font-size:12.5px;
font-family:ui-monospace,Menlo,monospace;background:var(--panel);
border:1px solid var(--rule);white-space:pre-wrap}
.glossary td{vertical-align:top}.glossary td:first-child{white-space:nowrap;width:1%}
"""


def _esc(s) -> str:
    return html.escape(str(s))


def _delta_rows(deltas: List[MetricDelta]) -> str:
    rows = []
    for d in deltas:
        if d.regressed:
            status = '<span class="pill fail">REGRESSED</span>'
        elif d.note in ("new",):
            status = '<span class="muted">new</span>'
        elif d.note and d.note != "untracked":
            status = f'<span class="warnc">{_esc(d.note)}</span>'
        else:
            status = '<span class="ok">ok</span>'
        base = "-" if d.baseline is None else f"{d.baseline:.4g}"
        cur = "-" if d.current is None else f"{d.current:.4g}"
        gate = "&#9679;" if d.gate else ""
        rows.append(
            f"<tr><td>{_esc(d.metric)} <span class='muted'>{gate}</span></td>"
            f"<td class='num'>{base}</td><td class='num'>{cur}</td>"
            f"<td>{status}</td></tr>"
        )
    return "\n".join(rows)


# One short sentence per metric, shown as a footer glossary on each report.
_GLOSSARY = [
    ("source_pages", "Number of pages in the original PDF."),
    ("output_pages", "Number of pages in the reflowed mobile PDF."),
    ("ref_words", "Word count of the source's reading-order text — the ground-truth reference."),
    ("out_words", "Word count read back out of the reflowed PDF."),
    ("matched", "Reference words that appear, in order, in the output."),
    ("w_minus", "Reference words missing from the output (dropped text)."),
    ("w_plus", "Output words not in the reference (spurious text, e.g. a kept running header)."),
    ("w_tilde", "Words that changed between source and output (garbled or re-segmented)."),
    ("retention", "Fraction of reference words preserved (matched ÷ ref_words) — the headline fidelity number."),
    ("headings", "Headings the analyzer detected in the source."),
    ("headings_kept", "Source headings whose text is found in the output."),
    ("heading_retention", "Fraction of source headings preserved (headings_kept ÷ headings)."),
    ("output_lines", "Total text lines rendered across the output pages."),
    ("clipped_lines", "Output lines whose text runs past the page edge — a visible layout break."),
    ("widow_lines", "Very short stranded lines (one or two characters alone on a line)."),
    ("pua_chars", "Private-use-area glyphs leaking into text (math-font garbage)."),
    ("figures_wanted", "Figures the analyzer decided to rasterize from the source."),
    ("images_rendered", "Images actually placed in the output PDF."),
    ("seconds", "Wall-clock time to reflow this fixture."),
]


def _glossary_section() -> str:
    rows = "".join(
        f"<tr><td><code>{_esc(name)}</code></td><td class='muted'>{_esc(desc)}</td></tr>"
        for name, desc in _GLOSSARY
    )
    return (
        "<h2>Metrics</h2>"
        "<table class='glossary'><tbody>" + rows + "</tbody></table>"
    )


def _word_chips(words: List[str]) -> str:
    return "".join(f"<span class='chip'>{_esc(w)}</span>" for w in words)


def _word_section(score: FixtureScore) -> str:
    """Show the actual dropped / spurious / changed words, not just counts."""
    m = score.metrics
    groups = [
        ("w_minus", "Dropped words (in source, not in output)",
         m.get("w_minus_words") or [], m.get("w_minus", 0)),
        ("w_plus", "Spurious words (in output, not in source)",
         m.get("w_plus_words") or [], m.get("w_plus", 0)),
        ("w_tilde", "Changed words (source → output)",
         m.get("w_tilde_words") or [], m.get("w_tilde", 0)),
    ]
    blocks = []
    for _, label, words, total in groups:
        if not total:
            continue
        shown = len(words)
        more = f" <span class='muted'>(showing {shown} of {total})</span>" if total > shown else ""
        blocks.append(
            f"<h3>{_esc(label)} — {total}{more}</h3>"
            f"<div class='chips'>{_word_chips(words)}</div>"
        )
    if not blocks:
        return ""
    return "<h2>Word differences</h2>" + "".join(blocks)


def fixture_page(score: FixtureScore, deltas: List[MetricDelta]) -> str:
    miss = score.metrics.get("headings_missing") or []
    miss_html = ""
    if miss:
        items = "".join(f"<li><code>{_esc(m)}</code></li>" for m in miss)
        miss_html = f"<h2>Headings missing from output ({len(miss)})</h2><ul>{items}</ul>"

    return f"""<div class="wrap">
<p class="muted"><a href="index.html">&larr; all fixtures</a></p>
<h1>{_esc(score.name)}</h1>
<p class="muted">{score.source_pages} source &rarr; {score.output_pages} output pages
&middot; {score.seconds:.2f}s</p>
<h2>Scorecard vs baseline <span class="muted" style="font-weight:400">(&#9679; = CI gate)</span></h2>
<table><thead><tr><th>metric</th><th class="num">baseline</th>
<th class="num">current</th><th>status</th></tr></thead>
<tbody>{_delta_rows(deltas)}</tbody></table>
{miss_html}
{_word_section(score)}
{_glossary_section()}
</div>"""


def index_page(rows: List[Dict[str, object]]) -> str:
    cards = []
    for r in rows:
        status = r["status"]
        pill = f'<span class="pill {"ok" if status=="PASS" else "fail"}">{status}</span>'
        cards.append(
            f'<a class="card" href="{_esc(r["href"])}"><h3>{_esc(r["name"])} {pill}</h3>'
            f'<div class="muted">retention <b>{r["retention"]:.3f}</b> &middot; '
            f'headings <b>{r["heading_retention"]:.2f}</b><br>'
            f'clipped <b class="{ "fail" if r["clipped"] else "ok"}">{r["clipped"]}</b> &middot; '
            f'{r["seconds"]:.2f}s</div></a>'
        )
    summary = _esc(rows and rows[0].get("_summary") or "")
    return f"""<div class="wrap">
<h1>pdf_reflow &mdash; verify report</h1>
<p class="muted">{summary}</p>
<div class="cards">{''.join(cards)}</div>
</div>"""


def write_html(path: str, title: str, body: str) -> None:
    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_esc(title)}</title><style>{_CSS}</style></head><body>"
        f"{body}</body></html>"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
