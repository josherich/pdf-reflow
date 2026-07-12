# Changelog

This changelog summarizes the merged pull requests and notable direct commits on the main branch history.

## Unreleased

### Verification and quality gates
- Added a rendering verification harness with baseline metrics, reporting, documentation, and tests for repeatable quality checks. (#14)
- Added visual feedback tooling for the verification workflow, including visual analysis helpers, a web review tool, fixture feedback files, and expanded documentation. (#15)

### Text layout and reflow quality
- Improved line breaking with Unicode UAX #14 support and Knuth-Plass paragraph breaking, plus focused line-break tests and updated web documentation. (#13)
- Fixed two-column false positives and mid-sentence paragraph breaks, and added layout regression coverage to guard against common document-structure failures. (#12)
- Added two-column layout detection with per-column analysis and tests for multi-column PDF reflow. (#3)

### CJK language support
- Added Chinese, Japanese, and Korean text support across extraction, layout, rendering, font handling, fixtures, and tests. (#9)
- Fixed CJK line joining and text-alignment detection, with expanded CJK regression tests. (#10)
- Exposed the CJK fonts module to the web and iOS bridge bundles. (#11)
- Added CJK sample PDFs for manual and automated validation.

### Table of contents support
- Added table-of-contents entry detection and specialized rendering, with test coverage for TOC-heavy PDFs. (#6)
- Added an iOS table-of-contents modal with page navigation. (#7)
- Added a TOC fixture PDF for regression testing.

### iOS app
- Added the initial native iOS app port with Swift UI, PDF viewing, JavaScript bridge integration, app icons, and project configuration. (#1)
- Added a recent PDFs library with sorting, persistence, cached reflow output, and settings UI. (#2)
- Added progress tracking and duration estimation for iOS reflow operations. (#4)
- Added progressive preview reflow for large PDFs so users can begin reviewing output before the full document finishes. (#5)
- Updated iOS project team configuration.

### Web app and documentation
- Added an interactive dataflow documentation page explaining the PDF reflow pipeline. (#8)
- Added GitHub Pages deployment automation for the web app.
- Fixed the web app source URL.
- Added before-and-after screenshots to the README.

### Initial project foundation
- Created the Python `pdf-reflow` project with extraction, analysis, layout, rendering, CLI entry points, tests, benchmark fixtures, and browser-based demo assets.
