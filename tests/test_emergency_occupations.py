"""End-to-end tests for the burst-communication emergency occupation feature.

An emergency occupation lets an operator grab spectrum ahead of normal
queued decisions: the read-only preview reports the current revision, the
ordinary effective licenses that would be force-suspended, active emergency
licenses that clash and the restriction windows hit.  The confirmation
re-checks everything under the shared revision lock and then force-suspends
the interfering ordinary licenses (one consecutive revision each, without a
waitlist drain), pulls a queued emergency application out of the queue and
approves the emergency license at the next revision.  The dedicated release
revokes the emergency license, restores still-force-suspended licenses in
suspension order (blocked ones stay suspended) and finally drains the waitlist,
all atomically.

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


def revoke(client, app_id, reason=""):
    return client.post(
        f"/api/applications/{app_id}/revoke", json={"reason": reason}
    )


def preview_emergency(client, app_id, reason="burst"):
    return client.post(
        "/api/emergency-occupations/preview",
        json={"application_id": app_id, "reason": reason},
    )


def confirm_emergency(client, app_id, expected, reason="burst"):
    return client.post(
        "/api/emergency-occupations",
        json={
            "application_id": app_id,
            "expected_revision": expected,
            "reason": reason,
        },
    )


def release_emergency(client, app_id, expected, reason="clear"):
    return client.post(
        f"/api/emergency-occupations/{app_id}/release",
        json={"expected_revision": expected, "reason": reason},
    )


def current_revision(client):
    return client.get("/api/revision/current").json()["current_revision"]


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #
class TestEmergencyPreview:
    def test_preview_is_read_only_and_lists_suspension_candidates(self, client):
        a = submit(client, 100, 110, "ordinary A")
        b = submit(client, 200, 210, "ordinary B")
        approve(client, a, 0)  # rev 1
        approve(client, b, 1)  # rev 2 (A and B are 90 MHz apart)
        em = submit(client, 110, 200, "emergency")  # touches both bands

        r = preview_emergency(client, em)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["current_revision"] == 2
        assert body["ready"] is True
        assert body["blockers"] == []
        assert sorted(body["force_suspend_application_ids"]) == [a, b]
        assert body["emergency_conflict_application_ids"] == []
        assert body["has_restriction_window_conflicts"] is False
        assert body["planned_first_force_suspension_revision"] == 3
        assert body["planned_approval_revision"] == 5
        # nothing changed
        assert current_revision(client) == 2
        cur = client.get("/api/revision/current").json()
        assert cur["effective_application_ids"] == [a, b]

    def test_preview_disjoint_band_is_ready_without_forced_suspensions(
        self, client
    ):
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)  # rev 1
        em = submit(client, 500, 510, "emergency")
        body = preview_emergency(client, em).json()
        assert body["ready"] is True
        assert body["force_suspend_application_ids"] == []
        assert body["planned_first_force_suspension_revision"] is None
        assert body["planned_approval_revision"] == 2

    def test_preview_unknown_application_is_blocker(self, client):
        body = preview_emergency(client, 99999).json()
        assert body["ready"] is False
        assert [b["code"] for b in body["blockers"]] == ["not_found"]

    def test_preview_non_pending_application_is_blocker(self, client):
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)
        body = preview_emergency(client, a).json()
        assert body["ready"] is False
        assert [b["code"] for b in body["blockers"]] == ["not_pending"]


# --------------------------------------------------------------------------- #
# Confirmation
# --------------------------------------------------------------------------- #
class TestEmergencyConfirm:
    def test_confirm_force_suspends_then_approves_on_consecutive_revisions(
        self, client
    ):
        a = submit(client, 100, 110, "ordinary A")
        b = submit(client, 200, 210, "ordinary B")
        approve(client, a, 0)  # rev 1
        approve(client, b, 1)  # rev 2
        em = submit(client, 104, 109, "emergency")

        r = confirm_emergency(client, em, 2)
        assert r.status_code == 201, r.text
        body = r.json()
        # A clashes -> force suspended at 3; B disjoint -> untouched;
        # emergency approval at 4.
        assert body["force_suspended_application_ids"] == [a]
        assert body["first_force_suspension_revision"] == 3
        assert body["last_force_suspension_revision"] == 3
        assert body["emergency_approval_revision"] == 4
        assert body["revision"] == 4
        assert body["to_revision"] == 4

        cur = client.get("/api/revision/current").json()
        assert cur["current_revision"] == 4
        assert cur["effective_application_ids"] == [b, em]
        assert cur["suspended_application_ids"] == [a]
        assert cur["force_suspended_application_ids"] == [a]
        assert cur["active_emergency_application_ids"] == [em]
        susp = cur["suspensions"][0]
        assert susp["revision"] == 3
        assert susp["application_id"] == a
        assert susp["forced"] is True
        assert susp["forced_by_emergency_application_id"] == em
        approval = [x for x in cur["approvals"] if x["application_id"] == em][0]
        assert approval["revision"] == 4
        assert approval["emergency"] is True
        assert approval["auto_approved"] is False

        # application view carries the live marker
        a_view = client.get(f"/api/applications/{a}").json()
        assert a_view["status"] == "suspended"
        assert a_view["force_suspended_by"] == em

    def test_confirm_force_suspension_order_is_fixed_and_multiple(self, client):
        # Two mutually-disjoint ordinary licenses both touch the emergency
        # band: both are force-suspended in id order before the approval.
        a = submit(client, 100, 110, "ordinary A")
        b = submit(client, 200, 210, "ordinary B")
        approve(client, a, 0)  # 1
        approve(client, b, 1)  # 2
        em = submit(client, 110, 200, "emergency")  # touches both
        body = confirm_emergency(client, em, 2).json()
        assert body["force_suspended_application_ids"] == [a, b]
        revs = [(s["revision"], s["application_id"]) for s in body["force_suspensions"]]
        assert revs == [(3, a), (4, b)]
        assert body["emergency_approval_revision"] == 5
        cur = client.get("/api/revision/current").json()
        assert cur["force_suspended_application_ids"] == [a, b]
        assert cur["active_emergency_application_ids"] == [em]

    def test_confirm_does_not_trigger_waitlist_drain(self, client):
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)  # 1
        # A standby that would pass exactly when A leaves the set; it must
        # NOT be auto-approved by the forced suspension.
        w = submit(client, 200, 210, "standby")
        rw = client.post(f"/api/applications/{w}/waitlist", json={})
        assert rw.status_code == 200
        em = submit(client, 104, 109, "emergency")
        confirm_emergency(client, em, 1)  # forced 2, approval 3
        cur = client.get("/api/revision/current").json()
        assert cur["waitlist_application_ids"] == [w]
        assert cur["current_revision"] == 3
        assert w not in cur["effective_application_ids"]

    def test_queued_emergency_application_leaves_queue(self, client):
        a = submit(client, 200, 210, "ordinary A")
        approve(client, a, 0)  # 1
        em = submit(client, 500, 510, "emergency")
        client.post(f"/api/applications/{em}/waitlist", json={})
        body = confirm_emergency(client, em, 1).json()
        assert body["was_waitlisted"] is True
        assert body["emergency_approval_revision"] == 2
        cur = client.get("/api/revision/current").json()
        assert cur["waitlist_application_ids"] == []

    def test_stale_revision_rejects_whole_occupation(self, client):
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)  # 1
        em = submit(client, 104, 109, "emergency")
        r = confirm_emergency(client, em, 0)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "stale_revision"
        assert current_revision(client) == 1

    def test_not_pending_and_unknown_rejected(self, client):
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)
        r = confirm_emergency(client, a, 1)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "not_pending"
        r = confirm_emergency(client, 99999, 1)
        assert r.status_code == 404

    def test_clashing_emergency_license_rejects_whole_occupation(self, client):
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)  # 1
        em1 = submit(client, 104, 109, "emergency one")
        confirm_emergency(client, em1, 1)  # A forced 2, em1 approved 3
        em2 = submit(client, 105, 108, "emergency two")
        r = confirm_emergency(client, em2, 3)
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "emergency_occupation_conflict"
        assert err["details"]["conflicts"][0]["application"]["id"] == em1
        # nothing changed: em2 pending, em1 effective, A still suspended
        cur = client.get("/api/revision/current").json()
        assert cur["current_revision"] == 3
        assert cur["effective_application_ids"] == [em1]
        assert cur["suspended_application_ids"] == [a]

    def test_restriction_window_hit_rejects_occupation(self, client):
        em = submit(client, 100, 110, "emergency",
                    start="2030-10-01T13:00:00Z", end="2030-10-01T14:00:00Z")
        window = {
            "circle": {"cx": 0, "cy": 0, "radius": 10},
            "frequency": {"low": 100, "high": 110},
            "time_window": {
                "start": "2030-10-01T13:00:00Z",
                "end": "2030-10-01T14:00:00Z",
            },
            "reason": "control",
            "expected_revision": 0,
        }
        wr = client.post("/api/restriction-windows", json=window)
        assert wr.status_code == 201, wr.text
        r = confirm_emergency(client, em, 1)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "restriction_window"
        assert current_revision(client) == 1

    def test_confirm_twice_rejected_as_not_pending(self, client):
        em = submit(client, 500, 510, "emergency")
        confirm_emergency(client, em, 0)  # approved at 1
        r = confirm_emergency(client, em, 1)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "not_pending"
        assert current_revision(client) == 1


# --------------------------------------------------------------------------- #
# Guards on ordinary operations while an emergency is active
# --------------------------------------------------------------------------- #
class TestEmergencyGuards:
    def test_emergency_license_cannot_be_ordinarily_suspended_or_revoked(
        self, client
    ):
        em = submit(client, 500, 510, "emergency")
        confirm_emergency(client, em, 0)  # approved at 1
        rs = client.post(
            f"/api/applications/{em}/suspend",
            json={"expected_revision": 1, "reason": "x"},
        )
        assert rs.status_code == 409
        assert rs.json()["error"]["code"] == "emergency_license_protected"
        rr = revoke(client, em)
        assert rr.status_code == 409
        assert rr.json()["error"]["code"] == "emergency_license_protected"
        assert current_revision(client) == 1

    def test_force_suspended_license_rejects_ordinary_resume_but_revokes(
        self, client
    ):
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)  # 1
        em = submit(client, 104, 109, "emergency")
        confirm_emergency(client, em, 1)  # A forced 2, em approved 3
        rr = client.post(
            f"/api/applications/{a}/resume",
            json={"expected_revision": 3},
        )
        assert rr.status_code == 409
        assert rr.json()["error"]["code"] == "force_suspended"
        assert current_revision(client) == 3

        # ordinary suspend is also refused on a force-suspended license
        rs = client.post(
            f"/api/applications/{a}/suspend",
            json={"expected_revision": 3},
        )
        assert rs.status_code == 409
        assert rs.json()["error"]["code"] == "force_suspended"

        # ... but it can be revoked as usual, which drains the waitlist
        revoked = revoke(client, a, "scrapped")
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["revision"] == 4
        cur = client.get("/api/revision/current").json()
        assert a not in cur["suspended_application_ids"]


# --------------------------------------------------------------------------- #
# Release
# --------------------------------------------------------------------------- #
class TestEmergencyRelease:
    def test_release_revokes_restores_in_order_then_drains_waitlist(self, client):
        a = submit(client, 100, 110, "ordinary A")
        b = submit(client, 200, 210, "ordinary B")
        approve(client, a, 0)  # 1
        approve(client, b, 1)  # 2
        em = submit(client, 110, 200, "emergency")  # touches both
        confirm_emergency(client, em, 2)  # A forced 3, B forced 4, em 5

        # A standby disjoint from the emergency band; it must be re-adjudicated
        # only after the forced restorations.
        w = submit(client, 600, 610, "standby")
        client.post(f"/api/applications/{w}/waitlist", json={})

        r = release_emergency(client, em, 5)
        assert r.status_code == 200, r.text
        body = r.json()
        # emergency revoked at 6; A resumes 7; B resumes 8; waitlist W auto 9
        assert body["emergency_revocation_revision"] == 6
        assert body["revision"] == 6
        assert [x["application_id"] for x in body["restorations"]] == [a, b]
        assert [x["revision"] for x in body["restorations"]] == [7, 8]
        assert body["restored_application_ids"] == [a, b]
        assert body["blocked_application_ids"] == []
        assert body["auto_approved_application_ids"] == [w]
        assert body["final_revision"] == 9

        cur = client.get("/api/revision/current").json()
        assert cur["effective_application_ids"] == [a, b, w]
        assert cur["suspended_application_ids"] == []
        assert cur["force_suspended_application_ids"] == []
        assert cur["active_emergency_application_ids"] == []
        assert cur["waitlist_application_ids"] == []
        rec = [x for x in cur["revocations"] if x["application_id"] == em][0]
        assert rec["emergency_release"] is True
        assert rec["emergency_application_id"] == em
        recoveries = [
            x for x in cur["resumptions"] if x["application_id"] in (a, b)
        ]
        assert all(x["forced_recovery"] for x in recoveries)

    def test_release_blocked_license_stays_suspended_as_ordinary(self, client):
        # A force-suspended; while the emergency is active another ordinary
        # license takes A's band (possible via a different disjoint-from-em
        # band setup): construct so that on release A cannot resume.
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)  # 1
        em = submit(client, 104, 109, "emergency")
        confirm_emergency(client, em, 1)  # A forced 2, em approved 3

        # While em occupies [104,109], approve C on [100,101] (overlaps A but
        # disjoint from em) -> possible manually while A is suspended.
        c = submit(client, 100, 101, "squatter C")
        approve(client, c, 3)  # 4

        body = release_emergency(client, em, 4).json()
        # em revoked 5; A re-adjudicated against C -> blocked, stays suspended
        assert body["emergency_revocation_revision"] == 5
        assert body["restored_application_ids"] == []
        assert body["blocked_application_ids"] == [a]
        blocked = body["blocked"][0]
        assert [x["application"]["id"] for x in blocked["conflicts"]] == [c]
        assert body["final_revision"] == 5

        cur = client.get("/api/revision/current").json()
        assert cur["effective_application_ids"] == [c]
        assert cur["suspended_application_ids"] == [a]
        # no longer force-linked: an ordinary resume path is open again
        a_view = client.get(f"/api/applications/{a}").json()
        assert a_view["force_suspended_by"] is None
        # ordinary resume is now accepted once the blocker is gone
        revoke(client, c).raise_for_status()  # 6 (may drain)
        # recompute current revision then resume
        rev = current_revision(client)
        rres = client.post(
            f"/api/applications/{a}/resume",
            json={"expected_revision": rev},
        )
        assert rres.status_code == 200, rres.text
        assert a in client.get("/api/revision/current").json()[
            "effective_application_ids"
        ]

    def test_release_revoked_force_suspended_license_is_not_restored(self, client):
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)  # 1
        em = submit(client, 104, 109, "emergency")
        confirm_emergency(client, em, 1)  # A forced 2, em 3
        revoke(client, a, "gone")  # 4
        body = release_emergency(client, em, 4).json()
        assert body["restored_application_ids"] == []
        assert body["blocked_application_ids"] == []
        # em revoked at 5; nothing else
        assert body["emergency_revocation_revision"] == 5
        assert body["final_revision"] == 5

    def test_release_uses_current_band_and_windows_when_readjudicating(
        self, client
    ):
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)  # 1
        em = submit(client, 104, 109, "emergency")
        confirm_emergency(client, em, 1)  # A forced 2, em 3
        # Tighten the guard band while the emergency is active (A stays
        # suspended); the release must restore A under the live band.
        gb = client.post(
            "/api/config/guard-band",
            json={"guard_band": 0.5, "expected_revision": 3},
        )
        assert gb.status_code == 200, gb.text  # 4
        body = release_emergency(client, em, 4).json()
        assert body["restored_application_ids"] == [a]
        restoration = body["restorations"][0]
        assert restoration["guard_band"] == 0.5

    def test_release_re_adjudication_respects_restriction_window(self, client):
        # A force-suspended; after release an active window covering A blocks
        # its restoration (it stays suspended).
        a = submit(client, 100, 110, "ordinary A",
                   start="2030-10-01T13:00:00Z", end="2030-10-01T14:00:00Z")
        approve(client, a, 0)  # 1
        em = submit(client, 104, 109, "emergency",
                    start="2030-10-01T13:00:00Z", end="2030-10-01T14:00:00Z")
        confirm_emergency(client, em, 1)  # A forced 2, em 3
        # Window covering A's band but not the emergency's band... the
        # emergency release itself is unconditional, but A's restoration must
        # be checked.  A window covering both bands still only affects the
        # restoration (release revocation is never window-gated).
        window = {
            "circle": {"cx": 0, "cy": 0, "radius": 10},
            "frequency": {"low": 100, "high": 102},
            "time_window": {
                "start": "2030-10-01T13:00:00Z",
                "end": "2030-10-01T14:00:00Z",
            },
            "reason": "control",
            "expected_revision": 3,
        }
        wr = client.post("/api/restriction-windows", json=window)
        assert wr.status_code == 201, wr.text  # 4
        body = release_emergency(client, em, 4).json()
        assert body["blocked_application_ids"] == [a]
        assert body["blocked"][0]["restriction_window_conflicts"]
        assert body["restored_application_ids"] == []
        cur = client.get("/api/revision/current").json()
        assert cur["active_emergency_application_ids"] == []

    def test_unknown_and_double_release_change_nothing(self, client):
        r = release_emergency(client, 99999, 0)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "emergency_occupation_not_found"

        em = submit(client, 500, 510, "emergency")
        confirm_emergency(client, em, 0)  # 1
        release_emergency(client, em, 1)  # revoke at 2
        again = release_emergency(client, em, 2)
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "emergency_occupation_not_active"
        assert current_revision(client) == 2

    def test_stale_release_changes_nothing(self, client):
        em = submit(client, 500, 510, "emergency")
        confirm_emergency(client, em, 0)  # 1
        r = release_emergency(client, em, 0)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "stale_revision"
        assert current_revision(client) == 1
        # the emergency license is still effective
        cur = client.get("/api/revision/current").json()
        assert cur["active_emergency_application_ids"] == [em]


# --------------------------------------------------------------------------- #
# History / snapshots / comparison
# --------------------------------------------------------------------------- #
class TestEmergencyHistory:
    def test_snapshots_restore_each_phase_revision_by_revision(self, client):
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)  # 1
        em = submit(client, 104, 109, "emergency")
        confirm_emergency(client, em, 1)  # A forced 2, em approved 3

        # at the forced-suspension revision: A suspended, em not yet approved
        s2 = client.get("/api/revision/2").json()
        assert s2["suspended_application_ids"] == [a]
        assert s2["force_suspended_application_ids"] == [a]
        assert s2["active_emergency_application_ids"] == []
        assert s2["emergency_occupation_events"] == []

        s3 = client.get("/api/revision/3").json()
        assert s3["effective_application_ids"] == [em]
        assert s3["suspended_application_ids"] == [a]
        assert s3["active_emergency_application_ids"] == [em]
        assert s3["force_suspended_application_ids"] == [a]
        occ = s3["emergency_occupations"][0]
        assert occ["application_id"] == em
        assert occ["status"] == "active"
        assert occ["force_suspended_application_ids"] == [a]

        release_emergency(client, em, 3)  # em revoked 4, A restored 5
        s4 = client.get("/api/revision/4").json()
        assert s4["active_emergency_application_ids"] == []
        assert s4["suspended_application_ids"] == [a]
        assert s4["force_suspended_application_ids"] == []
        s5 = client.get("/api/revision/5").json()
        assert s5["effective_application_ids"] == [a]
        assert s5["emergency_occupations"][0]["status"] == "released"
        assert s5["emergency_occupations"][0]["released_revision"] == 4
        assert s5["emergency_occupations"][0]["recovered_application_ids"] == [a]

    def test_comparison_reports_emergency_events(self, client):
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)  # 1
        em = submit(client, 104, 109, "emergency")
        confirm_emergency(client, em, 1)  # 2, 3
        release_emergency(client, em, 3)  # 4, 5
        cmp_ = client.get(
            "/api/revision/compare",
            params={"base_revision": 1, "target_revision": 5},
        ).json()
        types = [e["type"] for e in cmp_["emergency_occupation_events"]]
        assert types == [
            "emergency_occupation_activated",
            "emergency_occupation_released",
        ]
        # Neither endpoint holds an active emergency (it existed only inside
        # the interval), so the endpoint set diff is empty; its temporary
        # activation/release is carried by the event stream above.
        assert cmp_["active_emergency_application_ids"] == {
            "base": [],
            "target": [],
            "added": [],
            "removed": [],
            "added_to_target": [],
            "removed_from_base": [],
        }
        assert cmp_["force_suspended_application_ids"] == {
            "base": [],
            "target": [],
            "added": [],
            "removed": [],
            "added_to_target": [],
            "removed_from_base": [],
        }
        diff_types = [
            e["type"]
            for e in cmp_["events"]
            if e["type"].startswith("emergency_occupation")
        ]
        assert diff_types == [
            "emergency_occupation_activated",
            "emergency_occupation_released",
        ]


# --------------------------------------------------------------------------- #
# Listing endpoints
# --------------------------------------------------------------------------- #
class TestEmergencyListing:
    def test_list_active_and_released_and_get_one(self, client):
        em1 = submit(client, 500, 510, "emergency one")
        confirm_emergency(client, em1, 0)  # 1
        em2 = submit(client, 600, 610, "emergency two")
        confirm_emergency(client, em2, 1)  # 2
        release_emergency(client, em1, 2)  # 3

        active = client.get("/api/emergency-occupations").json()
        assert active["active_emergency_application_ids"] == [em2]
        all_ = client.get(
            "/api/emergency-occupations?include_inactive=true"
        ).json()
        assert [o["application_id"] for o in all_["occupations"]] == [em1, em2]
        released = client.get(
            "/api/emergency-occupations?status=released"
        ).json()
        assert [o["application_id"] for o in released["occupations"]] == [em1]

        one = client.get(f"/api/emergency-occupations/{em1}").json()
        assert one["occupation"]["status"] == "released"
        assert one["occupation"]["released_revision"] == 3
        missing = client.get("/api/emergency-occupations/99999")
        assert missing.status_code == 404
        assert (
            missing.json()["error"]["code"]
            == "emergency_occupation_not_found"
        )

    def test_invalid_status_filter_is_422(self, client):
        r = client.get("/api/emergency-occupations?status=bogus")
        assert r.status_code == 422
