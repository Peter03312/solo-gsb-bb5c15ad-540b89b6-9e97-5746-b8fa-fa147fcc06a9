"""Tests for the database candidate-set narrowing.

Every adjudication chain must first narrow the database candidate set by
time window, coverage and frequency band and then run the exact
in-process predicates.  The narrowing may over-fetch, but it must never
drop a row that could conflict -- including boundary cases (touching
circles, touching closed frequency endpoints, half-open time adjacency)
and the whole swept lower-bound interval of an available-ranges query.

These tests populate one close/boundary license plus many far-away
(time-disjoint) licenses and assert both outcomes (the boundary license
is still found; far-away rows are filtered out of the DB query).
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.adjudication import Circle, License, RestrictionWindow
from app.db import SessionLocal
from app.main import (
    _apply_application_filters,
    _available_ranges_envelope,
    _candidate_envelope,
    _proposed_window_envelope,
    _window_hit_envelope,
    load_active_window_ids,
    load_candidate_effective_licenses,
    load_candidate_windows,
    load_effective_licenses,
)
from app.models import Application

from factories import make_payload


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _current_revision(client) -> int:
    return client.get("/api/revision/current").json()["current_revision"]


def _submit(client, **overrides) -> int:
    return client.post(
        "/api/applications", json=make_payload(**overrides)
    ).json()["id"]


def _approve(client, **overrides) -> int:
    app_id = _submit(client, **overrides)
    client.post(
        f"/api/applications/{app_id}/approve",
        json={"expected_revision": _current_revision(client)},
    )
    return app_id


def _many_disjoint_apps(client, n) -> list[int]:
    """Many effective licenses far away in time/space/frequency.

    Each gets its own disjoint time day (the scenario the narrowing is
    meant to scale with: many mutually time-disjoint grants), while
    sharing far-away frequency and coverage.
    """
    ids = []
    for i in range(n):
        day = 10 + i
        ids.append(
            _approve(
                client,
                name=f"Far {i}",
                freq=(5000.0, 5001.0),
                circle=(5000.0, 5000.0, 1.0),
                start=f"2030-02-{day}T00:00:00Z",
                end=f"2030-02-{day}T01:00:00Z",
            )
        )
    return ids


CANDIDATE_KWARGS = dict(
    freq=(100.0, 110.0),
    circle=(0, 0, 10),
    start="2030-03-01T08:00:00Z",
    end="2030-03-01T10:00:00Z",
)


def _candidate() -> License:
    return License(
        999,
        Circle(0.0, 0.0, 10.0),
        100.0,
        110.0,
        _dt("2030-03-01T08:00:00Z"),
        _dt("2030-03-01T10:00:00Z"),
    )


@pytest.fixture()
def make_session():
    # Callers open the session only after the TestClient requests have
    # committed (SQLite's transaction isolation would otherwise hide
    # those writes from an earlier-opened session).
    return lambda: SessionLocal()


class TestEffectiveLicenseNarrowing:
    def test_far_away_licenses_are_filtered_out(self, client, make_session):
        far_ids = _many_disjoint_apps(client, 10)
        with make_session() as session:
            got = load_candidate_effective_licenses(
                session, _candidate_envelope(_candidate(), 2.0)
            )
            got_ids = {lic.application_id for lic in got}
            assert got_ids.isdisjoint(far_ids)
            # Sanity: the full loader would have seen every far license.
            assert {
                lic.application_id
                for lic in load_effective_licenses(session)
            } >= set(far_ids)

    def test_license_at_guard_band_gap_is_kept(self, client):
        # Existing [111,112] sits 1 MHz away (within the 2 MHz guard
        # band).  It MUST survive narrowing and be adjudicated as a
        # conflict, while far licenses never enter the candidate set.
        close_kwargs = {**CANDIDATE_KWARGS, "freq": (111.0, 112.0)}
        close_id = _approve(client, name="Close", **close_kwargs)
        _many_disjoint_apps(client, 10)
        candidate_id = _submit(client, name="Candidate", **CANDIDATE_KWARGS)
        preview = client.post(
            f"/api/applications/{candidate_id}/preview"
        ).json()
        conflict_ids = {c["application"]["id"] for c in preview["conflicts"]}
        assert conflict_ids == {close_id}

    def test_license_exactly_at_guard_band_is_kept_but_allowed(
        self, client, make_session
    ):
        # Gap exactly equal to the guard band is allowed, but the row
        # still survives narrowing (the exact predicate drops it).
        kwargs = {**CANDIDATE_KWARGS, "freq": (112.0, 113.0)}
        _approve(client, **kwargs)
        with make_session() as session:
            got = load_candidate_effective_licenses(
                session, _candidate_envelope(_candidate(), 2.0)
            )
        assert len(got) == 1

    def test_touching_circles_are_kept(self, client, make_session):
        # Centre distance exactly equal to the radius sum (20): touches.
        kwargs = {**CANDIDATE_KWARGS, "circle": (20.0, 0.0, 10.0)}
        _approve(client, **kwargs)
        with make_session() as session:
            got = load_candidate_effective_licenses(
                session, _candidate_envelope(_candidate(), 0.0)
            )
        assert len(got) == 1

    def test_diagonally_touching_circles_are_kept(self, client, make_session):
        # Diagonal touch: hypot == radius sum while each coordinate
        # separation is smaller; the axis-wise prefilter must keep it.
        d = 20.0 / (2 ** 0.5)
        kwargs = {**CANDIDATE_KWARGS, "circle": (d, d, 10.0)}
        _approve(client, **kwargs)
        with make_session() as session:
            got = load_candidate_effective_licenses(
                session, _candidate_envelope(_candidate(), 0.0)
            )
        assert len(got) == 1

    def test_adjacent_half_open_time_is_filtered_out(
        self, client, make_session
    ):
        # Existing ends exactly when candidate starts: [6,8) vs [8,10)
        # never overlap, so narrowing may drop it.
        kwargs = {
            **CANDIDATE_KWARGS,
            "start": "2030-03-01T06:00:00Z",
            "end": "2030-03-01T08:00:00Z",
        }
        _approve(client, **kwargs)
        with make_session() as session:
            got = load_candidate_effective_licenses(
                session, _candidate_envelope(_candidate(), 2.0)
            )
        assert got == []

    def test_results_stay_id_ordered(self, client, make_session):
        # Five effective licenses separated from each other by more than
        # the guard band (so all can coexist), each inside the candidate's
        # guard-widened frequency envelope.
        ids = []
        for i in range(5):
            low = 120.0 + i * 4.0  # 3 MHz gaps > 2 MHz guard
            ids.append(
                _approve(
                    client,
                    name=f"Close {i}",
                    freq=(low, low + 1.0),
                    circle=(0, 0, 10),
                    start="2030-03-01T08:00:00Z",
                    end="2030-03-01T10:00:00Z",
                )
            )
        with make_session() as session:
            got = load_candidate_effective_licenses(
                session, _candidate_envelope(_candidate(), 30.0)
            )
        assert [lic.application_id for lic in got] == ids


class TestWindowNarrowing:
    def _confirm_window(self, client, *, freq, circle, start, end, reason):
        client.post(
            "/api/restriction-windows",
            json={
                "circle": {
                    "cx": circle[0], "cy": circle[1], "radius": circle[2]
                },
                "frequency": {"low": freq[0], "high": freq[1]},
                "time_window": {"start": start, "end": end},
                "reason": reason,
                "expected_revision": _current_revision(client),
            },
        )

    def test_far_windows_filtered_but_boundary_window_kept(
        self, client, make_session
    ):
        # One hitting window that touches the candidate band at 110.
        self._confirm_window(
            client,
            freq=(110.0, 120.0), circle=(0, 0, 10),
            start="2030-03-01T09:00:00Z", end="2030-03-01T11:00:00Z",
            reason="close",
        )
        for i in range(5):
            self._confirm_window(
                client,
                freq=(8000.0, 8001.0), circle=(9000, 9000, 1),
                start="2030-01-01T00:00:00Z", end="2030-01-01T01:00:00Z",
                reason=f"far {i}",
            )
        with make_session() as session:
            rows = load_candidate_windows(
                session, _window_hit_envelope(_candidate())
            )
        assert len(rows) == 1
        assert rows[0].freq_low == 110.0  # closed-band touch survives

    def test_guard_band_gap_window_is_filtered(self, client, make_session):
        # Window [111,112] is 1 MHz away (within a 2 MHz guard band) but a
        # window hit requires an actual closed-band intersection; the gap
        # must not widen the window candidate set.
        self._confirm_window(
            client,
            freq=(111.0, 112.0), circle=(0, 0, 10),
            start="2030-03-01T08:00:00Z", end="2030-03-01T10:00:00Z",
            reason="gap",
        )
        with make_session() as session:
            assert load_candidate_windows(
                session, _window_hit_envelope(_candidate())
            ) == []
            # The window is still active; only adjudication narrows it.
            assert load_active_window_ids(session) == [1]


class TestAvailableRangesEnvelope:
    def test_envelope_covers_whole_swept_lower_bound_interval(
        self, client, make_session
    ):
        # Candidate width 5; swept lower bounds [90,130] -> bands span up
        # to [130,135].  License [134,136] only overlaps at the high end
        # and must survive narrowing.
        _approve(client, **{**CANDIDATE_KWARGS, "freq": (134.0, 136.0)})
        candidate = License(
            999, Circle(0, 0, 10), 0.0, 5.0,
            _dt("2030-03-01T08:00:00Z"), _dt("2030-03-01T10:00:00Z"),
        )
        envelope = _available_ranges_envelope(candidate, 0.0, 90.0, 130.0)
        assert envelope.f_low == 90.0
        assert envelope.f_high == 135.0
        with make_session() as session:
            got = load_candidate_effective_licenses(session, envelope)
        assert len(got) == 1

    def test_api_available_ranges_drops_no_boundary_grant(self, client):
        _many_disjoint_apps(client, 10)
        _approve(
            client,
            name="Span",
            **{**CANDIDATE_KWARGS, "freq": (120.0, 130.0)},
        )
        candidate_id = _submit(
            client, name="Tune",
            freq=(0.0, 5.0), circle=(0, 0, 10),
            start="2030-03-01T08:00:00Z", end="2030-03-01T10:00:00Z",
        )
        body = client.post(
            f"/api/applications/{candidate_id}/available-ranges",
            json={"candidate_low_min": 90.0, "candidate_low_max": 140.0},
        ).json()
        # The close license shapes the result; far licenses contribute
        # nothing.  The first available range begins at the query low.
        mins = [r["min"] for r in body["available_ranges"]]
        assert 90.0 in mins


class TestProposedWindowEnvelope:
    def test_window_preview_drops_no_hit_and_skips_far(
        self, client, make_session
    ):
        close = _approve(
            client,
            name="Close",
            **{**CANDIDATE_KWARGS, "freq": (108.0, 109.0)},
        )
        _many_disjoint_apps(client, 10)
        window = RestrictionWindow(
            1, Circle(0, 0, 10), 105.0, 110.0,
            _dt("2030-03-01T09:00:00Z"), _dt("2030-03-01T09:30:00Z"),
        )
        envelope = _proposed_window_envelope(window)
        with make_session() as session:
            rows = (
                _apply_application_filters(
                    session.query(Application), envelope, status="approved"
                )
                .order_by(Application.id)
                .all()
            )
        assert [r.id for r in rows] == [close]


class TestGuardBandChangeStillChecksAllPairs:
    def test_preview_and_confirm_scan_the_whole_effective_set(self, client):
        # A guard-band change must check EVERY pair of currently effective
        # licenses, including the many mutually time-disjoint grants, not
        # a narrowed subset.  Two far grants on the same day sit exactly
        # 2 MHz apart (allowed at band 2, conflicting at a proposed 3).
        far_ids = _many_disjoint_apps(client, 10)
        conflicting = _approve(
            client,
            name="Far clash",
            freq=(5003.0, 5004.0),
            circle=(5000.0, 5000.0, 1.0),
            start="2030-02-10T00:00:00Z",
            end="2030-02-10T01:00:00Z",
        )
        preview = client.post(
            "/api/config/guard-band/preview", json={"guard_band": 3.0}
        ).json()
        pair_ids = {
            frozenset((p["first"]["id"], p["second"]["id"]))
            for p in preview["interfering_pairs"]
        }
        assert frozenset((far_ids[0], conflicting)) in pair_ids
        # Confirming is refused on the same all-pairs check, revision held.
        revision_before = _current_revision(client)
        resp = client.post(
            "/api/config/guard-band",
            json={"guard_band": 3.0, "expected_revision": revision_before},
        )
        assert resp.status_code == 409
        assert _current_revision(client) == revision_before


class TestWaitlistDrainNarrowing:
    def test_drain_with_many_far_licenses_only_approves_clear_entry(
        self, client
    ):
        # Many time-disjoint effective licenses must not block (or be
        # loaded one-by-one into) the standby drain; an entry clear of
        # them auto-approves once a license is released.
        _many_disjoint_apps(client, 12)
        # One close effective license (same time/space/band as the queued
        # entry) that keeps the standby blocked until it is revoked.
        close = _approve(client, name="Gate", **CANDIDATE_KWARGS)
        standby_id = _submit(client, name="Standby", **CANDIDATE_KWARGS)
        reg = client.post(
            f"/api/applications/{standby_id}/waitlist",
            json={"reason": "busy"},
        ).json()
        assert reg["queue_position"] == 1
        gate_revision = _current_revision(client)
        body = client.post(
            f"/api/applications/{close}/revoke",
            json={"expected_revision": gate_revision, "reason": "release"},
        ).json()
        assert body["auto_approved_application_ids"] == [standby_id]

    def test_drain_conflict_detail_order_is_preserved(self, client):
        # Two close blockers, each overlapping the standby's circle but
        # disjoint from each other spatially (so both are effective), both
        # on the standby's band.  The pending detail must list them in
        # application-id order; far licenses never enter the candidate set.
        blocker_a = _approve(
            client, name="A",
            freq=(100.0, 110.0), circle=(-15.0, 0.0, 10.0),
            start="2030-03-01T08:00:00Z", end="2030-03-01T10:00:00Z",
        )
        blocker_b = _approve(
            client, name="B",
            freq=(100.0, 110.0), circle=(15.0, 0.0, 10.0),
            start="2030-03-01T08:00:00Z", end="2030-03-01T10:00:00Z",
        )
        _many_disjoint_apps(client, 12)
        standby_id = _submit(client, name="Standby", **CANDIDATE_KWARGS)
        client.post(
            f"/api/applications/{standby_id}/waitlist", json={"reason": "x"}
        )
        # Suspend a far license to trigger the FIFO drain; the standby
        # stays blocked by both spatial blockers.
        gate = _approve(
            client, name="Gate",
            freq=(6000.0, 6001.0), circle=(7000.0, 7000.0, 1.0),
            start="2030-04-01T00:00:00Z", end="2030-04-01T01:00:00Z",
        )
        body = client.post(
            f"/api/applications/{gate}/suspend",
            json={
                "expected_revision": _current_revision(client),
                "reason": "x",
            },
        ).json()
        pending = {d["application_id"]: d for d in body["waitlist_pending"]}
        assert set(pending) == {standby_id}
        conflict_ids = [
            c["application"]["id"]
            for c in pending[standby_id]["conflicts"]
        ]
        assert conflict_ids == [blocker_a, blocker_b]
