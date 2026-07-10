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
| `min_ssim` | worst per-page visual similarity (Layer 2) | ● |
| `output_pages`, `images_rendered`, `widow_lines`, `seconds` | informational | |

Numbers are compared to `verify/baseline.json` with per-metric directions and
tolerances (`tools/reflow_verify/baseline.py`). A metric moving the wrong way
past its tolerance fails the run. `--update-baseline` re-blesses the numbers
when a change is intentional; the JSON diff in the PR is the record.

## Layer 2 — visual golden snapshots (SSIM, not pixel-diff)

Each output page is rasterized and compared to a committed golden PNG with
**structural similarity** (`tools/reflow_verify/imaging.py`, pure Python).
SSIM is used over an exact pixel diff because it shrugs off the anti-aliasing
jitter that differs between MuPDF builds; the images are downsampled to a
fixed ~110-cell grid first, which also makes scoring fast and lets pages of
changed dimensions still be compared.

**Where the golden comes from:** you don't author it. On first run each page
is bootstrapped (written, scored 1.0). You then *look at the report* and, if
the rendering is acceptable, commit `verify/golden/*.png` — that commit is the
human sign-off. On later runs the harness SSIM-compares; below `--threshold`
(default 0.97) is a visual regression. When you improve the layout on purpose,
`--update-golden` overwrites the PNGs and the **git diff of those binaries is
the review artifact**. Goldens are stored at 48 DPI grayscale to stay small.

> Pin the environment: goldens are only reproducible for a fixed
> PyMuPDF/MuPDF version. The harness prints the MuPDF version on every run so a
> mass SSIM failure has an obvious cause (bump the goldens after an upgrade).

## Layer 3 — HTML iteration report

`--report` writes `verify/report/index.html`: a card per fixture, and a detail
page with the scorecard delta on top, a list of any headings that fell out of
the output, and every **source page beside its reflowed page** (SSIM-failing
pages boxed in red). This is the tuning loop: run → eyeball → adjust heuristic
→ rerun.

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
