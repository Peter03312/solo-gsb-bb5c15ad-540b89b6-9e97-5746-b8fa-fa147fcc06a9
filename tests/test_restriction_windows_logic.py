"""Unit tests for restriction-window hit rules and window-forbidden ranges.

A window hits a license iff the coverage circles intersect, the half-open
UTC intervals intersect and the closed frequency bands intersect (touching
band endpoints hit).  The guard band never extends a window: a mere gap
smaller than the guard band does not hit.
"""
from datetime import datetime

from app.adjudication import (
    Circle,
    License,
    RestrictionWindow,
    available_ranges,
    evaluate_restriction_windows,
    restriction_window_hit,
)

DT = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))


def lic(
    app_id=1,
    lo=100.0,
    hi=110.0,
    start="2026-10-01T08:00:00Z",
    end="2026-10-01T12:00:00Z",
    cx=0.0,
    cy=0.0,
    radius=100.0,
):
    return License(app_id, Circle(cx, cy, radius), lo, hi, DT(start), DT(end))


def win(
    window_id=7,
    lo=105.0,
    hi=108.0,
    start="2026-10-01T09:00:00Z",
    end="2026-10-01T11:00:00Z",
    cx=0.0,
    cy=0.0,
    radius=100.0,
):
    return RestrictionWindow(window_id, Circle(cx, cy, radius), lo, hi, DT(start), DT(end))


class TestWindowHits:
    def test_full_overlap_hits(self):
        assert restriction_window_hit(lic(), win()) is not None

    def test_touching_band_endpoints_hit(self):
        # Window band starts exactly at the license band's high bound:
        # closed intervals touch -> hit.
        assert restriction_window_hit(lic(lo=100, hi=110), win(lo=110, hi=120))
        assert restriction_window_hit(lic(lo=110, hi=120), win(lo=100, hi=110))

    def test_band_gap_never_hits_even_within_guard_band(self):
        # A 1 MHz gap would be within a 2 MHz guard band, but the guard
        # band never extends a window.
        assert restriction_window_hit(lic(lo=100, hi=110), win(lo=111, hi=112)) is None

    def test_half_open_time_boundary_does_not_hit(self):
        # Window starts exactly when the license interval ends.
        assert restriction_window_hit(
            lic(end="2026-10-01T12:00:00Z"),
            win(start="2026-10-01T12:00:00Z", end="2026-10-01T13:00:00Z"),
        ) is None

    def test_disjoint_circles_do_not_hit(self):
        assert restriction_window_hit(
            lic(cx=0, cy=0, radius=10), win(cx=1000, cy=0, radius=10)
        ) is None

    def test_touching_circles_hit(self):
        # Centre distance exactly equal to the sum of radii.
        assert restriction_window_hit(
            lic(cx=0, cy=0, radius=10), win(cx=20, cy=0, radius=10)
        )

    def test_evaluate_lists_only_hitting_windows(self):
        hits = evaluate_restriction_windows(
            lic(),
            [
                win(window_id=1),  # hits
                win(window_id=2, lo=300, hi=310),  # disjoint band
                win(window_id=3, start="2026-10-02T00:00:00Z",
                    end="2026-10-02T01:00:00Z"),  # disjoint time
            ],
        )
        assert [h.window.window_id for h in hits] == [1]
        hit = hits[0]
        assert hit.time_intersect and hit.area_intersect and hit.frequency_intersect
        assert hit.reason == "restriction_window"


class TestWindowForbiddenRanges:
    def test_window_forbids_closed_lower_bound_interval(self):
        # Candidate width 5; window band [105,108] forbids x in [100,108]
        # (both endpoints forbidden: touching closed bands hit).
        ranges = available_ranges(
            lic(2, lo=0, hi=5), [], 2.0, 90.0, 115.0, windows=[win(lo=105, hi=108)]
        )
        assert [(r.min, r.max, r.min_inclusive, r.max_inclusive) for r in ranges] == [
            (90.0, 100.0, True, False),
            (108.0, 115.0, False, True),
        ]

    def test_window_does_not_expand_with_guard_band(self):
        # A license guard band widens the forbidden interval; a window does
        # not.  With no licenses the forbidden interval stays exactly
        # [100,108] regardless of the guard band.
        for band in (0.0, 2.0, 10.0):
            ranges = available_ranges(
                lic(2, lo=0, hi=5), [], band, 100.0, 108.0,
                windows=[win(lo=105, hi=108)],
            )
            assert ranges == []

    def test_windows_only_block_when_time_and_area_intersect(self):
        ranges = available_ranges(
            lic(2, lo=0, hi=5), [], 0.0, 90.0, 115.0,
            windows=[win(lo=105, hi=108, start="2026-10-02T00:00:00Z",
                       end="2026-10-02T01:00:00Z")],
        )
        assert [(r.min, r.max) for r in ranges] == [(90.0, 115.0)]

    def test_window_closed_and_grant_open_intervals_compose(self):
        # Grant [110,120] with guard band 2 forbids open (103,122);
        # window [105,108] forbids closed [100,108].  Result complement in
        # [90,130]: [90,100] (100 forbidden), [122,130] (122 attainable).
        ranges = available_ranges(
            lic(2, lo=0, hi=5),
            [lic(3, lo=110, hi=120)],
            2.0,
            90.0,
            130.0,
            windows=[win(lo=105, hi=108)],
        )
        assert [(r.min, r.max, r.min_inclusive, r.max_inclusive) for r in ranges] == [
            (90.0, 100.0, True, False),
            (122.0, 130.0, True, True),
        ]
