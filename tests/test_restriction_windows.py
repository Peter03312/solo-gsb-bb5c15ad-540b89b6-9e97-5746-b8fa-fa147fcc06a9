"""End-to-end tests for temporary restriction windows.

Flow: an operator rehearses a window (read-only preview listing the hit
effective licenses and standby applications plus the base revision), then
confirms it with the expected revision (one new revision).  Confirmed
windows leave existing licenses effective but block, from that revision on,
pending previews, single/batch approvals, resumptions and post-release
waitlist auto-approvals; releasing the window with its id and expected
revision consumes one more revision and affects only later adjudication.

All applications use the default 2026-10-01 08:00-12:00 UTC window from the
test factories; restriction windows in these tests use a future day so the
not-already-expired rule never trips.
"""
from factories import make_payload


FUTURE_START = "2030-10-01T08:00:00Z"
FUTURE_END = "2030-10-01T12:00:00Z"


def submit(client, low, high, name="Event", **overrides):
    payload = make_payload(
        freq=(low, high), name=name, start=FUTURE_START, end=FUTURE_END, **overrides
    )
    r = client.post("/api/applications", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def window_payload(
    lo=105.0,
    hi=108.0,
    start="2030-10-01T09:00:00Z",
    end="2030-10-01T11:00:00Z",
    radius=100.0,
    reason="temporary control",
):
    return {
        "circle": {"cx": 0.0, "cy": 0.0, "radius": radius},
        "frequency": {"low": lo, "high": hi},
        "time_window": {"start": start, "end": end},
        "reason": reason,
    }


def preview_window(client, **kw):
    return client.post(
        "/api/restriction-windows/preview", json=window_payload(**kw)
    )


def activate_window(client, expected_revision, **kw):
    return client.post(
        "/api/restriction-windows",
        json={**window_payload(**kw), "expected_revision": expected_revision},
    )


def release_window(client, window_id, expected_revision, reason="control ended"):
    return client.post(
        f"/api/restriction-windows/{window_id}/release",
        json={"expected_revision": expected_revision, "reason": reason},
    )


def current_revision(client):
    return client.get("/api/revision/current").json()["current_revision"]


class TestWindowPreview:
    def test_preview_lists_hit_effective_licenses_and_standbys(self, client):
        a = submit(client, 100, 110, "A")
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()  # rev 1
        b = submit(client, 106, 107, "B")
        d = submit(client, 200, 210, "D")
        client.post(f"/api/applications/{b}/waitlist", json={}).raise_for_status()
        client.post(f"/api/applications/{d}/waitlist", json={}).raise_for_status()

        r = preview_window(client)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["current_revision"] == 1
        assert body["hit_effective_application_ids"] == [a]
        assert body["hit_waitlist_application_ids"] == [b]
        hit = body["hit_effective"][0]
        assert hit["trigger"] == {
            "time_intersect": True,
            "area_intersect": True,
            "frequency_intersect": True,
        }
        # The standby outside the window band is not listed.
        assert d not in body["hit_waitlist_application_ids"]

    def test_preview_is_non_mutating(self, client):
        submit(client, 100, 110)
        before = current_revision(client)
        preview_window(client).raise_for_status()
        assert current_revision(client) == before
        assert client.get("/api/restriction-windows").json()["active_windows"] == []

    def test_band_endpoint_touch_is_a_hit(self, client):
        a = submit(client, 100, 110, "A")
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()
        # Window band begins exactly at A's high bound (closed: touch hits).
        body = preview_window(client, lo=110, hi=120).json()
        assert body["hit_effective_application_ids"] == [a]

    def test_band_gap_is_not_a_hit_guard_band_not_extended(self, client):
        a = submit(client, 100, 110, "A")
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()
        # A 1 MHz gap is within the 2 MHz guard band but windows don't use it.
        body = preview_window(client, lo=111, hi=112).json()
        assert body["hit_effective_application_ids"] == []

    def test_half_open_time_boundary_is_not_a_hit(self, client):
        a = submit(client, 100, 110, "A")
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()
        body = preview_window(
            client,
            start="2030-10-01T12:00:00Z",
            end="2030-10-01T13:00:00Z",
        ).json()
        assert body["hit_effective_application_ids"] == []

    def test_expired_proposal_rejected_without_changes(self, client):
        r = preview_window(
            client,
            start="2000-01-01T00:00:00Z",
            end="2000-01-01T01:00:00Z",
        )
        assert r.status_code == 422
        details = r.json()["error"]["details"]
        assert any(
            d["code"] == "restriction_window_expired" for d in details
        )
        assert current_revision(client) == 0

    def test_illegal_shape_rejected_with_all_violations(self, client):
        r = client.post(
            "/api/restriction-windows/preview",
            json={
                "circle": {"cx": 0, "cy": 0, "radius": -3},
                "frequency": {"low": 200, "high": 100},
                "time_window": {
                    "start": "2030-10-01T12:00:00Z",
                    "end": "2030-10-01T11:00:00Z",
                },
            },
        )
        assert r.status_code == 422
        codes = {d["code"] for d in r.json()["error"]["details"]}
        assert {
            "radius_not_positive",
            "frequency_low_above_high",
            "time_start_not_before_end",
        } <= codes


class TestWindowActivation:
    def test_confirm_consumes_one_revision_and_lists_hits(self, client):
        a = submit(client, 100, 110, "A")
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()  # rev 1

        r = activate_window(client, 1)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["activated"] is True
        assert body["revision"] == 2
        assert isinstance(body["window_id"], int)
        assert body["window"]["revision"] == 2
        assert body["hit_effective_application_ids"] == [a]
        assert current_revision(client) == 2

    def test_stale_revision_refuses(self, client):
        a = submit(client, 100, 110, "A")
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()  # rev 1
        r = activate_window(client, 0)
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "stale_revision"
        assert err["details"]["current_revision"] == 1
        assert current_revision(client) == 1
        assert client.get("/api/restriction-windows").json()["active_windows"] == []

    def test_expired_confirm_refused_without_revision(self, client):
        payload = {
            **window_payload(
                start="2000-01-01T00:00:00Z", end="2000-01-01T01:00:00Z"
            ),
            "expected_revision": 0,
        }
        r = client.post("/api/restriction-windows", json=payload)
        assert r.status_code == 422
        assert any(
            d["code"] == "restriction_window_expired"
            for d in r.json()["error"]["details"]
        )
        assert current_revision(client) == 0

    def test_existing_hit_license_stays_effective(self, client):
        a = submit(client, 100, 110, "A")
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()
        activate_window(client, 1).raise_for_status()
        current = client.get("/api/revision/current").json()
        assert current["effective_application_ids"] == [a]
        app = client.get(f"/api/applications/{a}").json()
        assert app["status"] == "approved"


class TestWindowEnforcement:
    def _active_window(self, client, expected=0, **kw):
        r = activate_window(client, expected, **kw)
        assert r.status_code == 201, r.text
        return r.json()["window_id"]

    def test_pending_preview_reports_window_without_changing_conflicts_field(
        self, client
    ):
        # License D [200,210] is effective but disjoint from the window band
        # [105,108], so the pending app B only hits the window.
        d = submit(client, 200, 210, "D")
        client.post(
            f"/api/applications/{d}/approve", json={"expected_revision": 0}
        ).raise_for_status()  # rev 1
        self._active_window(client, expected=1)  # rev 2
        b = submit(client, 106, 107, "B")

        body = client.post(f"/api/applications/{b}/preview").json()
        assert body["has_conflicts"] is False
        assert body["conflicts"] == []
        assert body["has_restriction_window_conflicts"] is True
        hits = body["restriction_window_conflicts"]
        assert len(hits) == 1
        assert hits[0]["reason"] == "restriction_window"
        assert hits[0]["window"]["frequency"] == {"low": 105.0, "high": 108.0}
        assert hits[0]["trigger"] == {
            "time_intersect": True,
            "area_intersect": True,
            "frequency_intersect": True,
        }
        assert body["active_restriction_window_ids"] == [1]

    def test_single_approval_blocked_by_window(self, client):
        self._active_window(client, 0)  # rev 1
        b = submit(client, 106, 107, "B")
        r = client.post(
            f"/api/applications/{b}/approve", json={"expected_revision": 1}
        )
        assert r.status_code == 409, r.text
        err = r.json()["error"]
        assert err["code"] == "restriction_window"
        assert len(err["details"]["restriction_window_conflicts"]) == 1
        # License-conflict details are still present (empty here) so the
        # error envelope stays compatible.
        assert err["details"]["conflicts"] == []
        # No revision consumed; the application stays pending.
        assert current_revision(client) == 1
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"

    def test_touching_band_approval_blocked(self, client):
        self._active_window(client, 0, lo=110, hi=120)  # rev 1
        b = submit(client, 100, 110, "B")
        r = client.post(
            f"/api/applications/{b}/approve", json={"expected_revision": 1}
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "restriction_window"

    def test_non_hit_application_approved_normally(self, client):
        self._active_window(client, 0)  # rev 1
        b = submit(client, 300, 310, "B")
        r = client.post(
            f"/api/applications/{b}/approve", json={"expected_revision": 1}
        )
        assert r.status_code == 200, r.text
        assert r.json()["revision"] == 2

    def test_batch_approval_blocked_whole_group_rejected(self, client):
        self._active_window(client, 0)  # rev 1
        hit = submit(client, 106, 107, "hit")
        clean = submit(client, 300, 310, "clean")
        r = client.post(
            "/api/applications/batch-approve",
            json={"expected_revision": 1, "application_ids": [hit, clean]},
        )
        assert r.status_code == 409, r.text
        err = r.json()["error"]
        assert err["code"] == "restriction_window"
        apps = err["details"]["applications"]
        by_id = {a["application_id"]: a for a in apps}
        assert len(by_id[hit]["restriction_window_conflicts"]) == 1
        assert clean not in by_id
        assert current_revision(client) == 1
        for app_id in (hit, clean):
            assert client.get(f"/api/applications/{app_id}").json()["status"] == "pending"

    def test_batch_with_license_interference_keeps_interference_code(self, client):
        # Effective license A [100,110]; window [305,308].  Batch contains
        # B clashing with A (license interference) and C hitting the window.
        a = submit(client, 100, 110, "A")
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()  # rev 1
        self._active_window(client, 1, lo=305, hi=308)  # rev 2
        b = submit(client, 100, 110, "B")
        c = submit(client, 306, 307, "C")
        r = client.post(
            "/api/applications/batch-approve",
            json={"expected_revision": 2, "application_ids": [b, c]},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "interference"

    def test_resume_blocked_while_window_hits(self, client):
        a = submit(client, 100, 110, "A")
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()  # rev 1
        client.post(
            f"/api/applications/{a}/suspend",
            json={"expected_revision": 1, "reason": "downtime"},
        ).raise_for_status()  # rev 2
        wid = self._active_window(client, 2)  # rev 3

        r = client.post(
            f"/api/applications/{a}/resume", json={"expected_revision": 3}
        )
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "restriction_window"
        assert err["details"]["restriction_window_conflicts"][0]["window"]["id"] == wid
        # Stays suspended, revision untouched.
        assert client.get(f"/api/applications/{a}").json()["status"] == "suspended"
        assert current_revision(client) == 3

    def test_available_ranges_excludes_window_positions(self, client):
        self._active_window(client, 0, lo=105, hi=108)  # rev 1
        b = submit(client, 50, 55, "B")  # width 5
        r = client.post(
            f"/api/applications/{b}/available-ranges",
            json={"candidate_low_min": 90, "candidate_low_max": 115},
        )
        assert r.status_code == 200, r.text
        ranges = [
            (x["min"], x["max"], x["min_inclusive"], x["max_inclusive"])
            for x in r.json()["available_ranges"]
        ]
        # Window forbids closed x in [105-5, 108] = [100,108].
        assert ranges == [
            (90.0, 100.0, True, False),
            (108.0, 115.0, False, True),
        ]


class TestWaitlistDrainWithWindow:
    def test_blocked_standby_keeps_place_and_drain_continues(self, client):
        a = submit(client, 100, 110, "A")
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()  # rev 1
        # B is disjoint from A in frequency but inside the future window band;
        # D is disjoint from everyone.
        b = submit(client, 106, 107, "B")
        d = submit(client, 300, 310, "D")
        client.post(f"/api/applications/{b}/waitlist", json={}).raise_for_status()
        client.post(f"/api/applications/{d}/waitlist", json={}).raise_for_status()
        activate_window(client, 1).raise_for_status()  # rev 2

        # Suspend A (release, rev 3): B would be free of licenses but hits
        # the window -> stays queued; D passes -> auto approved rev 4.
        r = client.post(
            f"/api/applications/{a}/suspend",
            json={"expected_revision": 2, "reason": "re-tune"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["remaining_waitlist_application_ids"] == [b]
        assert body["auto_approved_application_ids"] == [d]
        assert body["final_revision"] == 4
        pending = body["waitlist_pending"][0]
        assert pending["application_id"] == b
        assert pending["conflicts"] == []
        assert len(pending["restriction_window_conflicts"]) == 1
        assert client.get("/api/waitlist").json()["waitlist_application_ids"] == [b]

    def test_releasing_window_does_not_auto_approve(self, client):
        a = submit(client, 100, 110, "A")
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()  # rev 1
        b = submit(client, 106, 107, "B")
        client.post(f"/api/applications/{b}/waitlist", json={}).raise_for_status()
        wid = activate_window(client, 1).json()["window_id"]  # rev 2

        r = release_window(client, wid, 2)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["revision"] == 3
        assert "auto_approved_application_ids" not in body
        assert client.get("/api/waitlist").json()["waitlist_application_ids"] == [b]
        assert client.get(f"/api/applications/{b}").json()["status"] == "pending"

    def test_after_release_later_release_drains_normally(self, client):
        # Window lifted; a subsequent license release then auto-approves the
        # queued application.
        a = submit(client, 100, 110, "A")
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()  # rev 1
        b = submit(client, 100, 110, "B")
        client.post(f"/api/applications/{b}/waitlist", json={}).raise_for_status()
        wid = activate_window(client, 1).json()["window_id"]  # rev 2
        release_window(client, wid, 2).raise_for_status()  # rev 3

        r = client.post(
            f"/api/applications/{a}/revoke", json={"reason": "gone"}
        )
        assert r.status_code == 200, r.text
        assert r.json()["auto_approved_application_ids"] == [b]


class TestWindowRelease:
    def test_release_consumes_one_revision_and_lifts_enforcement(self, client):
        wid = activate_window(client, 0).json()["window_id"]  # rev 1
        b = submit(client, 106, 107, "B")
        blocked = client.post(
            f"/api/applications/{b}/approve", json={"expected_revision": 1}
        )
        assert blocked.status_code == 409

        r = release_window(client, wid, 1)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["released"] is True
        assert body["window_id"] == wid
        assert body["revision"] == 2
        assert body["activated_at_revision"] == 1

        # After release the same pending application adjudicates normally.
        ok = client.post(
            f"/api/applications/{b}/approve", json={"expected_revision": 2}
        )
        assert ok.status_code == 200, ok.text

    def test_release_unknown_window_404_consumes_nothing(self, client):
        r = release_window(client, 9999, 0)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "restriction_window_not_found"
        assert current_revision(client) == 0

    def test_release_already_released_409(self, client):
        wid = activate_window(client, 0).json()["window_id"]  # rev 1
        release_window(client, wid, 1).raise_for_status()  # rev 2
        r = release_window(client, wid, 2)
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "restriction_window_not_active"
        assert err["details"]["released_at_revision"] == 2
        assert current_revision(client) == 2

    def test_release_stale_revision_consumes_nothing(self, client):
        wid = activate_window(client, 0).json()["window_id"]  # rev 1
        r = release_window(client, wid, 0)
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "stale_revision"
        assert current_revision(client) == 1
        # Window is still active.
        assert client.get(f"/api/restriction-windows/{wid}").json()["window"]["status"] == "active"

    def test_release_shares_revision_arbitration_with_approval(self, client):
        wid = activate_window(client, 0).json()["window_id"]  # rev 1
        # An approval advancing the revision first makes the release stale.
        b = submit(client, 300, 310, "B")
        client.post(
            f"/api/applications/{b}/approve", json={"expected_revision": 1}
        ).raise_for_status()  # rev 2
        r = release_window(client, wid, 1)
        assert r.status_code == 409
        assert r.json()["error"]["details"]["current_revision"] == 2


class TestWindowQueriesAndHistory:
    def test_list_and_get_windows(self, client):
        wid = activate_window(
            client, 0, reason="drone show"
        ).json()["window_id"]
        listing = client.get("/api/restriction-windows").json()
        assert listing["active_restriction_window_ids"] == [wid]
        assert len(listing["active_windows"]) == 1
        assert listing["active_windows"][0]["reason"] == "drone show"

        one = client.get(f"/api/restriction-windows/{wid}").json()["window"]
        assert one["status"] == "active"
        assert one["id"] == wid

        assert client.get("/api/restriction-windows/9999").status_code == 404

    def test_inactive_listing_reconstructs_released_windows(self, client):
        wid = activate_window(client, 0).json()["window_id"]  # rev 1
        release_window(client, wid, 1).raise_for_status()  # rev 2
        listing = client.get("/api/restriction-windows?include_inactive=true").json()
        assert listing["active_windows"] == []
        released = listing["released_windows"]
        assert len(released) == 1
        assert released[0]["id"] == wid
        assert released[0]["status"] == "released"
        assert released[0]["activated_at_revision"] == 1
        assert released[0]["released_at_revision"] == 2

    def test_snapshot_restores_active_and_released_windows(self, client):
        a = submit(client, 100, 110, "A")
        client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 0}
        ).raise_for_status()  # rev 1
        wid = activate_window(client, 1).json()["window_id"]  # rev 2
        release_window(client, wid, 2).raise_for_status()  # rev 3

        s1 = client.get("/api/revision/1").json()
        assert s1["active_restriction_window_ids"] == []
        assert s1["restriction_window_events"] == []

        s2 = client.get("/api/revision/2").json()
        assert s2["active_restriction_window_ids"] == [wid]
        event = s2["restriction_window_events"][0]
        assert event["event_type"] == "activated"
        assert event["window_id"] == wid
        assert event["window"]["frequency"] == {"low": 105.0, "high": 108.0}
        # Original approval event is not rewritten.
        assert s2["approvals"][0]["revision"] == 1
        assert s2["approvals"][0]["status_as_of"] == "approved"

        s3 = client.get("/api/revision/3").json()
        assert s3["active_restriction_window_ids"] == []
        assert [e["event_type"] for e in s3["restriction_window_events"]] == [
            "activated",
            "released",
        ]

        current = client.get("/api/revision/current").json()
        assert current["active_restriction_window_ids"] == []
        assert len(current["restriction_window_events"]) == 2

    def test_compare_reports_window_changes(self, client):
        wid = activate_window(client, 0).json()["window_id"]  # rev 1
        release_window(client, wid, 1).raise_for_status()  # rev 2

        forward = client.get(
            "/api/revision/compare?base_revision=0&target_revision=1"
        ).json()
        assert forward["active_restriction_window_ids"] == {
            "base": [],
            "target": [wid],
            "added": [wid],
            "removed": [],
            "added_to_target": [wid],
            "removed_from_base": [],
        }
        types = [e["type"] for e in forward["restriction_window_events"]]
        assert types == ["restriction_window_activated"]
        assert any(
            e["type"] == "restriction_window_activated"
            for e in forward["differences"]
        )
        assert forward["base"]["active_restriction_window_ids"] == []
        assert forward["target"]["active_restriction_window_ids"] == [wid]

        backward = client.get(
            "/api/revision/compare?base_revision=2&target_revision=1"
        ).json()
        assert backward["direction"] == "backward"
        # Added/removed are expressed against the caller-labelled sides:
        # target (rev1) still has the window, base (rev2) does not.
        assert backward["active_restriction_window_ids"]["added"] == [wid]
        assert backward["active_restriction_window_ids"]["removed"] == []
        assert any(
            e["type"] == "restriction_window_released"
            for e in backward["restriction_window_events"]
        )
        # The caller-labelled base/target views keep their own revision's set.
        assert backward["base"]["active_restriction_window_ids"] == []
        assert backward["target"]["active_restriction_window_ids"] == [wid]
