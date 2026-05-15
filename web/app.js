// Web demo for pdf_reflow.
//
// Runs the existing Python pipeline (pdf_reflow.reflow_pdf) entirely in the
// browser via Pyodide. PyMuPDF (MuPDF C code + SWIG bindings, compiled to
// WebAssembly) does the heavy parsing/rasterizing; the pure-Python layers
// (extract/analyze/layout/render dispatch) are loaded straight from
// ../src/pdf_reflow/.
//
// PyMuPDF's Pyodide wheel is not in Pyodide's default package index, so we
// ship it as a static asset under web/wheels/ and load it directly with
// pyodide.loadPackage(URL). The wheel is keyed to the exact Pyodide version
// loaded below — both must move together (Pyodide 0.27 = Python 3.12 +
// Emscripten 3.1.58, and the wheel's filename encodes those).

const PYODIDE_VERSION = "0.27.7";
const PYODIDE_INDEX_URL = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;
// Relative to this file. The wheel is built locally via
// `web/build-pymupdf-wheel.sh` and committed under web/wheels/.
const PYMUPDF_WHEEL_URL = "wheels/pymupdf.whl";

// Files fetched from src/pdf_reflow/ (relative to site root) and copied into
// the Pyodide virtual filesystem under /pdf_reflow/. The deploy workflow puts
// src/pdf_reflow/*.py alongside the web/ files so this path resolves on both
// GitHub Pages and a local server started from the repo root.
const PDF_REFLOW_SOURCES = [
  "__init__.py",
  "__main__.py",
  "extract.py",
  "analyze.py",
  "layout.py",
  "render.py",
  "reflow.py",
];

const PRESETS = {
  "iphone17":     { width: 360, height: 640, label: "iPhone 17 (360×640)" },
  "iphone-mini":  { width: 320, height: 568, label: "iPhone mini (320×568)" },
  "ipad-mini":    { width: 480, height: 640, label: "iPad mini (480×640)" },
  "kindle":       { width: 380, height: 540, label: "Kindle (380×540)" },
};

const $ = (sel) => document.querySelector(sel);

const ui = {
  status:   $("#status"),
  progress: $("#progress"),
  progressFill: null,           // wired in wire()
  progressEta:  null,           // wired in wire()
  fileInput: $("#file"),
  reflowBtn: $("#reflow"),
  download: $("#download"),
  preset: $("#preset"),
  bodySize: $("#body-size"),
  figureDpi: $("#figure-dpi"),
  stats: $("#stats"),
  log: $("#log"),
};

// Reflow runtime estimate: linear least-squares fit to measured timings
// 0.2 MB → 2.8s, 9 MB → 20s, 12 MB → 26s
//   → seconds ≈ 2.4 + 2.0 · MB
// (off by < 0.5s for all three observations.)
function estimateReflowMs(byteSize) {
  const mb = byteSize / (1024 * 1024);
  return Math.max(1500, Math.round((2.4 + 2.0 * mb) * 1000));
}

// Progress bar driver. The fill animates via CSS `transform: scaleX(...)`
// on the compositor thread so it keeps moving while pyodide.runPython
// blocks the main JS thread for the reflow's full duration.
const progress = {
  ticker: null,
  startedAt: 0,
  estimateMs: 0,

  show() {
    ui.progress.hidden = false;
  },
  hide() {
    ui.progress.hidden = true;
    this._stopTicker();
    this._setFill({ transition: "none", scaleX: 0 });
    ui.progressFill.classList.remove("determinate", "finishing", "indeterminate");
    ui.progressEta.textContent = "";
  },

  indeterminate(label = "") {
    this.show();
    this._stopTicker();
    ui.progressFill.classList.remove("determinate", "finishing");
    // Force a reflow before re-adding so the keyframes restart cleanly.
    void ui.progressFill.offsetWidth;
    ui.progressFill.classList.add("indeterminate");
    ui.progressEta.textContent = label;
  },

  determinate(estimateMs) {
    this.show();
    this._stopTicker();
    this.estimateMs = estimateMs;
    this.startedAt = performance.now();

    ui.progressFill.classList.remove("indeterminate", "finishing");
    // Reset to 0 instantly.
    this._setFill({ transition: "none", scaleX: 0 });
    // Schedule the run on the next frame so the reset paints first.
    requestAnimationFrame(() => {
      ui.progressFill.classList.add("determinate");
      this._setFill({
        transition: `transform ${(estimateMs / 1000).toFixed(2)}s linear`,
        // Hold at 99% so we never claim "done" before runPython returns.
        scaleX: 0.99,
      });
    });

    // ETA countdown: the page is non-interactive while runPython blocks,
    // but the requestAnimationFrame callbacks still fire between Python
    // chunks if MuPDF yields. We tolerate a stale ETA.
    this.ticker = setInterval(() => {
      const elapsed = performance.now() - this.startedAt;
      const remaining = Math.max(0, estimateMs - elapsed);
      ui.progressEta.textContent = `~${Math.ceil(remaining / 1000)}s`;
    }, 250);
    ui.progressEta.textContent = `~${Math.ceil(estimateMs / 1000)}s`;
  },

  finish() {
    this._stopTicker();
    ui.progressFill.classList.remove("indeterminate");
    ui.progressFill.classList.add("determinate", "finishing");
    ui.progressEta.textContent = "done";
    // Hide shortly after the fade-out transition completes.
    setTimeout(() => this.hide(), 700);
  },

  _stopTicker() {
    if (this.ticker) { clearInterval(this.ticker); this.ticker = null; }
  },
  _setFill({ transition, scaleX }) {
    const f = ui.progressFill;
    if (transition !== undefined) f.style.transition = transition;
    if (scaleX !== undefined)     f.style.transform  = `scaleX(${scaleX})`;
  },
};

function setStatus(msg, kind = "info") {
  ui.status.textContent = msg;
  ui.status.dataset.kind = kind;
}

function logLine(msg) {
  const ts = new Date().toLocaleTimeString();
  ui.log.textContent += `[${ts}] ${msg}\n`;
  ui.log.scrollTop = ui.log.scrollHeight;
}

function fillPresets() {
  ui.preset.innerHTML = "";
  for (const [key, p] of Object.entries(PRESETS)) {
    const opt = document.createElement("option");
    opt.value = key;
    opt.textContent = p.label;
    ui.preset.appendChild(opt);
  }
  ui.preset.value = "iphone17";
}

let pyodideReady = null;

async function ensurePyodide() {
  if (pyodideReady) return pyodideReady;
  pyodideReady = (async () => {
    setStatus("Loading Python runtime (Pyodide)…", "busy");
    logLine(`Loading Pyodide v${PYODIDE_VERSION} from CDN…`);
    // loadPyodide is injected globally by the <script> tag in index.html.
    const pyodide = await loadPyodide({ indexURL: PYODIDE_INDEX_URL });
    logLine("Pyodide loaded.");

    setStatus("Installing PyMuPDF (MuPDF compiled to WASM)…", "busy");
    // The PyMuPDF Pyodide wheel ships four shared objects:
    //   pymupdf.libs/libmupdf.so     — MuPDF C runtime
    //   pymupdf.libs/libmupdfcpp.so  — MuPDF C++ bindings
    //   pymupdf/_mupdf.so            — SWIG Python extension (NEEDs the two libs)
    //   pymupdf/_extra.so            — small helper extension (NEEDs the two libs)
    //
    // pyodide.loadPackage(URL) calls loadDynlibsFromPackage which iterates
    // zip-order — so _extra.so loads first and its NEEDED deps can't be
    // resolved (the deps live elsewhere and aren't loaded yet). The silent
    // failure surfaces later as "Could not load dynamic lib … Didn't expect
    // to load any more file_packager files!" when Python tries to import
    // pymupdf. We bypass loadPackage and do the staging ourselves:
    //   1. unpackArchive: drop wheel files into site-packages without
    //      touching the dynamic linker.
    //   2. loadDynlib (global): MuPDF libs first, into the global symbol
    //      table so the extension modules can resolve their NEEDs.
    //   3. loadDynlib (local): the extension modules themselves.
    const wheelUrl = new URL(PYMUPDF_WHEEL_URL, document.baseURI).href;
    const wheelBytes = new Uint8Array(
      await (await fetch(wheelUrl)).arrayBuffer(),
    );
    const sitePackages = pyodide.runPython(
      "import sysconfig; sysconfig.get_paths()['purelib']",
    );
    pyodide.unpackArchive(wheelBytes, "wheel", { extractDir: sitePackages });
    const libsDir = `${sitePackages}/pymupdf.libs`;
    const pkgDir = `${sitePackages}/pymupdf`;
    // pyodide._api is documented as a stable entry point for advanced
    // loaders that need to bypass loadPackage.
    await pyodide._api.loadDynlib(`${libsDir}/libmupdf.so`, true);
    await pyodide._api.loadDynlib(`${libsDir}/libmupdfcpp.so`, true);
    await pyodide._api.loadDynlib(`${pkgDir}/_mupdf.so`, false, [libsDir]);
    await pyodide._api.loadDynlib(`${pkgDir}/_extra.so`, false, [libsDir]);
    logLine(`PyMuPDF installed from ${PYMUPDF_WHEEL_URL}.`);

    setStatus("Loading pdf_reflow Python sources…", "busy");
    pyodide.FS.mkdirTree("/pdf_reflow");
    for (const name of PDF_REFLOW_SOURCES) {
      const resp = await fetch(`src/pdf_reflow/${name}`);
      if (!resp.ok) {
        throw new Error(`Failed to fetch src/pdf_reflow/${name}: ${resp.status}`);
      }
      const text = await resp.text();
      pyodide.FS.writeFile(`/pdf_reflow/${name}`, text);
    }
    // Make /pdf_reflow importable.
    pyodide.runPython(`
import sys
if "/" not in sys.path:
    sys.path.insert(0, "/")
import importlib, pdf_reflow
importlib.reload(pdf_reflow)
`);
    logLine("pdf_reflow package mounted at /pdf_reflow/.");
    setStatus("Ready. Pick a PDF to reflow.", "ready");
    return pyodide;
  })().catch((err) => {
    pyodideReady = null;
    setStatus(`Initialization failed: ${err.message}`, "error");
    logLine(`ERROR: ${err.stack || err.message}`);
    throw err;
  });
  return pyodideReady;
}

function reflowPython(pyodide, cfg) {
  // Invokes pdf_reflow.reflow_pdf on /in.pdf -> /out.pdf inside the
  // Pyodide virtual filesystem. cfg keys mirror ReflowConfig fields.
  const pyCode = `
import json
from pdf_reflow import reflow_pdf, ReflowConfig

cfg = ReflowConfig(
    page_width=${cfg.page_width},
    page_height=${cfg.page_height},
    body_size=${cfg.body_size},
    figure_dpi=${cfg.figure_dpi},
    workers=1,
)
stats = reflow_pdf("/in.pdf", "/out.pdf", cfg)
json.dumps(stats)
`;
  return pyodide.runPython(pyCode);
}

async function handleReflow() {
  const file = ui.fileInput.files?.[0];
  if (!file) {
    setStatus("Pick a PDF first.", "warn");
    return;
  }
  ui.reflowBtn.disabled = true;
  ui.download.hidden = true;
  ui.stats.textContent = "";

  try {
    // If pyodide is still loading, the bar runs indeterminate until ready.
    if (!pyodideReady || !(await Promise.resolve(pyodideReady).then(() => true).catch(() => false))) {
      progress.indeterminate("loading");
    }
    const pyodide = await ensurePyodide();

    setStatus("Reading PDF…", "busy");
    const bytes = new Uint8Array(await file.arrayBuffer());
    pyodide.FS.writeFile("/in.pdf", bytes);
    logLine(`Loaded ${file.name} (${bytes.byteLength.toLocaleString()} bytes).`);

    const preset = PRESETS[ui.preset.value] || PRESETS["iphone17"];
    const cfg = {
      page_width: preset.width,
      page_height: preset.height,
      body_size: parseFloat(ui.bodySize.value) || 11.0,
      figure_dpi: parseFloat(ui.figureDpi.value) || 150.0,
    };

    const estimateMs = estimateReflowMs(bytes.byteLength);
    setStatus(
      `Reflowing… estimated ~${Math.ceil(estimateMs / 1000)}s for ` +
      `${(bytes.byteLength / (1024 * 1024)).toFixed(1)} MB`,
      "busy",
    );
    progress.determinate(estimateMs);
    logLine(`Reflowing with preset=${ui.preset.value} body=${cfg.body_size}pt dpi=${cfg.figure_dpi}…`);

    const t0 = performance.now();
    // runPython is synchronous from JS' perspective; yield twice so the
    // determinate bar's first transition frame and the status text both
    // paint before MuPDF starts hogging the main thread.
    await new Promise((r) => requestAnimationFrame(() => r()));
    await new Promise((r) => requestAnimationFrame(() => r()));
    const statsJson = reflowPython(pyodide, cfg);
    const dt = ((performance.now() - t0) / 1000).toFixed(2);

    const stats = JSON.parse(statsJson);
    logLine(
      `Done in ${dt}s: ${stats.source_pages} → ${stats.output_pages} pages, ` +
      `${stats.items} content items, body=${stats.source_body_size}pt.`,
    );
    ui.stats.innerHTML = `
      <strong>${stats.source_pages}</strong> source pages →
      <strong>${stats.output_pages}</strong> mobile pages
      &middot; ${stats.items} items
      &middot; body ${stats.source_body_size}pt
      &middot; ${dt}s
    `;

    const out = pyodide.FS.readFile("/out.pdf");
    const baseName = file.name.replace(/\.pdf$/i, "");
    const outName = `${baseName}-mobile.pdf`;
    // pyodide.FS.readFile returns a Uint8Array. Copy into a fresh ArrayBuffer
    // so the Blob isn't backed by Pyodide's heap (which can move).
    const blob = new Blob([out.slice().buffer], { type: "application/pdf" });
    const url = URL.createObjectURL(blob);
    ui.download.href = url;
    ui.download.download = outName;
    ui.download.textContent = `Download ${outName} (${(out.byteLength / 1024).toFixed(0)} KB)`;
    ui.download.hidden = false;
    progress.finish();
    setStatus("Reflow complete.", "ready");
  } catch (err) {
    progress.hide();
    setStatus(`Reflow failed: ${err.message}`, "error");
    logLine(`ERROR: ${err.stack || err.message}`);
  } finally {
    ui.reflowBtn.disabled = false;
  }
}

function wire() {
  ui.progressFill = $(".progress-fill");
  ui.progressEta = $("#progress-eta");
  fillPresets();
  ui.reflowBtn.addEventListener("click", handleReflow);
  ui.fileInput.addEventListener("change", () => {
    ui.download.hidden = true;
    if (ui.fileInput.files?.[0]) {
      setStatus(`Picked ${ui.fileInput.files[0].name}. Press Reflow to start.`, "ready");
    }
  });
  setStatus("Click Reflow to load the Python runtime on demand.", "info");
}

// Eagerly start loading Pyodide the moment the page is interactive — the
// download is large (~10 MB) so we want it in flight before the user picks
// a file.
window.addEventListener("DOMContentLoaded", () => {
  wire();
  ensurePyodide().catch(() => { /* surfaced via setStatus */ });
});
