#!/usr/bin/env python3
"""Repeatable end-to-end benchmarks, paired with the verify quality gates.

Run the same command before and after a change. Timing includes extraction,
analysis, layout, rendering, outline mapping, font subsetting, and saving.
Scoring and warmups are excluded from the recorded timing samples.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reflow_verify.baseline import compare_fixture, has_gating_regression
from reflow_verify.metrics import score_fixture
from reflow_verify.probes import run_probes
from pdf_reflow import ReflowConfig, reflow_pdf

ROOT = Path(__file__).resolve().parents[1]


def compare_runs(before: dict, after: dict, max_slowdown: float = 0.15) -> list[str]:
    """Fail incompatible comparisons, quality regressions, and median slowdowns."""
    failures = []
    if before["schema"] != after["schema"]:
        failures.append("benchmark schemas differ")
    if before.get("quality_schema", 1) != after.get("quality_schema", 1):
        failures.append("quality scoring schemas differ; rescore both algorithms")
    for key in ("config", "runtime"):
        if before[key] != after[key]:
            failures.append(f"incompatible {key}; use matching settings and runtime")
    if set(before["fixtures"]) != set(after["fixtures"]):
        failures.append("fixture sets differ; benchmark the same corpus")
    for name, current in after["fixtures"].items():
        old = before["fixtures"].get(name)
        if old is None:
            continue
        if old["sha256"] != current["sha256"]:
            failures.append(f"{name}: fixture bytes changed")
        if has_gating_regression(compare_fixture(old["quality"], current["quality"])):
            failures.append(f"{name}: verify quality regressed")
        if current["median_seconds"] > old["median_seconds"] * (1 + max_slowdown):
            failures.append(f"{name}: median runtime regressed >{max_slowdown:.0%}")
    for name, passed in after["probes"].items():
        if not passed:
            failures.append(f"accuracy probe failed: {name}")
    if set(before["probes"]) - set(after["probes"]):
        failures.append("accuracy probes removed")
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixtures", nargs="+", default=None)
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--warmup", type=int, default=1)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--compare", type=Path)
    ap.add_argument("--max-slowdown", type=float, default=0.15)
    args = ap.parse_args()
    if args.repeat < 1 or args.warmup < 0 or args.workers < 1 or args.max_slowdown < 0:
        ap.error("repeat/workers must be positive; warmup/max-slowdown must be nonnegative")
    paths = [ROOT / "tests/fixtures" / name for name in args.fixtures] if args.fixtures else sorted((ROOT / "tests/fixtures").glob("*.pdf"))
    if not paths or any(not p.is_file() for p in paths):
        ap.error("fixtures must name existing PDF files")
    result = {
        "schema": 1,
        "quality_schema": 2,
        "runtime": {"python": platform.python_version(), "pymupdf": fitz.VersionBind,
                    "platform": platform.platform(), "machine": platform.machine()},
        "config": {"workers": args.workers, "page_width": 360, "page_height": 600,
                   "body_size": 11, "figure_dpi": 150},
        "repeat": args.repeat, "warmup": args.warmup,
        "probes": run_probes(), "fixtures": {},
    }
    cfg = ReflowConfig(workers=args.workers)
    import pdf_reflow
    source_root = Path(pdf_reflow.__file__).resolve().parent
    source_hash = hashlib.sha256()
    for source in sorted(source_root.glob("*.py")):
        source_hash.update(source.name.encode())
        source_hash.update(source.read_bytes())
    result["source_sha256"] = source_hash.hexdigest()
    before = json.loads(args.compare.read_text()) if args.compare else None
    with tempfile.TemporaryDirectory() as td:
        for path in paths:
            out = str(Path(td) / path.name)
            for _ in range(args.warmup):
                reflow_pdf(str(path), out, cfg)
            samples = []
            for _ in range(args.repeat):
                t0 = time.perf_counter()
                reflow_pdf(str(path), out, cfg)
                samples.append(time.perf_counter() - t0)
            # An independent verify run checks the saved PDF; its scoring
            # time is deliberately outside the performance measurements.
            score = score_fixture(str(path), out, cfg)
            quality = score.flat()
            quality.pop("seconds", None)
            row = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                   "samples_seconds": samples, "median_seconds": statistics.median(samples),
                   "min_seconds": min(samples), "max_seconds": max(samples),
                   "output_bytes": Path(out).stat().st_size, "quality": quality}
            result["fixtures"][path.name] = row
            delta = ""
            if before and path.name in before["fixtures"]:
                old = before["fixtures"][path.name]["median_seconds"]
                delta = f"  speedup={old / row['median_seconds']:.2f}x"
            print(f"{path.name:36} median={row['median_seconds']:.4f}s "
                  f"ret={quality['retention']:.4f} clip={quality['clipped_lines']}{delta}", flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print("Accuracy probes:", result["probes"], flush=True)
    failures = compare_runs(before, result, args.max_slowdown) if before else []
    for failure in failures:
        print(f"FAIL: {failure}")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
