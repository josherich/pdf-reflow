"""The Layer-2 web tool: golden-image upload/compare and page annotation.

A small stdlib-only HTTP server (``tools/verify.py --serve``) with two views
per fixture:

* **/compare/<stem>** -- rendered output pages side by side with uploaded
  golden images. Uploads are saved to ``verify/golden/<stem>/page-NNN.<ext>``
  and each pair shows its pixel diff ratio.
* **/annotate/<stem>** -- draw a box on a rendered output page and attach a
  text note. Saved to ``verify/feedback/<stem>.json`` on every change.

Both stores are plain files a coding agent can consume directly, or through
``tools/verify.py --feedback`` / ``GET /api/feedback`` which emit the same
JSON summary.
"""

from __future__ import annotations

import glob
import html
import json
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple

from . import visual
from .report import _CSS

_MAX_UPLOAD = 32 * 1024 * 1024
_SAFE_NAME = re.compile(r"^[\w.-]+$")

_TOOL_CSS = _CSS + """
.topnav{margin:0 0 18px}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:14px 0 26px}
figure{margin:0;background:var(--panel);border:1px solid var(--rule);
border-radius:10px;padding:10px}
figcaption{font-size:13px;color:var(--dim);margin-bottom:8px;display:flex;
gap:8px;align-items:center;flex-wrap:wrap}
figure img{width:100%;height:auto;display:block;border:1px solid var(--rule);
border-radius:4px;background:#fff}
.slot{display:flex;align-items:center;justify-content:center;min-height:180px;
border:1px dashed var(--rule);border-radius:4px;color:var(--dim);font-size:13px}
.btn{display:inline-block;padding:2px 10px;border-radius:6px;font-size:12.5px;
border:1px solid var(--rule);background:var(--panel);color:var(--ink);cursor:pointer}
.btn:hover{border-color:var(--accent)}
input[type=file]{font-size:12px;color:var(--dim);max-width:210px}
.imgwrap{position:relative;user-select:none}
.imgwrap img{-webkit-user-drag:none}
.box{position:absolute;border:2px solid var(--bad);background:rgba(247,118,142,.12);
border-radius:2px;pointer-events:none}
.box.resolved{border-color:var(--good);background:rgba(158,206,106,.10)}
.box .tag{position:absolute;top:-10px;left:-10px;background:var(--bad);color:#fff;
border-radius:50%;width:19px;height:19px;font-size:11.5px;font-weight:700;
display:flex;align-items:center;justify-content:center}
.box.resolved .tag{background:var(--good);color:#1a1f2e}
.notes{margin:8px 0 0;font-size:13.5px}
.notes li{margin:4px 0}
.notes .btn{margin-left:6px}
.diff-good{color:var(--good)}.diff-bad{color:var(--bad)}
.hintbar{background:var(--panel);border:1px solid var(--rule);border-radius:10px;
padding:10px 14px;font-size:13.5px;color:var(--dim);margin:0 0 18px}
"""


def _esc(s) -> str:
    return html.escape(str(s))


def _doc(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_esc(title)}</title><style>{_TOOL_CSS}</style></head>"
        f"<body>{body}</body></html>"
    ).encode("utf-8")


def _diff_badge(ratio: Optional[float]) -> str:
    if ratio is None:
        return ""
    cls = "diff-good" if ratio <= 0.05 else "diff-bad"
    return f"<b class='{cls}'>diff {ratio * 100:.1f}%</b>"


class _Ctx:
    """Paths + fixture registry shared by all requests."""

    def __init__(self, fixture_dir: str, out_dir: str, pages_dir: str,
                 golden_dir: str, feedback_dir: str) -> None:
        self.fixture_dir = fixture_dir
        self.out_dir = out_dir
        self.pages_dir = pages_dir
        self.golden_dir = golden_dir
        self.feedback_dir = feedback_dir

    def stems(self) -> List[str]:
        return sorted(visual.stem_of(p)
                      for p in glob.glob(os.path.join(self.out_dir, "*.pdf")))

    def out_pdf(self, stem: str) -> str:
        return os.path.join(self.out_dir, f"{stem}.pdf")

    def ensure_pages(self, stem: str) -> List[str]:
        return visual.render_pdf_pages(self.out_pdf(stem),
                                       os.path.join(self.pages_dir, stem))


class FeedbackHandler(BaseHTTPRequestHandler):
    server_version = "reflow-verify"

    @property
    def ctx(self) -> _Ctx:
        return self.server.ctx  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    # -- helpers ---------------------------------------------------------

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, code: int, msg: str):
        self._send(code, _doc("error", f"<div class='wrap'><h1>{code}</h1>"
                                       f"<p>{_esc(msg)}</p></div>"))

    def _route(self) -> Tuple[str, Dict[str, str], List[str]]:
        parsed = urllib.parse.urlparse(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        parts = [urllib.parse.unquote(p) for p in parsed.path.split("/") if p]
        return parsed.path, query, parts

    def _stem(self, name: str) -> Optional[str]:
        if _SAFE_NAME.match(name) and name in self.ctx.stems():
            return name
        return None

    def _body(self) -> Optional[bytes]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > _MAX_UPLOAD:
            return None
        return self.rfile.read(length)

    # -- GET -------------------------------------------------------------

    def do_GET(self):
        _, _, parts = self._route()
        try:
            if not parts:
                return self._send(200, self._index())
            if parts[0] == "api" and parts[1:] == ["feedback"]:
                return self._json(visual.feedback_summary(
                    self.ctx.out_dir, self.ctx.pages_dir,
                    self.ctx.golden_dir, self.ctx.feedback_dir))
            if len(parts) == 2 and parts[0] in ("compare", "annotate"):
                stem = self._stem(parts[1])
                if not stem:
                    return self._err(404, "unknown fixture")
                page = self._compare(stem) if parts[0] == "compare" else self._annotate(stem)
                return self._send(200, page)
            if len(parts) == 2 and parts[0] == "annotations":
                stem = self._stem(parts[1])
                if not stem:
                    return self._err(404, "unknown fixture")
                return self._json(visual.load_annotations(self.ctx.feedback_dir, stem))
            if len(parts) == 4 and parts[0] == "img" and parts[1] in ("pages", "golden"):
                return self._image(parts[1], parts[2], parts[3])
            return self._err(404, "not found")
        except BrokenPipeError:
            pass
        except Exception as e:  # surface server bugs in the browser, not a hang
            self._err(500, f"{type(e).__name__}: {e}")

    def _image(self, kind: str, stem: str, name: str):
        stem = self._stem(stem)
        if not stem or not _SAFE_NAME.match(name):
            return self._err(404, "not found")
        base = self.ctx.pages_dir if kind == "pages" else self.ctx.golden_dir
        path = os.path.join(base, stem, name)
        if not os.path.isfile(path):
            return self._err(404, "no such image")
        ext = os.path.splitext(name)[1].lower()
        ctype = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".webp": "image/webp"}.get(ext, "image/png")
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype)

    # -- POST ------------------------------------------------------------

    def do_POST(self):
        _, query, parts = self._route()
        try:
            if len(parts) == 3 and parts[0] == "upload":
                return self._upload(parts[1], parts[2], query)
            if len(parts) == 4 and parts[0] == "golden" and parts[3] == "delete":
                return self._delete_golden(parts[1], parts[2])
            if len(parts) == 2 and parts[0] == "annotations":
                return self._save_annotations(parts[1])
            return self._err(404, "not found")
        except BrokenPipeError:
            pass
        except Exception as e:
            self._err(500, f"{type(e).__name__}: {e}")

    def _upload(self, stem: str, page_s: str, query: Dict[str, str]):
        stem = self._stem(stem)
        if not stem or not page_s.isdigit():
            return self._err(404, "unknown fixture/page")
        page = int(page_s)
        body = self._body()
        if body is None:
            return self._err(400, "missing or oversized upload body")
        ext = os.path.splitext(query.get("name", ""))[1].lower() or ".png"
        if ext not in visual.GOLDEN_EXTS:
            return self._err(400, f"unsupported image type {ext!r} "
                                  f"(use {', '.join(visual.GOLDEN_EXTS)})")
        g_dir = os.path.join(self.ctx.golden_dir, stem)
        os.makedirs(g_dir, exist_ok=True)
        stem_name = visual.page_image_name(page, "")
        for old in glob.glob(os.path.join(g_dir, stem_name + ".*")):
            os.remove(old)
        path = os.path.join(g_dir, stem_name + ext)
        with open(path, "wb") as f:
            f.write(body)
        self._json({"saved": os.path.relpath(path, os.getcwd())})

    def _delete_golden(self, stem: str, page_s: str):
        stem = self._stem(stem)
        if not stem or not page_s.isdigit():
            return self._err(404, "unknown fixture/page")
        goldens = visual.golden_images(os.path.join(self.ctx.golden_dir, stem))
        name = goldens.get(int(page_s))
        if name:
            os.remove(os.path.join(self.ctx.golden_dir, stem, name))
        self._json({"deleted": bool(name)})

    def _save_annotations(self, stem: str):
        stem = self._stem(stem)
        if not stem:
            return self._err(404, "unknown fixture")
        body = self._body()
        if body is None:
            return self._err(400, "missing body")
        try:
            payload = json.loads(body.decode("utf-8"))
            anns = payload["annotations"]
            assert isinstance(anns, list)
        except (ValueError, KeyError, AssertionError, UnicodeDecodeError):
            return self._err(400, "expected JSON {\"annotations\": [...]}")
        data = visual.save_annotations(self.ctx.feedback_dir, stem, anns)
        self._json(data)

    # -- pages -----------------------------------------------------------

    def _index(self) -> bytes:
        rows = []
        for stem in self.ctx.stems():
            n_gold = len(visual.golden_images(os.path.join(self.ctx.golden_dir, stem)))
            n_open = visual.open_annotation_count(self.ctx.feedback_dir, stem)
            rows.append(
                f"<tr><td><code>{_esc(stem)}.pdf</code></td>"
                f"<td class='num'>{n_gold}</td><td class='num'>{n_open}</td>"
                f"<td><a href='/compare/{_esc(stem)}'>compare</a> &middot; "
                f"<a href='/annotate/{_esc(stem)}'>annotate</a></td></tr>"
            )
        body = f"""<div class="wrap">
<h1>pdf_reflow &mdash; visual feedback</h1>
<p class="muted">Upload golden images of how reflowed pages should look, and
annotate rendered output pages with notes. Everything lands in
<code>verify/golden/</code> and <code>verify/feedback/</code>, and is exported
for coding agents by <code>python tools/verify.py --feedback</code>
(or <a href="/api/feedback">/api/feedback</a>).</p>
<table><thead><tr><th>fixture</th><th class="num">golden images</th>
<th class="num">open notes</th><th>tools</th></tr></thead>
<tbody>{''.join(rows) or '<tr><td colspan=4 class="muted">no reflowed outputs in verify/out/ — run tools/verify.py first</td></tr>'}</tbody></table>
</div>"""
        return _doc("pdf_reflow — visual feedback", body)

    def _compare(self, stem: str) -> bytes:
        names = self.ctx.ensure_pages(stem)
        g_dir = os.path.join(self.ctx.golden_dir, stem)
        goldens = visual.golden_images(g_dir)
        ratios = visual.golden_compare(
            self.ctx.out_pdf(stem), os.path.join(self.ctx.pages_dir, stem), g_dir)

        pairs = []
        for i, name in enumerate(names, start=1):
            gname = goldens.get(i)
            if gname:
                golden_html = (
                    f"<img src='/img/golden/{_esc(stem)}/{_esc(gname)}' alt=''>"
                )
                actions = (f"{_diff_badge(ratios.get(i))} "
                           f"<button class='btn' data-del='{i}'>remove</button>")
            else:
                golden_html = "<div class='slot'>no golden image yet</div>"
                actions = ""
            pairs.append(f"""<div class="pair">
<figure><figcaption>generated &middot; page {i}</figcaption>
<img src="/img/pages/{_esc(stem)}/{_esc(name)}" alt=""></figure>
<figure><figcaption>golden &middot; page {i} {actions}
<input type="file" accept="image/*" data-page="{i}"></figcaption>
{golden_html}</figure>
</div>""")

        body = f"""<div class="wrap">
<p class="topnav muted"><a href="/">&larr; all fixtures</a> &middot;
<a href="/annotate/{_esc(stem)}">annotate this fixture</a></p>
<h1>{_esc(stem)}.pdf &mdash; golden compare</h1>
<p class="hintbar">Left: the current reflow output. Right: upload an image of
how the page <em>should</em> look (a screenshot, a mockup, an edited render).
Uploads are saved to <code>verify/golden/{_esc(stem)}/</code>; the diff badge
is the fraction of pixels that differ. The bulk picker assigns files to pages
1..{len(names)} in filename order.
&nbsp;Bulk: <input type="file" accept="image/*" id="bulk" multiple></p>
{''.join(pairs)}
</div>
<script>
const STEM = {json.dumps(stem)};
async function upload(page, file) {{
  await fetch(`/upload/${{STEM}}/${{page}}?name=` + encodeURIComponent(file.name),
              {{method: 'POST', body: file}});
}}
document.querySelectorAll('input[data-page]').forEach(inp => {{
  inp.onchange = async () => {{
    if (inp.files[0]) {{ await upload(inp.dataset.page, inp.files[0]); location.reload(); }}
  }};
}});
document.getElementById('bulk').onchange = async (e) => {{
  const files = [...e.target.files].sort((a, b) => a.name.localeCompare(b.name));
  for (let i = 0; i < files.length; i++) await upload(i + 1, files[i]);
  location.reload();
}};
document.querySelectorAll('button[data-del]').forEach(btn => {{
  btn.onclick = async () => {{
    await fetch(`/golden/${{STEM}}/${{btn.dataset.del}}/delete`, {{method: 'POST'}});
    location.reload();
  }};
}});
</script>"""
        return _doc(f"{stem} — golden compare", body)

    def _annotate(self, stem: str) -> bytes:
        names = self.ctx.ensure_pages(stem)
        data = visual.load_annotations(self.ctx.feedback_dir, stem)

        pages = []
        for i, name in enumerate(names, start=1):
            pages.append(f"""<h2>page {i}</h2>
<div class="pair"><div>
<div class="imgwrap" data-page="{i}">
<img src="/img/pages/{_esc(stem)}/{_esc(name)}" alt="" draggable="false">
</div></div>
<div><ul class="notes" data-page="{i}"></ul></div></div>""")

        body = f"""<div class="wrap">
<p class="topnav muted"><a href="/">&larr; all fixtures</a> &middot;
<a href="/compare/{_esc(stem)}">golden compare for this fixture</a></p>
<h1>{_esc(stem)}.pdf &mdash; annotate</h1>
<p class="hintbar">Drag a box over anything that looks wrong on a page, then
type a note describing the problem. Annotations save automatically to
<code>verify/feedback/{_esc(stem)}.json</code> and are handed to coding
agents via <code>python tools/verify.py --feedback</code>. Mark a note
<em>resolved</em> once the reflow is fixed.</p>
{''.join(pages)}
</div>
<script>
const STEM = {json.dumps(stem)};
let anns = {json.dumps(data.get("annotations") or [])};

async function save() {{
  await fetch(`/annotations/${{STEM}}`, {{
    method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{annotations: anns}}),
  }});
}}

function render() {{
  document.querySelectorAll('.box').forEach(b => b.remove());
  document.querySelectorAll('ul.notes').forEach(u => u.innerHTML = '');
  anns.forEach((a, idx) => {{
    const wrap = document.querySelector(`.imgwrap[data-page="${{a.page}}"]`);
    const list = document.querySelector(`ul.notes[data-page="${{a.page}}"]`);
    if (!wrap || !list) return;
    const [x0, y0, x1, y1] = a.rect;
    const box = document.createElement('div');
    box.className = 'box' + (a.status === 'resolved' ? ' resolved' : '');
    box.style.left = (x0 * 100) + '%'; box.style.top = (y0 * 100) + '%';
    box.style.width = ((x1 - x0) * 100) + '%'; box.style.height = ((y1 - y0) * 100) + '%';
    const tag = document.createElement('div');
    tag.className = 'tag'; tag.textContent = idx + 1;
    box.appendChild(tag); wrap.appendChild(box);

    const li = document.createElement('li');
    const label = document.createElement('b');
    label.textContent = `#${{idx + 1}} [${{a.status}}] `;
    const note = document.createElement('span');
    note.textContent = a.note || '(no note)';
    const resolve = document.createElement('button');
    resolve.className = 'btn';
    resolve.textContent = a.status === 'resolved' ? 'reopen' : 'resolve';
    resolve.onclick = () => {{
      a.status = a.status === 'resolved' ? 'open' : 'resolved';
      save(); render();
    }};
    const edit = document.createElement('button');
    edit.className = 'btn'; edit.textContent = 'edit';
    edit.onclick = () => {{
      const t = prompt('Note:', a.note || '');
      if (t !== null) {{ a.note = t.trim(); save(); render(); }}
    }};
    const del = document.createElement('button');
    del.className = 'btn'; del.textContent = 'delete';
    del.onclick = () => {{ anns = anns.filter(x => x !== a); save(); render(); }};
    li.append(label, note, resolve, edit, del);
    list.appendChild(li);
  }});
}}

document.querySelectorAll('.imgwrap').forEach(wrap => {{
  let start = null, tmp = null;
  const norm = (e) => {{
    const r = wrap.getBoundingClientRect();
    return [Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1),
            Math.min(Math.max((e.clientY - r.top) / r.height, 0), 1)];
  }};
  wrap.addEventListener('mousedown', (e) => {{
    e.preventDefault();
    start = norm(e);
    tmp = document.createElement('div');
    tmp.className = 'box'; wrap.appendChild(tmp);
  }});
  wrap.addEventListener('mousemove', (e) => {{
    if (!start || !tmp) return;
    const [x, y] = norm(e);
    tmp.style.left = (Math.min(start[0], x) * 100) + '%';
    tmp.style.top = (Math.min(start[1], y) * 100) + '%';
    tmp.style.width = (Math.abs(x - start[0]) * 100) + '%';
    tmp.style.height = (Math.abs(y - start[1]) * 100) + '%';
  }});
  wrap.addEventListener('mouseup', (e) => {{
    if (!start) return;
    const [x, y] = norm(e);
    const rect = [Math.min(start[0], x), Math.min(start[1], y),
                  Math.max(start[0], x), Math.max(start[1], y)];
    start = null; if (tmp) {{ tmp.remove(); tmp = null; }}
    if ((rect[2] - rect[0]) < 0.01 || (rect[3] - rect[1]) < 0.01) return;
    const note = prompt('What is wrong here?');
    if (note === null) return;
    anns.push({{page: parseInt(wrap.dataset.page, 10),
               rect: rect.map(v => Math.round(v * 1e4) / 1e4),
               note: note.trim(), status: 'open'}});
    save(); render();
  }});
}});
render();
</script>"""
        return _doc(f"{stem} — annotate", body)


def make_server(fixture_dir: str, out_dir: str, pages_dir: str,
                golden_dir: str, feedback_dir: str,
                host: str = "127.0.0.1", port: int = 8017) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), FeedbackHandler)
    httpd.ctx = _Ctx(fixture_dir, out_dir, pages_dir, golden_dir, feedback_dir)  # type: ignore[attr-defined]
    return httpd
