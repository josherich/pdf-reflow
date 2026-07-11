#!/usr/bin/env python3
"""Verify harness for pdf_reflow -- iterate on and guard reflow quality.

    uv run python tools/verify.py                 # score all fixtures, gate vs baseline
    uv run python tools/verify.py --report        # + write an HTML scorecard report
    uv run python tools/verify.py --update-baseline   # re-bless the numeric baseline
    uv run python tools/verify.py --fixtures bitcoin.pdf two-column.pdf
    uv run python tools/verify.py --report --open # open the report in a browser
    uv run python tools/verify.py --serve         # visual feedback web tool
    uv run python tools/verify.py --feedback      # dump human feedback as JSON

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
from reflow_verify import baseline as bl  # noqa: E402
from reflow_verify import report as rpt  # noqa: E402
from reflow_verify import visual as vz  # noqa: E402
from reflow_verify import webtool as wt  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(ROOT, "tests", "fixtures")
VERIFY_DIR = os.path.join(ROOT, "verify")
OUT_DIR = os.path.join(VERIFY_DIR, "out")
REPORT_DIR = os.path.join(VERIFY_DIR, "report")
PAGES_DIR = os.path.join(VERIFY_DIR, "pages")
GOLDEN_DIR = os.path.join(VERIFY_DIR, "golden")
FEEDBACK_DIR = os.path.join(VERIFY_DIR, "feedback")
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


def ensure_outputs(fixtures: List[str]) -> None:
    """Reflow any fixture whose output PDF is missing or stale, without scoring.

    Used by --serve / --feedback so the visual tools always look at output
    from the current code, even when the scorecard hasn't been run yet.
    """
    from pdf_reflow import reflow_pdf, ReflowConfig

    for pdf in fixtures:
        out_pdf = os.path.join(OUT_DIR, os.path.basename(pdf))
        if (not os.path.exists(out_pdf)
                or os.path.getmtime(out_pdf) < os.path.getmtime(pdf)):
            print(f"  reflowing {os.path.basename(pdf)} ...")
            reflow_pdf(pdf, out_pdf, ReflowConfig())


def serve(port: int) -> int:
    httpd = wt.make_server(FIXTURE_DIR, OUT_DIR, PAGES_DIR, GOLDEN_DIR,
                           FEEDBACK_DIR, port=port)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"\n  visual feedback tool: {url}  (Ctrl-C to stop)")
    print(f"  golden images  -> {GOLDEN_DIR}")
    print(f"  annotations    -> {FEEDBACK_DIR}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixtures", nargs="*", default=[],
                    help="fixture filenames (default: all tests/fixtures/*.pdf)")
    ap.add_argument("--report", action="store_true", help="write HTML scorecard report")
    ap.add_argument("--update-baseline", action="store_true", help="re-bless numeric baseline")
    ap.add_argument("--open", action="store_true", help="open the report when done")
    ap.add_argument("--serve", action="store_true",
                    help="launch the visual feedback web tool (golden compare + annotate)")
    ap.add_argument("--port", type=int, default=8017, help="port for --serve (0 = any free)")
    ap.add_argument("--feedback", action="store_true",
                    help="print human visual feedback (golden diffs + annotations) as JSON")
    args = ap.parse_args()

    fixtures = discover_fixtures(args.fixtures)
    os.makedirs(OUT_DIR, exist_ok=True)

    # ---- Layer 2: visual feedback modes (no scoring) -------------------
    if args.serve or args.feedback:
        ensure_outputs(fixtures)
        if args.feedback:
            summary = vz.feedback_summary(OUT_DIR, PAGES_DIR, GOLDEN_DIR, FEEDBACK_DIR)
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            return 0
        return serve(args.port)

    baseline = load_baseline()

    scores: List[FixtureScore] = []
    new_baseline: Dict[str, Dict[str, float]] = {}
    any_gate_fail = False
    any_feedback = False
    index_rows: List[Dict[str, object]] = []

    print(f"pdf_reflow verify  {DIM}(MuPDF {fitz.VersionFitz}, PyMuPDF {fitz.VersionBind}){RESET}\n")

    for pdf in fixtures:
        name = os.path.basename(pdf)
        out_pdf = os.path.join(OUT_DIR, name)
        score = score_fixture(pdf, out_pdf)
        scores.append(score)

        # Layer-2 signals, when the user has provided them: mean pixel diff
        # vs uploaded golden page images, and open annotation notes.
        stem = os.path.splitext(name)[0]
        ratios = vz.golden_compare(out_pdf, os.path.join(PAGES_DIR, stem),
                                   os.path.join(GOLDEN_DIR, stem))
        if ratios:
            score.metrics["golden_diff"] = round(sum(ratios.values()) / len(ratios), 4)
        open_notes = vz.open_annotation_count(FEEDBACK_DIR, stem)
        any_feedback = any_feedback or bool(ratios) or bool(open_notes)

        flat = score.flat()
        new_baseline[name] = flat

        deltas = bl.compare_fixture(baseline.get(name, {}), flat)
        gate_fail = bl.has_gating_regression(deltas)
        any_gate_fail = any_gate_fail or gate_fail

        status = "FAIL" if gate_fail else "PASS"
        color = RED if gate_fail else GREEN
        extra = ""
        if ratios:
            extra += f" gold={flat['golden_diff']:.3f}"
        if open_notes:
            extra += _c(f" notes={open_notes}", YELLOW)
        print(
            f"  {_c(status, color)}  {name:<34} "
            f"ret={flat['retention']:.3f} head={flat['heading_retention']:.2f} "
            f"clip={flat['clipped_lines']} pua={flat['pua_chars']} "
            f"{flat['output_pages']}pg {score.seconds:.2f}s{extra}"
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
            "seconds": score.seconds,
        })

    # ---- report -------------------------------------------------------
    if args.report:
        os.makedirs(REPORT_DIR, exist_ok=True)
        for score in scores:
            name = score.name
            stem = os.path.splitext(name)[0]
            deltas = bl.compare_fixture(baseline.get(name, {}), new_baseline[name])
            body = rpt.fixture_page(score, deltas)
            rpt.write_html(os.path.join(REPORT_DIR, f"{stem}.html"),
                           f"{name} — verify", body)
        n_fail = sum(1 for r in index_rows if r["status"] == "FAIL")
        if index_rows:
            index_rows[0]["_summary"] = (
                f"{len(index_rows)} fixtures, {n_fail} failing "
                f"(MuPDF {fitz.VersionFitz}). Click a fixture for its scorecard."
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
    if any_feedback:
        print(_c("  human visual feedback present — read it with: "
                 "uv run python tools/verify.py --feedback", DIM))
    if any_gate_fail:
        print(_c("  REGRESSION: a gating metric moved the wrong way vs baseline.", RED))
        return 1
    print(_c("  all fixtures within baseline tolerances.", GREEN))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
