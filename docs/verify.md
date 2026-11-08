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
| `math_span_retention` | source math-font spans accounted for by complete formulas or figures | ● |
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

## Repeated benchmarks and independent accuracy probes

Use the benchmark runner for measured hill climbing. It records warmups
separately, keeps every timing sample, and compares median **full production
reflow** time (including outlines, font subsetting, compression, and saving).
Quality scoring runs outside those timing samples. Run the two commands on
the same machine and runtime, without competing workloads:

```bash
# Before changing the algorithm:
uv run python tools/benchmark.py --repeat 5 --warmup 1 --output /tmp/before.json

# After changing it:
uv run python tools/benchmark.py --repeat 5 --warmup 1 \
  --compare /tmp/before.json --output /tmp/after.json

# A faster loop on selected fixtures; both runs must use the same selection:
uv run python tools/benchmark.py --fixtures bitcoin.pdf two-column.pdf \
  --repeat 5 --output /tmp/small.json
```

The JSON includes source-code and fixture SHA-256 fingerprints, runtime,
configuration, raw timings, output sizes, and verify metrics. Comparisons
reject changed fixtures/runtimes/settings, fail existing quality gates, and
fail median slowdowns above 15% (configurable with `--max-slowdown`). Timings
are machine-specific; committed examples in `verify/benchmarks/` document a
single before/after experiment, rather than portable CI time budgets.
`tests/test_benchmark.py` continues to enforce generous CI budgets.

The ordinary verify command also runs seven **independent accuracy
probes**, with expectations written independently of the analyzer's output:

- preserve a compound hyphen before an uppercase line continuation;
- fit very long code lines without losing characters or indentation;
- keep a heading with the first following body line;
- stop cross-page paragraph merging at a deliberate same-column boundary;
- keep equation (7) on original `llm-cjk.pdf` page 11 out of the following definition paragraph;
- keep that page's inline radical, its bar, and its radicand on the body baseline;
- preserve all rows and subscripts of the MIT sample's aligned equation.

These probes gate verification even when no numeric baseline exists or a
baseline is being updated. They address a limitation of the original
retention reference: because it comes from the analyzer, it cannot detect
text that the analyzer itself already lost or merged incorrectly. The
probes cover specific regressions, not general ground-truth reading order.


## Formula verification and score schema 2

The word score is a **prose transport** check, not a mathematical correctness
score. Inline formula objects are excluded from its reference because their
source outlines are rendered as paths. The independent `math_span_retention`
metric enumerates math-font spans directly from the source PDF, including
space-encoded integral glyphs, and checks that each is accounted for in an
inline glyph selection or a complete display/figure crop. A loss of any
previously retained span fails the gate. Component accounting alone does not
prove the formula's geometry or semantics: source-coordinate regressions
and rendered inspection remain necessary.

`tests/test_math_flow.py` pins the user's original page-11 case (equation
bounds, intact surrounding definition, radical rule and baseline), inline
sum/integral limits, aligned rows, ordinary-font scripted variables, and
narrow-page wrapping. Glyph isolation has a separate nonblank-outline check,
so an empty SVG conversion cannot pass merely because an image/object exists.

Schema 2 also tokenizes CJK one character at a time. A whole unspaced Chinese
run was formerly counted as one word, making harmless mobile line breaks
look like missing text. Benchmark JSON now carries `quality_schema` and
rejects comparisons across scoring versions. The numeric baseline was
first migrated by rescoring the **previous implementation**, then promoted
after the paired comparison and formula regressions passed; paired formula
benchmarks retain both runs.
English word tokenization and existing gate tolerances are unchanged.
