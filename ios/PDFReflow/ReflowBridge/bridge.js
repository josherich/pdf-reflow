// PDFReflow iOS — WKWebView bridge to the existing pdf_reflow Python pipeline.
//
// Mirrors web/app.js but headless: no UI, exposes one async function
// (window.reflowBase64) that the Swift side invokes via callAsyncJavaScript.
// PyMuPDF and the pdf_reflow .py sources are served from the app bundle by
// BridgeSchemeHandler, so the runtime is loaded the first time the user
// hits Reflow and reused for subsequent reflows in the same app launch.

const PYODIDE_VERSION = "0.27.7";
const PYODIDE_INDEX_URL = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;
const PYMUPDF_WHEEL_URL = "pymupdf.whl";
const PDF_REFLOW_SOURCES = [
  "__init__.py",
  "__main__.py",
  "extract.py",
  "analyze.py",
  "layout.py",
  "render.py",
  "reflow.py",
];

function postReady() {
  window.webkit?.messageHandlers?.ready?.postMessage("ok");
}
function postError(err) {
  const msg = err && err.stack ? err.stack : String(err);
  window.webkit?.messageHandlers?.engineError?.postMessage(msg);
}
function setStatus(s) {
  const el = document.getElementById("status");
  if (el) el.textContent = s;
}

let pyodideReady = null;

async function ensurePyodide() {
  if (pyodideReady) return pyodideReady;
  pyodideReady = (async () => {
    setStatus("Loading Pyodide…");
    const pyodide = await loadPyodide({ indexURL: PYODIDE_INDEX_URL });

    // Same staged-install dance as web/app.js — see web/README.md "Trap 2".
    setStatus("Installing PyMuPDF (WASM)…");
    const wheelUrl = new URL(PYMUPDF_WHEEL_URL, document.baseURI).href;
    const wheelBytes = new Uint8Array(
      await (await fetch(wheelUrl)).arrayBuffer()
    );
    const sitePackages = pyodide.runPython(
      "import sysconfig; sysconfig.get_paths()['purelib']"
    );
    pyodide.unpackArchive(wheelBytes, "wheel", { extractDir: sitePackages });
    const libsDir = `${sitePackages}/pymupdf.libs`;
    const pkgDir = `${sitePackages}/pymupdf`;
    await pyodide._api.loadDynlib(`${libsDir}/libmupdf.so`, true);
    await pyodide._api.loadDynlib(`${libsDir}/libmupdfcpp.so`, true);
    await pyodide._api.loadDynlib(`${pkgDir}/_mupdf.so`, false, [libsDir]);
    await pyodide._api.loadDynlib(`${pkgDir}/_extra.so`, false, [libsDir]);

    setStatus("Loading pdf_reflow sources…");
    pyodide.FS.mkdirTree("/pdf_reflow");
    for (const name of PDF_REFLOW_SOURCES) {
      const resp = await fetch(`pdf_reflow/${name}`);
      if (!resp.ok) {
        throw new Error(`Failed to fetch pdf_reflow/${name}: ${resp.status}`);
      }
      pyodide.FS.writeFile(`/pdf_reflow/${name}`, await resp.text());
    }
    pyodide.runPython(`
import sys
if "/" not in sys.path:
    sys.path.insert(0, "/")
import importlib, pdf_reflow
importlib.reload(pdf_reflow)
`);

    setStatus("Ready.");
    return pyodide;
  })();
  return pyodideReady;
}

function b64decode(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
function b64encode(bytes) {
  let bin = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode.apply(
      null,
      bytes.subarray(i, Math.min(i + CHUNK, bytes.length))
    );
  }
  return btoa(bin);
}

window.reflowBase64 = async function (b64, cfg) {
  const pyodide = await ensurePyodide();
  pyodide.FS.writeFile("/in.pdf", b64decode(b64));
  const pageStart = Number.isFinite(cfg.page_start) ? cfg.page_start : 0;
  const pageEndLiteral =
    cfg.page_end == null || !Number.isFinite(cfg.page_end)
      ? "None"
      : String(cfg.page_end);
  pyodide.runPython(`
from pdf_reflow import reflow_pdf, ReflowConfig
reflow_pdf(
    "/in.pdf",
    "/out.pdf",
    ReflowConfig(
        page_width=${cfg.page_width},
        page_height=${cfg.page_height},
        body_size=${cfg.body_size},
        figure_dpi=${cfg.figure_dpi},
        workers=1,
        page_start=${pageStart},
        page_end=${pageEndLiteral},
    ),
)
`);
  const out = pyodide.FS.readFile("/out.pdf");
  return b64encode(out);
};

// Eagerly warm Pyodide so the first user-initiated reflow is fast.
window.addEventListener("DOMContentLoaded", () => {
  ensurePyodide().then(postReady).catch(postError);
});
