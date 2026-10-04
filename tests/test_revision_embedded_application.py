"""Regression tests: embedded applications in revision history are as-of views.

``GET /api/revision/{n}`` and ``GET /api/revision/compare`` embed an
``application`` object inside every approval record.  That object must be
the state the *event replay* reconstructs at the queried revision (status,
``approval_revision``, ``suspended_revision`` and ``force_suspended_by``),
not the live row: a license approved at revision 1 and suspended at
revision 2 must still show ``approved`` with no suspension marker inside
the revision-1 view, and a later resume, revocation or emergency release
must never rewrite an older view.  Current-state endpoints keep reflecting
the live row.

Guard band is the test default (2.0); every application shares the default
coverage circle and UTC window, so only frequency bands decide interference.
"""
from factories import make_payload


def submit(client, low, high, name="Event"):
    r = client.post(
        "/api/applications", json=make_payload(freq=(low, high), name=name)
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def approve(client, app_id, expected):
    r = client.post(
        f"/api/applications/{app_id}/approve",
        json={"expected_revision": expected},
    )
    assert r.status_code == 200, r.text
    return r.json()


def suspend(client, app_id, expected, reason="outage"):
    r = client.post(
        f"/api/applications/{app_id}/suspend",
        json={"expected_revision": expected, "reason": reason},
    )
    assert r.status_code == 200, r.text
    return r.json()


def resume(client, app_id, expected, reason="fixed"):
    r = client.post(
        f"/api/applications/{app_id}/resume",
        json={"expected_revision": expected, "reason": reason},
    )
    assert r.status_code == 200, r.text
    return r.json()


def revoke(client, app_id, reason=""):
    r = client.post(
        f"/api/applications/{app_id}/revoke", json={"reason": reason}
    )
    assert r.status_code == 200, r.text
    return r.json()


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
    r = client.post(
        f"/api/emergency-occupations/{app_id}/release",
        json={"expected_revision": expected, "reason": reason},
    )
    assert r.status_code == 200, r.text
    return r.json()


def snapshot(client, revision):
    r = client.get(f"/api/revision/{revision}")
    assert r.status_code == 200, r.text
    return r.json()


def compare(client, base_revision, target_revision, application_id=None):
    params = {
        "base_revision": base_revision,
        "target_revision": target_revision,
    }
    if application_id is not None:
        params["application_id"] = application_id
    r = client.get("/api/revision/compare", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def approval_of(body, application_id):
    """The single approval record of one application in a snapshot body."""
    (record,) = [
        a for a in body["approvals"] if a["application_id"] == application_id
    ]
    return record


class TestSnapshotEmbeddedApplication:
    def test_embedded_application_follows_the_event_replay(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # revision 1
        suspend(client, a, 1)  # revision 2

        # The exact audit trap: at revision 1 the grant is approved, so the
        # embedded application must not show the later suspension.
        rec1 = approval_of(snapshot(client, 1), a)
        assert rec1["status_as_of"] == "approved"
        assert rec1["application"]["status"] == "approved"
        assert rec1["application"]["approval_revision"] == 1
        assert rec1["application"]["suspended_revision"] is None
        assert rec1["application"]["force_suspended_by"] is None

        # At revision 2 the embedded application is the suspended license.
        rec2 = approval_of(snapshot(client, 2), a)
        assert rec2["status_as_of"] == "suspended"
        assert rec2["suspended_at_revision"] == 2
        assert rec2["application"]["status"] == "suspended"
        assert rec2["application"]["approval_revision"] == 1
        assert rec2["application"]["suspended_revision"] == 2
        assert rec2["application"]["force_suspended_by"] is None

        # A later resume must not rewrite the older views.
        resume(client, a, 2)  # revision 3
        rec1_after = approval_of(snapshot(client, 1), a)
        rec2_after = approval_of(snapshot(client, 2), a)
        assert rec1_after["application"]["status"] == "approved"
        assert rec1_after["application"]["suspended_revision"] is None
        assert rec2_after["application"]["status"] == "suspended"
        assert rec2_after["application"]["suspended_revision"] == 2

        rec3 = approval_of(snapshot(client, 3), a)
        assert rec3["status_as_of"] == "approved"
        assert rec3["application"]["status"] == "approved"
        assert rec3["application"]["approval_revision"] == 1
        assert rec3["application"]["suspended_revision"] is None

        # Current-state queries still reflect the live row.
        live = client.get(f"/api/applications/{a}").json()
        assert live["status"] == "approved"
        assert live["suspended_revision"] is None
        current = client.get("/api/revision/current").json()
        (current_rec,) = current["approvals"]
        assert current_rec["application"]["status"] == "approved"
        assert current_rec["application"]["suspended_revision"] is None

    def test_revocation_keeps_the_suspension_marker_as_of(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # 1
        suspend(client, a, 1)  # 2
        revoke(client, a)  # 3 (revoked while suspended)

        # At the revocation revision the embedded application is revoked and
        # keeps the suspension revision as a historical marker (mirroring
        # the live row's semantics).
        rec3 = approval_of(snapshot(client, 3), a)
        assert rec3["status_as_of"] == "revoked"
        assert rec3["revoked_at_revision"] == 3
        assert rec3["application"]["status"] == "revoked"
        assert rec3["application"]["approval_revision"] == 1
        assert rec3["application"]["suspended_revision"] == 2
        assert rec3["application"]["force_suspended_by"] is None

        # The revocation does not rewrite the suspended view at revision 2.
        rec2 = approval_of(snapshot(client, 2), a)
        assert rec2["application"]["status"] == "suspended"
        assert rec2["application"]["suspended_revision"] == 2

        live = client.get(f"/api/applications/{a}").json()
        assert live["status"] == "revoked"
        assert live["suspended_revision"] == 2

    def test_multiple_cycles_each_revision_keeps_its_own_view(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # 1
        suspend(client, a, 1)  # 2
        resume(client, a, 2)  # 3
        suspend(client, a, 3)  # 4

        views = {
            1: ("approved", None),
            2: ("suspended", 2),
            3: ("approved", None),
            4: ("suspended", 4),
        }
        for revision, (status, suspended_revision) in views.items():
            record = approval_of(snapshot(client, revision), a)
            assert record["application"]["status"] == status
            assert (
                record["application"]["suspended_revision"]
                == suspended_revision
            )
            assert record["application"]["approval_revision"] == 1


class TestComparisonEmbeddedApplication:
    def test_approval_records_are_as_of_the_target_side(self, client):
        a = submit(client, 100, 110, "A")
        approve(client, a, 0)  # 1
        suspend(client, a, 1)  # 2

        # The approval event falls inside (0, 2]; its embedded application
        # is the replay as of the newer compared revision (2), not the live
        # row.
        body = compare(client, 0, 2)
        (record,) = body["approvals"]
        assert record["revision"] == 1
        assert record["status_as_of"] == "suspended"
        assert record["application"]["status"] == "suspended"
        assert record["application"]["suspended_revision"] == 2
        diff_record = next(
            e for e in body["differences"] if e["type"] == "approval"
        )
        assert diff_record["application"]["status"] == "suspended"
        assert diff_record["application"]["suspended_revision"] == 2

        # A later resume (revision 3) must not rewrite the comparison.
        resume(client, a, 2)  # 3
        again = compare(client, 0, 2)
        (record,) = again["approvals"]
        assert record["application"]["status"] == "suspended"
        assert record["application"]["suspended_revision"] == 2

        # Comparing against the newer endpoint replays up to revision 3.
        forward = compare(client, 0, 3)
        (record,) = forward["approvals"]
        assert record["application"]["status"] == "approved"
        assert record["application"]["suspended_revision"] is None

    def test_comparison_with_application_filter(self, client):
        a = submit(client, 100, 110, "A")
        b = submit(client, 200, 210, "B")
        approve(client, a, 0)  # 1
        approve(client, b, 1)  # 2
        suspend(client, b, 2)  # 3

        body = compare(client, 0, 3, application_id=b)
        (record,) = body["approvals"]
        assert record["application_id"] == b
        assert record["application"]["status"] == "suspended"
        assert record["application"]["suspended_revision"] == 3
        # A later resume of B must not rewrite the filtered comparison.
        resume(client, b, 3)  # 4
        again = compare(client, 0, 3, application_id=b)
        (record,) = again["approvals"]
        assert record["application"]["status"] == "suspended"
        assert record["application"]["suspended_revision"] == 3


class TestEmergencyEmbeddedApplication:
    def test_force_suspension_source_is_replayed_per_revision(self, client):
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)  # 1
        em = submit(client, 104, 109, "emergency")
        confirm_emergency(client, em, 1)  # A forced 2, em approved 3

        # While the occupation is active the embedded application names the
        # emergency application that forced the license out.
        rec2 = approval_of(snapshot(client, 2), a)
        assert rec2["application"]["status"] == "suspended"
        assert rec2["application"]["suspended_revision"] == 2
        assert rec2["application"]["force_suspended_by"] == em
        rec3 = approval_of(snapshot(client, 3), a)
        assert rec3["application"]["force_suspended_by"] == em

        # Releasing the occupation (em revoked 4, A restored 5) must not
        # rewrite the revisions recorded while it was active.
        release_emergency(client, em, 3)
        rec2_after = approval_of(snapshot(client, 2), a)
        assert rec2_after["application"]["status"] == "suspended"
        assert rec2_after["application"]["force_suspended_by"] == em
        rec3_after = approval_of(snapshot(client, 3), a)
        assert rec3_after["application"]["force_suspended_by"] == em

        # At the release revision the license is an ordinary suspension
        # again (its restoration lands on the next revision).
        rec4 = approval_of(snapshot(client, 4), a)
        assert rec4["application"]["status"] == "suspended"
        assert rec4["application"]["suspended_revision"] == 2
        assert rec4["application"]["force_suspended_by"] is None
        rec5 = approval_of(snapshot(client, 5), a)
        assert rec5["application"]["status"] == "approved"
        assert rec5["application"]["suspended_revision"] is None
        assert rec5["application"]["force_suspended_by"] is None

        # The emergency application's own record replays approved -> revoked.
        em_rec3 = approval_of(snapshot(client, 3), em)
        assert em_rec3["application"]["status"] == "approved"
        assert em_rec3["application"]["approval_revision"] == 3
        em_rec5 = approval_of(snapshot(client, 5), em)
        assert em_rec5["application"]["status"] == "revoked"

        # Live state still comes from the live row.
        live = client.get(f"/api/applications/{a}").json()
        assert live["status"] == "approved"
        assert live["force_suspended_by"] is None

    def test_blocked_restoration_becomes_ordinary_suspension_as_of_release(
        self, client
    ):
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)  # 1
        em = submit(client, 105, 106, "emergency")
        confirm_emergency(client, em, 1)  # A forced 2, em approved 3
        # C interferes with A but not with the emergency license (band gap
        # equals the guard band), so it is approved while A is forced out.
        c = submit(client, 108, 109, "C")
        approve(client, c, 3)  # 4

        released = release_emergency(client, em, 4)  # em revoked 5; A blocked
        assert released["blocked_application_ids"] == [a]

        # At the release revision A stays suspended but the emergency marker
        # is already lifted (it is an ordinary outage from then on).
        rec5 = approval_of(snapshot(client, 5), a)
        assert rec5["application"]["status"] == "suspended"
        assert rec5["application"]["suspended_revision"] == 2
        assert rec5["application"]["force_suspended_by"] is None
        # The revisions during the occupation still name the source.
        rec2 = approval_of(snapshot(client, 2), a)
        assert rec2["application"]["force_suspended_by"] == em

        # A later ordinary resume must not rewrite those views either.
        revoke(client, c)  # 6
        resume(client, a, 6)  # 7
        rec5_after = approval_of(snapshot(client, 5), a)
        assert rec5_after["application"]["status"] == "suspended"
        assert rec5_after["application"]["force_suspended_by"] is None
        rec2_after = approval_of(snapshot(client, 2), a)
        assert rec2_after["application"]["force_suspended_by"] == em

        live = client.get(f"/api/applications/{a}").json()
        assert live["status"] == "approved"
        assert live["force_suspended_by"] is None

    def test_comparison_replays_the_force_suspension_source(self, client):
        a = submit(client, 100, 110, "ordinary A")
        approve(client, a, 0)  # 1
        em = submit(client, 104, 109, "emergency")
        confirm_emergency(client, em, 1)  # 2, 3

        body = compare(client, 0, 3)
        (record,) = [
            e for e in body["approvals"] if e["application_id"] == a
        ]
        assert record["application"]["status"] == "suspended"
        assert record["application"]["force_suspended_by"] == em

        # After the release the same comparison still replays the occupation.
        release_emergency(client, em, 3)  # 4, 5
        again = compare(client, 0, 3)
        (record,) = [
            e for e in again["approvals"] if e["application_id"] == a
        ]
        assert record["application"]["status"] == "suspended"
        assert record["application"]["force_suspended_by"] == em
