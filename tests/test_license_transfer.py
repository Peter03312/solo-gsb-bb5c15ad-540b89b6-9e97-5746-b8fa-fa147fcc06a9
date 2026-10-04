"""End-to-end tests for the active-device license handover feature.

A handover revokes one currently effective license and approves one pending
application atomically under the shared revision arbitration.  The read-only
preview reports the current revision, the interference after excluding the
source license and the hit restriction windows; the confirmation consumes
two consecutive revisions (revocation then successor approval), pulls a
queued successor out of the waitlist first and then re-adjudicates the
remaining standbys by the normal FIFO rules.  Any stale revision, status
change or conflict rejects the whole transaction without touching licenses,
the queue or the revision.

Guard band is the test default (2.0); every application here shares the
default coverage circle and UTC window, so only frequency bands decide
interference.
"""
from factories import make_payload


def submit(client, low, high, name="Event", **overrides):
    payload = make_payload(freq=(low, high), name=name, **overrides)
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


def register(client, app_id, reason=""):
    return client.post(
        f"/api/applications/{app_id}/waitlist", json={"reason": reason}
    )


def queue_ids(client):
    return client.get("/api/waitlist").json()["waitlist_application_ids"]


def transfer_preview(client, source, successor, reason="swap"):
    return client.post(
        "/api/license-transfers/preview",
        json={
            "source_application_id": source,
            "successor_application_id": successor,
            "reason": reason,
        },
    )


def transfer(client, source, successor, expected, reason="swap"):
    return client.post(
        "/api/license-transfers",
        json={
            "source_application_id": source,
            "successor_application_id": successor,
            "expected_revision": expected,
            "reason": reason,
        },
    )


def confirm_emergency(client, app_id, expected, reason="burst"):
    r = client.post(
        "/api/emergency-occupations",
        json={
            "application_id": app_id,
            "expected_revision": expected,
            "reason": reason,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def release_emergency(client, app_id, expected, reason="clear"):
    return client.post(
        f"/api/emergency-occupations/{app_id}/release",
        json={"expected_revision": expected, "reason": reason},
    )


class TestPreview:
    def test_preview_is_read_only_and_lists_current_revision(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # revision 1
        b = submit(client, 120, 130, "B")  # 10 MHz gap, free

        r = transfer_preview(client, a, b)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ready"] is True
        assert body["blockers"] == []
        assert body["current_revision"] == 1
        assert body["guard_band"] == 2.0
        assert body["source_application_id"] == a
        assert body["successor_application_id"] == b
        # The source is excluded from the set the successor is checked
        # against, so it is not listed as a conflict.
        assert body["effective_application_ids"] == []
        assert body["has_conflicts"] is False
        assert body["conflicts"] == []
        assert body["has_restriction_window_conflicts"] is False
        assert body["planned_revoc_revision"] == 2
        assert body["planned_successor_revision"] == 3

        # Nothing was mutated.
        assert client.get(f"/api/applications/{a}").json()["status"] == "approved"
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 1
        )

    def test_preview_excludes_source_but_reports_other_conflicts(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # revision 1
        # C is another effective license far from A but close to B.
        c = submit(client, 130, 140, "C")
        approve(client, c, 1)  # revision 2
        # B would be free after A leaves but is 1 MHz away from C (< 2 band).
        b = submit(client, 128, 129, "B")

        body = transfer_preview(client, a, b).json()
        assert body["ready"] is False
        assert body["effective_application_ids"] == [c]
        assert body["has_conflicts"] is True
        assert [x["application"]["id"] for x in body["conflicts"]] == [c]
        codes = [item["code"] for item in body["blockers"]]
        assert "interference" in codes

        # Still read-only.
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 2
        )

    def test_preview_without_excluding_source_shows_no_conflict(self, client):
        # B sits inside A's band: normally it conflicts, but handing A over
        # removes A from the effective set, so the handover is clear.
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        b = submit(client, 105, 108, "B")

        body = transfer_preview(client, a, b).json()
        assert body["ready"] is True
        assert body["conflicts"] == []

    def test_preview_reports_status_blockers(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        b = submit(client, 120, 130, "B")
        approve(client, b, 1)  # successor already approved

        body = transfer_preview(client, a, b).json()
        assert body["ready"] is False
        blocker = next(
            item
            for item in body["blockers"]
            if item["code"] == "successor_not_pending"
        )
        assert blocker["status"] == "approved"

        # A pending application cannot be a handover source.
        d = submit(client, 200, 210, "D")
        body = transfer_preview(client, d, d).json()
        # Same id is a hard 422 at the endpoint, so use distinct ids here.
        e = submit(client, 220, 230, "E")
        body = transfer_preview(client, d, e).json()
        assert any(
            item["code"] == "source_not_effective" for item in body["blockers"]
        )

    def test_preview_unknown_applications_are_blockers_not_errors(self, client):
        body = transfer_preview(client, 999, 1000).json()
        codes = {item["code"] for item in body["blockers"]}
        assert codes == {"not_found"}
        assert body["ready"] is False


class TestConfirmHappyPath:
    def test_handover_revokes_and_approves_on_consecutive_revisions(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # revision 1
        b = submit(client, 105, 108, "B")

        r = transfer(client, a, b, expected=1, reason="device swap")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["transferred"] is True
        assert body["from_revision"] == 1
        assert body["revocation_revision"] == 2
        assert body["successor_approval_revision"] == 3
        assert body["revision"] == 3
        assert body["to_revision"] == 3
        assert body["final_revision"] == 3
        assert body["reason"] == "device swap"
        assert body["successor_was_waitlisted"] is False
        assert body["approval"]["auto_approved"] is False

        # The source is revoked, the successor is approved at revision 3.
        source = client.get(f"/api/applications/{a}").json()
        assert source["status"] == "revoked"
        successor = client.get(f"/api/applications/{b}").json()
        assert successor["status"] == "approved"
        assert successor["approval_revision"] == 3

        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 3
        assert current["effective_application_ids"] == [b]

    def test_handover_uses_ordinary_revocation_and_approval_events(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        b = submit(client, 105, 108, "B")
        transfer(client, a, b, expected=1)

        snap = client.get("/api/revision/3").json()
        assert [e["application_id"] for e in snap["revocations"]] == [a]
        assert snap["revocations"][0]["revision"] == 2
        approvals = {e["application_id"]: e for e in snap["approvals"]}
        assert approvals[a]["status_as_of"] == "revoked"
        assert approvals[a]["revoked_at_revision"] == 2
        assert approvals[b]["status_as_of"] == "approved"
        assert approvals[b]["revision"] == 3
        # A handover approval is a manual one.
        assert approvals[b]["auto_approved"] is False
        assert approvals[b]["release_revision"] is None

        # The audit link ties the two consecutive revisions together.
        assert snap["license_transfers"] == [
            {
                "revision": 2,
                "source_application_id": a,
                "successor_application_id": b,
                "successor_approval_revision": 3,
                "reason": "swap",
                "created_at": snap["license_transfers"][0]["created_at"],
            }
        ]

    def test_each_intermediate_revision_reconstructs(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        b = submit(client, 105, 108, "B")
        transfer(client, a, b, expected=1)

        at_2 = client.get("/api/revision/2").json()
        # The source is already revoked at revision 2; the successor approval
        # is not there yet, so the effective set is momentarily empty.
        assert at_2["effective_application_ids"] == []
        assert at_2["license_transfers"][0]["revision"] == 2
        assert [e["application_id"] for e in at_2["approvals"]] == [a]
        assert at_2["approvals"][0]["status_as_of"] == "revoked"

        at_1 = client.get("/api/revision/1").json()
        assert at_1["license_transfers"] == []
        assert at_1["effective_application_ids"] == [a]


class TestConfirmRejections:
    def test_stale_revision_changes_nothing(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # revision 1
        b = submit(client, 105, 108, "B")

        r = transfer(client, a, b, expected=0)
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "stale_revision"
        assert err["details"] == {"expected_revision": 0, "current_revision": 1}

        assert client.get(f"/api/applications/{a}").json()["status"] == "approved"
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 1
        )

    def test_source_no_longer_effective_changes_nothing(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # revision 1
        b = submit(client, 105, 108, "B")
        # A gets revoked by the ordinary revoke at revision 2.
        client.post(f"/api/applications/{a}/revoke", json={"reason": "x"})

        r = transfer(client, a, b, expected=2)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "source_not_effective"
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 2
        )
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"

    def test_suspended_source_rejected(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        client.post(
            f"/api/applications/{a}/suspend",
            json={"expected_revision": 1, "reason": "outage"},
        )  # revision 2
        b = submit(client, 105, 108, "B")

        r = transfer(client, a, b, expected=2)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "source_not_effective"
        # The license stays suspended (not revoked by the handover).
        assert client.get(f"/api/applications/{a}").json()["status"] == "suspended"

    def test_successor_no_longer_pending_changes_nothing(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # revision 1
        b = submit(client, 130, 140, "B")
        approve(client, b, 1)  # revision 2

        r = transfer(client, a, b, expected=2)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "successor_not_pending"
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 2
        )
        assert client.get(f"/api/applications/{a}").json()["status"] == "approved"

    def test_revoked_successor_rejected_with_already_revoked(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # revision 1
        # B is approved at revision 2 (free band) then revoked at revision 3.
        b = submit(client, 130, 140, "B")
        approve(client, b, 1)  # revision 2
        client.post(f"/api/applications/{b}/revoke", json={"reason": "x"})

        r = transfer(client, a, b, expected=3)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "already_revoked"
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 3
        )
        assert client.get(f"/api/applications/{a}").json()["status"] == "approved"

    def test_conflict_at_confirm_changes_nothing(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # revision 1
        c = submit(client, 130, 140, "C")
        approve(client, c, 1)  # revision 2
        b = submit(client, 128, 129, "B")  # 1 MHz from C (< 2 band)

        r = transfer(client, a, b, expected=2)
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "interference"
        assert [x["application"]["id"] for x in err["details"]["conflicts"]] == [c]
        assert client.get(f"/api/applications/{a}").json()["status"] == "approved"
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 2
        )

    def test_unknown_ids_are_404(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        r = transfer(client, a, 9999, expected=1)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"
        r = transfer(client, 9999, a, expected=1)
        assert r.status_code == 404

    def test_same_source_and_successor_is_422(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        for path in ("/api/license-transfers/preview", "/api/license-transfers"):
            body = {
                "source_application_id": a,
                "successor_application_id": a,
                "reason": "x",
            }
            if path == "/api/license-transfers":
                body["expected_revision"] = 1
            r = client.post(path, json=body)
            assert r.status_code == 422, r.text
            detail = r.json()["error"]["details"][0]
            assert detail["code"] == "same_application"

    def test_structural_validation_uses_envelope(self, client):
        r = client.post(
            "/api/license-transfers",
            json={
                "source_application_id": 1,
                "successor_application_id": 2,
                "expected_revision": -1,
            },
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "validation_failed"


class TestRestrictionWindows:
    def _activate_window(self, client, freq=(100, 110), revision=1):
        payload = {
            "circle": {"cx": 0, "cy": 0, "radius": 10},
            "frequency": {"low": freq[0], "high": freq[1]},
            "time_window": {
                "start": "2026-10-01T08:00:00Z",
                "end": "2026-12-31T00:00:00Z",
            },
            "reason": "control",
            "expected_revision": revision,
        }
        r = client.post("/api/restriction-windows", json=payload)
        assert r.status_code == 201, r.text
        return r.json()

    def test_preview_reports_hit_window(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # revision 1
        self._activate_window(client, freq=(104, 106), revision=1)  # rev 2
        b = submit(client, 105, 105.5, "B")  # hits the window

        body = transfer_preview(client, a, b).json()
        assert body["current_revision"] == 2
        assert body["ready"] is False
        assert body["has_restriction_window_conflicts"] is True
        assert len(body["restriction_window_conflicts"]) == 1
        assert any(
            item["code"] == "restriction_window" for item in body["blockers"]
        )
        # Read-only.
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 2
        )

    def test_confirm_blocked_by_window_changes_nothing(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # revision 1
        self._activate_window(client, freq=(104, 106), revision=1)  # rev 2
        b = submit(client, 105, 105.5, "B")

        r = transfer(client, a, b, expected=2)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "restriction_window"
        assert client.get(f"/api/applications/{a}").json()["status"] == "approved"
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 2
        )


class TestWaitlist:
    def test_queued_successor_leaves_queue_and_is_manual_approval(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # revision 1
        b = submit(client, 105, 108, "B")  # blocked by A
        register(client, b)
        assert queue_ids(client) == [b]

        body = transfer(client, a, b, expected=1).json()
        assert body["successor_was_waitlisted"] is True
        assert body["approval"]["auto_approved"] is False
        # No other standbys: the handover itself ends at revision 3.
        assert body["final_revision"] == 3
        assert queue_ids(client) == []
        approvals = client.get("/api/revision/current").json()["approvals"]
        successor_event = next(e for e in approvals if e["application_id"] == b)
        assert successor_event["auto_approved"] is False
        assert successor_event["release_revision"] is None

    def test_remaining_standbys_are_readjudicated_fifo(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # revision 1
        # B is the queued successor (frees A's exact band); C is another
        # standby that becomes approvable only once A is gone; D stays
        # blocked by C afterwards.
        b = submit(client, 100, 102, "B")
        c = submit(client, 130, 132, "C")
        d = submit(client, 131, 133, "D")  # intersects C
        register(client, b)
        register(client, c)
        register(client, d)
        assert queue_ids(client) == [b, c, d]

        body = transfer(client, a, b, expected=1).json()
        # Revocation rev 2, B approval rev 3, then the FIFO drain: C passes at
        # rev 4 (A is gone, band 2 MHz from nothing at 130); D intersects C so
        # it keeps its place.
        assert body["revocation_revision"] == 2
        assert body["successor_approval_revision"] == 3
        assert body["auto_approved_application_ids"] == [c]
        auto = body["auto_approvals"][0]
        assert auto["revision"] == 4
        assert auto["release_revision"] == 2
        assert body["remaining_waitlist_application_ids"] == [d]
        assert body["final_revision"] == 4
        assert queue_ids(client) == [d]

        current = client.get("/api/revision/current").json()
        assert current["effective_application_ids"] == [b, c]
        assert client.get(f"/api/applications/{d}").json()["status"] == "pending"

    def test_handover_does_not_drain_when_rejected(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # revision 1
        c = submit(client, 130, 140, "C")
        approve(client, c, 1)  # revision 2
        b = submit(client, 128, 129, "B")  # blocked by C
        d = submit(client, 200, 210, "D")  # would pass once A is gone
        register(client, b)
        register(client, d)

        r = transfer(client, a, b, expected=2)
        assert r.status_code == 409  # successor conflicts with C
        # Nothing happened: A stays effective, the queue is untouched, D was
        # not auto-approved and the revision stays at 2.
        assert queue_ids(client) == [b, d]
        assert client.get(f"/api/applications/{d}").json()["status"] == "pending"
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 2
        )


class TestEmergencyProtection:
    """An active emergency license can only end via its dedicated release.

    Handing such a license over as the swap source used to rehearse as
    ready and the confirmation revoked the emergency license and approved
    the successor while leaving the emergency occupation record live.  The
    preview must now list an ``emergency_license_protected`` blocker and the
    confirmation must reject with 409 ``emergency_license_protected`` inside
    the shared revision arbitration, leaving everything untouched.
    """

    def test_preview_lists_emergency_protection_blocker(self, client):
        # The emergency application occupies spectrum straight from pending;
        # a disjoint band means no ordinary license has to be force-suspended.
        em = submit(client, 100, 110, "emergency source")
        body = confirm_emergency(client, em, 0)  # emergency approval rev 1
        b = submit(client, 105, 108, "successor")

        r = transfer_preview(client, em, b)
        assert r.status_code == 200, r.text
        preview = r.json()
        assert preview["ready"] is False
        blocker = next(
            item
            for item in preview["blockers"]
            if item["code"] == "emergency_license_protected"
        )
        assert blocker["field"] == "source_application_id"
        assert blocker["status"] == "approved"
        # The interference computation still runs and remains clear (the
        # source would be excluded as in any other rehearsal).
        assert preview["has_conflicts"] is False

        # Read-only as always.
        assert client.get(f"/api/applications/{em}").json()["status"] == "approved"
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 1
        )
        assert (
            client.get(f"/api/emergency-occupations/{em}").json()[
                "occupation"
            ]["status"]
            == "active"
        )
        assert body["emergency_approval_revision"] == 1

    def test_confirm_rejects_and_changes_nothing(self, client):
        em = submit(client, 100, 110, "emergency source")
        confirm_emergency(client, em, 0)  # revision 1
        b = submit(client, 105, 108, "successor")
        # A queued standby that must not be touched by the rejected swap.
        d = submit(client, 200, 205, "standby")
        register(client, b)
        register(client, d)

        r = transfer(client, em, b, expected=1)
        assert r.status_code == 409, r.text
        err = r.json()["error"]
        assert err["code"] == "emergency_license_protected"
        assert err["details"]["application_id"] == em
        assert err["details"]["status"] == "approved"
        assert err["details"]["current_revision"] == 1
        assert err["details"]["emergency_approval_revision"] == 1

        # Source, successor, waitlist, occupation record and revision: all
        # exactly as before the attempt.
        assert client.get(f"/api/applications/{em}").json()["status"] == "approved"
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"
        cur = client.get("/api/revision/current").json()
        assert cur["current_revision"] == 1
        assert cur["effective_application_ids"] == [em]
        assert cur["active_emergency_application_ids"] == [em]
        assert cur["force_suspended_application_ids"] == []
        assert queue_ids(client) == [b, d]
        occupation = client.get(
            f"/api/emergency-occupations/{em}"
        ).json()["occupation"]
        assert occupation["status"] == "active"
        assert "released_revision" not in occupation

    def test_stale_revision_still_rejected_inside_same_arbitration(self, client):
        em = submit(client, 100, 110, "emergency source")
        confirm_emergency(client, em, 0)  # revision 1
        b = submit(client, 105, 108, "successor")

        # Status protection precedes the revision check, same precedence as
        # the ordinary source_not_effective guard; a stale expected revision
        # therefore does not mask it.
        r = transfer(client, em, b, expected=0)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "emergency_license_protected"
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 1
        )

    def test_protection_holds_when_emergency_force_suspended_others(self, client):
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)  # revision 1
        # Emergency band clashes with A, so A is force-suspended before the
        # emergency license is approved at revision 3.
        em = submit(client, 104, 109, "emergency source")
        body = confirm_emergency(client, em, 1)
        assert body["force_suspended_application_ids"] == [a]
        b = submit(client, 105, 108, "successor")

        preview = transfer_preview(client, em, b).json()
        assert any(
            item["code"] == "emergency_license_protected"
            for item in preview["blockers"]
        )
        r = transfer(client, em, b, expected=3)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "emergency_license_protected"

        # Nothing moved: A stays force-suspended, the emergency stays live.
        cur = client.get("/api/revision/current").json()
        assert cur["current_revision"] == 3
        assert cur["active_emergency_application_ids"] == [em]
        assert cur["force_suspended_application_ids"] == [a]
        assert client.get(f"/api/applications/{a}").json()["status"] == "suspended"

    def test_released_emergency_source_falls_back_to_non_effective_rule(
        self, client
    ):
        em = submit(client, 100, 110, "emergency source")
        confirm_emergency(client, em, 0)  # revision 1
        b = submit(client, 105, 108, "successor")

        # The dedicated release is still the only way the emergency ends.
        r = release_emergency(client, em, expected=1)
        assert r.status_code == 200, r.text
        release_revision = r.json()["revision"]

        # The released emergency application is now an ordinary revoked
        # license, so the pre-existing non-effective source rule applies;
        # there is no emergency_license_protected blocker anymore.
        preview = transfer_preview(client, em, b).json()
        codes = [item["code"] for item in preview["blockers"]]
        assert "source_not_effective" in codes
        assert "emergency_license_protected" not in codes

        r = transfer(client, em, b, expected=release_revision)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "source_not_effective"
        assert (
            client.get("/api/revision/current").json()["current_revision"]
            == release_revision
        )
        occupation = client.get(
            f"/api/emergency-occupations/{em}"
        ).json()["occupation"]
        assert occupation["status"] == "released"
        assert occupation["released_revision"] == release_revision


class TestHistory:
    def test_comparison_restores_the_handover(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # revision 1
        b = submit(client, 105, 108, "B")
        transfer(client, a, b, expected=1)  # revisions 2, 3

        r = client.get(
            "/api/revision/compare",
            params={"base_revision": 1, "target_revision": 3},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["effective_application_ids"]["base"] == [a]
        assert body["effective_application_ids"]["target"] == [b]
        assert body["license_transfers"] == [
            {
                "type": "license_transfer",
                "revision": 2,
                "source_application_id": a,
                "successor_application_id": b,
                "successor_approval_revision": 3,
                "reason": "swap",
                "created_at": body["license_transfers"][0]["created_at"],
            }
        ]
        types = [e["type"] for e in body["events"]]
        assert types == ["revocation", "license_transfer", "approval"]
        # Status changes come only from the real lifecycle events; the link
        # row does not manufacture a third change.
        changes = {
            (c["application_id"], c["from_status"], c["to_status"])
            for c in body["status_changes"]
        }
        assert (a, "approved", "revoked") in changes
        assert (b, "pending", "approved") in changes
        assert len(body["status_changes"]) == 2

    def test_comparison_application_filter_covers_both_parties(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        b = submit(client, 105, 108, "B")
        transfer(client, a, b, expected=1)

        for party in (a, b):
            body = client.get(
                "/api/revision/compare",
                params={
                    "base_revision": 1,
                    "target_revision": 3,
                    "application_id": party,
                },
            ).json()
            assert [t["revision"] for t in body["license_transfers"]] == [2]

    def test_current_snapshot_carries_transfers(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)
        b = submit(client, 105, 108, "B")
        transfer(client, a, b, expected=1)
        current = client.get("/api/revision/current").json()
        assert [t["revision"] for t in current["license_transfers"]] == [2]
