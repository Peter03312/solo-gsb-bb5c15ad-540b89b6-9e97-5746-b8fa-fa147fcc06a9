"""Pure adjudication rules for radio applications.

An application grants:

* a coverage **circle** in planar coordinates ``(cx, cy, r)``;
* a **closed** frequency interval ``[freq_low, freq_high]``;
* a **half-open** UTC time interval ``[start_time, end_time)``.

Two *effective* (approved and not later revoked) applications interfere iff
ALL of the following hold:

1. the time intervals intersect;
2. the coverage circles intersect;
3. the closed frequency bands intersect, OR the minimum gap between them is
   strictly smaller than the non-negative guard band.

The module is free of framework / database code so the rules can be unit
tested directly.  ``available_ranges`` inverts the same rules: given a
candidate whose band width is fixed, it computes every maximal interval of
band lower bounds that would preview conflict-free.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

# Conflict reason codes (also documented in the README).
REASON_FREQUENCY_INTERSECTION = "frequency_intersection"
REASON_WITHIN_GUARD_BAND = "within_guard_band"
# A hit against an active temporary restriction window (circle + half-open
# time + closed frequency band all intersect; the guard band never extends a
# window).
REASON_RESTRICTION_WINDOW = "restriction_window"


@dataclass(frozen=True)
class Circle:
    cx: float
    cy: float
    radius: float


@dataclass(frozen=True)
class License:
    """An effective approved application, as seen by the adjudicator."""

    application_id: int
    circle: Circle
    freq_low: float
    freq_high: float
    start_time: datetime
    end_time: datetime


@dataclass(frozen=True)
class Conflict:
    other: License
    time_overlap: bool
    area_overlap: bool
    frequency_intersects: bool
    minimum_frequency_gap: float
    reason: str  # REASON_*; only set when all three trigger conditions hold


@dataclass(frozen=True)
class AvailableRange:
    """One maximal continuous range of attainable candidate lower bounds.

    ``min``/``max`` bound the candidate band's *lower* bound ``x``; the band
    itself is ``[x, x + width]`` with the width fixed by the application.
    ``min_inclusive``/``max_inclusive`` tell whether the endpoint itself is
    attainable.  A range may degenerate to a single attainable point
    (``min == max``, both inclusive).
    """

    min: float
    max: float
    min_inclusive: bool
    max_inclusive: bool


@dataclass(frozen=True)
class PairConflict:
    """An interfering pair inside a set of licenses (symmetric view).

    Used when a whole set is checked against itself, e.g. previewing a
    proposed guard band against the current effective set.
    """

    first: License
    second: License
    time_overlap: bool
    area_overlap: bool
    frequency_intersects: bool
    minimum_frequency_gap: float
    reason: str  # REASON_*


@dataclass(frozen=True)
class RestrictionWindow:
    """An active temporary spectrum-control window.

    A window hits a license/application when its coverage circle intersects
    the other circle, its half-open UTC time interval intersects the other
    half-open interval, and its closed frequency band intersects the other
    closed band (touching endpoints intersect).  The guard band never
    extends a window: only an actual closed-band intersection counts.
    """

    window_id: int
    circle: Circle
    freq_low: float
    freq_high: float
    start_time: datetime
    end_time: datetime
    reason: str = ""
    revision: int | None = None


@dataclass(frozen=True)
class WindowHit:
    """One restriction window hit by a candidate license.

    The trigger flags mirror :class:`Conflict` so existing clients can reuse
    the same rendering; ``frequency_intersects`` is the only frequency
    condition a window has (``minimum_frequency_gap`` is 0.0 on a hit and the
    guard band is irrelevant).
    """

    window: RestrictionWindow
    time_intersect: bool
    area_intersect: bool
    frequency_intersect: bool
    reason: str = REASON_RESTRICTION_WINDOW


def restriction_window_hit(
    lic: License, window: RestrictionWindow
) -> WindowHit | None:
    """Whether one license/application hits one restriction window.

    All three dimensions must intersect: circles (touching counts),
    half-open time intervals (end == start does not overlap) and closed
    frequency bands (touching endpoints intersect).  No guard band is
    applied.
    """
    t = half_open_intervals_intersect(
        lic.start_time, lic.end_time, window.start_time, window.end_time
    )
    a = circles_intersect(lic.circle, window.circle)
    f = closed_intervals_intersect(
        lic.freq_low, lic.freq_high, window.freq_low, window.freq_high
    )
    if not (t and a and f):
        return None
    return WindowHit(
        window=window,
        time_intersect=True,
        area_intersect=True,
        frequency_intersect=True,
    )


def evaluate_restriction_windows(
    candidate: License, windows: list[RestrictionWindow]
) -> list[WindowHit]:
    """Every active restriction window hit by ``candidate``.

    The guard band never extends a window, so only geometric (circle,
    half-open time, closed frequency) intersections are considered.
    """
    return [
        hit
        for window in windows
        if (hit := restriction_window_hit(candidate, window)) is not None
    ]


def circles_intersect(a: Circle, b: Circle) -> bool:
    """Planar disks intersect iff centre distance <= sum of radii.

    Touching at one point (distance exactly equal to the sum) counts as an
    intersection.
    """
    distance = math.hypot(a.cx - b.cx, a.cy - b.cy)
    return distance <= a.radius + b.radius


def closed_intervals_intersect(
    low_a: float, high_a: float, low_b: float, high_b: float
) -> bool:
    """Closed intervals: touching endpoints intersect (a1 <= b0 and b1 <= a0)."""
    return low_a <= high_b and low_b <= high_a


def half_open_intervals_intersect(
    start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime
) -> bool:
    """[start_a, end_a) vs [start_b, end_b): end == start does NOT overlap."""
    return start_a < end_b and start_b < end_a


def minimum_frequency_gap(
    low_a: float, high_a: float, low_b: float, high_b: float
) -> float:
    """0 when the closed bands intersect, otherwise the gap between them."""
    if closed_intervals_intersect(low_a, high_a, low_b, high_b):
        return 0.0
    return low_b - high_a if high_a < low_b else low_a - high_b


def frequency_conflicts(
    low_a: float,
    high_a: float,
    low_b: float,
    high_b: float,
    guard_band: float,
) -> tuple[bool, bool, float, str | None]:
    """Return (conflicts, intersects, minimum_gap, reason).

    ``guard_band`` must be non-negative.  A gap exactly equal to the guard
    band is allowed ("strictly smaller than" triggers the conflict).
    """
    if closed_intervals_intersect(low_a, high_a, low_b, high_b):
        return True, True, 0.0, REASON_FREQUENCY_INTERSECTION
    gap = minimum_frequency_gap(low_a, high_a, low_b, high_b)
    if gap < guard_band:
        return True, False, gap, REASON_WITHIN_GUARD_BAND
    return False, False, gap, None


def _pair_trigger(
    a: License, b: License, guard_band: float
) -> tuple[bool, float, str] | None:
    """Shared interference gate for one pair of licenses.

    Returns ``(frequency_intersects, minimum_gap, reason)`` when the pair
    interferes under ``guard_band``, else ``None``.  Time and area
    intersection are necessary conditions; frequency overlap (or a
    guard-band violation) is the third.
    """
    t_overlap = half_open_intervals_intersect(
        a.start_time, a.end_time, b.start_time, b.end_time
    )
    a_overlap = circles_intersect(a.circle, b.circle)
    if not (t_overlap and a_overlap):
        return None
    freq_conflicts, freq_intersects, gap, reason = frequency_conflicts(
        a.freq_low, a.freq_high, b.freq_low, b.freq_high, guard_band
    )
    if not freq_conflicts:
        return None
    return freq_intersects, gap, (reason or REASON_FREQUENCY_INTERSECTION)


def evaluate(
    candidate: License, effective: list[License], guard_band: float
) -> list[Conflict]:
    """All interference conflicts between ``candidate`` and effective grants.

    Every checked grant is returned with its trigger flags so callers can
    preview exactly which conditions fired.
    """
    conflicts: list[Conflict] = []
    for other in effective:
        trigger = _pair_trigger(candidate, other, guard_band)
        if trigger is None:
            continue
        freq_intersects, gap, reason = trigger
        conflicts.append(
            Conflict(
                other=other,
                time_overlap=True,
                area_overlap=True,
                frequency_intersects=freq_intersects,
                minimum_frequency_gap=gap,
                reason=reason,
            )
        )
    return conflicts


def find_interfering_pairs(
    licenses: list[License], guard_band: float
) -> list[PairConflict]:
    """Every interfering pair *within* ``licenses`` under ``guard_band``.

    Used to (pre-)check a proposed guard band against the current effective
    set: the change may only be confirmed when this returns an empty list.
    Each interfering combination is reported once, in input order.
    """
    pairs: list[PairConflict] = []
    for i in range(len(licenses)):
        for j in range(i + 1, len(licenses)):
            trigger = _pair_trigger(licenses[i], licenses[j], guard_band)
            if trigger is None:
                continue
            freq_intersects, gap, reason = trigger
            pairs.append(
                PairConflict(
                    first=licenses[i],
                    second=licenses[j],
                    time_overlap=True,
                    area_overlap=True,
                    frequency_intersects=freq_intersects,
                    minimum_frequency_gap=gap,
                    reason=reason,
                )
            )
    return pairs


def evaluate_group(
    candidates: list[License], effective: list[License], guard_band: float
) -> tuple[dict[int, list[Conflict]], dict[int, list[Conflict]]]:
    """Adjudicate an ordered group as one atomic decision.

    Returns ``(against_effective, within_group)``, each keyed by the
    candidate's ``application_id`` (every candidate gets a key, possibly
    mapped to an empty list):

    * ``against_effective[id]`` - clashes with the currently effective grants,
      in effective-set order;
    * ``within_group[id]``      - clashes with other members of this group.

    Interference is symmetric, so an intra-group clash between two members is
    reported on *both* members (each listing the other as the conflict
    object).  Input order determines only the order in which a successful
    group consumes its consecutive revisions; it never hides a clash.
    """
    against_effective: dict[int, list[Conflict]] = {
        candidate.application_id: [] for candidate in candidates
    }
    within_group: dict[int, list[Conflict]] = {
        candidate.application_id: [] for candidate in candidates
    }

    for candidate in candidates:
        against_effective[candidate.application_id].extend(
            evaluate(candidate, effective, guard_band)
        )

    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            # evaluate() with a singleton other returns 0 or 1 conflict;
            # run both directions so each side gets itself as the subject.
            forward = evaluate(candidates[i], [candidates[j]], guard_band)
            if not forward:
                continue
            backward = evaluate(candidates[j], [candidates[i]], guard_band)
            within_group[candidates[i].application_id].append(forward[0])
            within_group[candidates[j].application_id].append(backward[0])

    return against_effective, within_group


def available_ranges(
    candidate: License,
    effective: list[License],
    guard_band: float,
    low_min: float,
    low_max: float,
    windows: list[RestrictionWindow] | None = None,
) -> list[AvailableRange]:
    """Maximal lower-bound ranges in ``[low_min, low_max]`` free of conflicts.

    The candidate keeps its circle, UTC time window and band *width*
    (``freq_high - freq_low``, possibly zero for a single frequency); only
    the lower bound ``x`` of its band varies over the closed candidate
    interval ``[low_min, low_max]``.

    A grant intersecting the candidate in time *and* area forbids exactly
    the lower bounds ``(freq_low - width - guard_band, freq_high +
    guard_band)``: inside it the shifted bands intersect or sit strictly
    within the guard band.  With a zero guard band the endpoints stay
    forbidden (touching closed bands interfere), so the forbidden interval
    is closed; with a positive guard band a gap exactly equal to the band
    is allowed, so it is open.  Grants disjoint in time or area forbid
    nothing.

    Active restriction windows contribute additional forbidden ranges: a
    window intersecting the candidate in time *and* area forbids the closed
    lower-bound interval ``[window_low - width, window_high]`` (both
    endpoints forbidden, because touching closed bands hit the window).
    The guard band never extends a window, so its forbidden interval is
    never widened.

    The result is the complement of the merged forbidden intervals inside
    ``[low_min, low_max]``: every maximal continuous available range, in
    ascending order, each endpoint flagged attainable or not; empty when no
    lower bound in the candidate interval is usable.  Consistent with
    :func:`evaluate` plus :func:`evaluate_restriction_windows`: a lower
    bound is attainable exactly when the shifted candidate would preview
    with no license conflict and hit no window.  Attainable endpoints of
    open (guard-band) intervals are nudged by ulps until they pass the very
    frequency predicate :func:`evaluate` applies, so a preview at a reported
    endpoint agrees with the range even under floating-point rounding.
    """
    width = candidate.freq_high - candidate.freq_low
    # Forbidden intervals as (start, end, closed): a closed interval forbids
    # its own endpoints; an open one leaves them attainable.
    intervals: list[tuple[float, float, bool]] = []
    for other in effective:
        if not half_open_intervals_intersect(
            candidate.start_time,
            candidate.end_time,
            other.start_time,
            other.end_time,
        ):
            continue
        if not circles_intersect(candidate.circle, other.circle):
            continue
        start = other.freq_low - width - guard_band
        end = other.freq_high + guard_band
        closed = guard_band == 0
        if not closed:
            # The endpoints are reported as attainable (a gap exactly equal
            # to the guard band is allowed).  Rounding may leave the closed
            # form an ulp inside the forbidden zone, so nudge outward until
            # the shifted band passes evaluate()'s frequency predicate.
            while frequency_conflicts(
                start, start + width,
                other.freq_low, other.freq_high, guard_band,
            )[0]:
                start = math.nextafter(start, -math.inf)
            while frequency_conflicts(
                end, end + width,
                other.freq_low, other.freq_high, guard_band,
            )[0]:
                end = math.nextafter(end, math.inf)
        intervals.append((start, end, closed))

    for window in windows or []:
        if not half_open_intervals_intersect(
            candidate.start_time,
            candidate.end_time,
            window.start_time,
            window.end_time,
        ):
            continue
        if not circles_intersect(candidate.circle, window.circle):
            continue
        # Closed bands: x in [window_low - width, window_high] hits (the
        # touching endpoints themselves intersect and are forbidden).
        intervals.append(
            (window.freq_low - width, window.freq_high, True)
        )

    intervals.sort()

    # Merge connected forbidden intervals.  Each merged interval remembers
    # whether its two endpoints are forbidden themselves.  A closed
    # interval connects to the next when they touch at an endpoint; open
    # endpoints leave that shared point available and stay separate.
    merged: list[list] = []
    for start, end, closed in intervals:
        if merged:
            last_start, last_end, last_start_closed, last_end_closed = merged[-1]
            if start < last_end or (
                start == last_end and last_end_closed and closed
            ):
                if end > last_end:
                    merged[-1] = [
                        last_start,
                        end,
                        last_start_closed,
                        closed,
                    ]
                elif end == last_end:
                    merged[-1][3] = last_end_closed or closed
                continue
        merged.append([start, end, closed, closed])

    # Complement of the merged intervals inside [low_min, low_max],
    # tracking endpoint attainability.
    ranges: list[AvailableRange] = []

    def emit(lo: float, hi: float, lo_incl: bool, hi_incl: bool) -> None:
        if lo < hi or (lo == hi and lo_incl and hi_incl):
            ranges.append(AvailableRange(lo, hi, lo_incl, hi_incl))

    cursor = low_min
    cursor_inclusive = True  # the candidate interval is closed at low_min
    for start, end, start_closed, end_closed in merged:
        if end < low_min or start > low_max:
            continue
        if cursor <= start:
            # The right side is attainable only when the forbidden interval
            # starts with an open endpoint.
            emit(cursor, start, cursor_inclusive, not start_closed)
        if end >= cursor:
            cursor = end
            cursor_inclusive = not end_closed
    if cursor <= low_max:
        emit(cursor, low_max, cursor_inclusive, True)
    return ranges
