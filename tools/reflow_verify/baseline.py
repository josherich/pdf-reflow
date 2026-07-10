"""Baseline comparison with per-metric tolerances.

The scorecard is only useful as a *gate* if we know which direction each
metric should move and how much wobble is noise. This table encodes that.
A run's numbers are compared to a committed ``baseline.json``; a metric that
moves the wrong way past its tolerance is a regression and fails CI (unless
``--update-baseline`` re-blesses the numbers).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

# direction: "up" = higher is better, "down" = lower is better,
#            "flat" = any change is worth flagging (structural).
# tol: allowed adverse move before it counts as a regression.
#   For fractional metrics tol is absolute (0.01 = one point).
#   For count metrics tol is an absolute count.
#   None under "flat" means any change fails.
METRIC_RULES: Dict[str, Dict[str, object]] = {
    "retention":         {"dir": "up",   "tol": 0.01, "gate": True},
    "heading_retention": {"dir": "up",   "tol": 0.01, "gate": True},
    "clipped_lines":     {"dir": "down", "tol": 0,    "gate": True},
    "pua_chars":         {"dir": "down", "tol": 0,    "gate": True},
    "w_minus":           {"dir": "down", "tol": 5,    "gate": True},
    "w_tilde":           {"dir": "down", "tol": 5,    "gate": True},
    "w_plus":            {"dir": "down", "tol": 10,   "gate": False},
    "min_ssim":          {"dir": "up",   "tol": 0.02, "gate": True},
    "output_pages":      {"dir": "flat", "tol": None, "gate": False},
    "widow_lines":       {"dir": "down", "tol": 5,    "gate": False},
    "images_rendered":   {"dir": "flat", "tol": None, "gate": False},
    "seconds":           {"dir": "down", "tol": 1.0,  "gate": False},
}


@dataclass
class MetricDelta:
    metric: str
    baseline: Optional[float]
    current: Optional[float]
    regressed: bool
    gate: bool
    note: str = ""

    @property
    def delta(self) -> Optional[float]:
        if self.baseline is None or self.current is None:
            return None
        return self.current - self.baseline


def compare_fixture(
    baseline: Dict[str, float], current: Dict[str, float]
) -> List[MetricDelta]:
    deltas: List[MetricDelta] = []
    for metric, cur in current.items():
        rule = METRIC_RULES.get(metric)
        base = baseline.get(metric)
        if rule is None:
            deltas.append(MetricDelta(metric, base, cur, False, False, "untracked"))
            continue
        if base is None:
            deltas.append(MetricDelta(metric, None, cur, False, bool(rule["gate"]), "new"))
            continue
        direction = rule["dir"]
        tol = rule["tol"]
        regressed = False
        note = ""
        if direction == "up":
            if cur < base - tol:
                regressed = True
                note = f"-{base - cur:.4g}"
        elif direction == "down":
            if cur > base + tol:
                regressed = True
                note = f"+{cur - base:.4g}"
        elif direction == "flat":
            if cur != base:
                note = f"{'+' if cur > base else ''}{cur - base:.4g}"
        deltas.append(
            MetricDelta(metric, base, cur, regressed, bool(rule["gate"]), note)
        )
    return deltas


def has_gating_regression(deltas: List[MetricDelta]) -> bool:
    return any(d.regressed and d.gate for d in deltas)
