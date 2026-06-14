"""Knuth-Plass optimal line breaking ("total fit").

The classic greedy / first-fit wrapper decides each line in isolation and
never reconsiders, which leaves a ragged right edge: one line crams in a
long word and the next is nearly empty.  Knuth & Plass's algorithm
("Breaking Paragraphs into Lines", *Software—Practice and Experience*,
1981 — the algorithm behind TeX) instead chooses *all* the break points
together so the paragraph as a whole is as even as possible.

The paragraph is modelled as a stream of three item kinds:

  * ``Box``     — an unbreakable chunk of text, with a fixed width.
  * ``Glue``    — flexible space; a legal break point (when preceded by a
                  box).  Carries a natural width plus how far it may
                  stretch / shrink.
  * ``Penalty`` — an optional break point with an associated cost
                  (``-INF`` forces a break, ``+INF`` forbids one) and an
                  optional width that only materialises if the line breaks
                  there (e.g. a hyphen).

We measure each candidate line by its *adjustment ratio* (how much the
glue must stretch to fill the column), turn that into a *badness*, and
add per-line and hyphenation penalties to get *demerits*.  A shortest-
path search over break points — pruned with an active-node list so it
stays close to linear — minimises the total demerits.

This tool sets text ragged-right (the renderer does not justify), so the
configuration penalises short lines without ever overrunning the column;
the result is the minimum-raggedness paragraph reachable through the
legal (UAX #14) break opportunities.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple


INF = 1_000_000.0          # stands in for infinity in penalties / stretch
_MAX_BADNESS = 10_000.0    # cap so a hopeless line can't overflow demerits


@dataclass
class Box:
    width: float


@dataclass
class Glue:
    width: float
    stretch: float
    shrink: float


@dataclass
class Penalty:
    width: float
    penalty: float
    flagged: bool = False


Item = object  # Box | Glue | Penalty


@dataclass(eq=False)
class _Node:
    """An active breakpoint considered by the search."""
    position: int        # item index of this breakpoint (-1 = paragraph start)
    line: int            # number of lines up to and including this break
    demerits: float      # minimum total demerits to reach this breakpoint
    previous: Optional["_Node"]


@dataclass
class BreakParams:
    line_penalty: float = 10.0      # bias toward fewer lines / cohesion
    flagged_penalty: float = 100.0  # discourage two hyphenated lines in a row
    # Per-line stretchability used to turn leftover space into an
    # adjustment ratio. Set at build time from the space width.
    default_stretch: float = 12.0


def _line_metrics(
    items: List[Item],
    cumw: List[float],
    cumy: List[float],
    cumz: List[float],
    a: int,
    b: int,
) -> Tuple[float, float, float]:
    """Natural width / total stretch / total shrink of the line that runs
    from breakpoint ``a`` (exclusive) to breakpoint ``b`` (the break)."""
    start = a + 1
    width = cumw[b] - cumw[start]
    item_b = items[b]
    if isinstance(item_b, Penalty):
        width += item_b.width
    stretch = cumy[b] - cumy[start]
    shrink = cumz[b] - cumz[start]
    return width, stretch, shrink


def _badness(natural: float, stretch: float, line_width: float,
             default_stretch: float) -> Optional[float]:
    """Badness of a line, or None when the line cannot fit (overfull).

    With no shrink configured (ragged-right), any line wider than the
    column is rejected outright so text never spills into the margin."""
    if natural > line_width + 1e-6:
        return None
    slack = line_width - natural
    if slack <= 1e-6:
        return 0.0
    eff = stretch if stretch > 0 else default_stretch
    r = slack / eff
    return min(100.0 * (r ** 3), _MAX_BADNESS)


def _is_break(items: List[Item], i: int) -> bool:
    """True if a line may break at item ``i``."""
    it = items[i]
    if isinstance(it, Penalty):
        return it.penalty < INF
    if isinstance(it, Glue):
        # Glue is a breakpoint only when preceded by a non-discardable box.
        return i > 0 and isinstance(items[i - 1], Box)
    return False


def break_lines(items: List[Item], line_width: float,
                params: Optional[BreakParams] = None) -> List[int]:
    """Return the chosen breakpoint item indices for ``items``.

    ``items`` must already end with a forced break (a final ``Glue`` with
    large stretch followed by a ``Penalty(-INF)``); :func:`add_final_break`
    appends it. The returned list is the ordered item indices at which the
    paragraph breaks, the last entry being that forced terminal break.
    """
    params = params or BreakParams()
    m = len(items)
    if m == 0:
        return []

    # Prefix sums of width / stretch / shrink (penalty widths excluded —
    # they only count when a line actually breaks at that penalty).
    cumw = [0.0] * (m + 1)
    cumy = [0.0] * (m + 1)
    cumz = [0.0] * (m + 1)
    for i, it in enumerate(items):
        if isinstance(it, Box):
            w, y, z = it.width, 0.0, 0.0
        elif isinstance(it, Glue):
            w, y, z = it.width, it.stretch, it.shrink
        else:
            w = y = z = 0.0
        cumw[i + 1] = cumw[i] + w
        cumy[i + 1] = cumy[i] + y
        cumz[i + 1] = cumz[i] + z

    start = _Node(position=-1, line=0, demerits=0.0, previous=None)
    active: List[_Node] = [start]
    best_terminal: Optional[_Node] = None

    for b in range(m):
        if not _is_break(items, b):
            continue
        item_b = items[b]
        forced = isinstance(item_b, Penalty) and item_b.penalty <= -INF
        pen = item_b.penalty if isinstance(item_b, Penalty) else 0.0
        flagged_b = isinstance(item_b, Penalty) and item_b.flagged

        best_node: Optional[_Node] = None
        best_demerits = INF * INF
        survivors: List[_Node] = []
        emergency: Optional[_Node] = None

        for node in active:
            natural, stretch, shrink = _line_metrics(
                items, cumw, cumy, cumz, node.position, b)
            # Track the nearest feasible-by-construction parent so a
            # breakpoint always has *some* parent even if every active
            # node turned out overfull (should not happen once oversize
            # boxes are pre-split, but keeps the search total).
            if emergency is None or node.position > emergency.position:
                emergency = node
            badness = _badness(natural, stretch, line_width,
                               params.default_stretch)
            if badness is None:
                # Overfull for this break — and for every later break too,
                # since widths only grow — so retire this node.
                continue
            survivors.append(node)

            d = (params.line_penalty + badness) ** 2
            if pen >= 0 and pen < INF:
                d += pen * pen
            elif pen > -INF:
                d -= pen * pen
            if flagged_b and _node_flagged(items, node):
                d += params.flagged_penalty
            total = node.demerits + d
            if total < best_demerits:
                best_demerits = total
                best_node = node

        if best_node is None and emergency is not None:
            # Nothing fit (should not happen once oversize boxes are
            # pre-split); force a break from the closest parent so the
            # search stays total instead of dropping the breakpoint.
            best_node = emergency
            best_demerits = emergency.demerits

        active = survivors
        if best_node is not None:
            new_node = _Node(position=b, line=best_node.line + 1,
                             demerits=best_demerits, previous=best_node)
            if forced:
                best_terminal = new_node
                # A forced break starts a fresh paragraph segment; clear
                # the active list so nothing leaks across it.
                active = [new_node]
            else:
                active.append(new_node)

    if best_terminal is None:
        # No forced terminal was supplied; take the best active node.
        best_terminal = min(active, key=lambda n: n.demerits, default=start)

    breaks: List[int] = []
    node: Optional[_Node] = best_terminal
    while node is not None and node.position >= 0:
        breaks.append(node.position)
        node = node.previous
    breaks.reverse()
    return breaks


def _node_flagged(items: List[Item], node: _Node) -> bool:
    if node.position < 0:
        return False
    it = items[node.position]
    return isinstance(it, Penalty) and it.flagged


def add_final_break(items: List[Item]) -> List[Item]:
    """Append the canonical terminal sequence (finishing glue + forced
    penalty) so the last line is free to be short."""
    out = list(items)
    out.append(Glue(width=0.0, stretch=INF, shrink=0.0))
    out.append(Penalty(width=0.0, penalty=-INF, flagged=False))
    return out
