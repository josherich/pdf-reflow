# Prebuilt PyMuPDF Pyodide wheel

`pymupdf.whl` is a Pyodide-compatible WebAssembly build of PyMuPDF.
The browser demo loads it with `pyodide.loadPackage(URL)` at startup.

## Current wheel

| Field            | Value                                              |
|------------------|----------------------------------------------------|
| Source build tag | `pymupdf-1.27.2.3-cp312-abi3-pyemscripten_2024_0_wasm32.whl` |
| PyMuPDF version  | 1.27.2.3                                           |
| MuPDF branch     | 1.27.x                                             |
| Target Pyodide   | 0.27.7 (ABI tag `pyemscripten_2024_0`)             |
| CPython          | 3.12                                               |
| Emscripten       | 3.1.58                                             |
| Wheel size       | ~17 MB                                             |

## How it was built

```bash
cd pdf_reflow/web
./build-pymupdf-wheel.sh
```

The script clones PyMuPDF + MuPDF, sets up the Pyodide cross-build
environment with Emscripten, runs `pyodide build --exports
whole_archive` with the env flags PyMuPDF's
[Pyodide page](https://pymupdf.readthedocs.io/en/latest/pyodide.html)
prescribes, and drops the wheel here as `pymupdf.whl`.

## Bumping

If you change `PYODIDE_VERSION` in `web/index.html` and `web/app.js`,
rebuild the wheel — the Pyodide ABI tag inside the wheel filename
(e.g. `pyemscripten_2024_0` for Pyodide 0.27.x) must match what
`pyodide.loadPackage` accepts at runtime.
