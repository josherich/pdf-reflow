# Verify harness

A three-layer harness for iterating on and guarding the reflow **rendering**
quality. It lives in `tools/verify.py` (+ the `tools/reflow_verify/` package)
and needs **no dependencies beyond what the library already uses** — stdlib +
PyMuPDF. Run it in the same `uv` venv as the package.

```bash
uv run python tools/verify.py                    # score every fixture, gate vs baseline
uv run python tools/verify.py --report           # + HTML side-by-side report
uv run python tools/verify.py --golden           # + SSIM visual golden comparison
uv run python tools/verify.py --report --golden --open   # everything, open in browser
uv run python tools/verify.py --fixtures bitcoin.pdf two-column.pdf
uv run python tools/verify.py --update-baseline  # re-bless the numeric baseline
uv run python tools/verify.py --update-golden    # re-bless the golden PNGs
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
| `ssim` | stitched-column density-map similarity (Layer 2) | ● |
| `output_pages`, `images_rendered`, `widow_lines`, `seconds` | informational | |

Numbers are compared to `verify/baseline.json` with per-metric directions and
tolerances (`tools/reflow_verify/baseline.py`). A metric moving the wrong way
past its tolerance fails the run. `--update-baseline` re-blesses the numbers
when a change is intentional; the JSON diff in the PR is the record.

## Layer 2 — pagination-invariant visual golden (SSIM on a density map)

The naive approach — rasterize output page N, SSIM it against golden page N —
is wrong for a reflow tool. Reflow *re-paginates* on almost every meaningful
change (a spacing tweak moves a paragraph from the bottom of p5 to the top of
p6), so page-N-vs-page-N misaligns everything after the shift and produces a
cascade of false failures on exactly the changes you're making. And it isn't
comparing to the source either — it's output-vs-blessed-output.

Instead (`golden.py` + `imaging.py`, pure Python) we reconstruct the
**continuous reflowed column** and compare that:

1. **Stitch.** Crop each output page to its inked content bbox (dropping page
   margins and the partial-page whitespace at each break) and stack the crops
   into one tall grayscale strip. Where the page breaks fall no longer
   matters — the strip is the same whether a line sits at the bottom of one
   page or the top of the next.
2. **Density map.** Rendered text at strip resolution is high-frequency
   noise: two independent renderings of the *same* content barely correlate
   pixel-for-pixel (raw-strip SSIM ≈ 0.5 even when identical). So the strip is
   reduced to a coarse **ink-density map** (`imaging.density_map`, ~32 cells
   wide). Averaging turns text into a stable "ink per region" signal:
   identical content → identical map (SSIM 1.0); a pure pagination shift
   barely moves it; a genuine re-layout (different column width, changed
   spacing) clearly changes it.

The gate compares the current density map to the blessed golden with SSIM;
below `--threshold` (default 0.97) is a visual regression. Because the
pipeline is deterministic, an *unchanged* pipeline scores 1.0, so the
threshold only fires when the rendering actually changes — and then it's **one
score per fixture** with a meaningful whole-document comparison, not a
per-page cascade.

**Where the golden comes from:** you don't author it. First run bootstraps it
(written, scored 1.0). You *look at the report*, and if the rendering is
acceptable, commit `verify/golden/*.png` (one small density-map PNG per
fixture, ~8 KB) — that commit is the human sign-off. When you change the
layout on purpose, `--update-golden` overwrites them and the git diff is the
review artifact.

> The harness prints the MuPDF version on every run. Density-map SSIM absorbs
> cross-build anti-aliasing jitter (that's why identical content stays at
> 1.0), so a version bump is far less likely to mass-fail than raw pixel
> comparison would be.

## Layer 3 — HTML iteration report

`--report` writes `verify/report/index.html`: a card per fixture, and a detail
page with the scorecard delta on top, a banner when the SSIM gate fails, a
list of any headings that fell out of the output, and the **source and
reflowed pages as two independent galleries**. They're galleries, not pairs,
because reflow re-paginates — source page N and output page N are not the same
content. This is the tuning loop: run → eyeball → adjust heuristic → rerun.

## What's committed vs generated

Committed: `verify/baseline.json`, `verify/golden/*.png`.
Generated (gitignored): `verify/out/` (reflowed PDFs), `verify/actual/`
(current rasterizations), `verify/report/`.

## Growing the corpus

Five-plus fixtures overfit. To find real weaknesses, drop a varied set of
PDFs (arXiv papers across fields, manuals, datasheets, more CJK) into
`tests/fixtures/`, run the harness, and triage the lowest `retention` /
`heading_retention` scores in the report — no annotation required. (The
current llm-cjk run, for instance, flags several CJK body sentences the
classifier mis-tags as headings.)

## Extending it

- **New metric:** add it to the dict returned by a function in
  `metrics.py`, then add a rule in `baseline.py:METRIC_RULES` (direction,
  tolerance, whether it gates).
- **VLM-as-judge (optional Layer 4):** the report already pairs source and
  output page images; feed a page pair to a multimodal model with a fixed
  rubric (reading order intact? figure near its reference? heading hierarchy
  preserved? 0–2 each) and append the scores. Keep it out of the CI gate —
  it's nondeterministic — and use it for periodic qualitative sweeps as the
  corpus grows past what you can eyeball.
