# Verify harness

A harness for iterating on and guarding reflow quality: a deterministic
scorecard (Layer 1), human visual feedback tools (Layer 2), and an HTML
report (Layer 3). It lives in `tools/verify.py` (+ the
`tools/reflow_verify/` package) and needs **no dependencies beyond what the
library already uses** — stdlib + PyMuPDF. Run it in the same `uv` venv as
the package.

```bash
uv run python tools/verify.py                    # score every fixture, gate vs baseline
uv run python tools/verify.py --report           # + HTML scorecard report
uv run python tools/verify.py --report --open    # + open the report in a browser
uv run python tools/verify.py --fixtures bitcoin.pdf two-column.pdf
uv run python tools/verify.py --update-baseline  # re-bless the numeric baseline
uv run python tools/verify.py --serve            # visual feedback web tool (Layer 2)
uv run python tools/verify.py --feedback         # dump human feedback as JSON (for agents)
```

Exit code is non-zero when a **gating** metric regresses vs the committed
baseline, so it drops straight into CI.

## The key idea: the source is the ground truth

Unlike an OCR benchmark, reflow evaluation needs **no hand annotation**.
Reflow must carry every word across in reading order while only changing
geometry, so the source PDF's own reading-order text (as the analyzer sees
it) *is* the reference. The harness reflows a fixture, reads the text back
out of the output PDF, and diffs the two token streams. Every new PDF you
drop into `tests/fixtures/` is therefore free signal — that's the property
that makes the corpus cheap to grow.

## Layer 1 — deterministic scorecard (CI backbone)

Per fixture, fast, fully reproducible. Metrics follow the Bast/Korzen
PDF-extraction taxonomy plus reflow-specific invariants:

| metric | meaning | gate |
|--------|---------|------|
| `retention` | matched words / reference words — the headline | ● |
| `w_minus` / `w_plus` / `w_tilde` | words dropped / spurious / changed | ● (−, ~) |
| `heading_retention` | source headings that survive into output | ● |
| `clipped_lines` | lines whose text runs past the page edge (looks broken) | ● |
| `pua_chars` | private-use math-font glyphs leaking into text | ● |
| `output_pages`, `images_rendered`, `widow_lines`, `seconds` | informational | |

Numbers are compared to `verify/baseline.json` with per-metric directions and
tolerances (`tools/reflow_verify/baseline.py`). A metric moving the wrong way
past its tolerance fails the run. `--update-baseline` re-blesses the numbers
when a change is intentional; the JSON diff in the PR is the record.

## Layer 2 — human visual feedback (golden images + annotations)

Some reflow quality is visual and can't be derived from the source text:
spacing that looks cramped, a figure placed awkwardly, a margin that's too
wide. Layer 2 captures that judgement from a human and hands it to a coding
agent as a structured signal. `--serve` starts a local, stdlib-only web tool
(default `http://127.0.0.1:8017/`) with two views per fixture:

- **Golden compare** (`/compare/<fixture>`): the rendered output pages next
  to user-uploaded *golden* images of how each page **should** look (a
  screenshot, a mockup, an edited render). Uploads are saved to
  `verify/golden/<fixture>/page-NNN.png` and each pair shows a pixel diff
  ratio (0 = matches the desired look; scale-invariant, antialiasing
  ignored).
- **Annotate** (`/annotate/<fixture>`): drag a box over anything that looks
  wrong on a rendered output page and type a note ("heading merged into
  body", "figure clipped"). Annotations save to
  `verify/feedback/<fixture>.json` with rects in normalized page
  coordinates, and can be marked *resolved* once fixed.

Both stores are plain committed files, so feedback survives across sessions
and shows up in PR diffs.

**The agent loop.** `--feedback` (or `GET /api/feedback` while serving)
emits one JSON document with everything an agent needs: per-page golden
diff ratios with paths to both images, and each annotation's note plus its
rect converted to PDF points of the output document (`rect_pdf`), so the
note can be tied back to the geometry it concerns. The intended loop:

1. the user uploads goldens / annotates pages via `--serve`;
2. the agent runs `--feedback`, reads the notes, opens the referenced page
   images, and adjusts the reflow heuristics;
3. the agent re-runs `--feedback` — falling diff ratios mean the output is
   converging on the golden; the user (or agent, by editing the JSON)
   flips notes to `resolved`.

When goldens exist, the normal scorecard run also folds in a `golden_diff`
metric (mean diff ratio, tracked in the baseline as informational) and
prints an open-note count per fixture, so visual feedback is visible in the
same place as the numbers.

## Layer 3 — HTML scorecard report

`--report` writes `verify/report/index.html`: a card per fixture, and a detail
page with the scorecard delta vs baseline and a list of any headings that fell
out of the output. It's the numbers made easy to read and diff — no page
images. This is the tuning loop: run → read → adjust heuristic → rerun.

## What's committed vs generated

Committed: `verify/baseline.json`, `verify/golden/` (uploaded golden
images), `verify/feedback/` (annotations).
Generated (gitignored): `verify/out/` (reflowed PDFs), `verify/report/`,
`verify/pages/` (rendered output page images).

## Growing the corpus

Five-plus fixtures overfit. To find real weaknesses, drop a varied set of
PDFs (arXiv papers across fields, manuals, datasheets, more CJK) into
`tests/fixtures/`, run the harness, and triage the lowest `retention` /
`heading_retention` scores in the report — no annotation required.

## Extending it

- **New metric:** add it to the dict returned by a function in
  `metrics.py`, then add a rule in `baseline.py:METRIC_RULES` (direction,
  tolerance, whether it gates).
