#!/usr/bin/env python3
"""Verify harness for pdf_reflow -- iterate on and guard rendering quality.

    uv run python tools/verify.py                 # score all fixtures, gate vs baseline
    uv run python tools/verify.py --report        # + write an HTML side-by-side report
    uv run python tools/verify.py --golden        # + SSIM-compare against golden PNGs
    uv run python tools/verify.py --update-baseline   # re-bless the numeric baseline
    uv run python tools/verify.py --update-golden     # re-bless the golden PNGs
    uv run python tools/verify.py --fixtures bitcoin.pdf two-column.pdf
    uv run python tools/verify.py --open          # open the report in a browser

Exit code is non-zero when a gating metric regressed vs baseline, so it drops
straight into CI. See docs/verify.md for the design.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import webbrowser
from typing import Dict, List

import fitz

# Make the sibling package importable when run as a script.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reflow_verify.metrics import score_fixture, FixtureScore  # noqa: E402
from reflow_verify.golden import check_golden, GoldenResult  # noqa: E402
from reflow_verify import baseline as bl  # noqa: E402
from reflow_verify import report as rpt  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(ROOT, "tests", "fixtures")
VERIFY_DIR = os.path.join(ROOT, "verify")
OUT_DIR = os.path.join(VERIFY_DIR, "out")
GOLDEN_DIR = os.path.join(VERIFY_DIR, "golden")
ACTUAL_DIR = os.path.join(VERIFY_DIR, "actual")
REPORT_DIR = os.path.join(VERIFY_DIR, "report")
BASELINE_PATH = os.path.join(VERIFY_DIR, "baseline.json")

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def _c(s: str, color: str) -> str:
    return f"{color}{s}{RESET}" if sys.stdout.isatty() else s


def discover_fixtures(names: List[str]) -> List[str]:
    if names:
        out = []
        for n in names:
            p = n if os.path.isabs(n) else os.path.join(FIXTURE_DIR, n)
            if not os.path.exists(p):
                sys.exit(f"fixture not found: {p}")
            out.append(p)
        return out
    return sorted(glob.glob(os.path.join(FIXTURE_DIR, "*.pdf")))


def load_baseline() -> Dict[str, Dict[str, float]]:
    if os.path.exists(BASELINE_PATH):
        with open(BASELINE_PATH) as f:
            return json.load(f)
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixtures", nargs="*", default=[],
                    help="fixture filenames (default: all tests/fixtures/*.pdf)")
    ap.add_argument("--report", action="store_true", help="write HTML report")
    ap.add_argument("--golden", action="store_true", help="run SSIM golden comparison")
    ap.add_argument("--update-baseline", action="store_true", help="re-bless numeric baseline")
    ap.add_argument("--update-golden", action="store_true", help="re-bless golden PNGs")
    ap.add_argument("--threshold", type=float, default=0.97, help="SSIM pass threshold")
    ap.add_argument("--open", action="store_true", help="open the report when done")
    args = ap.parse_args()

    # --update-golden implies running the golden layer.
    do_golden = args.golden or args.update_golden

    fixtures = discover_fixtures(args.fixtures)
    os.makedirs(OUT_DIR, exist_ok=True)
    baseline = load_baseline()

    scores: List[FixtureScore] = []
    goldens: Dict[str, GoldenResult] = {}
    new_baseline: Dict[str, Dict[str, float]] = {}
    any_gate_fail = False
    index_rows: List[Dict[str, object]] = []

    print(f"pdf_reflow verify  {DIM}(MuPDF {fitz.VersionFitz}, PyMuPDF {fitz.VersionBind}){RESET}\n")

    for pdf in fixtures:
        name = os.path.basename(pdf)
        out_pdf = os.path.join(OUT_DIR, name)
        score = score_fixture(pdf, out_pdf)
        scores.append(score)

        flat = score.flat()

        golden = None
        if do_golden:
            golden = check_golden(
                name, out_pdf, GOLDEN_DIR, ACTUAL_DIR,
                threshold=args.threshold, update=args.update_golden,
            )
            goldens[name] = golden
            flat["ssim"] = round(golden.score, 4)

        new_baseline[name] = flat

        deltas = bl.compare_fixture(baseline.get(name, {}), flat)
        gate_fail = bl.has_gating_regression(deltas)
        any_gate_fail = any_gate_fail or gate_fail

        status = "FAIL" if gate_fail else "PASS"
        color = RED if gate_fail else GREEN
        ssim_txt = f" ssim={golden.score:.3f}" if golden else ""
        boot = ""
        if golden and golden.bootstrapped:
            boot = _c(" [golden bootstrapped]", YELLOW)
        if golden and golden.updated:
            boot = _c(" [golden updated]", YELLOW)
        print(
            f"  {_c(status, color)}  {name:<34} "
            f"ret={flat['retention']:.3f} head={flat['heading_retention']:.2f} "
            f"clip={flat['clipped_lines']} pua={flat['pua_chars']} "
            f"{flat['output_pages']}pg {score.seconds:.2f}s{ssim_txt}{boot}"
        )
        for d in deltas:
            if d.regressed:
                tag = "gate" if d.gate else "warn"
                col = RED if d.gate else YELLOW
                print(_c(f"        - {d.metric}: {d.baseline} -> {d.current} ({d.note}) [{tag}]", col))

        index_rows.append({
            "name": name,
            "href": f"{os.path.splitext(name)[0]}.html",
            "status": status,
            "retention": flat["retention"],
            "heading_retention": flat["heading_retention"],
            "clipped": flat["clipped_lines"],
            "ssim": flat.get("ssim", 1.0),
            "seconds": score.seconds,
        })

    # ---- report -------------------------------------------------------
    if args.report:
        img_dir = os.path.join(REPORT_DIR, "img")
        os.makedirs(img_dir, exist_ok=True)
        for score in scores:
            name = score.name
            stem = os.path.splitext(name)[0]
            src_pdf = os.path.join(FIXTURE_DIR, name)
            out_pdf = os.path.join(OUT_DIR, name)
            src_thumbs = rpt.render_thumbs(src_pdf, img_dir, f"src_{stem}")
            out_thumbs = rpt.render_thumbs(out_pdf, img_dir, f"out_{stem}")
            deltas = bl.compare_fixture(baseline.get(name, {}), new_baseline[name])
            body = rpt.fixture_page(score, deltas, src_thumbs, out_thumbs, goldens.get(name))
            rpt.write_html(os.path.join(REPORT_DIR, f"{stem}.html"),
                           f"{name} — verify", body)
        n_fail = sum(1 for r in index_rows if r["status"] == "FAIL")
        if index_rows:
            index_rows[0]["_summary"] = (
                f"{len(index_rows)} fixtures, {n_fail} failing "
                f"(MuPDF {fitz.VersionFitz}). Click a fixture for the side-by-side."
            )
        rpt.write_html(os.path.join(REPORT_DIR, "index.html"),
                       "pdf_reflow verify", rpt.index_page(index_rows))
        report_index = os.path.join(REPORT_DIR, "index.html")
        print(f"\n  report: {report_index}")
        if args.open:
            webbrowser.open(f"file://{report_index}")

    # ---- baseline write ----------------------------------------------
    if args.update_baseline:
        os.makedirs(VERIFY_DIR, exist_ok=True)
        with open(BASELINE_PATH, "w") as f:
            json.dump(new_baseline, f, indent=2, sort_keys=True)
            f.write("\n")
        print(f"\n  {_c('baseline updated', GREEN)}: {BASELINE_PATH}")
        return 0

    if not baseline:
        print(f"\n  {_c('no baseline yet', YELLOW)} — review the numbers above, then:"
              f"\n      uv run python tools/verify.py --update-baseline")
        return 0

    print()
    if any_gate_fail:
        print(_c("  REGRESSION: a gating metric moved the wrong way vs baseline.", RED))
        return 1
    print(_c("  all fixtures within baseline tolerances.", GREEN))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
