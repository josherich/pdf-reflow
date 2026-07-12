# Changelog

This changelog is indexed by commit date and summarizes merged pull requests plus notable direct commits on the main branch history.

## 2026-07-11

- [#15](https://github.com/josherich/pdf-reflow/pull/15) Added visual feedback tooling for the verification workflow, including visual analysis helpers, a web review tool, fixture feedback files, and expanded documentation.
- [#14](https://github.com/josherich/pdf-reflow/pull/14) Added a rendering verification harness with baseline metrics, reporting, documentation, and tests for repeatable quality checks.

## 2026-06-14

- [#13](https://github.com/josherich/pdf-reflow/pull/13) Improved line breaking with Unicode UAX #14 support and Knuth-Plass paragraph breaking, plus focused line-break tests and updated web documentation.

## 2026-06-11

- [#12](https://github.com/josherich/pdf-reflow/pull/12) Fixed two-column false positives and mid-sentence paragraph breaks, and added layout regression coverage to guard against common document-structure failures.

## 2026-05-26

- [#11](https://github.com/josherich/pdf-reflow/pull/11) Exposed the CJK fonts module to the web and iOS bridge bundles.
- [#10](https://github.com/josherich/pdf-reflow/pull/10) Fixed CJK line joining and text-alignment detection, with expanded CJK regression tests.

## 2026-05-25

- [#9](https://github.com/josherich/pdf-reflow/pull/9) Added Chinese, Japanese, and Korean text support across extraction, layout, rendering, font handling, fixtures, and tests.
- Notable commit `c2401e7`: added the `llm-cjk.pdf` CJK validation fixture.

## 2026-05-24

- [#7](https://github.com/josherich/pdf-reflow/pull/7) Added an iOS table-of-contents modal with page navigation.

## 2026-05-23

- [#8](https://github.com/josherich/pdf-reflow/pull/8) Added an interactive dataflow documentation page explaining the PDF reflow pipeline.

## 2026-05-17

- [#6](https://github.com/josherich/pdf-reflow/pull/6) Added table-of-contents entry detection and specialized rendering, with test coverage for TOC-heavy PDFs.
- Notable commit `9432b2b`: added a TOC fixture PDF for regression testing.
- [#5](https://github.com/josherich/pdf-reflow/pull/5) Added progressive preview reflow for large PDFs so users can begin reviewing output before the full document finishes.
- Notable commit `835728e`: fixed the web app source URL.
- [#4](https://github.com/josherich/pdf-reflow/pull/4) Added progress tracking and duration estimation for iOS reflow operations.
- Notable commit `f6e19d0`: updated iOS project team configuration.
- [#3](https://github.com/josherich/pdf-reflow/pull/3) Added two-column layout detection with per-column analysis and tests for multi-column PDF reflow.
- Notable commit `e06c08b`: added a two-column sample PDF.
- [#2](https://github.com/josherich/pdf-reflow/pull/2) Added a recent PDFs library with sorting, persistence, cached reflow output, and settings UI.
- [#1](https://github.com/josherich/pdf-reflow/pull/1) Added the initial native iOS app port with Swift UI, PDF viewing, JavaScript bridge integration, app icons, and project configuration.

## 2026-05-15

- Notable commit `e17744f`: added before-and-after screenshots to the README.
- Notable commit `a322b95`: added GitHub Pages deployment automation for the web app.
- Initial commit `9de94cf`: created the Python `pdf-reflow` project with extraction, analysis, layout, rendering, CLI entry points, tests, benchmark fixtures, and browser-based demo assets.
