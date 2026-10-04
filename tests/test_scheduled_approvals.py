"""End-to-end tests for operator-booked scheduled auto-approvals.

A booking names a UTC instant strictly after now and strictly before the
application's own window end; booking consumes no revision and at most one
waiting appointment exists per application.  Due processing (exercised
deterministically through the administration trigger, the same code the
background sweeper and the startup catch-up use) re-adjudicates inside the
shared revision arbitration: a pass consumes one revision as an ordinary
approval event flagged ``scheduled``; a clash/window hit records blockers,
keeps the application pending and consumes no revision.  Decisions through
any other path invalidate the waiting appointment.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from factories import make_payload


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# Times are derived from the real clock so the "strictly in the future" rule
# holds whenever the suite runs; the application window sits 2-3 days ahead.
NOW = datetime.now(timezone.utc)
WINDOW_START = NOW + timedelta(days=2)
WINDOW_END = NOW + timedelta(days=3)
FUTURE = NOW + timedelta(hours=1)


def submit(client, low, high, name="Event", *, start=None, end=None, **kw):
    payload = make_payload(
        freq=(low, high),
        name=name,
        start=iso(start or WINDOW_START),
        end=iso(end or WINDOW_END),
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


def book(client, app_id, when, reason="booked", expect_status=201):
    body = {"scheduled_at": iso(when), "reason": reason}
    r = client.post(
        f"/api/applications/{app_id}/scheduled-approvals", json=body
    )
    assert r.status_code == expect_status, r.text
    return r


def reschedule(client, app_id, when, reason="moved"):
    r = client.post(
        f"/api/applications/{app_id}/scheduled-approvals/reschedule",
        json={"scheduled_at": iso(when), "reason": reason},
    )
    assert r.status_code == 200, r.text
    return r.json()


def cancel(client, app_id, reason="never mind"):
    r = client.post(
        f"/api/applications/{app_id}/scheduled-approvals/cancel",
        json={"reason": reason},
    )
    assert r.status_code == 200, r.text
    return r.json()


def process_due(client):
    r = client.post("/api/scheduled-approvals/process-due")
    assert r.status_code == 200, r.text
    return r.json()


def book_due(client, app_id, reason="due"):
    """Book an appointment a short moment in the future, then wait it over."""
    when = datetime.now(timezone.utc) + timedelta(milliseconds=120)
    resp = book(client, app_id, when, reason=reason)
    time.sleep(0.3)
    return resp.json()["scheduled_approval_id"]


def book_at(client, app_id, milliseconds_ahead, reason="due"):
    """Book at a near-future offset in milliseconds (no waiting)."""
    when = datetime.now(timezone.utc) + timedelta(milliseconds=milliseconds_ahead)
    return book(client, app_id, when, reason=reason).json()["scheduled_approval_id"]


def get_booking(client, booking_id):
    r = client.get(f"/api/scheduled-approvals/{booking_id}")
    assert r.status_code == 200, r.text
    return r.json()


class TestBooking:
    def test_book_pending_returns_id_status_and_consumes_no_revision(self, client):
        a = submit(client, 100, 110, "A")
        due = NOW + timedelta(hours=1)
        body = book(client, a, due).json()
        assert body["action"] == "booked"
        assert body["status"] == "scheduled"
        assert body["scheduled_approval_id"] >= 1
        assert body["application_id"] == a
        assert body["scheduled_at"] == iso(due)
        assert body["current_revision"] == 0
        # Application untouched, revision untouched.
        assert client.get(f"/api/applications/{a}").json()["status"] == "pending"
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 0
        )

    def test_booking_in_the_past_rejected_without_appointment(self, client):
        a = submit(client, 100, 110, "A")
        r = book(client, a, NOW - timedelta(seconds=1), expect_status=422)
        code = r.json()["error"]
        assert code["code"] == "scheduled_time_invalid"
        fields = {d["field"]: d["code"] for d in code["details"]}
        assert fields["scheduled_at"] == "scheduled_at_not_in_future"
        listing = client.get("/api/scheduled-approvals").json()
        assert listing["count"] == 0

    def test_booking_equal_to_now_rejected(self, client):
        a = submit(client, 100, 110, "A")
        r = book(client, a, NOW, expect_status=422)
        assert r.json()["error"]["code"] == "scheduled_time_invalid"

    def test_booking_at_or_after_window_end_rejected(self, client):
        a = submit(client, 100, 110, "A")
        r = book(client, a, WINDOW_END, expect_status=422)
        code = r.json()["error"]
        assert code["code"] == "scheduled_time_invalid"
        assert any(
            d["code"] == "scheduled_at_not_before_window_end"
            for d in code["details"]
        )
        # Just past the end also rejected.
        r = book(client, a, WINDOW_END + timedelta(minutes=1), expect_status=422)
        assert r.json()["error"]["code"] == "scheduled_time_invalid"

    def test_booking_naive_instant_rejected(self, client):
        a = submit(client, 100, 110, "A")
        naive = (NOW + timedelta(hours=1)).replace(tzinfo=None).isoformat()
        r = client.post(
            f"/api/applications/{a}/scheduled-approvals",
            json={"scheduled_at": naive},
        )
        # A naive datetime passes Pydantic but is rejected whole-request by
        # the semantic time validation, with no appointment left behind.
        assert r.status_code == 422
        code = r.json()["error"]
        assert code["code"] == "scheduled_time_invalid"
        assert any(
            d["code"] == "naive_datetime" for d in code["details"]
        )
        assert client.get("/api/scheduled-approvals").json()["count"] == 0

    def test_unknown_application_404(self, client):
        r = client.post(
            "/api/applications/999999/scheduled-approvals",
            json={"scheduled_at": iso(NOW + timedelta(hours=1))},
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"

    def test_non_pending_application_rejected(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        r = book(client, a, NOW + timedelta(hours=1), expect_status=409)
        assert r.json()["error"]["code"] == "not_pending"
        assert client.get("/api/scheduled-approvals").json()["count"] == 0

    def test_only_one_waiting_appointment_per_application_rebooking_reschedules(
        self, client
    ):
        a = submit(client, 100, 110, "A")
        first = book(client, a, NOW + timedelta(hours=1)).json()
        # Second booking on the same application reschedules, no new row.
        second = book(
            client, a, NOW + timedelta(hours=2), reason="later", expect_status=200
        )
        assert second.status_code == 200
        body = second.json()
        assert body["action"] == "rescheduled"
        assert body["scheduled_approval_id"] == first["scheduled_approval_id"]
        assert body["scheduled_at"] == iso(NOW + timedelta(hours=2))
        listing = client.get(f"/api/applications/{a}/scheduled-approvals").json()
        assert listing["count"] == 1
        assert listing["scheduled_approvals"][0]["status"] == "scheduled"

    def test_reschedule_validates_the_new_instant(self, client):
        a = submit(client, 100, 110, "A")
        original = book(client, a, NOW + timedelta(hours=1)).json()
        # Invalid instant keeps the existing appointment untouched.
        r = client.post(
            f"/api/applications/{a}/scheduled-approvals/reschedule",
            json={"scheduled_at": iso(WINDOW_END + timedelta(hours=1))},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "scheduled_time_invalid"
        current = get_booking(client, original["scheduled_approval_id"])
        assert current["scheduled_at"] == iso(NOW + timedelta(hours=1))

    def test_reschedule_unknown_application_404(self, client):
        r = client.post(
            "/api/applications/999999/scheduled-approvals/reschedule",
            json={"scheduled_at": iso(NOW + timedelta(hours=1))},
        )
        assert r.status_code == 404

    def test_reschedule_without_waiting_appointment_409(self, client):
        a = submit(client, 100, 110, "A")
        r = client.post(
            f"/api/applications/{a}/scheduled-approvals/reschedule",
            json={"scheduled_at": iso(NOW + timedelta(hours=1))},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "scheduled_approval_not_active"

    def test_cancel_removes_waiting_appointment_and_allows_rebooking(self, client):
        a = submit(client, 100, 110, "A")
        first = book(client, a, NOW + timedelta(hours=1)).json()
        body = cancel(client, a)
        assert body["status"] == "cancelled"
        assert body["outcome_reason"] == "never mind"
        assert get_booking(client, first["scheduled_approval_id"])["status"] == (
            "cancelled"
        )
        # A new booking after cancellation is allowed (new row).
        again = book(client, a, NOW + timedelta(hours=2)).json()
        assert again["scheduled_approval_id"] != first["scheduled_approval_id"]
        assert again["status"] == "scheduled"

    def test_cancel_without_waiting_appointment_409(self, client):
        a = submit(client, 100, 110, "A")
        r = client.post(f"/api/applications/{a}/scheduled-approvals/cancel")
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "scheduled_approval_not_active"

    def test_cancel_consumes_no_revision(self, client):
        a = submit(client, 100, 110, "A")
        book(client, a, NOW + timedelta(hours=1))
        cancel(client, a)
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 0
        )


class TestDueProcessingApproved:
    def test_due_free_application_approved_on_one_revision(self, client):
        a = submit(client, 100, 110, "A")
        booking_id = book_due(client, a)
        summary = process_due(client)
        assert summary["processed_count"] == 1
        assert summary["approved_application_ids"] == [a]
        assert summary["blocked_application_ids"] == []
        assert summary["from_revision"] == 0
        assert summary["final_revision"] == 1
        result = summary["results"][0]
        assert result["outcome"] == "approved"
        assert result["revision"] == 1
        assert "processed_at" in result

        booking = get_booking(client, booking_id)
        assert booking["status"] == "approved"
        assert booking["approval_revision"] == 1
        assert booking["processed_guard_band"] == 2.0
        assert booking["processed_at"]
        app = client.get(f"/api/applications/{a}").json()
        assert app["status"] == "approved"
        assert app["approval_revision"] == 1

    def test_approved_event_flagged_scheduled_in_history(self, client):
        a = submit(client, 100, 110, "A")
        book_due(client, a)
        process_due(client)
        snapshot = client.get("/api/revision/1").json()
        approval = snapshot["approvals"][0]
        assert approval["revision"] == 1
        assert approval["scheduled"] is True
        # A scheduled approval is NOT a waitlist drain approval.
        assert approval["auto_approved"] is False
        assert approval["release_revision"] is None
        # The snapshot audit lists the appointment too.
        records = snapshot["scheduled_approvals"]
        assert records[0]["status"] == "approved"
        assert records[0]["approval_revision"] == 1

    def test_not_yet_due_appointment_is_not_processed(self, client):
        a = submit(client, 100, 110, "A")
        booking_id = book(client, a, NOW + timedelta(days=1)).json()[
            "scheduled_approval_id"
        ]
        summary = process_due(client)
        assert summary["processed_count"] == 0
        assert get_booking(client, booking_id)["status"] == "scheduled"
        assert client.get(f"/api/applications/{a}").json()["status"] == "pending"
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 0
        )

    def test_processing_is_idempotent_no_double_approval(self, client):
        a = submit(client, 100, 110, "A")
        book_due(client, a)
        first = process_due(client)
        second = process_due(client)
        assert first["final_revision"] == 1
        assert second["processed_count"] == 0
        assert second["final_revision"] == 1
        approvals = client.get("/api/revision/1").json()["approvals"]
        assert len(approvals) == 1

    def test_due_appointments_processed_in_booking_order(self, client):
        # Two free applications on disjoint bands; same circle/window but
        # far apart frequencies pass independently.
        a = submit(client, 100, 110, "A")
        b = submit(client, 200, 210, "B")
        book_at(client, b, 250)
        book_at(client, a, 100)
        time.sleep(0.5)
        summary = process_due(client)
        assert summary["approved_application_ids"] == [a, b]
        assert summary["final_revision"] == 2
        assert summary["results"][0]["revision"] == 1
        assert summary["results"][1]["revision"] == 2

    def test_later_due_appointment_re_adjudicated_against_earlier_approval(
        self, client
    ):
        # Both free at booking time but the two applications interfere, so
        # whichever fires first joins the effective set and blocks the other.
        a = submit(client, 100, 110, "A")
        b = submit(client, 105, 108, "B")
        book_at(client, a, 100)
        book_at(client, b, 250)
        time.sleep(0.5)
        summary = process_due(client)
        assert summary["approved_application_ids"] == [a]
        assert summary["blocked_application_ids"] == [b]
        assert summary["final_revision"] == 1  # blocked consumes no revision
        # The blocked application stays pending and is not retried by another
        # sweep.
        again = process_due(client)
        assert again["processed_count"] == 0
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"

    def test_waitlisted_application_leaves_queue_when_scheduled_passes(
        self, client
    ):
        # C is queued on a band that is currently free (registration does not
        # require a conflict).  Its due scheduled approval must pass and take
        # it out of the waitlist.
        c = submit(client, 300, 310, "C")
        client.post(f"/api/applications/{c}/waitlist")
        assert client.get("/api/waitlist").json()["waitlist_application_ids"] == [
            c
        ]
        c_booking = book_due(client, c)
        process_due(client)
        assert client.get(f"/api/applications/{c}").json()["status"] == "approved"
        ids = client.get("/api/waitlist").json()["waitlist_application_ids"]
        assert c not in ids
        result = get_booking(client, c_booking)
        assert result["status"] == "approved"

    def test_scheduled_approval_does_not_trigger_waitlist_drain(self, client):
        # A occupies the band; B is queued (blocked).  C has a separate free
        # band with a due appointment.  When C's appointment approves, B must
        # stay queued -- scheduled approvals never re-adjudicate the waitlist.
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        b = submit(client, 105, 108, "B")
        client.post(f"/api/applications/{b}/waitlist")
        c = submit(client, 200, 210, "C")
        book_due(client, c)
        process_due(client)
        ids = client.get("/api/waitlist").json()["waitlist_application_ids"]
        assert ids == [b]
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"


class TestDueProcessingBlocked:
    def test_interference_at_due_time_records_blockers_no_revision(self, client):
        lic = submit(client, 100, 110, "lic")
        approve(client, lic, 0)  # revision 1
        b = submit(client, 105, 108, "B")
        booking_id = book_due(client, b)
        summary = process_due(client)
        assert summary["approved_application_ids"] == []
        assert summary["blocked_application_ids"] == [b]
        # The blocking sweep consumed no revision.
        assert summary["from_revision"] == 1
        assert summary["final_revision"] == 1
        blocked = summary["results"][0]
        assert blocked["outcome"] == "blocked"
        assert blocked["blocked_details"]["conflicts"][0]["application"]["id"] == (
            lic
        )

        booking = get_booking(client, booking_id)
        assert booking["status"] == "blocked"
        assert booking.get("approval_revision") is None
        assert booking["processed_at"]
        assert booking["processed_guard_band"] == 2.0
        assert booking["blocked_details"]["conflicts"][0]["reason"] in (
            "frequency_intersection",
            "within_guard_band",
        )
        # Still pending, no revision, no retry.
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"
        assert process_due(client)["processed_count"] == 0

    def test_restriction_window_hit_at_due_time_blocks(self, client):
        b = submit(client, 100, 110, "B")
        booking_id = book_at(client, b, 700)
        # Activate a window that fully covers the application.
        window = {
            "circle": {"cx": 0, "cy": 0, "radius": 10},
            "frequency": {"low": 100, "high": 110},
            "time_window": {
                "start": iso(WINDOW_START),
                "end": iso(WINDOW_END),
            },
            "reason": "control",
            "expected_revision": 0,
        }
        r = client.post("/api/restriction-windows", json=window)
        assert r.status_code == 201, r.text  # revision 1
        time.sleep(0.8)
        summary = process_due(client)
        assert summary["blocked_application_ids"] == [b]
        assert summary["final_revision"] == 1
        booking = get_booking(client, booking_id)
        assert booking["status"] == "blocked"
        hits = booking["blocked_details"]["restriction_window_conflicts"]
        assert len(hits) == 1
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"

    def test_blocked_appointment_cannot_be_cancelled_or_rescheduled(self, client):
        lic = submit(client, 100, 110, "lic")
        approve(client, lic, 0)
        b = submit(client, 105, 108, "B")
        book_due(client, b)
        process_due(client)  # blocks
        r = client.post(f"/api/applications/{b}/scheduled-approvals/cancel")
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "scheduled_approval_not_active"
        r = client.post(
            f"/api/applications/{b}/scheduled-approvals/reschedule",
            json={"scheduled_at": iso(NOW + timedelta(hours=2))},
        )
        assert r.status_code == 409
        # A new booking is possible on the still-pending application.
        again = book(client, b, NOW + timedelta(hours=2)).json()
        assert again["status"] == "scheduled"


class TestInvalidationByOtherDecisions:
    def test_manual_approval_invalidates_appointment(self, client):
        b = submit(client, 100, 110, "B")
        booking_id = book(client, b, NOW + timedelta(days=1)).json()[
            "scheduled_approval_id"
        ]
        approve(client, b, 0)  # revision 1
        booking = get_booking(client, booking_id)
        assert booking["status"] == "invalidated"
        assert booking["outcome_reason"] == "manual_approval"
        # Sweep does nothing, revision unchanged.
        assert process_due(client)["processed_count"] == 0
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 1
        )

    def test_batch_approval_invalidates_appointments(self, client):
        a = submit(client, 100, 110, "A")
        b = submit(client, 200, 210, "B")
        id_a = book(client, a, NOW + timedelta(days=1)).json()[
            "scheduled_approval_id"
        ]
        id_b = book(client, b, NOW + timedelta(days=1)).json()[
            "scheduled_approval_id"
        ]
        r = client.post(
            "/api/applications/batch-approve",
            json={"expected_revision": 0, "application_ids": [a, b]},
        )
        assert r.status_code == 200, r.text
        assert get_booking(client, id_a)["status"] == "invalidated"
        assert get_booking(client, id_b)["status"] == "invalidated"

    def test_waitlist_auto_approval_on_revoke_invalidates_appointment(
        self, client
    ):
        lic = submit(client, 100, 110, "lic")
        approve(client, lic, 0)
        b = submit(client, 105, 108, "B")
        client.post(f"/api/applications/{b}/waitlist")
        booking_id = book(client, b, NOW + timedelta(days=1)).json()[
            "scheduled_approval_id"
        ]
        # Revoking the incumbent drains the queue and auto-approves B.
        r = client.post(f"/api/applications/{lic}/revoke", json={"reason": "x"})
        assert r.status_code == 200, r.text
        assert b in r.json()["auto_approved_application_ids"]
        booking = get_booking(client, booking_id)
        assert booking["status"] == "invalidated"
        assert booking["outcome_reason"] == "waitlist_auto_approval"
        # The auto approval event is waitlist-flagged, not scheduled-flagged.
        auto = [
            ev
            for ev in client.get("/api/revision/current").json()["approvals"]
            if ev["application_id"] == b
        ]
        assert auto[0]["auto_approved"] is True
        assert auto[0]["scheduled"] is False
        assert process_due(client)["processed_count"] == 0

    def test_license_transfer_invalidates_successor_appointment(self, client):
        source = submit(client, 100, 110, "source")
        approve(client, source, 0)
        successor = submit(client, 100, 110, "successor")
        booking_id = book(client, successor, NOW + timedelta(days=1)).json()[
            "scheduled_approval_id"
        ]
        r = client.post(
            "/api/license-transfers",
            json={
                "source_application_id": source,
                "successor_application_id": successor,
                "expected_revision": 1,
                "reason": "swap",
            },
        )
        assert r.status_code == 200, r.text
        booking = get_booking(client, booking_id)
        assert booking["status"] == "invalidated"
        assert booking["outcome_reason"] == "license_transfer"

    def test_emergency_occupation_invalidates_appointment(self, client):
        e = submit(client, 100, 110, "emergency")
        booking_id = book(client, e, NOW + timedelta(days=1)).json()[
            "scheduled_approval_id"
        ]
        r = client.post(
            "/api/emergency-occupations",
            json={"application_id": e, "expected_revision": 0, "reason": "burst"},
        )
        assert r.status_code == 201, r.text
        booking = get_booking(client, booking_id)
        assert booking["status"] == "invalidated"
        assert booking["outcome_reason"] == "emergency_occupation"


class TestStartupCatchup:
    def test_due_appointment_while_process_down_processed_at_restart(
        self, client
    ):
        # Simulate an appointment that came due while no sweeper ran: insert
        # one already in the past directly through the ORM, then trigger the
        # exact startup catch-up call the lifespan performs.
        from app.db import SessionLocal
        from app.models import ScheduledApproval

        a = submit(client, 100, 110, "A")
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        with SessionLocal() as s, s.begin():
            s.add(
                ScheduledApproval(
                    application_id=a,
                    scheduled_at=past,
                    status="scheduled",
                )
            )
            booking_id = s.query(ScheduledApproval).one().id
        # No sweep happened yet; application still pending.
        assert client.get(f"/api/applications/{a}").json()["status"] == "pending"
        from app.main import run_scheduled_approval_sweep

        summary = run_scheduled_approval_sweep()
        assert summary["approved_application_ids"] == [a]
        assert summary["final_revision"] == 1
        assert get_booking(client, booking_id)["status"] == "approved"
        # A second catch-up (another restart) does not double-approve.
        again = run_scheduled_approval_sweep()
        assert again["processed_count"] == 0
        assert client.get("/api/revision/current").json()["current_revision"] == 1

    def test_blocked_at_restart_stays_pending_and_not_retried(self, client):
        from app.db import SessionLocal
        from app.models import ScheduledApproval

        lic = submit(client, 100, 110, "lic")
        approve(client, lic, 0)
        b = submit(client, 105, 108, "B")
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        with SessionLocal() as s, s.begin():
            s.add(ScheduledApproval(application_id=b, scheduled_at=past))
            booking_id = s.query(ScheduledApproval).one().id
        from app.main import run_scheduled_approval_sweep

        summary = run_scheduled_approval_sweep()
        assert summary["blocked_application_ids"] == [b]
        assert summary["final_revision"] == 1
        booking = get_booking(client, booking_id)
        assert booking["status"] == "blocked"
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"


class TestCompetition:
    def test_cancel_racing_due_processing_only_one_outcome_wins(self, client):
        # After the appointment fires, a late cancel fails (it is no longer
        # waiting); the approval stays.
        a = submit(client, 100, 110, "A")
        booking_id = book_due(client, a)
        process_due(client)
        assert get_booking(client, booking_id)["status"] == "approved"
        r = client.post(f"/api/applications/{a}/scheduled-approvals/cancel")
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "scheduled_approval_not_active"
        assert client.get(f"/api/applications/{a}").json()["status"] == "approved"

    def test_manual_approval_racing_due_processing_does_not_double_decide(
        self, client
    ):
        # The appointment is invalidated first by a manual approval; the later
        # sweep finds nothing to do and never grants a second approval.
        a = submit(client, 100, 110, "A")
        booking_id = book(client, a, NOW + timedelta(days=1)).json()[
            "scheduled_approval_id"
        ]
        approve(client, a, 0)
        assert get_booking(client, booking_id)["status"] == "invalidated"
        assert process_due(client)["processed_count"] == 0
        approvals = client.get("/api/revision/current").json()["approvals"]
        assert len(approvals) == 1

    def test_blocked_then_rebooked_and_freed_approves_on_new_appointment(
        self, client
    ):
        # First appointment blocks (incumbent present); the application stays
        # pending.  After the incumbent is revoked, a new appointment fires.
        lic = submit(client, 100, 110, "lic")
        approve(client, lic, 0)
        b = submit(client, 105, 108, "B")
        first = book_due(client, b)
        process_due(client)
        assert get_booking(client, first)["status"] == "blocked"
        # Revoke the incumbent: the application is not waitlisted, so nothing
        # auto-approves; revision 2 is the revoke.
        r = client.post(f"/api/applications/{lic}/revoke", json={"reason": "x"})
        assert r.status_code == 200, r.text
        assert r.json()["auto_approved_application_ids"] == []
        second = book_due(client, b)
        process_due(client)
        assert get_booking(client, second)["status"] == "approved"
        app = client.get(f"/api/applications/{b}").json()
        assert app["status"] == "approved"
        assert app["approval_revision"] == 3


class TestQueries:
    def test_list_and_filter_appointments(self, client):
        a = submit(client, 100, 110, "A")
        b = submit(client, 200, 210, "B")
        booking_a = book_due(client, a)
        book(client, b, NOW + timedelta(days=1))
        process_due(client)  # A approved, B waiting
        all_rows = client.get("/api/scheduled-approvals").json()
        assert all_rows["count"] == 2
        approved = client.get(
            "/api/scheduled-approvals", params={"status": "approved"}
        ).json()
        assert [r["id"] for r in approved["scheduled_approvals"]] == [booking_a]
        for_app = client.get(
            "/api/scheduled-approvals", params={"application_id": b}
        ).json()
        assert for_app["count"] == 1
        assert for_app["scheduled_approvals"][0]["status"] == "scheduled"

    def test_get_unknown_booking_404(self, client):
        r = client.get("/api/scheduled-approvals/4242")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "scheduled_approval_not_found"

    def test_application_listing_unknown_404(self, client):
        r = client.get("/api/applications/999999/scheduled-approvals")
        assert r.status_code == 404

    def test_invalid_status_filter_422(self, client):
        r = client.get("/api/scheduled-approvals", params={"status": "bogus"})
        assert r.status_code == 422

    def test_current_revision_lists_waiting_appointments(self, client):
        a = submit(client, 100, 110, "A")
        booking_id = book(client, a, NOW + timedelta(days=1)).json()[
            "scheduled_approval_id"
        ]
        body = client.get("/api/revision/current").json()
        assert body["scheduled_approval_ids"] == [booking_id]
        assert body["scheduled_approvals"][0]["application_id"] == a
        cancel(client, a)
        body = client.get("/api/revision/current").json()
        assert body["scheduled_approval_ids"] == []
