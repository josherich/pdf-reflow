# pdf_reflow — WebAssembly demo

A single-page browser demo that runs the entire reflow pipeline on the
client. You drop a PDF, the page hands you back a reflowed mobile PDF.
No upload, no server.

## How the WASM port works

There are two parts that have to run in the browser:

1. **MuPDF (the PDF parser/renderer).** PyMuPDF — the same SWIG-wrapped
   MuPDF binding the desktop pipeline uses — has a Pyodide build path
   documented at <https://pymupdf.readthedocs.io/en/latest/pyodide.html>.
   The wheel is **not** in Pyodide's default package index and **not** on
   PyPI, so we build it locally (see `build-pymupdf-wheel.sh`) and ship
   the resulting `pymupdf-*-emscripten_3_1_58_wasm32.whl` as a static
   asset under `wheels/`. The JS driver loads it with
   `pyodide.loadPackage(URL)` — `micropip.install()` does not work for
   this wheel because it bundles shared libraries.

2. **The reflow algorithm (everything else).** The five-stage pipeline
   in `extract.py`, `analyze.py`, `layout.py`, `render.py`, and
   `reflow.py` is pure Python — no native code. We `fetch()` those
   source files at page load and write them into Pyodide's virtual
   filesystem under `/pdf_reflow/`. The Python `import pdf_reflow`
   then resolves against that path.

The browser calls Python like this:

```js
pyodide.FS.writeFile("/in.pdf", uint8ArrayFromUpload);
pyodide.runPython(`
  from pdf_reflow import reflow_pdf, ReflowConfig
  reflow_pdf("/in.pdf", "/out.pdf",
             ReflowConfig(page_width=360, page_height=640, workers=1))
`);
const reflowed = pyodide.FS.readFile("/out.pdf"); // Uint8Array
```

`workers=1` matters: the desktop pipeline parallelizes page extraction
and figure rasterization with `ProcessPoolExecutor`. Browsers don't
have processes, so the demo always takes the sequential branch (which
the existing code already supports — see `extract_document` and
`_prerasterize_figures`'s `workers <= 1` paths).

## What runs where

| Layer                     | Where it runs                            |
|---------------------------|------------------------------------------|
| HTML/CSS UI               | Native browser                           |
| `pdf_reflow.*` Python     | CPython 3.12 compiled to WASM (Pyodide)  |
| `fitz` / PyMuPDF          | SWIG wrapper compiled to WASM            |
| MuPDF (parse, rasterize)  | C → WASM via Emscripten                  |
| File I/O                  | Pyodide's MEMFS virtual filesystem       |

## Run it locally

You need the PyMuPDF wheel before the page can work. Two options:

**Option A — use the wheel that ships in this branch.** If
`wheels/pymupdf.whl` already exists in the repo, you're set. Skip
to step 3.

**Option B — build the wheel from source.** Required when bumping the
Pyodide version or refreshing PyMuPDF/MuPDF. The build needs Python
3.12 + ~700 MB of disk for the toolchains and takes 20–40 minutes
wall time on a modern machine.

```bash
cd pdf_reflow/web
./build-pymupdf-wheel.sh
# Produces wheels/pymupdf.whl
```

The script:
1. Creates a Python 3.12 venv and installs `pyodide-build`.
2. Installs the Pyodide xbuildenv + Emscripten 3.1.58.
3. Clones PyMuPDF (`1.27.x`) and MuPDF (`1.27.x`).
4. Runs `pyodide build --exports whole_archive` with the env flags
   the PyMuPDF docs prescribe (`OS=pyodide`, no libcrypto, no
   tesseract).
5. Copies the produced `pymupdf-*-emscripten_3_1_58_wasm32.whl` into
   `web/wheels/`.

**Step 3 — serve the project root.** The page fetches
`../src/pdf_reflow/*.py` over HTTP, so you can't open `index.html`
via `file://`. Serve from `pdf_reflow/`:

```bash
# from readings/pdf_reflow/
python3 -m http.server 8000
# open http://localhost:8000/web/
```

That's it. The first run downloads ~5 MB of Pyodide runtime from the
jsdelivr CDN and the local ~50 MB PyMuPDF wheel; cached afterwards,
the page is fully offline.

## Deploy as a static site

Copy `web/` and `src/pdf_reflow/` to any static host (GitHub Pages,
Cloudflare Pages, Netlify, S3+CloudFront, …). The relative paths in
`app.js` assume the layout:

```
site-root/
  web/index.html
  web/app.js
  web/style.css
  web/wheels/pymupdf.whl          # built locally, committed as LFS or asset
  src/pdf_reflow/*.py
```

No build step at deploy time. No bundler. Pyodide is loaded from the
jsdelivr CDN; the PyMuPDF wheel is loaded from the same origin as
the demo.

The wheel is around 30–60 MB. If you don't want it inside your repo,
upload it to a CORS-enabled bucket and change `PYMUPDF_WHEEL_URL` in
`app.js` to the absolute URL.

## What you'll see

- Initial load: ~5–15 s while Pyodide + the PyMuPDF wheel stream in.
- Reflow of the Bitcoin whitepaper (9 pages): typically 1–3 s in
  Chrome on a modern laptop — slower than native (~0.4 s) because
  the WASM build is unthreaded.
- Large technical reports (30+ pages with many figures): expect tens
  of seconds. The desktop CLI's 4-worker parallel path is the right
  tool for those.

## Known limits vs the desktop CLI

- **No parallelism.** Pyodide doesn't expose
  `multiprocessing.ProcessPoolExecutor`. The `workers` field on
  `ReflowConfig` is forced to `1` by the JS driver.
- **Memory.** Reflowing very large PDFs (hundreds of pages or
  high-DPI figures) can push WASM's per-tab heap. If you hit
  `RangeError: WebAssembly.instantiate(): Out of memory`, drop
  the figure DPI or split the source PDF.
- **Browser fonts.** The output uses the same base14 PDF fonts as
  the desktop pipeline (`tiro`, `tibo`, `tiit`, `cour`). It does
  not depend on any browser-side font.
- **Experimental wheel.** PyMuPDF's docs label its Pyodide build
  "experimental". Most things work; if you hit a behavior that
  differs from the CLI, check
  <https://github.com/pymupdf/PyMuPDF/issues?q=pyodide>.

## Notes from porting this to WebAssembly

Three traps cost real time getting this demo to load. Documenting them
so the next port (or the next Pyodide bump) is faster.

### Trap 1 — `loadPackage("pymupdf")` silently does not work

The first attempt was the obvious one:

```js
await pyodide.loadPackage("pymupdf");
```

This failed with:

```
Error: No known package with name 'pymupdf'
```

Reason: `pyodide.loadPackage(name)` only resolves names that appear in
the Pyodide release's `pyodide-lock.json` (numpy, scipy, lxml, pillow,
…). PyMuPDF is **not** in that lockfile. It is not on PyPI as an
Emscripten wheel either — `pip download pymupdf` only shows
`manylinux`, `macosx`, `musllinux`, and `win` wheels.

Fix path: build the wheel ourselves, host it, and call
`pyodide.loadPackage(URL)` (the URL form bypasses the lockfile
lookup). The build recipe in PyMuPDF's docs
(<https://pymupdf.readthedocs.io/en/latest/pyodide.html>) is canonical:
`pyodide-build` + `emsdk` + clone PyMuPDF and MuPDF + `pyodide build
--exports whole_archive`. About 4 minutes wall-clock on a 4-core box
once the toolchain is cached.

One gotcha: the example in PyMuPDF's docs passes
`--shallow-submodules` and `--recursive` to the MuPDF git spec.
PyMuPDF's bundled `pipcl` only recognises `--branch`, `--tag`,
`--depth`, and the remote URL, so the build aborts with
`AssertionError: Unrecognised arg='--shallow-submodules'`. Drop those
two flags.

### Trap 2 — "Didn't expect to load any more file_packager files!"

With a hosted wheel, the load reported "PyMuPDF installed" and then
the very next line — `import pymupdf` — blew up:

```
ImportError: Could not load dynamic lib: /lib/python3.12/site-packages/pymupdf/_extra.so
Error: Didn't expect to load any more file_packager files!
```

That second line is Emscripten's way of saying "your dlopen needs
something I'd have to fetch via the `file_packager` preload
mechanism, but that phase is finished and I can't reach back into
it." It is **not** about the `.so` file Python asked for — it is
about a dependency of that `.so` whose data Emscripten cannot
materialise on demand.

PyMuPDF's wheel ships four shared objects:

| File                          | Role                                          |
|-------------------------------|-----------------------------------------------|
| `pymupdf.libs/libmupdf.so`    | MuPDF C runtime                               |
| `pymupdf.libs/libmupdfcpp.so` | MuPDF C++ bindings                            |
| `pymupdf/_mupdf.so`           | SWIG Python extension; `NEEDS` the two libs   |
| `pymupdf/_extra.so`           | small helper extension; `NEEDS` the two libs  |

Pyodide's `Installer.install` (the path `loadPackage(URL)` takes) ends
up calling `loadDynlibsFromPackage(filename, dynlibs)` with the
`.so` list in **zip order**. The zip order in this wheel is
`_extra.so`, `_mupdf.so`, `libmupdf.so`, `libmupdfcpp.so`. So
`_extra.so` is dlopened first, can't resolve `libmupdf`, the failure
is caught and only logged as a console warning (string match on
"need to see wasm magic number"), and the next two libs load fine.
Later, when Python's import machinery tries to load `_extra` for
real, Emscripten errors with the file_packager message instead of
the real "missing symbol from libmupdf".

There are two layered problems and you need to fix both:

1. **Wheel layout.** Pyodide's loader passes
   `${sitepackages}/${pkg}.libs/` as the extra dlopen search path
   (hard-coded; see `loadDynlibsFromPackage` in `pyodide.asm.js`).
   PyMuPDF's wheel puts shared libs in `pymupdf/`, not
   `pymupdf.libs/`. Repack to move `libmupdf.so` and `libmupdfcpp.so`
   into `pymupdf.libs/` and rewrite `RECORD`. This is what
   `auditwheel-emscripten` does for properly conformant Pyodide
   wheels; PyMuPDF's CI doesn't run it.

2. **Load order.** Even after the relocation, the silent-fail-then-late-import
   bug remains because Pyodide still loads `.so`s in zip order.
   `loadPackage` doesn't give you a hook to reorder. The way out is
   to stop using `loadPackage` for this wheel and stage the install
   yourself:

   ```js
   pyodide.unpackArchive(wheelBytes, "wheel", { extractDir: sitePackages });
   // Libs first, into the GLOBAL symbol table…
   await pyodide._api.loadDynlib(`${libsDir}/libmupdf.so`,    true);
   await pyodide._api.loadDynlib(`${libsDir}/libmupdfcpp.so`, true);
   // …then the extensions, scoped, with libsDir on the search path.
   await pyodide._api.loadDynlib(`${pkgDir}/_mupdf.so`, false, [libsDir]);
   await pyodide._api.loadDynlib(`${pkgDir}/_extra.so`, false, [libsDir]);
   ```

   `unpackArchive` materialises the wheel into the FS without
   triggering the dynamic-linker pass. `pyodide._api.loadDynlib` is
   the same function Pyodide calls internally; it stays exposed on
   the public object as `pyodide._api`. `global=true` puts the lib's
   symbols in the global namespace so the extensions that follow can
   resolve their `NEEDS` against them.

That's the whole fix. After it, `import pymupdf` is a no-op for
`dlopen` (libs are already mapped) and just registers the Python
module objects.

### Trap 3 — the CSS progress bar freezes during reflow

`pyodide.runPython()` is synchronous from JS' perspective and holds
the main thread for the entire reflow. Anything that needs the main
thread to animate — `<progress>` value updates, `width` transitions,
JS-driven `requestAnimationFrame` — freezes for 5–30 seconds and the
page looks hung.

Fix: animate `transform: scaleX(…)` instead of `width`. Transforms
run on the compositor thread, which is independent of the blocked
main thread. Set the transition duration to the estimated runtime
once, before `runPython`, and the bar keeps moving on its own.

### How to debug Pyodide loaders without round-tripping the browser

Standing up a real test loop (start static server → reload page →
hard-refresh → read the in-page console) is slow. What worked:

- **`node-pyodide` for fast iteration.** The npm `pyodide` package
  runs the same `pyodide.asm.js` Node-side. A 40-line `.mjs` that
  does `loadPyodide() → loadDynlib chain → import pymupdf → open a
  PDF` reproduces every browser failure mode and prints clean
  tracebacks. The wheel test that nailed Trap 2 above was a Node
  script.

- **Read `pyodide.asm.js` directly.** It is minified but greppable;
  the relevant classes (`DynlibLoader`, `Installer`) and their
  methods (`loadDynlibsFromPackage`, `createDynlibFS`,
  `loadDynlib`) are short. The `${pkg}.libs` convention is not
  documented anywhere I could find — it is just hard-coded in
  `loadDynlibsFromPackage`. Confirm assumptions against the code.

- **Compare to a known-good package.** Look at the `pyodide-lock.json`
  entry for a registered shared-library package (e.g. `openblas` →
  `package_type: shared_library`, `install_dir: dynlib`) and at the
  layout of an extension wheel that depends on it (e.g. `numpy`,
  whose binary deps live in `numpy.libs/`). Most "Pyodide didn't
  load my wheel" bugs reduce to a layout mismatch versus that
  convention.

- **Skip silent warnings on `loadDynlib`.** The catch block at
  `DynlibLoader.loadDynlib` only swallows errors that string-match
  `"need to see wasm magic number"`. Anything else re-throws. If
  your wheel loads "successfully" but imports fail, it was the
  silent path — open the browser console and look for the warning
  Pyodide logs (`Failed to load dynlib …`).

- **`pyodide.unpackArchive(buf, "wheel", { extractDir })`** lets you
  manually stage a wheel for any case where `loadPackage` does too
  much or too little. Use it whenever you need explicit control over
  the dynamic-linker pass.
