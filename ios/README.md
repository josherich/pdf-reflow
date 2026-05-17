# PDFReflow — iOS app

A SwiftUI / PDFKit port of the [pdf_reflow](../README.md) CLI. The screen
shows a PDF you pick from the device file system; the reflow icon in the
top-right toggles between the original PDF and a single-column,
phone-sized reflowed version. Tap again to flip back.

## How it runs the reflow

iOS doesn't have a stock PDF parser at the glyph/drawing level the
reflow heuristics need, so this app reuses the existing Python pipeline
unchanged. A hidden `WKWebView` loads [Pyodide][pyodide] and the
PyMuPDF-WASM wheel exactly like the [web demo](../web/) does:

```
+----------+          +-----------------+          +------------------+
| SwiftUI  | base64   |    WKWebView    |  Python  |   pdf_reflow.*   |
| PDFKit   +--------->+  bridge.js +    +--------->+   (unchanged)    |
|          |          |  Pyodide+MuPDF  |          |   PyMuPDF WASM   |
+----------+   PDF    +-----------------+          +------------------+
                              ^
                              | served via custom URL scheme
                              | from the app bundle:
                              |   pymupdf.whl
                              |   pdf_reflow/*.py
```

`BridgeSchemeHandler` exposes the bundled assets to the web view under
a `pdfreflow://` scheme so the page sidesteps the `file://` CORS quirks
WKWebView has when fetching same-directory resources.

## Build

Prerequisites:

- Xcode 15+ on macOS (iOS 17 deployment target).
- The PyMuPDF wheel at `web/wheels/pymupdf.whl`. If it isn't checked
  into your branch, build it once with `web/build-pymupdf-wheel.sh`
  (20–40 min, see [`web/README.md`](../web/README.md)).
- A 1024×1024 `AppIcon.png` at
  `PDFReflow/Assets.xcassets/AppIcon.appiconset/AppIcon.png`. The
  asset catalog references it by filename; Xcode synthesises all
  smaller sizes at build time.

Then either:

```bash
# Hand-crafted project (preferred — just open and run).
open ios/PDFReflow.xcodeproj
```

…or regenerate from the [XcodeGen][xcodegen] spec:

```bash
brew install xcodegen
cd ios && xcodegen generate && open PDFReflow.xcodeproj
```

Pick a development team in **Signing & Capabilities** → **Team** the
first time you build for a device. The simulator works without code
signing.

### Why a build-phase script copies the Python sources

To stay byte-identical with the CLI / web demo, the build phase named
**Copy pdf_reflow assets into bundle** copies the canonical
`src/pdf_reflow/*.py` and `web/wheels/pymupdf.whl` into the app
bundle's `ReflowBridge/` directory at build time. Edit the Python
sources in their canonical home — the iOS app picks them up on the
next build. No symlinks, no duplication in git.

## Usage

1. Launch the app on device or simulator.
2. Tap the **folder** icon (top-left) and pick any `.pdf` from
   Files / iCloud / On My iPhone.
3. The PDF renders in the standard PDFKit viewer.
4. Tap the **phone** icon (top-right) to reflow it. The first reflow
   downloads ~5 MB of Pyodide from jsdelivr and unpacks the PyMuPDF
   wheel — expect 8–15 s on a clean install. Subsequent reflows within
   the same launch are 1–5 s for a 10-page paper.
5. Tap the icon again to switch back to the original. The reflowed
   PDF is cached for the current document, so the toggle is instant
   after the first reflow.

## Network use

The first reflow fetches Pyodide's JS + WASM runtime from
`cdn.jsdelivr.net`. After that the app caches it for the lifetime of
the WKWebView (i.e. until you kill the app). To make the app fully
offline, mirror the Pyodide files into `PDFReflow/ReflowBridge/pyodide/`
and change `PYODIDE_INDEX_URL` in `bridge.js` accordingly — same
trade-off the upstream web demo makes.

## What's where

```
ios/
├── PDFReflow.xcodeproj/             # hand-crafted Xcode project
├── PDFReflow/
│   ├── PDFReflowApp.swift           # @main
│   ├── ContentView.swift            # toolbar + state machine
│   ├── PDFViewer.swift              # UIViewRepresentable over PDFView
│   ├── ReflowEngine.swift           # WKWebView host, callAsyncJavaScript
│   ├── BridgeSchemeHandler.swift    # serves bundle files to WebKit
│   ├── ReflowBridge/
│   │   ├── index.html               # headless bridge page
│   │   └── bridge.js                # adapted from web/app.js
│   ├── Assets.xcassets/
│   └── Preview Content/
├── project.yml                      # XcodeGen spec (regenerable)
└── README.md                        # this file
```

## Limits

Same as the WASM demo (see [`web/README.md`](../web/README.md)):

- No parallelism — `workers=1` only.
- Memory ceiling — very large PDFs can exhaust WASM's heap.
- First-launch latency dominated by the Pyodide CDN download.

Patches welcome to bundle the runtime locally, or to port a fast-path
in Swift for documents that fit a simpler heuristic (e.g. text-only PDFs
without figures).

[pyodide]: https://pyodide.org
[xcodegen]: https://github.com/yonaskolb/XcodeGen
