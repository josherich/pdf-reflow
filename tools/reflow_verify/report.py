"""Layer 3: the HTML iteration report.

Per fixture: the scorecard delta vs baseline on top, then source pages beside
reflowed pages so tuning a heuristic is run -> eyeball -> adjust -> rerun.
Pages that fail the SSIM golden are flagged. Pure string templating -- no
templating engine, no JS framework.
"""

from __future__ import annotations

import html
import os
from typing import Dict, List, Optional

import fitz

from .baseline import MetricDelta
from .golden import GoldenResult
from .metrics import FixtureScore

_THUMB_DPI = 72

_CSS = """
:root{--bg:#0b0e14;--panel:#131826;--ink:#e6e9ef;--dim:#9aa4b8;--rule:#232b3d;
--good:#9ece6a;--bad:#f7768e;--warn:#e0af68;--accent:#7aa2f7}
@media(prefers-color-scheme:light){:root{--bg:#f7f8fb;--panel:#fff;--ink:#1a1f2e;
--dim:#5a6478;--rule:#e2e7ef}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:24px 18px 64px}
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
.pair{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:14px 0;
padding:12px;background:var(--panel);border:1px solid var(--rule);border-radius:10px}
.pair.badpair{border-color:var(--bad)}
.pair figure{margin:0}.pair img{width:100%;height:auto;border:1px solid var(--rule);
border-radius:4px;background:#fff}
.pair figcaption{color:var(--dim);font-size:12px;margin-bottom:6px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--rule);border-radius:10px;padding:14px}
.card h3{margin:0 0 6px;font-size:15px}
code{font-family:ui-monospace,Menlo,monospace;font-size:12.5px}
"""


def _esc(s) -> str:
    return html.escape(str(s))


def render_thumbs(pdf_path: str, img_dir: str, prefix: str, dpi: int = _THUMB_DPI,
                  max_pages: int = 40) -> List[str]:
    """Rasterize pages to PNG thumbnails, return relative paths (from report root)."""
    os.makedirs(img_dir, exist_ok=True)
    rels: List[str] = []
    doc = fitz.open(pdf_path)
    try:
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            pix = page.get_pixmap(dpi=dpi)
            fn = f"{prefix}_p{i:03d}.png"
            pix.save(os.path.join(img_dir, fn))
            rels.append(f"img/{fn}")
    finally:
        doc.close()
    return rels


def _delta_rows(deltas: List[MetricDelta]) -> str:
    rows = []
    for d in deltas:
        if d.regressed:
            status = f'<span class="pill fail">REGRESSED</span>'
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


def fixture_page(
    score: FixtureScore,
    deltas: List[MetricDelta],
    src_thumbs: List[str],
    out_thumbs: List[str],
    golden: Optional[GoldenResult],
) -> str:
    fail_pages = set()
    ssim_by_page: Dict[int, float] = {}
    if golden:
        for p in golden.pages:
            if p.page >= 0:
                ssim_by_page[p.page] = p.score
                if not p.ok:
                    fail_pages.add(p.page)

    pairs = []
    n = max(len(src_thumbs), len(out_thumbs))
    for i in range(n):
        s = src_thumbs[i] if i < len(src_thumbs) else None
        o = out_thumbs[i] if i < len(out_thumbs) else None
        bad = "badpair" if i in fail_pages else ""
        cap = f"reflowed p{i+1}"
        if i in ssim_by_page:
            cls = "fail" if i in fail_pages else "ok"
            cap += f" &middot; <span class='{cls}'>SSIM {ssim_by_page[i]:.3f}</span>"
        left = f'<img src="{_esc(s)}" loading="lazy">' if s else '<div class="muted">—</div>'
        right = f'<img src="{_esc(o)}" loading="lazy">' if o else '<div class="muted">— (no output page)</div>'
        pairs.append(
            f'<div class="pair {bad}">'
            f'<figure><figcaption>source p{i+1}</figcaption>{left}</figure>'
            f'<figure><figcaption>{cap}</figcaption>{right}</figure></div>'
        )

    miss = score.metrics.get("headings_missing") or []
    miss_html = ""
    if miss:
        items = "".join(f"<li><code>{_esc(m)}</code></li>" for m in miss)
        miss_html = f"<h2>Headings missing from output ({len(miss)})</h2><ul>{items}</ul>"

    ssim_note = f" &middot; SSIM min {golden.min_score:.3f}" if golden else ""
    return f"""<div class="wrap">
<p class="muted"><a href="index.html">&larr; all fixtures</a></p>
<h1>{_esc(score.name)}</h1>
<p class="muted">{score.source_pages} source &rarr; {score.output_pages} output pages
&middot; {score.seconds:.2f}s{ssim_note}</p>
<h2>Scorecard vs baseline <span class="muted" style="font-weight:400">(&#9679; = CI gate)</span></h2>
<table><thead><tr><th>metric</th><th class="num">baseline</th>
<th class="num">current</th><th>status</th></tr></thead>
<tbody>{_delta_rows(deltas)}</tbody></table>
{miss_html}
<h2>Source vs reflowed</h2>
{''.join(pairs)}
</div>"""


def index_page(rows: List[Dict[str, object]]) -> str:
    cards = []
    for r in rows:
        status = r["status"]
        cls = {"PASS": "ok", "FAIL": "fail"}.get(status, "warnc")
        pill = f'<span class="pill {"ok" if status=="PASS" else "fail"}">{status}</span>'
        cards.append(
            f'<a class="card" href="{_esc(r["href"])}"><h3>{_esc(r["name"])} {pill}</h3>'
            f'<div class="muted">retention <b>{r["retention"]:.3f}</b> &middot; '
            f'headings <b>{r["heading_retention"]:.2f}</b><br>'
            f'clipped <b class="{ "fail" if r["clipped"] else "ok"}">{r["clipped"]}</b> &middot; '
            f'SSIM <b>{r["min_ssim"]:.3f}</b> &middot; {r["seconds"]:.2f}s</div></a>'
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
