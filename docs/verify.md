# Verify harness

A harness for iterating on and guarding reflow quality: a deterministic
scorecard (Layer 1) and an HTML report of it (Layer 3). It lives in
`tools/verify.py` (+ the `tools/reflow_verify/` package) and needs **no
dependencies beyond what the library already uses** — stdlib + PyMuPDF. Run it
in the same `uv` venv as the package.

```bash
uv run python tools/verify.py                    # score every fixture, gate vs baseline
uv run python tools/verify.py --report           # + HTML scorecard report
uv run python tools/verify.py --report --open    # + open the report in a browser
uv run python tools/verify.py --fixtures bitcoin.pdf two-column.pdf
uv run python tools/verify.py --update-baseline  # re-bless the numeric baseline
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

## Layer 3 — HTML scorecard report

`--report` writes `verify/report/index.html`: a card per fixture, and a detail
page with the scorecard delta vs baseline and a list of any headings that fell
out of the output. It's the numbers made easy to read and diff — no page
images. This is the tuning loop: run → read → adjust heuristic → rerun.

## What's committed vs generated

Committed: `verify/baseline.json`.
Generated (gitignored): `verify/out/` (reflowed PDFs), `verify/report/`.

## Growing the corpus

Five-plus fixtures overfit. To find real weaknesses, drop a varied set of
PDFs (arXiv papers across fields, manuals, datasheets, more CJK) into
`tests/fixtures/`, run the harness, and triage the lowest `retention` /
`heading_retention` scores in the report — no annotation required.

## Extending it

- **New metric:** add it to the dict returned by a function in
  `metrics.py`, then add a rule in `baseline.py:METRIC_RULES` (direction,
  tolerance, whether it gates).
