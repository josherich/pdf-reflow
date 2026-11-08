# Formula accuracy experiment

Original page 11 of `llm-cjk.pdf` is the primary regression. The previous
implementation let inline √d extend the display-equation band through
the first definition line, and dropped the radical from the following line.
The new implementation separates display math and flows intact inline objects
with original glyph outlines, scripts, rules, and baseline geometry.

Both algorithms were evaluated with the same schema-2 scorer and source PDFs.
The previous implementation is the source fingerprint in `before.json`
(the checkpoint after the initial performance improvements). Warmup: 1;
measured runs: 3; workers: 1. Times include the complete production pipeline.

| Fixture | Before math span accounting | After | Before time | After time |
|---|---:|---:|---:|---:|
| `TABLE-OF-CONTENTS-SP2018.pdf` | 0/0 | 0/0 | 0.0320 s | 0.0356 s |
| `bitcoin.pdf` | 38/42 | 42/42 | 0.1918 s | 0.2138 s |
| `llm-cjk.pdf` | 222/459 | 459/459 | 4.4233 s | 4.9241 s |
| `mit_latex_sample.pdf` | 29/53 | 53/53 | 0.0771 s | 0.1175 s |
| `old-chinese-a-new-construction.pdf` | 0/0 | 0/0 | 1.3465 s | 1.4148 s |
| `tech_report.pdf` | 64/155 | 155/155 | 5.6963 s | 7.1121 s |
| `two-column.pdf` | 0/1 | 1/1 | 0.0877 s | 0.1086 s |

Source span accounting is not a semantic-equation accuracy percentage. It
detects lost components; separate source-coordinate and rendered regressions
check the reported clipping, radical placement, scripts, and aligned rows.
The three new formula probes fail on the previous implementation and pass
on the new implementation; all seven probes pass after the change.

There are no regressions in the existing prose/heading/clipping/PUA quality gates.
CJK tokenization is invariant under line wrapping in schema 2. The numeric
baseline was first rescored using the previous implementation, then promoted
after the paired quality comparison and regressions passed.

Preserving math costs additional time, especially on short LaTeX samples.
The comparison explicitly used `--max-slowdown 1.0` to allow that accuracy
tradeoff; this is not a claim that all fixtures became faster. The normal
15% timing threshold and existing unit-test runtime budgets are unchanged.

Profiling found unrelated images being PNG-encoded during formula SVG export. A private text-only page copy reduced the technical report from 8.899 s to 7.112 s without changing formula pixels in the image-bearing-page regression.

Validation: full suite ran 201 tests successfully (4 font-dependent skips).
Additional bookmark and text-only SVG pixel-equivalence checks passed.
The reported page-11 region and MIT formula pages were rendered and inspected.

Raw samples: [before](before.json), [first formula implementation](after-unoptimized.json),
[optimized implementation](after.json).
