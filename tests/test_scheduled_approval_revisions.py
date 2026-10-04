"""Revision-pinned visibility of scheduled auto-approval appointments.

The revision snapshot (``GET /api/revision/{n}``) replays the immutable
``scheduled_approval_events`` stream instead of reading the mutable
appointment row, so the audit view of a past revision never leaks later
decisions:

* an appointment booked later is absent from earlier revisions (booking
  pins at the current revision of the booking call and consumes none);
* a due approval is visible only from the revision it consumed, and an
  invalidation only from the revision of the approval that decided the
  application through another path (manual single/batch, waitlist auto,
  license transfer, emergency occupation) -- older revisions still show
  the appointment waiting to fire and never leak its approval revision;
* booking/rescheduling/cancellation consume no revision, so they present
  their current state within the revision they ran in.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from factories import make_payload


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


NOW = datetime.now(timezone.utc)
WINDOW_START = NOW + timedelta(days=2)
WINDOW_END = NOW + timedelta(days=3)

SNAPSHOT_KEYS = {
    "id",
    "application_id",
    "scheduled_at",
    "status",
    "reason",
    "processed_at",
    "approval_revision",
    "processed_guard_band",
    "blocked_details",
    "outcome_reason",
    "created_at",
    "updated_at",
}


def submit(client, low, high, name="Event", **kw):
    payload = make_payload(
        freq=(low, high),
        name=name,
        start=iso(WINDOW_START),
        end=iso(WINDOW_END),
        **kw,
    )
    r = client.post("/api/applications", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def approve(client, app_id, expected):
    r = client.post(
        f"/api/applications/{app_id}/approve",
        json={"expected_revision": expected},
    )
    assert r.status_code == 200, r.text
    return r.json()


def book(client, app_id, when, reason="booked"):
    r = client.post(
        f"/api/applications/{app_id}/scheduled-approvals",
        json={"scheduled_at": iso(when), "reason": reason},
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


def book_due(client, app_id, reason="due"):
    """Book an appointment a short moment ahead, then wait it over."""
    when = datetime.now(timezone.utc) + timedelta(milliseconds=120)
    body = book(client, app_id, when, reason=reason)
    time.sleep(0.3)
    return body["scheduled_approval_id"]


def process_due(client):
    r = client.post("/api/scheduled-approvals/process-due")
    assert r.status_code == 200, r.text
    return r.json()


def snapshot(client, revision):
    r = client.get(f"/api/revision/{revision}")
    assert r.status_code == 200, r.text
    return r.json()


def appointments_at(client, revision):
    return snapshot(client, revision)["scheduled_approvals"]


def only_appointment_at(client, revision):
    rows = appointments_at(client, revision)
    assert len(rows) == 1
    return rows[0]


class TestBookingVisibility:
    def test_appointment_booked_later_is_absent_from_earlier_revisions(
        self, client
    ):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # revision 1
        b = submit(client, 200, 210, "B")
        booking = book(client, b, NOW + timedelta(days=1))
        assert booking["current_revision"] == 1

        # The appointment did not exist at revision 0.
        assert appointments_at(client, 0) == []
        row = only_appointment_at(client, 1)
        assert row["id"] == booking["scheduled_approval_id"]
        assert row["application_id"] == b
        assert row["status"] == "scheduled"
        assert row["approval_revision"] is None
        assert row["processed_at"] is None
        assert row["outcome_reason"] is None

    def test_snapshot_rows_keep_their_field_shape(self, client):
        a = submit(client, 100, 110, "A")
        book(client, a, NOW + timedelta(days=1))
        row = only_appointment_at(client, 0)
        assert set(row) == SNAPSHOT_KEYS


class TestOutcomeVisibilityFromDecisionRevision:
    def test_manual_approval_invalidation_not_visible_before_its_revision(
        self, client
    ):
        # The reported audit time-travel: booked at revision 0, manually
        # approved at revision 1 -> revision 0 must still show the
        # appointment waiting to fire, not "invalidated".
        b = submit(client, 100, 110, "B")
        book(client, b, NOW + timedelta(days=1))
        approve(client, b, 0)  # revision 1

        row0 = only_appointment_at(client, 0)
        assert row0["status"] == "scheduled"
        assert row0["outcome_reason"] is None
        assert row0["approval_revision"] is None

        row1 = only_appointment_at(client, 1)
        assert row1["status"] == "invalidated"
        assert row1["outcome_reason"] == "manual_approval"
        # An invalidation is not an approval: no approval revision appears.
        assert row1["approval_revision"] is None

    def test_due_approval_visible_from_the_consumed_revision_only(self, client):
        a = submit(client, 100, 110, "A")
        booking_id = book_due(client, a)
        summary = process_due(client)
        assert summary["final_revision"] == 1

        row0 = only_appointment_at(client, 0)
        assert row0["id"] == booking_id
        assert row0["status"] == "scheduled"
        # The approval revision must not leak into the earlier revision.
        assert row0["approval_revision"] is None
        assert row0["processed_at"] is None
        assert row0["processed_guard_band"] is None

        row1 = only_appointment_at(client, 1)
        assert row1["status"] == "approved"
        assert row1["approval_revision"] == 1
        assert row1["processed_guard_band"] == 2.0
        assert row1["processed_at"] is not None

    def test_batch_approval_invalidates_each_member_at_its_own_revision(
        self, client
    ):
        a = submit(client, 100, 110, "A")
        b = submit(client, 200, 210, "B")
        id_a = book(client, a, NOW + timedelta(days=1))["scheduled_approval_id"]
        id_b = book(client, b, NOW + timedelta(days=1))["scheduled_approval_id"]
        r = client.post(
            "/api/applications/batch-approve",
            json={"expected_revision": 0, "application_ids": [a, b]},
        )
        assert r.status_code == 200, r.text  # revisions 1 and 2

        rows0 = {row["id"]: row for row in appointments_at(client, 0)}
        assert rows0[id_a]["status"] == "scheduled"
        assert rows0[id_b]["status"] == "scheduled"

        # A was approved at revision 1, B at revision 2: at revision 1 only
        # A's appointment is invalidated; B's is still waiting.
        rows1 = {row["id"]: row for row in appointments_at(client, 1)}
        assert rows1[id_a]["status"] == "invalidated"
        assert rows1[id_a]["outcome_reason"] == "batch_approval"
        assert rows1[id_b]["status"] == "scheduled"

        rows2 = {row["id"]: row for row in appointments_at(client, 2)}
        assert rows2[id_a]["status"] == "invalidated"
        assert rows2[id_b]["status"] == "invalidated"
        assert rows2[id_b]["outcome_reason"] == "batch_approval"

    def test_waitlist_auto_approval_invalidation_pins_at_auto_revision(
        self, client
    ):
        lic = submit(client, 100, 110, "lic")
        approve(client, lic, 0)  # revision 1
        b = submit(client, 105, 108, "B")
        client.post(f"/api/applications/{b}/waitlist")
        book(client, b, NOW + timedelta(days=1))
        # Revoking the incumbent (revision 2) drains the queue and
        # auto-approves B at revision 3.
        r = client.post(f"/api/applications/{lic}/revoke", json={"reason": "x"})
        assert r.status_code == 200, r.text
        assert r.json()["auto_approved_application_ids"] == [b]
        assert r.json()["final_revision"] == 3

        for rev in (1, 2):
            row = only_appointment_at(client, rev)
            assert row["status"] == "scheduled"
            assert row["outcome_reason"] is None
        row3 = only_appointment_at(client, 3)
        assert row3["status"] == "invalidated"
        assert row3["outcome_reason"] == "waitlist_auto_approval"

    def test_license_transfer_invalidation_pins_at_successor_revision(
        self, client
    ):
        source = submit(client, 100, 110, "source")
        approve(client, source, 0)  # revision 1
        successor = submit(client, 100, 110, "successor")
        book(client, successor, NOW + timedelta(days=1))
        r = client.post(
            "/api/license-transfers",
            json={
                "source_application_id": source,
                "successor_application_id": successor,
                "expected_revision": 1,
                "reason": "swap",
            },
        )
        assert r.status_code == 200, r.text  # revisions 2 (revoke) and 3

        for rev in (1, 2):
            row = only_appointment_at(client, rev)
            assert row["status"] == "scheduled"
        row3 = only_appointment_at(client, 3)
        assert row3["status"] == "invalidated"
        assert row3["outcome_reason"] == "license_transfer"

    def test_emergency_occupation_invalidation_pins_at_approval_revision(
        self, client
    ):
        e = submit(client, 100, 110, "emergency")
        book(client, e, NOW + timedelta(days=1))
        r = client.post(
            "/api/emergency-occupations",
            json={"application_id": e, "expected_revision": 0, "reason": "burst"},
        )
        assert r.status_code == 201, r.text  # revision 1

        row0 = only_appointment_at(client, 0)
        assert row0["status"] == "scheduled"
        row1 = only_appointment_at(client, 1)
        assert row1["status"] == "invalidated"
        assert row1["outcome_reason"] == "emergency_occupation"

    def test_blocked_visible_from_decision_revision_without_consuming_one(
        self, client
    ):
        b = submit(client, 105, 108, "B")
        booking_id = book_due(client, b)
        lic = submit(client, 100, 110, "lic")
        approve(client, lic, 0)  # revision 1
        summary = process_due(client)
        assert summary["blocked_application_ids"] == [b]
        assert summary["final_revision"] == 1  # a block consumes no revision

        row0 = only_appointment_at(client, 0)
        assert row0["id"] == booking_id
        assert row0["status"] == "scheduled"
        assert row0["blocked_details"] is None

        row1 = only_appointment_at(client, 1)
        assert row1["status"] == "blocked"
        assert row1["approval_revision"] is None
        assert row1["processed_at"] is not None
        assert row1["processed_guard_band"] == 2.0
        assert row1["blocked_details"]["conflicts"][0]["application"]["id"] == lic


class TestNoRevisionOperationsPresentCurrentState:
    def test_cancel_within_the_same_revision_shows_cancelled(self, client):
        a = submit(client, 100, 110, "A")
        book(client, a, NOW + timedelta(days=1))
        r = client.post(
            f"/api/applications/{a}/scheduled-approvals/cancel",
            json={"reason": "never mind"},
        )
        assert r.status_code == 200, r.text
        # No revision was consumed; the same revision already presents the
        # cancellation.
        assert client.get("/api/revision/current").json()["current_revision"] == 0
        row = only_appointment_at(client, 0)
        assert row["status"] == "cancelled"
        assert row["outcome_reason"] == "never mind"

    def test_cancel_pins_at_the_current_revision_of_the_call(self, client):
        a = submit(client, 100, 110, "A")
        book(client, a, NOW + timedelta(days=1))
        other = submit(client, 200, 210, "other")
        approve(client, other, 0)  # revision 1
        client.post(f"/api/applications/{a}/scheduled-approvals/cancel")

        row0 = only_appointment_at(client, 0)
        assert row0["status"] == "scheduled"
        row1 = only_appointment_at(client, 1)
        assert row1["status"] == "cancelled"

    def test_reschedule_within_the_same_revision_shows_the_new_instant(
        self, client
    ):
        a = submit(client, 100, 110, "A")
        book(client, a, NOW + timedelta(hours=1))
        moved = NOW + timedelta(hours=2)
        r = client.post(
            f"/api/applications/{a}/scheduled-approvals/reschedule",
            json={"scheduled_at": iso(moved)},
        )
        assert r.status_code == 200, r.text
        row = only_appointment_at(client, 0)
        assert row["status"] == "scheduled"
        assert row["scheduled_at"] == iso(moved)

    def test_reschedule_pins_at_the_current_revision_of_the_call(self, client):
        a = submit(client, 100, 110, "A")
        first = NOW + timedelta(hours=1)
        book(client, a, first)
        other = submit(client, 200, 210, "other")
        approve(client, other, 0)  # revision 1
        moved = NOW + timedelta(hours=2)
        r = client.post(
            f"/api/applications/{a}/scheduled-approvals/reschedule",
            json={"scheduled_at": iso(moved)},
        )
        assert r.status_code == 200, r.text

        row0 = only_appointment_at(client, 0)
        assert row0["scheduled_at"] == iso(first)
        row1 = only_appointment_at(client, 1)
        assert row1["scheduled_at"] == iso(moved)

    def test_rebooking_after_cancel_keeps_both_audit_rows(self, client):
        a = submit(client, 100, 110, "A")
        first = book(client, a, NOW + timedelta(hours=1))
        client.post(f"/api/applications/{a}/scheduled-approvals/cancel")
        second = book(client, a, NOW + timedelta(hours=2))
        assert second["scheduled_approval_id"] != first["scheduled_approval_id"]

        rows = {row["id"]: row for row in appointments_at(client, 0)}
        assert rows[first["scheduled_approval_id"]]["status"] == "cancelled"
        assert rows[second["scheduled_approval_id"]]["status"] == "scheduled"


class TestCompatibility:
    def test_future_revision_still_404_and_negative_422(self, client):
        a = submit(client, 100, 110, "A")
        book(client, a, NOW + timedelta(days=1))
        r = client.get("/api/revision/999")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "revision_out_of_range"
        r = client.get("/api/revision/-1")
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "revision_out_of_range"

    def test_current_revision_lists_only_waiting_appointments(self, client):
        a = submit(client, 100, 110, "A")
        booking = book(client, a, NOW + timedelta(days=1))
        body = client.get("/api/revision/current").json()
        assert body["scheduled_approval_ids"] == [
            booking["scheduled_approval_id"]
        ]
        assert body["scheduled_approvals"][0]["status"] == "scheduled"

    def test_live_appointment_queries_unaffected_by_history_fix(self, client):
        b = submit(client, 100, 110, "B")
        booking = book(client, b, NOW + timedelta(days=1))
        approve(client, b, 0)  # revision 1 invalidates the appointment
        live = client.get(
            f"/api/scheduled-approvals/{booking['scheduled_approval_id']}"
        ).json()
        assert live["status"] == "invalidated"
        assert live["outcome_reason"] == "manual_approval"
        listing = client.get(
            "/api/scheduled-approvals", params={"status": "invalidated"}
        ).json()
        assert listing["count"] == 1


class TestLegacyRowBackfill:
    def test_appointments_predating_the_event_stream_are_backfilled(
        self, client
    ):
        # Simulate a database written before the lifecycle event stream
        # existed: an approved appointment row (and its approval event) with
        # no scheduled_approval_events rows at all.
        from app.db import SessionLocal, _ensure_scheduled_approval_events
        from app.models import (
            Application,
            ApprovalEvent,
            RevisionMeta,
            ScheduledApproval,
        )

        a = submit(client, 100, 110, "A")
        with SessionLocal() as s, s.begin():
            meta = s.get(RevisionMeta, 1)
            meta.revision = 1
            s.add(
                ApprovalEvent(
                    revision=1, application_id=a, guard_band=2.0, scheduled=True
                )
            )
            s.get(Application, a).status = "approved"
            legacy = ScheduledApproval(
                application_id=a,
                scheduled_at=NOW + timedelta(hours=1),
                status="approved",
                processed_at=NOW,
                approval_revision=1,
                processed_guard_band=2.0,
            )
            s.add(legacy)
            s.flush()
            booking_id = legacy.id

        # The startup migration seeds the missing lifecycle events once.
        _ensure_scheduled_approval_events()
        row0 = only_appointment_at(client, 0)
        assert row0["id"] == booking_id
        assert row0["status"] == "scheduled"
        assert row0["approval_revision"] is None
        row1 = only_appointment_at(client, 1)
        assert row1["status"] == "approved"
        assert row1["approval_revision"] == 1

        # Idempotent: a second run changes nothing.
        _ensure_scheduled_approval_events()
        assert only_appointment_at(client, 1)["status"] == "approved"
