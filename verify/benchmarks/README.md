# Reflow benchmark experiment

Measured November 7, 2026 on macOS arm64, Python 3.13.0, PyMuPDF 1.27.2.3.

One warmup followed by three measured runs per fixture, one worker, default
360 × 600 pt output. Values are medians of the complete production reflow,
including font subsetting and saving. These are local measurements, not CI budgets.

The original algorithm was loaded from an isolated copy of commit
`6c6233c9e3cbac9f2496806d1f7d7a7007e04bd9`. The same benchmark runner,
probes, configuration, PDF bytes, and runtime were used for both runs.
Each JSON records the source fingerprint and every timing sample.

| Fixture | Before | After | Speedup |
|---|---:|---:|---:|
| `TABLE-OF-CONTENTS-SP2018.pdf` | 0.0454 s | 0.0332 s | 1.37× |
| `bitcoin.pdf` | 0.2625 s | 0.1947 s | 1.35× |
| `llm-cjk.pdf` | 5.3621 s | 4.5852 s | 1.17× |
| `mit_latex_sample.pdf` | 0.0935 s | 0.0785 s | 1.19× |
| `old-chinese-a-new-construction.pdf` | 1.4625 s | 1.3548 s | 1.08× |
| `tech_report.pdf` | 9.2711 s | 5.8781 s | 1.58× |
| `two-column.pdf` | 0.1147 s | 0.0846 s | 1.36× |

All seven fixtures preserve their previous word and heading retention scores,
with zero clipped lines or private-use glyph leaks. Existing numeric baseline
thresholds were not changed. Keeping headings with following text adds one
output page to the technical report and CJK lecture; page count is informational.

Independent accuracy probes improve from **0/4 to 4/4**: compound hyphens,
overlong code, heading placement, and cross-page paragraph boundaries.
These targeted probes do not imply perfect extraction on arbitrary PDFs.

Validation: 189 tests ran successfully (four font-dependent tests skipped on
this host). Extraction matches the legacy glyph-union text and geometry across
all fixtures. Batched rendering matches legacy pixels and extracted text in
a mixed-font/graphics overlap regression. Representative technical-report
and CJK math pages were also rendered with Poppler and visually inspected.

Raw results: [before.json](before.json), [after.json](after.json).
See [the benchmark workflow](../../docs/verify.md#repeated-benchmarks-and-independent-accuracy-probes)
for commands to repeat this experiment or compare a later change.
