#!/usr/bin/env bash
# End-to-end curl walkthrough for the Temporary Event Radio Spectrum API.
# Usage: ./examples/walkthrough.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8000}"
J() { python3 -m json.tool; }

post() { curl -s -X POST "$BASE$1" -H 'Content-Type: application/json' -d "${2:-}"; }

echo "== health =="
curl -s "$BASE/health"; echo

echo "== 1. invalid submission (422, whole request rejected) =="
post /api/applications '{
  "name":"bad",
  "circle":{"cx":0,"cy":0,"radius":-5},
  "frequency":{"low":200,"high":100},
  "time_window":{"start":"2026-10-01T10:00:00Z","end":"2026-10-01T08:00:00Z"}
}' | J

echo "== 2. submit A =="
A=$(post /api/applications '{
  "name":"Festival A",
  "circle":{"cx":0,"cy":0,"radius":1000},
  "frequency":{"low":100,"high":110},
  "time_window":{"start":"2026-10-01T08:00:00Z","end":"2026-10-01T12:00:00Z"}
}')
echo "$A" | J
A_ID=$(echo "$A" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

echo "== 3. preview A =="
post "/api/applications/$A_ID/preview" | J

echo "== 4. approve A at revision 0 =="
post "/api/applications/$A_ID/approve" '{"expected_revision":0}' | J

echo "== 5. submit B (1 MHz gap < 2 MHz guard band) =="
B=$(post /api/applications '{
  "name":"Marathon B",
  "circle":{"cx":100,"cy":0,"radius":1000},
  "frequency":{"low":111,"high":112},
  "time_window":{"start":"2026-10-01T09:00:00Z","end":"2026-10-01T11:00:00Z"}
}')
B_ID=$(echo "$B" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$B_ID/preview" | J

echo "== 6. stale approval (expected 0, current 1) =="
post "/api/applications/$B_ID/approve" '{"expected_revision":0}' | J || true

echo "== 7. current revision but interference =="
post "/api/applications/$B_ID/approve" '{"expected_revision":1}' | J || true

echo "== 8. revoke A =="
post "/api/applications/$A_ID/revoke" '{"reason":"postponed"}' | J

echo "== 9. approve B at revision 2 =="
post "/api/applications/$B_ID/approve" '{"expected_revision":2}' | J

echo "== 10. history snapshots =="
curl -s "$BASE/api/revision/1" | J
curl -s "$BASE/api/revision/2" | J
curl -s "$BASE/api/revision/current" | J

echo "== 11. submit three more devices for one event (disjoint bands) =="
mk() { post /api/applications "{
  \"name\":\"$1\",
  \"circle\":{\"cx\":0,\"cy\":0,\"radius\":1000},
  \"frequency\":{\"low\":$2,\"high\":$3},
  \"time_window\":{\"start\":\"2026-10-01T13:00:00Z\",\"end\":\"2026-10-01T18:00:00Z\"}
}"; }
C=$(mk "Device C" 200 210); C_ID=$(echo "$C" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
D=$(mk "Device D" 300 310); D_ID=$(echo "$D" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
E=$(mk "Device E" 400 410); E_ID=$(echo "$E" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
echo "ids: $C_ID $D_ID $E_ID"

echo "== 12. batch approve at revision 3, input order D,C,E -> revisions 4,5,6 =="
post /api/applications/batch-approve \
  "{\"expected_revision\":3,\"application_ids\":[$D_ID,$C_ID,$E_ID]}" | J

echo "== 13. snapshot as of the second item (revision 5) has D and C effective =="
curl -s "$BASE/api/revision/5" | J

echo "== 14. failing batch: two members clash with each other -> whole group rejected =="
F=$(mk "Device F" 500 510); F_ID=$(echo "$F" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
G=$(mk "Device G" 505 515); G_ID=$(echo "$G" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
H=$(mk "Device H" 600 610); H_ID=$(echo "$H" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post /api/applications/batch-approve \
  "{\"expected_revision\":6,\"application_ids\":[$F_ID,$G_ID,$H_ID]}" | J || true
echo "revision is still 6 and all three are pending:"
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print("current_revision =", json.load(sys.stdin)["current_revision"])'

echo "== 15. malformed batches (duplicate ids / missing id) =="
post /api/applications/batch-approve \
  "{\"expected_revision\":6,\"application_ids\":[$F_ID,$F_ID]}" | J || true
post /api/applications/batch-approve \
  "{\"expected_revision\":6,\"application_ids\":[999999]}" | J || true

echo "== 16. guard band: current config (live value, initial value, revision) =="
curl -s "$BASE/api/config/guard-band" | J

echo "== 17. preview a wider band (100): pairs among the effective grants appear =="
echo "    (devices C/D/E sit 90 MHz apart; B's time window does not overlap theirs)"
post /api/config/guard-band/preview '{"guard_band":100}' | J

echo "== 18. confirming it is refused (409 interference); nothing changes =="
post /api/config/guard-band '{"guard_band":100,"expected_revision":6}' | J || true
curl -s "$BASE/api/config/guard-band" | J

echo "== 19. tighten to 1.0: accepted, exactly one new revision (7) =="
post /api/config/guard-band '{"guard_band":1.0,"expected_revision":6}' | J

echo "== 20. the change shares revision arbitration: stale confirms/approvals lose =="
post /api/config/guard-band '{"guard_band":0.5,"expected_revision":6}' | J || true
post "/api/applications/$F_ID/approve" '{"expected_revision":6}' | J || true

echo "== 21. invalid values are rejected with 422 (no revision consumed) =="
post /api/config/guard-band '{"guard_band":-5,"expected_revision":7}' | J || true

echo "== 22. history restores the band in force at each revision =="
curl -s "$BASE/api/revision/6" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("revision", d["revision"], "guard_band =", d["guard_band"])'
curl -s "$BASE/api/revision/7" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("revision", d["revision"], "guard_band =", d["guard_band"], "changes =", [(c["revision"], c["previous_guard_band"], c["guard_band"]) for c in d["guard_band_changes"]])'
echo "    approval events keep the band they were decided under (not rewritten):"
curl -s "$BASE/api/revision/7" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("approval guard_bands =", [(a["revision"], a["guard_band"]) for a in d["approvals"]])'
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("current_revision =", d["current_revision"], "guard_band =", d["guard_band"])'

echo "== 23. available-ranges search for pending device F (band [500,510], width 10) =="
echo "    effective grants in its 13:00-18:00 window: C [200,210], D [300,310], E [400,410]; guard band is now 1.0"
post "/api/applications/$F_ID/available-ranges" \
  '{"candidate_low_min":180,"candidate_low_max":430}' | J
echo "    forbidden by C: (200-10-1, 210+1) = (189,211); by D: (289,311); by E: (389,411)"
echo "    -> free lower bounds: [180,189], [211,289], [311,389], [411,430] (endpoints attainable)"

echo "== 24. a reported endpoint previews conflict-free; a forbidden point does not =="
SHIFTED=$(post /api/applications "{
  \"name\":\"F shifted to 211\",
  \"circle\":{\"cx\":0,\"cy\":0,\"radius\":1000},
  \"frequency\":{\"low\":211,\"high\":221},
  \"time_window\":{\"start\":\"2026-10-01T13:00:00Z\",\"end\":\"2026-10-01T18:00:00Z\"}
}")
SHIFTED_ID=$(echo "$SHIFTED" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$SHIFTED_ID/preview" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("shifted to 211 (gap == guard): has_conflicts =", d["has_conflicts"])'
CLASH=$(post /api/applications "{
  \"name\":\"F shifted to 200\",
  \"circle\":{\"cx\":0,\"cy\":0,\"radius\":1000},
  \"frequency\":{\"low\":200,\"high\":210},
  \"time_window\":{\"start\":\"2026-10-01T13:00:00Z\",\"end\":\"2026-10-01T18:00:00Z\"}
}")
CLASH_ID=$(echo "$CLASH" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$CLASH_ID/preview" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("shifted to 200 (inside forbidden): has_conflicts =", d["has_conflicts"])'

echo "== 25. the search is non-mutating and validates its inputs =="
post "/api/applications/$F_ID/available-ranges" \
  '{"candidate_low_min":430,"candidate_low_max":180}' | J || true
post "/api/applications/999999/available-ranges" \
  '{"candidate_low_min":180,"candidate_low_max":430}' | J || true
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("current_revision is still", d["current_revision"])'

echo "== 26. suspend C (temporary device outage): leaves the effective set, one revision (8) =="
post "/api/applications/$C_ID/suspend" \
  '{"expected_revision":7,"reason":"device downtime"}' | J
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("effective =", d["effective_application_ids"], "suspended =", d["suspended_application_ids"])'

echo "== 27. while C is suspended, a device I overlapping C's band [205,208] can be approved (9) =="
I=$(post /api/applications '{
  "name":"Device I",
  "circle":{"cx":0,"cy":0,"radius":1000},
  "frequency":{"low":205,"high":208},
  "time_window":{"start":"2026-10-01T13:00:00Z","end":"2026-10-01T18:00:00Z"}
}')
I_ID=$(echo "$I" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$I_ID/preview" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("has_conflicts =", d["has_conflicts"], "(C is suspended)")'
post "/api/applications/$I_ID/approve" '{"expected_revision":8}' | J

echo "== 28. resume C re-adjudicates: clashes with I -> 409 interference, C stays suspended =="
post "/api/applications/$C_ID/resume" '{"expected_revision":9}' | J || true
post "/api/applications/$C_ID/resume" '{"expected_revision":9}' >/dev/null || true
echo "    a stale resume returns the current revision and changes nothing either:"
post "/api/applications/$C_ID/resume" '{"expected_revision":8}' | J || true
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("current_revision is still", d["current_revision"], "; C status =", [a["status"] for a in d["suspended_applications"]])'

echo "== 29. permanently revoke I (revision 10), then resume C succeeds (revision 11) =="
post "/api/applications/$I_ID/revoke" '{"reason":"obsolete"}' | J
post "/api/applications/$C_ID/resume" \
  '{"expected_revision":10,"reason":"fault cleared"}' | J

echo "== 30. history restores suspend/resume records and each effective set =="
curl -s "$BASE/api/revision/8" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("rev 8: effective =", d["effective_application_ids"], "suspended =", d["suspended_application_ids"], "suspensions =", [(s["revision"], s["application_id"]) for s in d["suspensions"]])'
curl -s "$BASE/api/revision/9" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("rev 9: effective =", d["effective_application_ids"], "suspended =", d["suspended_application_ids"])'
curl -s "$BASE/api/revision/11" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("rev 11: effective =", d["effective_application_ids"], "suspended =", d["suspended_application_ids"]); print("        suspensions =", [(s["revision"], s["application_id"]) for s in d["suspensions"]], "resumptions =", [(s["revision"], s["application_id"], s["guard_band"]) for s in d["resumptions"]], "revocations =", [(s["revision"], s["application_id"]) for s in d["revocations"]])'

echo "== 31. unknown ids and illegal states are refused without consuming a revision =="
post /api/applications/999999/suspend '{"expected_revision":11}' | J || true
post "/api/applications/$H_ID/suspend" '{"expected_revision":11}' | J || true
post "/api/applications/$D_ID/resume" '{"expected_revision":11}' | J || true
post "/api/applications/$C_ID/suspend" '{"expected_revision":11}' >/dev/null
post "/api/applications/$C_ID/suspend" '{"expected_revision":12}' | J || true
post "/api/applications/$C_ID/resume" '{"expected_revision":12}' >/dev/null
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("current_revision =", d["current_revision"], "(the rejections consumed none)")'

echo "== 32. waitlist: register pending apps while the band is busy (no revision used) =="
echo "    effective in the 13:00-18:00 window: C [200,210], D [300,310], E [400,410]; guard band is 1.0"
X=$(mk "Standby X" 205 215); X_ID=$(echo "$X" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
Y=$(mk "Standby Y" 213 220); Y_ID=$(echo "$Y" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
Z=$(mk "Standby Z" 550 560); Z_ID=$(echo "$Z" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$X_ID/waitlist" '{"reason":"band busy"}' | J
post "/api/applications/$Y_ID/waitlist" | J
post "/api/applications/$Z_ID/waitlist" | J
echo "ids: X=$X_ID Y=$Y_ID Z=$Z_ID"
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("current_revision is still", d["current_revision"], "; queue =", d["waitlist_application_ids"])'

echo "== 33. duplicate / non-pending registration, lookup and cancel =="
post "/api/applications/$X_ID/waitlist" '{}' | J || true
post "/api/applications/$C_ID/waitlist" '{}' | J || true
post /api/applications/999999/waitlist '{}' | J || true
curl -s "$BASE/api/waitlist" | J
curl -s "$BASE/api/applications/$Y_ID/waitlist" | J
echo "    register F and cancel again (still no revision; application stays pending):"
post "/api/applications/$F_ID/waitlist" '{}' | J
post "/api/applications/$F_ID/waitlist/cancel" | J
curl -s "$BASE/api/applications/$F_ID" | python3 -c 'import json,sys; print("F status =", json.load(sys.stdin)["status"])'

echo "== 34. suspend C (revision 14) releases the band: queue drains in order =="
echo "    X [205,215] passes -> auto rev 15; Y [213,220] clashes with the just-approved X -> stays; Z [550,560] passes -> auto rev 16"
post "/api/applications/$C_ID/suspend" '{"expected_revision":13,"reason":"re-tune for standbys"}' | J

echo "== 35. history restores each automatic approval revision by revision =="
curl -s "$BASE/api/revision/15" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("rev 15: effective =", d["effective_application_ids"]); print("        autos =", [(a["application_id"], a["auto_approved"], a["release_revision"]) for a in d["approvals"]])'
curl -s "$BASE/api/revision/16" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("rev 16: effective =", d["effective_application_ids"], "suspended =", d["suspended_application_ids"])'

echo "== 36. revoke X (revision 17) releases again: Y now passes -> auto rev 18 =="
post "/api/applications/$X_ID/revoke" '{"reason":"moved away"}' | python3 -c 'import json,sys; d=json.load(sys.stdin); print({k: d[k] for k in ("revision","auto_approved_application_ids","remaining_waitlist_application_ids","final_revision")})'

echo "== 37. resume C (revision 19): ordinary resume, never drains a queue =="
post "/api/applications/$C_ID/resume" '{"expected_revision":18,"reason":"fault cleared"}' | python3 -c 'import json,sys; d=json.load(sys.stdin); print("resumed revision", d["revision"], "; waitlist keys present:", "auto_approved_application_ids" in d)'
curl -s "$BASE/api/waitlist" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("queue is now", d["waitlist_application_ids"], "at revision", d["current_revision"])'

echo "== 38. restriction window: rehearsal preview (read-only, no revision) =="
echo "    set the scene in a future-day window 2030-10-01 13:00-14:00:"
echo "    effective license J [100,110], standby K [106,107], clean standby L [200,210]"
mkfuture() { post /api/applications "{
  \"name\":\"$1\",
  \"circle\":{\"cx\":0,\"cy\":0,\"radius\":1000},
  \"frequency\":{\"low\":$2,\"high\":$3},
  \"time_window\":{\"start\":\"2030-10-01T13:00:00Z\",\"end\":\"2030-10-01T14:00:00Z\"}
}"; }
J=$(mkfuture "Future J" 100 110); J_ID=$(echo "$J" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
REV=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])')
post "/api/applications/$J_ID/approve" "{\"expected_revision\":$REV}" >/dev/null
REV=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])')
K=$(mkfuture "Future K" 106 107); K_ID=$(echo "$K" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
L=$(mkfuture "Future L" 200 210); L_ID=$(echo "$L" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$K_ID/waitlist" '{}' >/dev/null
post "/api/applications/$L_ID/waitlist" '{}' >/dev/null
WINDOW='{
  "circle":{"cx":0,"cy":0,"radius":1000},
  "frequency":{"low":105,"high":108},
  "time_window":{"start":"2030-10-01T13:00:00Z","end":"2030-10-01T14:00:00Z"},
  "reason":"VIP visit restriction"
}'
echo "    rehearsal lists the hit effective license and the hit standby (no mutation):"
post /api/restriction-windows/preview "$WINDOW" | J
post /api/restriction-windows/preview "$WINDOW" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("hit effective:", d["hit_effective_application_ids"], "hit waitlist:", d["hit_waitlist_application_ids"])'
echo "    touching band endpoints hit, but a gap inside the guard band does not:"
post /api/restriction-windows/preview "$(echo "$WINDOW" | sed 's/"low":105,"high":108/"low":110,"high":120/')" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("window [110,120] hits effective:", d["hit_effective_application_ids"])'
post /api/restriction-windows/preview "$(echo "$WINDOW" | sed 's/"low":105,"high":108/"low":111,"high":112/')" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("window [111,112] (1 MHz gap to J [100,110]) hits:", d["hit_effective_application_ids"], "(guard band never extends a window)")'
echo "    an already-expired proposal is rejected 422 (nothing changes):"
post /api/restriction-windows/preview '{"circle":{"cx":0,"cy":0,"radius":1000},"frequency":{"low":105,"high":108},"time_window":{"start":"2000-01-01T00:00:00Z","end":"2000-01-01T01:00:00Z"}}' \
  | python3 -c 'import json,sys; print([d["code"] for d in json.load(sys.stdin)["error"]["details"]])'
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print("current_revision is still", json.load(sys.stdin)["current_revision"])'

echo "== 39. confirm the window (expected = current revision); existing licenses stay effective =="
REV=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])')
post /api/restriction-windows "$(echo "$WINDOW" | sed 's/}$/,"expected_revision":'$REV'}/')" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("activated:", d["activated"], "window_id:", d["window_id"], "revision:", d["revision"])
print("hit effective:", d["hit_effective_application_ids"], "hit waitlist:", d["hit_waitlist_application_ids"])'
W_ID=$(curl -s "$BASE/api/restriction-windows" | python3 -c 'import json,sys; print(json.load(sys.stdin)["active_restriction_window_ids"][0])')
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("J still effective:", '$J_ID' in d["effective_application_ids"], "; active windows:", d["active_restriction_window_ids"])'
ACT_REV=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])')

echo "== 40. window blocks writes: pending preview flags it; approve/resume get 409 restriction_window =="
post "/api/applications/$K_ID/preview" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("K has (license) conflicts:", d["has_conflicts"], "; has window conflicts:", d["has_restriction_window_conflicts"], "; windows:", d["active_restriction_window_ids"], "(both reported separately)")'
post "/api/applications/$K_ID/approve" "{\"expected_revision\":$ACT_REV}" | python3 -c 'import json,sys; e=json.load(sys.stdin)["error"]; print("code:", e["code"], "; hit windows:", len(e["details"]["restriction_window_conflicts"]))' || true
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print("revision unchanged at", json.load(sys.stdin)["current_revision"])'
curl -s "$BASE/api/waitlist" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("queue (K kept, L still queued):", d["waitlist_application_ids"])'

echo "== 41. release J frees the band: K now passes licenses but hits the window -> stays; L auto-approved =="
post "/api/applications/$J_ID/revoke" '{"reason":"free up"}' | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("revoke revision:", d["revision"], "final:", d["final_revision"])
print("auto approved:", d["auto_approved_application_ids"], "(L clean); kept waiting:", d["remaining_waitlist_application_ids"], "(K hits window)")
print("K pending reason keys:", sorted(d["waitlist_pending"][0].keys()))'

echo "== 42. available-ranges exclude the window closed forbidden lower-bound interval =="
post "/api/applications/$K_ID/available-ranges" '{"candidate_low_min":100,"candidate_low_max":115}' \
  | python3 -c 'import json,sys; print([(r["min"],r["max"],r["min_inclusive"],r["max_inclusive"]) for r in json.load(sys.stdin)["available_ranges"]])'
echo "    (window [105,108], K width 1 -> closed forbidden x in [104,108]; both endpoints unattainable)"

echo "== 43. release the window: only later adjudication changes, no waitlist drain =="
REV=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])')
post "/api/restriction-windows/$W_ID/release" "{\"expected_revision\":$REV,\"reason\":\"control ended\"}" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("released:", d["released"], "revision:", d["revision"])'
REL_REV=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])')
post "/api/restriction-windows/$W_ID/release" "{\"expected_revision\":$REL_REV}" | python3 -c 'import json,sys; print("double release ->", json.load(sys.stdin)["error"]["code"])' || true
curl -s "$BASE/api/restriction-windows/999999" | python3 -c 'import json,sys; print("unknown window ->", json.load(sys.stdin)["error"]["code"])'
curl -s "$BASE/api/revision/$ACT_REV" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("rev", d["revision"], "active windows:", d["active_restriction_window_ids"], "; events:", [(e["event_type"],e["window_id"]) for e in d["restriction_window_events"]])'
curl -s "$BASE/api/revision/compare?base_revision=$ACT_REV&target_revision=$REL_REV" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("window removed:", d["active_restriction_window_ids"]["removed"], "; event types:", [e["type"] for e in d["restriction_window_events"]])'
curl -s "$BASE/api/restriction-windows?include_inactive=true" | python3 -c 'import json,sys; d=json.load(sys.stdin); w=d["released_windows"][0]; print("released window:", w["id"], w["status"], w["activated_at_revision"], "->", w["released_at_revision"])'
echo "    release did not auto-approve K; it is still queued and pending:"
curl -s "$BASE/api/waitlist" | python3 -c 'import json,sys; print("queue:", json.load(sys.stdin)["waitlist_application_ids"])'
post "/api/applications/$K_ID/approve" "{\"expected_revision\":$REL_REV}" | python3 -c 'import json,sys; print("after release, approving K now succeeds at revision", json.load(sys.stdin)["revision"])'

echo "== 44. active-device handover: preview (source excluded), confirm on consecutive revisions, FIFO re-adjudication =="
mkx() { post /api/applications "{
  \"name\":\"$1\",
  \"circle\":{\"cx\":0,\"cy\":0,\"radius\":10},
  \"frequency\":{\"low\":$2,\"high\":$3},
  \"time_window\":{\"start\":\"2026-12-15T08:00:00Z\",\"end\":\"2026-12-15T12:00:00Z\"}
}"; }
S=$(mkx "source-license" 400 410); SRC_ID=$(echo "$S" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
X=$(mkx "successor" 400 402); X_ID=$(echo "$X" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
Y=$(mkx "standby-pass" 430 432); Y_ID=$(echo "$Y" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
Z=$(mkx "standby-blocked" 431 433); Z_ID=$(echo "$Z" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
TR_REV=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])')
post "/api/applications/$SRC_ID/approve" "{\"expected_revision\":$TR_REV}" | python3 -c 'import json,sys; print("source approved at revision", json.load(sys.stdin)["revision"])'
post "/api/applications/$X_ID/waitlist" '{"reason":"waiting for source"}' > /dev/null
post "/api/applications/$Y_ID/waitlist" > /dev/null
post "/api/applications/$Z_ID/waitlist" > /dev/null
TR_REV=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])')

post /api/license-transfers/preview "{\"source_application_id\":$SRC_ID,\"successor_application_id\":$X_ID,\"reason\":\"device swap\"}" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("preview ready:", d["ready"], "; current:", d["current_revision"], "; effective excluding source:", d["effective_application_ids"], "; successor queue pos:", d["successor_waitlist_position"], "; planned revisions:", d["planned_revoc_revision"], d["planned_successor_revision"])'

post /api/license-transfers "{\"source_application_id\":$SRC_ID,\"successor_application_id\":$X_ID,\"expected_revision\":$TR_REV,\"reason\":\"device swap\"}" \
  | python3 -c 'import json,sys
d=json.load(sys.stdin)
print("transferred:", d["transferred"], "; revocation rev:", d["revocation_revision"], "; successor rev:", d["successor_approval_revision"], "; final:", d["final_revision"])
print("successor was waitlisted:", d["successor_was_waitlisted"], "; manual approval:", (not d["approval"]["auto_approved"]))
print("auto approved after handover:", d["auto_approved_application_ids"], "release_revision:", [a["release_revision"] for a in d["auto_approvals"]])
print("still waiting:", d["remaining_waitlist_application_ids"])'

curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("effective:", d["effective_application_ids"], "; waitlist:", d["waitlist_application_ids"], "; transfers:", [(t["revision"],t["source_application_id"],t["successor_application_id"],t["successor_approval_revision"]) for t in d["license_transfers"]])'
SRC_REV=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print([t["revision"] for t in d["license_transfers"]][0])')
curl -s "$BASE/api/revision/$SRC_REV" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("at revocation revision", d["revision"], "effective (source gone, successor not yet approved):", d["effective_application_ids"])'

echo "== 45. burst emergency occupation: rehearsal preview (read-only) =="
mke() { post /api/applications "{
  \"name\":\"$1\",
  \"circle\":{\"cx\":0,\"cy\":0,\"radius\":10},
  \"frequency\":{\"low\":$2,\"high\":$3},
  \"time_window\":{\"start\":\"2027-06-01T08:00:00Z\",\"end\":\"2027-06-01T12:00:00Z\"}
}"; }
P=$(mke "ordinary P" 700 710); P_ID=$(echo "$P" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
Q=$(mke "ordinary Q" 800 810); Q_ID=$(echo "$Q" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
EM_REV=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])')
post "/api/applications/$P_ID/approve" "{\"expected_revision\":$EM_REV}" >/dev/null
EM_REV=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])')
post "/api/applications/$Q_ID/approve" "{\"expected_revision\":$EM_REV}" >/dev/null
# Emergency band [710,800] touches both ordinary bands [700,710] and [800,810].
E=$(mke "burst emergency" 710 800); E_ID=$(echo "$E" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post /api/emergency-occupations/preview "{\"application_id\":$E_ID,\"reason\":\"burst traffic\"}" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("ready:", d["ready"], "; current:", d["current_revision"], "; force-suspend:", d["force_suspend_application_ids"], "; emergency clashes:", d["emergency_conflict_application_ids"], "; planned approval rev:", d["planned_approval_revision"])'
EM_REV=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])')

echo "== 46. confirm: P and Q force-suspended (one rev each, no waitlist drain), emergency approved next =="
post /api/emergency-occupations "{\"application_id\":$E_ID,\"expected_revision\":$EM_REV,\"reason\":\"burst traffic\"}" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("occupied:", d["occupied"], "; force suspensions:", [(s["revision"],s["application_id"]) for s in d["force_suspensions"]], "; emergency approval rev:", d["emergency_approval_revision"])'
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("effective:", d["effective_application_ids"], "; suspended:", d["suspended_application_ids"], "; force-suspended:", d["force_suspended_application_ids"], "; active emergencies:", d["active_emergency_application_ids"])'

echo "== 47. ordinary ops are guarded while the emergency is active =="
post "/api/applications/$E_ID/suspend" '{"expected_revision":999}' | python3 -c 'import json,sys; print("suspend emergency ->", json.load(sys.stdin)["error"]["code"])'
post "/api/applications/$E_ID/revoke" '{}' | python3 -c 'import json,sys; print("revoke emergency ->", json.load(sys.stdin)["error"]["code"])'
post "/api/applications/$P_ID/resume" '{"expected_revision":999}' \
  | python3 -c 'import json,sys; e=json.load(sys.stdin)["error"]; print("resume force-suspended P ->", e["code"], "; force_suspended_by:", e["details"]["force_suspended_by"])'
echo "    (P can still be revoked as an ordinary force-suspended license:)"
post "/api/applications/$P_ID/revoke" '{"reason":"scrapped anyway"}' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("revoked P at revision", d["revision"])'

echo "== 47b. the active emergency license cannot be handed over in an equipment swap =="
HSUCC=$(mke "swap successor" 712 714); HSUCC_ID=$(echo "$HSUCC" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post /api/license-transfers/preview "{\"source_application_id\":$E_ID,\"successor_application_id\":$HSUCC_ID,\"reason\":\"device swap\"}" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("preview ready:", d["ready"], "; blocker codes:", [b["code"] for b in d["blockers"]])'
EM_REV=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])')
post /api/license-transfers "{\"source_application_id\":$E_ID,\"successor_application_id\":$HSUCC_ID,\"expected_revision\":$EM_REV,\"reason\":\"device swap\"}" \
  | python3 -c 'import json,sys; e=json.load(sys.stdin)["error"]; print("confirm ->", e["code"], "; occupation:", e["details"]["emergency_occupation_id"], "; revision untouched:", e["details"]["current_revision"])'
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("effective:", d["effective_application_ids"], "; active emergencies:", d["active_emergency_application_ids"], "; current revision:", d["current_revision"])'
curl -s "$BASE/api/applications/$HSUCC_ID" | python3 -c 'import json,sys; print("successor stays:", json.load(sys.stdin)["status"])'

echo "== 48. a second emergency clashing with the active one is refused (emergency never suspends emergency) =="
E2=$(mke "second burst" 750 760); E2_ID=$(echo "$E2" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
EM_REV=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])')
post /api/emergency-occupations/preview "{\"application_id\":$E2_ID}" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("ready:", d["ready"], "; blocker codes:", [b["code"] for b in d["blockers"]])'
post /api/emergency-occupations "{\"application_id\":$E2_ID,\"expected_revision\":$EM_REV}" \
  | python3 -c 'import json,sys; print("confirm ->", json.load(sys.stdin)["error"]["code"])'

echo "== 49. release: revoke emergency, restore Q (P was revoked), then FIFO re-adjudicate =="
post "/api/emergency-occupations/$E_ID/release" "{\"expected_revision\":$EM_REV,\"reason\":\"burst over\"}" \
  | python3 -c 'import json,sys
d=json.load(sys.stdin)
print("emergency revocation rev:", d["emergency_revocation_revision"], "; restored:", [(r["revision"],r["application_id"]) for r in d["restorations"]], "; blocked:", d["blocked_application_ids"], "; final:", d["final_revision"])'
curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("effective:", d["effective_application_ids"], "; active emergencies:", d["active_emergency_application_ids"], "; force-suspended:", d["force_suspended_application_ids"])'

echo "== 50. repeated / unknown release changes nothing; history restores every phase =="
FINAL=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])')
post "/api/emergency-occupations/$E_ID/release" "{\"expected_revision\":$FINAL}" \
  | python3 -c 'import json,sys; print("repeat release ->", json.load(sys.stdin)["error"]["code"])'
post "/api/emergency-occupations/999999/release" "{\"expected_revision\":$FINAL}" \
  | python3 -c 'import json,sys; print("unknown release ->", json.load(sys.stdin)["error"]["code"])'
curl -s "$BASE/api/emergency-occupations?include_inactive=true" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("occupations:", [(o["id"],o["application_id"],o["status"],o.get("released_revision")) for o in d["occupations"]])'


# ---------------------------------------------------------------------------
# Scheduled auto-approvals (operator-booked UTC appointment)
# ---------------------------------------------------------------------------
mks() { post /api/applications "{
  \"name\":\"$1\",
  \"circle\":{\"cx\":0,\"cy\":0,\"radius\":10},
  \"frequency\":{\"low\":$2,\"high\":$3},
  \"time_window\":{\"start\":\"2027-06-01T08:00:00Z\",\"end\":\"2027-06-01T12:00:00Z\"}
}"; }

echo "== 51. book a scheduled auto-approval: invalid instants are rejected with no appointment =="
S1=$(mks "scheduled free" 900 910); S1_ID=$(echo "$S1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
post "/api/applications/$S1_ID/scheduled-approvals" '{"scheduled_at":"2020-01-01T00:00:00Z"}' \
  | python3 -c 'import json,sys; print("past instant ->", json.load(sys.stdin)["error"]["code"])'
post "/api/applications/$S1_ID/scheduled-approvals" '{"scheduled_at":"2027-06-01T12:00:00Z"}' \
  | python3 -c 'import json,sys; e=json.load(sys.stdin)["error"]; print("at window end ->", e["code"], ";", [d["code"] for d in e["details"]])'
curl -s "$BASE/api/scheduled-approvals" \
  | python3 -c 'import json,sys; print("appointments left behind:", json.load(sys.stdin)["count"])'

echo "== 52. book a valid near-future appointment; re-booking reschedules the same row =="
DUE=$(python3 -c 'import datetime; print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))')
B1=$(post "/api/applications/$S1_ID/scheduled-approvals" "{\"scheduled_at\":\"$DUE\",\"reason\":\"booked\"}")
B1_ID=$(echo "$B1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["scheduled_approval_id"])')
echo "    booked: $(echo "$B1" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["action"], d["status"], d["scheduled_approval_id"])')"
DUE2=$(python3 -c 'import datetime; print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=3)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))')
post "/api/applications/$S1_ID/scheduled-approvals" "{\"scheduled_at\":\"$DUE2\"}" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("re-book ->", d["action"], "same id:", d["scheduled_approval_id"]=='"$B1_ID"')'

echo "== 53. trigger due processing (same path as the background poll and the startup catch-up) =="
sleep 4
post /api/scheduled-approvals/process-due \
  | python3 -c 'import json,sys
d=json.load(sys.stdin)
print("processed:", d["processed_count"], "; approved:", d["approved_application_ids"], "; blocked:", d["blocked_application_ids"], "; final revision:", d["final_revision"])'
curl -s "$BASE/api/scheduled-approvals/$B1_ID" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("appointment:", d["status"], "; approval_revision:", d.get("approval_revision"), "; processed_at present:", bool(d.get("processed_at")))'
curl -s "$BASE/api/revision/current" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); ev=[a for a in d["approvals"] if a["application_id"]=='"$S1_ID"'][0]; print("approval flags -> scheduled:", ev["scheduled"], "auto_approved:", ev["auto_approved"])'

echo "== 54. a clashing due appointment is blocked: stays pending, no revision, no retry =="
S2=$(mks "incumbent" 920 930); S2_ID=$(echo "$S2" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
REV=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])')
post "/api/applications/$S2_ID/approve" "{\"expected_revision\":$REV}" >/dev/null
S3=$(mks "blocked scheduled" 925 928); S3_ID=$(echo "$S3" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
DUE3=$(python3 -c 'import datetime; print((datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))')
B3_ID=$(post "/api/applications/$S3_ID/scheduled-approvals" "{\"scheduled_at\":\"$DUE3\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["scheduled_approval_id"])')
sleep 2
post /api/scheduled-approvals/process-due \
  | python3 -c 'import json,sys
d=json.load(sys.stdin)
print("processed:", d["processed_count"], "; approved:", d["approved_application_ids"], "; blocked:", d["blocked_application_ids"], "; final revision:", d["final_revision"])'
curl -s "$BASE/api/scheduled-approvals/$B3_ID" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("appointment:", d["status"], "; blockers:", len(d["blocked_details"]["conflicts"]) + len(d["blocked_details"]["restriction_window_conflicts"]), "; approval_revision:", d.get("approval_revision"))'
curl -s "$BASE/api/applications/$S3_ID" \
  | python3 -c 'import json,sys; print("application still:", json.load(sys.stdin)["status"])'
post /api/scheduled-approvals/process-due \
  | python3 -c 'import json,sys; print("retry sweep processed:", json.load(sys.stdin)["processed_count"])'

echo "== 55. a competing manual decision invalidates the waiting appointment; cancel after firing is refused =="
S4=$(mks "competing scheduled" 940 950); S4_ID=$(echo "$S4" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
FAR="2027-05-31T00:00:00Z"
B4_ID=$(post "/api/applications/$S4_ID/scheduled-approvals" "{\"scheduled_at\":\"$FAR\"}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["scheduled_approval_id"])')
REV=$(curl -s "$BASE/api/revision/current" | python3 -c 'import json,sys; print(json.load(sys.stdin)["current_revision"])')
post "/api/applications/$S4_ID/approve" "{\"expected_revision\":$REV}" >/dev/null
curl -s "$BASE/api/scheduled-approvals/$B4_ID" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print("after manual approval ->", d["status"], ";", d["outcome_reason"])'
post "/api/applications/$S1_ID/scheduled-approvals/cancel" '{}' \
  | python3 -c 'import json,sys; print("cancel an already approved appointment ->", json.load(sys.stdin)["error"]["code"])'
post /api/scheduled-approvals/process-due \
  | python3 -c 'import json,sys; print("final sweep processed:", json.load(sys.stdin)["processed_count"])'
