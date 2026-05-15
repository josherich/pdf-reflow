#!/usr/bin/env bash
#
# Build a Pyodide-compatible PyMuPDF wheel and drop it into web/wheels/.
#
# PyMuPDF doesn't publish a Pyodide wheel to PyPI or to Pyodide's package
# index, so the demo ships with a wheel we built ourselves. Re-run this
# script when bumping the Pyodide version in web/index.html and web/app.js.
#
# References:
#   https://pymupdf.readthedocs.io/en/latest/pyodide.html
#   https://pyodide.org/en/stable/development/building-and-testing-packages.html
#
# Roughly:
#   1. Set up a Python 3.12 venv with pyodide-build + emsdk.
#   2. Clone PyMuPDF (1.27.x) and MuPDF (1.27.x).
#   3. `pyodide build` with OS=pyodide and the Pyodide-required flags.
#   4. Copy the resulting *.whl to web/wheels/pymupdf.whl.
#
# Cost: ~700 MB disk for the toolchains, ~20-40 minutes wall time.

set -euo pipefail

# --- Configurable knobs ---------------------------------------------------
PYODIDE_VERSION="${PYODIDE_VERSION:-0.27.7}"
PYMUPDF_TAG="${PYMUPDF_TAG:-1.27.2.3}"
MUPDF_BRANCH="${MUPDF_BRANCH:-1.27.x}"
WORK_DIR="${WORK_DIR:-$(mktemp -d)/pymupdf-wasm}"
PY=python3.12

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="${THIS_DIR}/wheels"

echo "==> Building PyMuPDF Pyodide wheel"
echo "    Pyodide:  ${PYODIDE_VERSION}"
echo "    PyMuPDF:  ${PYMUPDF_TAG}"
echo "    MuPDF:    ${MUPDF_BRANCH}"
echo "    workdir:  ${WORK_DIR}"
echo "    dest:     ${DEST_DIR}"

command -v $PY >/dev/null || { echo "need $PY in PATH"; exit 1; }

mkdir -p "${WORK_DIR}" "${DEST_DIR}"
cd "${WORK_DIR}"

# --- venv -----------------------------------------------------------------
if [[ ! -d venv ]]; then
  $PY -m venv venv
  ./venv/bin/pip install --quiet --upgrade pip
  ./venv/bin/pip install --quiet pyodide-build
fi
. ./venv/bin/activate

# --- xbuildenv + Emscripten ----------------------------------------------
# pyodide xbuildenv install pulls the cross-build env tarball from
# github.com/pyodide/pyodide; install-emscripten then clones emsdk and
# downloads LLVM (~500 MB on first run, cached afterwards).
if ! pyodide xbuildenv versions 2>/dev/null | grep -q "${PYODIDE_VERSION}"; then
  pyodide xbuildenv install "${PYODIDE_VERSION}"
fi
pyodide xbuildenv install-emscripten || true  # idempotent
EMSDK_ROOT="$(find "${HOME}/.cache" -type d -name emsdk -maxdepth 5 -print -quit)"
. "${EMSDK_ROOT}/emsdk_env.sh"

# --- PyMuPDF checkout -----------------------------------------------------
if [[ ! -d PyMuPDF ]]; then
  git clone --depth 1 --branch "${PYMUPDF_TAG}" https://github.com/pymupdf/PyMuPDF.git
fi

# --- Build ----------------------------------------------------------------
cd PyMuPDF
export OS=pyodide
export PYMUPDF_SETUP_FLAVOUR=pb
export PYMUPDF_SETUP_MUPDF_TESSERACT=0
export HAVE_LIBCRYPTO=no
export PYMUPDF_SETUP_MUPDF_BUILD="git:--branch ${MUPDF_BRANCH} --depth 1 https://github.com/ArtifexSoftware/mupdf.git"

pyodide build --exports whole_archive

# --- Install -------------------------------------------------------------
shopt -s nullglob
wheels=(dist/pymupdf-*emscripten*_wasm32.whl)
if (( ${#wheels[@]} == 0 )); then
  echo "no Pyodide wheel produced under PyMuPDF/dist/" >&2
  ls -la dist/ >&2 || true
  exit 1
fi
src="${wheels[0]}"
echo "==> Wheel: ${src}"
cp -v "${src}" "${DEST_DIR}/pymupdf.whl"
cp -v "${src}" "${DEST_DIR}/$(basename "${src}")"
echo "Wrote ${DEST_DIR}/pymupdf.whl ($(du -h "${DEST_DIR}/pymupdf.whl" | cut -f1))"
