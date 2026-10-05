"""FastAPI application: temporary-event radio spectrum requests.

Endpoints
---------
POST   /api/applications                 submit (whole-request validation)
GET    /api/applications                 list submissions
GET    /api/applications/{id}            fetch one submission
POST   /api/applications/{id}/preview    conflicts + current revision
POST   /api/applications/{id}/available-ranges
                                       maximal free lower-bound ranges
POST   /api/applications/{id}/approve    conditional approval (expected rev.)
POST   /api/applications/batch-approve   atomic group approval (expected rev.)
POST   /api/applications/{id}/suspend    temporary outage: leave effective set
POST   /api/applications/{id}/resume     re-adjudicate and restore the license
POST   /api/applications/{id}/revoke     revoke (changes future effective set)
POST   /api/applications/{id}/waitlist   register a pending app as a standby
POST   /api/applications/{id}/waitlist/cancel
                                         withdraw a standby (no revision used)
GET    /api/waitlist                     the FIFO standby queue
GET    /api/applications/{id}/waitlist   one standby position
POST   /api/restriction-windows/preview  rehearse a control window (dry-run)
POST   /api/restriction-windows          confirm a window (expected revision)
POST   /api/restriction-windows/{id}/release
                                         lift a window (expected revision)
GET    /api/restriction-windows          list active (+ released) windows
GET    /api/restriction-windows/{id}     one window (active or released)
POST   /api/license-transfers/preview    rehearse an effective->pending handover
POST   /api/license-transfers            confirm a handover (expected revision)
POST   /api/emergency-occupations/preview rehearse a burst emergency frequency grab
POST   /api/emergency-occupations         confirm an emergency occupation
POST   /api/emergency-occupations/{id}/release
                                         end an emergency (revoke/restore/drain)
GET    /api/emergency-occupations        list active (+ released) occupations
GET    /api/emergency-occupations/{id}    one occupation (active or released)
POST   /api/applications/{id}/scheduled-approvals
                                         book a UTC auto-approval appointment
POST   /api/applications/{id}/scheduled-approvals/reschedule
                                         move the appointment before it fires
POST   /api/applications/{id}/scheduled-approvals/cancel
                                         withdraw the appointment (no revision)
GET    /api/applications/{id}/scheduled-approvals
                                         one application's appointments
GET    /api/scheduled-approvals          list/filter appointments
GET    /api/scheduled-approvals/{id}      one appointment with its outcome
POST   /api/scheduled-approvals/process-due
                                         process every due appointment now
GET    /api/revision/current             current revision + effective set
GET    /api/revision/{revision}          as-of snapshot (all event types)
GET    /api/revision/compare             compare two as-of revision snapshots
GET    /api/config/guard-band            live guard band + current revision
POST   /api/config/guard-band/preview    dry-run a proposed band (non-mutating)
POST   /api/config/guard-band            confirm a new band (expected rev.)
GET    /health                           liveness probe

Concurrency
-----------
Approval locks the singleton ``revision_meta`` row (``SELECT ... FOR UPDATE``)
for the duration of the transaction.  Concurrent single approvals, batch
approvals, suspensions, resumptions, revocations and guard-band changes
therefore serialise on the same revision arbitration: losers re-adjudicate
against the winner's commit and are refused with ``stale_revision`` or
``interference``.  A batch either consumes one consecutive revision per
member in input order, or nothing at all; a guard-band change, a suspension
or a resumption consumes exactly one revision.

Temporary outages
-----------------
A suspension immediately removes an approved license from the effective set
without touching its original approval.  A resumption re-adjudicates the
license against the live guard band and all currently effective licenses; a
stale revision or renewed interference is refused (returning the current
revision or the conflicting licenses) while the license stays suspended and
no revision is consumed.  A suspended license may also be permanently
revoked with the ordinary revoke operation; a revoked license can never be
resumed.  Both operations append immutable events so historical snapshots
restore the suspended/effective status and the effective set at every
revision.

Guard band
----------
The live guard band is stored on the ``revision_meta`` row and seeded once
from the ``GUARD_BAND`` environment variable (restarts never overwrite it).
Every adjudication (preview, single/batch approval, resumption) uses the
live value; each confirmed change is appended as an immutable
``guard_band_events`` record so historical revision snapshots restore the
band in force at the time.  Approval and resumption events keep their own
copy of the band they were decided under and are never rewritten.

Waitlist
--------
A pending application may be registered as a standby while the band is
busy.  Registration and cancellation consume no revision and never change
the application's status; an application is queued at most once.  When a
suspension or a revoke releases an effective license, the same transaction
drains the queue in registration order: each entry is re-adjudicated under
the live guard band against the licenses effective so far (including the
automatic approvals just granted), passers are approved immediately and
consume the following consecutive revisions, blockers keep their place and
the drain continues.  A manual approval removes the queue entry first, so
a manually decided application is never auto-approved.  Every automatic
approval is an ordinary ``ApprovalEvent`` flagged ``auto_approved`` with the
release revision, which lets revision snapshots restore the automatic
decisions one revision at a time.

Restriction windows
-------------------
Operators temporarily control part of the spectrum with restriction
windows: a coverage circle, a closed frequency band and a half-open UTC
interval, plus a reason.  A read-only rehearsal preview lists the currently
effective licenses and standby applications the proposal would hit (and the
revision it is based on) without changing state; the confirmation carries
the expected revision, consumes one revision and activates the window.
Existing effective licenses that the window hits stay effective, but from
that revision onwards pending previews, single/batch approvals, suspended
license resumptions and post-release waitlist auto-approvals all report hit
windows, and no write may make a hit application effective.  A hit needs the
circle, half-open time and closed frequency band all to intersect (touching
band endpoints hit); the guard band never extends a window.  Releasing a
window by its id and the expected revision consumes another revision but
only affects later adjudication (it never auto-approves the waitlist).
Activations and releases are immutable events, so revision snapshots and
comparisons restore the windows active at the time; original approval
events are never rewritten.

License handover (active-device swap)
-------------------------------------
When an active device is replaced, the operator hands one currently
effective license to a pending application.  A read-only rehearsal names
the source license, the successor application and an operator reason and
returns the current revision, the interference the successor would still
see once the source is excluded from the effective set, and the active
restriction windows it hits; it never mutates state.  The confirmation adds
the expected revision and runs inside the same singleton revision
arbitration as every other writer: it re-checks that the source is still
effective, the successor is still pending, the revision is current, and
that (with the source removed) no license interferes and no restriction
window is hit.  Only then does the whole transaction take effect: the
ordinary source revocation consumes one revision and the ordinary successor
approval consumes the next consecutive revision.  A successor queued as a
standby first leaves the queue (its approval is a manual one and is never
flagged ``auto_approved``); afterwards the remaining standbys are
re-adjudicated by the existing FIFO drain, whose auto approvals consume the
following revisions and keep ``release_revision`` pointing at the handover's
revocation revision.  A stale revision, a status change, interference or a
window hit rejects the whole handover: licenses, the queue and the revision
are left untouched.  The handover appends one immutable
``license_transfer_events`` row linking its two revisions, so revision
snapshots and comparisons restore every revision exactly; existing
revocation and approval semantics (and startup) are unchanged.

Burst-communication emergency occupations
-----------------------------------------
When burst traffic must take spectrum urgently, an operator rehearses the
grab with a pending application id and an operator reason: the read-only
preview returns the current revision, the ordinary effective licenses the
grab would force-suspend, any *already-emergency* licenses it would
interfere with (one emergency never suspends another), and the active
restriction windows it hits; state is untouched.  The confirmation carries
the expected revision and, inside the shared revision arbitration,
re-checks that the emergency application is still pending, hits no window
and clashes with no other emergency license -- otherwise the whole grab is
rejected and nothing changes.  On success the interfering ordinary
licenses are force-suspended first, one consecutive revision each, as
ordinary ``SuspensionEvent`` rows flagged ``forced`` (this deliberately
does not trigger a waitlist re-adjudication); a queued emergency
application leaves the queue; the emergency license is then approved at
the next consecutive revision as an ordinary ``ApprovalEvent`` flagged
``emergency``.  Original approval records are never rewritten.

A force-suspended license can be revoked as usual, but an ordinary resume
is refused while its emergency is active; an emergency license can be
neither ordinarily suspended nor ordinarily revoked -- the occupation ends
only through the dedicated release, which names the emergency application
id and the expected revision.  The release is one atomic transaction: it
first revokes the emergency license (an ordinary ``RevocationEvent``
flagged ``emergency_release``), then re-adjudicates the licenses still
force-suspended by that occupation in their original suspension order
against the live guard band, the effective set so far and every active
window; passers resume one consecutive revision each (ordinary
``ResumptionEvent`` rows flagged ``forced_recovery``), blocked licenses
stay suspended as ordinary outages (their emergency marker is cleared) and
their blocking conflicts/window hits are returned; finally the standby
queue is re-adjudicated by the existing FIFO drain.  A stale revision or a
repeated release changes nothing.  Snapshots and comparisons restore every
phase revision by revision through the ordinary event replay plus the
``emergency_occupations`` audit rows.

Scheduled auto-approvals
------------------------
An operator may book a UTC auto-approval appointment for a pending
application at an instant strictly after the booking moment and strictly
before the application's own time window ends.  Booking consumes no
revision; an unknown/non-pending application or an invalid instant is
rejected without leaving an appointment.  At most one waiting appointment
exists per application and it may be cancelled or rescheduled before it
fires.  A background sweeper (with a startup catch-up pass) processes due
appointments inside the shared revision arbitration: at the actual
processing instant the application is re-adjudicated against the live
guard band, every effective license and every active restriction window.
A pass approves it on one new revision as an ordinary ``ApprovalEvent``
flagged ``scheduled`` (a queued application leaves the waitlist first;
the waitlist is not re-adjudicated afterwards); a clash or window hit is
recorded as the appointment's blocking detail, the application stays
pending, no revision is consumed and nothing is retried automatically.
A decision through any other path -- manual single/batch approval,
waitlist auto-approval, a handover or an emergency occupation --
invalidates the waiting appointment in the same transaction, so due
processing, cancellation, rescheduling and other decisions compete under
one lock and exactly one outcome wins; a restart never double-approves.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from . import adjudication
from .adjudication import Circle, Conflict, License, PairConflict, RestrictionWindow, WindowHit
from .config import settings
from .db import SessionLocal, engine, init_db, wait_for_database
from .errors import APIError, api_error_handler
from .models import (
    Application,
    ApprovalEvent,
    EmergencyOccupation,
    GuardBandEvent,
    LicenseTransferEvent,
    ResumptionEvent,
    RestrictionWindow as RestrictionWindowRow,
    RestrictionWindowEvent,
    RevisionMeta,
    RevocationEvent,
    ScheduledApproval,
    ScheduledApprovalChange,
    SuspensionEvent,
    WaitlistEntry,
)
from .schemas import (
    ApplicationCreate,
    ApprovalRequest,
    AvailableRangesRequest,
    BatchApprovalRequest,
    EmergencyOccupationConfirmRequest,
    EmergencyOccupationReleaseRequest,
    EmergencyOccupationRequest,
    GuardBandPreviewRequest,
    GuardBandUpdateRequest,
    LicenseTransferConfirmRequest,
    LicenseTransferRequest,
    ResumptionRequest,
    RestrictionWindowConfirmRequest,
    RestrictionWindowReleaseRequest,
    RestrictionWindowRequest,
    RevocationRequest,
    ScheduledApprovalCancelRequest,
    ScheduledApprovalRequest,
    SuspensionRequest,
    WaitlistRegistrationRequest,
)
from .semantic_validation import (
    validate_application,
    validate_candidate_low_range,
    validate_guard_band,
    validate_restriction_window,
)

log = logging.getLogger("radio.api")

# Error codes
E_VALIDATION = "validation_failed"
E_NOT_FOUND = "not_found"
E_NOT_PENDING = "not_pending"
E_ALREADY_REVOKED = "already_revoked"
E_NOT_APPROVED = "not_approved"
E_ALREADY_SUSPENDED = "already_suspended"
E_NOT_SUSPENDED = "not_suspended"
E_REVISION_OUT_OF_RANGE = "revision_out_of_range"
E_STALE_REVISION = "stale_revision"
E_INTERFERENCE = "interference"
E_DUPLICATE_APPLICATION = "duplicate_application_id"
E_ALREADY_WAITLISTED = "already_waitlisted"
E_NOT_WAITLISTED = "not_waitlisted"
# A write was blocked because the target hits an active restriction window
# (HTTP 409; same envelope shape as ``interference``).
E_RESTRICTION_WINDOW = "restriction_window"
E_WINDOW_NOT_FOUND = "restriction_window_not_found"
E_WINDOW_NOT_ACTIVE = "restriction_window_not_active"
# A handover source is not a currently effective (approved, non-suspended,
# non-revoked) license (HTTP 409).
E_SOURCE_NOT_EFFECTIVE = "source_not_effective"
# A handover successor is not a still-pending application (HTTP 409).
E_SUCCESSOR_NOT_PENDING = "successor_not_pending"
# A handover named the same application as source and successor (HTTP 422).
E_SAME_APPLICATION = "same_application"
# An emergency grab clashes with an already-active emergency occupation
# (HTTP 409): emergency licenses are never force-suspended by another
# emergency, so the whole occupation is rejected.
E_EMERGENCY_CONFLICT = "emergency_occupation_conflict"
# An ordinary suspend/resume targeted a license that is the active emergency
# license (suspend/revoke) or a license force-suspended by an active
# emergency (resume); HTTP 409.
E_EMERGENCY_LICENSE_PROTECTED = "emergency_license_protected"
E_FORCE_SUSPENDED = "force_suspended"
# A release named an emergency application that never had an occupation
# (HTTP 404) or one whose occupation has already been released (HTTP 409).
E_EMERGENCY_NOT_FOUND = "emergency_occupation_not_found"
E_EMERGENCY_NOT_ACTIVE = "emergency_occupation_not_active"
# A scheduled auto-approval appointment named an application/time the rules
# reject: unknown application (404), a non-pending application (409), an
# instant not strictly in the future or not strictly before the application's
# window end (422), or an appointment that does not exist / is no longer
# waiting to fire (404/409).
E_SCHEDULE_NOT_FOUND = "scheduled_approval_not_found"
E_SCHEDULE_NOT_ACTIVE = "scheduled_approval_not_active"
E_SCHEDULE_TIME_INVALID = "scheduled_time_invalid"
E_SCHEDULE_EXISTS = "scheduled_approval_exists"


# --------------------------------------------------------------------------- #
# Serialisation helpers
# --------------------------------------------------------------------------- #
def _utc(value: datetime) -> datetime:
    """Coerce a possibly naive timestamp (e.g. under SQLite) to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stored_instant(value: datetime) -> datetime:
    """Normalise an aware instant for comparisons against stored columns.

    PostgreSQL keeps timezone-aware ``DateTime(timezone=True)`` values; the
    SQLite test backend stores them as naive UTC and hands back naive
    timestamps, so comparisons against a query bound need the same shape.
    """
    aware = value.astimezone(timezone.utc)
    if engine.dialect.name == "sqlite":
        return aware.replace(tzinfo=None)
    return aware


def iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def serialize_application(app: Application) -> dict[str, Any]:
    return {
        "id": app.id,
        "name": app.name,
        "circle": {"cx": app.cx, "cy": app.cy, "radius": app.radius},
        "frequency": {"low": app.freq_low, "high": app.freq_high},
        "time_window": {"start": iso(app.start_time), "end": iso(app.end_time)},
        "status": app.status,
        "approval_revision": app.approval_revision,
        "suspended_revision": app.suspended_revision,
        # Set while an active emergency occupation has force-suspended the
        # license: the emergency application that forced it out (NULL for
        # ordinary suspensions and effective licenses).
        "force_suspended_by": app.force_suspended_by,
        "created_at": iso(app.created_at),
    }


def serialize_application_as_of(
    app: Application, fields: dict[str, Any]
) -> dict[str, Any]:
    """Application view rewound to a past revision's replayed state.

    The immutable columns (name, geometry, band, time window, ``created_at``)
    come from the stored row; the mutable lifecycle fields (``status``,
    ``approval_revision``, ``suspended_revision``, ``force_suspended_by``)
    are the values the event replay reconstructed *as of* the queried
    revision.  Historical snapshots and comparisons serialise embedded
    applications through this so a later resume, revocation or emergency
    release never rewrites an old view.
    """
    body = serialize_application(app)
    body["status"] = fields["status"]
    body["approval_revision"] = fields["approval_revision"]
    body["suspended_revision"] = fields["suspended_revision"]
    body["force_suspended_by"] = fields["force_suspended_by"]
    return body


def _license_view(lic: License) -> dict[str, Any]:
    """The public shape of a license as a conflict/snapshot party."""
    return {
        "id": lic.application_id,
        "circle": {
            "cx": lic.circle.cx,
            "cy": lic.circle.cy,
            "radius": lic.circle.radius,
        },
        "frequency": {"low": lic.freq_low, "high": lic.freq_high},
        "time_window": {
            "start": iso(lic.start_time),
            "end": iso(lic.end_time),
        },
    }


def serialize_conflict(conflict: Conflict, guard_band: float) -> dict[str, Any]:
    """One conflicting effective grant with the trigger conditions."""
    return {
        "application": _license_view(conflict.other),
        "trigger": {
            "time_intersect": conflict.time_overlap,
            "area_intersect": conflict.area_overlap,
            "frequency_intersect": conflict.frequency_intersects,
            "minimum_frequency_gap": conflict.minimum_frequency_gap,
            "within_guard_band": (
                conflict.minimum_frequency_gap < guard_band
            ),
        },
        "reason": conflict.reason,
    }


def serialize_pair_conflict(
    pair: PairConflict, guard_band: float
) -> dict[str, Any]:
    """One interfering pair of effective licenses with trigger conditions.

    Same trigger shape as a preview conflict so existing clients can reuse
    their rendering.
    """
    return {
        "first": _license_view(pair.first),
        "second": _license_view(pair.second),
        "trigger": {
            "time_intersect": pair.time_overlap,
            "area_intersect": pair.area_overlap,
            "frequency_intersect": pair.frequency_intersects,
            "minimum_frequency_gap": pair.minimum_frequency_gap,
            "within_guard_band": (
                pair.minimum_frequency_gap < guard_band
            ),
        },
        "reason": pair.reason,
    }


def to_license(app: Application) -> License:
    return License(
        application_id=app.id,
        circle=Circle(app.cx, app.cy, app.radius),
        freq_low=app.freq_low,
        freq_high=app.freq_high,
        start_time=_utc(app.start_time),
        end_time=_utc(app.end_time),
    )


def serialize_group_conflict(
    conflict: Conflict, scope: str, guard_band: float
) -> dict[str, Any]:
    """A conflict inside a group decision, tagged with where it came from.

    ``scope`` is ``"effective"`` when the other side is a currently valid
    license, or ``"group"`` when it is another application in the same batch.
    The body is the same shape as a preview conflict so existing clients can
    reuse their rendering.
    """
    body = serialize_conflict(conflict, guard_band)
    body["scope"] = scope
    return body


def load_effective_licenses(session) -> list[License]:
    """Approved and neither suspended nor revoked = the effective set."""
    rows = (
        session.query(Application)
        .filter(Application.status == "approved")
        .order_by(Application.id)
        .all()
    )
    return [to_license(row) for row in rows]


# --------------------------------------------------------------------------- #
# Database candidate-set narrowing
# --------------------------------------------------------------------------- #
# Every adjudication chain first narrows the database candidate set by the
# three dimensions (half-open time window, coverage circle, frequency band)
# and only then runs the exact in-process predicates in ``adjudication``.
# The narrowing is deliberately an over-approximation: it may keep extra
# rows (which the exact rules then drop), but it must never drop a row that
# could conflict.  The coverage bounding box is a strict superset of the
# exact disk test, and every bound additionally carries a tiny relative
# slack so database float rounding can never turn a real conflict into a
# filtered-out row.
_PREFILTER_SLACK = 1e-12


@dataclass(frozen=True)
class _Envelope:
    """Over-approximate bounding region of one candidate or a group.

    ``[t_start, t_end)`` is the half-open time envelope; coverage is an
    axis-aligned bounding square described by its centre ``(ccx, ccy)``
    and half-width ``reach`` (for a single candidate its circle centre and
    radius; for a group union the enclosing square); ``[f_low, f_high]``
    is the closed frequency envelope (already widened by the guard band
    for license-vs-license interference; never widened for restriction
    windows).
    """

    t_start: datetime
    t_end: datetime
    ccx: float
    ccy: float
    reach: float
    f_low: float
    f_high: float


def _bound_slack(bound: float) -> float:
    """A few ulps of outward slack for a float prefilter bound."""
    if bound == 0.0:
        return _PREFILTER_SLACK
    return abs(bound) * _PREFILTER_SLACK


def _union_envelopes(envelopes: Sequence[_Envelope]) -> _Envelope:
    """Axis-aligned union of several envelopes (an over-approximation)."""
    x_min = min(e.ccx - e.reach for e in envelopes)
    x_max = max(e.ccx + e.reach for e in envelopes)
    y_min = min(e.ccy - e.reach for e in envelopes)
    y_max = max(e.ccy + e.reach for e in envelopes)
    return _Envelope(
        t_start=min(e.t_start for e in envelopes),
        t_end=max(e.t_end for e in envelopes),
        ccx=(x_min + x_max) / 2.0,
        ccy=(y_min + y_max) / 2.0,
        reach=max(x_max - x_min, y_max - y_min) / 2.0,
        f_low=min(e.f_low for e in envelopes),
        f_high=max(e.f_high for e in envelopes),
    )


def _candidate_envelope(
    candidate: License, guard_band: float = 0.0
) -> _Envelope:
    """Envelope of one license-shaped candidate for license interference.

    The frequency envelope is widened by the non-negative guard band: a
    license conflicts when the closed bands intersect *or* their gap is
    strictly smaller than the band, so every possible interferer must lie
    inside ``[freq_low - guard, freq_high + guard]``.
    """
    return _Envelope(
        t_start=candidate.start_time,
        t_end=candidate.end_time,
        ccx=candidate.circle.cx,
        ccy=candidate.circle.cy,
        # Outward slack lives in the SQL space predicate (which performs
        # column arithmetic); see ``_space_filters``.
        reach=candidate.circle.radius,
        f_low=candidate.freq_low - guard_band,
        f_high=candidate.freq_high + guard_band,
    )


def _window_hit_envelope(candidate: License) -> _Envelope:
    """Envelope for candidate-vs-window hits.

    Window hits require an actual closed-band intersection; the guard band
    never extends a window, so no frequency widening is applied.
    """
    return _candidate_envelope(candidate, 0.0)


def _group_envelope(
    candidates: Sequence[License], guard_band: float
) -> _Envelope:
    """Union envelope of a batch's members for narrowing the effective set."""
    return _union_envelopes(
        [_candidate_envelope(c, guard_band) for c in candidates]
    )


def _available_ranges_envelope(
    candidate: License,
    guard_band: float,
    low_min: float,
    low_max: float,
) -> _Envelope:
    """Envelope sweeping every attainable band position in an ranges query.

    The candidate keeps its circle and time window and its band width; the
    lower bound varies over the closed ``[low_min, low_max]`` interval, so
    the frequency envelope spans the whole swept span plus the guard band
    on each side.  A license outside it forbids no attainable lower bound.
    """
    width = candidate.freq_high - candidate.freq_low
    base = _candidate_envelope(candidate, guard_band)
    return _Envelope(
        t_start=base.t_start,
        t_end=base.t_end,
        ccx=base.ccx,
        ccy=base.ccy,
        reach=base.reach,
        f_low=low_min - guard_band,
        f_high=low_max + width + guard_band,
    )


def _proposed_window_envelope(window: RestrictionWindow) -> _Envelope:
    """Envelope of a (possibly not-yet-persisted) restriction window."""
    return _Envelope(
        t_start=window.start_time,
        t_end=window.end_time,
        ccx=window.circle.cx,
        ccy=window.circle.cy,
        reach=window.circle.radius,
        f_low=window.freq_low,
        f_high=window.freq_high,
    )


def _space_filters(model, envelope: _Envelope):
    """Necessary bounding-square overlap conditions for disk intersection.

    Two disks with centres ``(x, y)`` / ``(ccx, ccy)`` and radii
    ``radius`` / ``reach`` can intersect only if the centre distance is at
    most ``radius + reach``, which implies each coordinate separation is
    bounded by that sum.  The inequality carries a relative slack term so
    database-side float arithmetic (``x - ccx`` and ``radius + reach``)
    can never round a genuinely touching/overlapping pair out of the
    candidate set; it only keeps a few extra ulps of irrelevant rows,
    which the exact in-process predicates drop.
    """
    reach = envelope.reach + _bound_slack(envelope.reach)
    margin = model.radius + reach
    # The margin slack also covers the coordinate subtraction rounding:
    # ``1 + |dx|`` dominates both |dx| and the radius sum's magnitude.
    eps_margin = (margin + 1.0) * _PREFILTER_SLACK
    return [
        func.abs(model.cx - envelope.ccx) <= margin + eps_margin,
        func.abs(model.cy - envelope.ccy) <= margin + eps_margin,
    ]


def _apply_application_filters(query, envelope: _Envelope, *, status=None):
    """Narrow an ``Application`` query to an envelope's possible matches.

    Time uses the exact half-open overlap condition (``start < other_end``
    and ``other_start < end``); space uses the necessary bounding-square
    condition (a disk pair whose centres are farther apart on either axis
    than the sum of radii cannot intersect); frequency uses closed-band
    overlap against the envelope's (possibly guard-widened) span.
    """
    if status is not None:
        query = query.filter(Application.status == status)
    query = query.filter(
        Application.start_time < _stored_instant(envelope.t_end),
        Application.end_time > _stored_instant(envelope.t_start),
        Application.freq_low <= envelope.f_high,
        Application.freq_high >= envelope.f_low,
    )
    return query.filter(*_space_filters(Application, envelope))


def _apply_window_filters(query, envelope: _Envelope):
    """Narrow a ``RestrictionWindow`` query to an envelope's possible hits."""
    query = query.filter(
        RestrictionWindowRow.start_time < _stored_instant(envelope.t_end),
        RestrictionWindowRow.end_time > _stored_instant(envelope.t_start),
        RestrictionWindowRow.freq_low <= envelope.f_high,
        RestrictionWindowRow.freq_high >= envelope.f_low,
    )
    return query.filter(*_space_filters(RestrictionWindowRow, envelope))


def load_candidate_effective_licenses(
    session,
    envelope: _Envelope,
    *,
    exclude_ids: Iterable[int] = (),
    extra_licenses: Sequence[License] = (),
) -> list[License]:
    """Effective licenses that could interfere with ``envelope``.

    Narrowed counterpart of :func:`load_effective_licenses`: the database
    only returns effective rows whose time window, coverage bounding box
    and (guard-widened) frequency span can meet the envelope, and the
    exact rules run afterwards.  ``exclude_ids`` drops rows by id (e.g. a
    handover source).  ``extra_licenses`` appends already-known in-memory
    licenses (e.g. ones approved earlier in the same drain) in their given
    order, preserving the exact conflict ordering of a full scan.
    """
    excluded = set(exclude_ids)
    query = _apply_application_filters(
        session.query(Application), envelope, status="approved"
    )
    if excluded:
        query = query.filter(~Application.id.in_(excluded))
    rows = query.order_by(Application.id).all()
    licenses = [to_license(row) for row in rows]
    licenses.extend(extra_licenses)
    return licenses


def load_candidate_windows(
    session, envelope: _Envelope
) -> list[RestrictionWindowRow]:
    """Active window rows that could hit ``envelope`` (id ordered).

    The guard band never extends a window: callers pass an envelope built
    without frequency widening.
    """
    return (
        _apply_window_filters(
            session.query(RestrictionWindowRow), envelope
        )
        .order_by(RestrictionWindowRow.id)
        .all()
    )


def load_active_window_ids(session) -> list[int]:
    """Ids of every active window in confirmation order (for response fields)."""
    return [
        row_id
        for (row_id,) in session.query(RestrictionWindowRow.id)
        .order_by(RestrictionWindowRow.id)
        .all()
    ]


# --------------------------------------------------------------------------- #
# Burst-communication emergency frequency occupations
# --------------------------------------------------------------------------- #
def load_active_emergencies(session) -> list[EmergencyOccupation]:
    """Emergency occupations confirmed and not yet released, in id order."""
    return (
        session.query(EmergencyOccupation)
        .filter(EmergencyOccupation.released_revision.is_(None))
        .order_by(EmergencyOccupation.id)
        .all()
    )


def emergency_application_ids(
    occupations: list[EmergencyOccupation],
) -> set[int]:
    """Application ids that currently hold an active emergency license."""
    return {occ.emergency_application_id for occ in occupations}


def load_emergency_licenses(session) -> list[License]:
    """The effective licenses held by active emergency occupations."""
    ids = emergency_application_ids(load_active_emergencies(session))
    if not ids:
        return []
    rows = (
        session.query(Application)
        .filter(Application.id.in_(ids))
        .order_by(Application.id)
        .all()
    )
    return [to_license(row) for row in rows]


def serialize_emergency_occupation(
    session, occ: EmergencyOccupation
) -> dict[str, Any]:
    """Public shape of one emergency occupation (active or released)."""
    emergency_app = session.get(Application, occ.emergency_application_id)
    forced_rows = (
        session.query(SuspensionEvent)
        .filter(
            SuspensionEvent.forced.is_(True),
            SuspensionEvent.forced_by_emergency_application_id
            == occ.emergency_application_id,
        )
        .order_by(SuspensionEvent.revision)
        .all()
    )
    forced_ids = [ev.application_id for ev in forced_rows]

    # Licenses still forced out right now: the occupation is active and the
    # application is still suspended *by this* emergency (it may have been
    # revoked in the meantime, or already turned into an ordinary suspension
    # at release time).
    still_forced: list[int] = []
    if occ.released_revision is None:
        for app_id in forced_ids:
            row = session.get(Application, app_id)
            if (
                row is not None
                and row.status == "suspended"
                and row.force_suspended_by == occ.emergency_application_id
            ):
                still_forced.append(app_id)

    body: dict[str, Any] = {
        "id": occ.id,
        "application_id": occ.emergency_application_id,
        "emergency_application_id": occ.emergency_application_id,
        "status": "active" if occ.released_revision is None else "released",
        "reason": occ.reason,
        "emergency_approval_revision": occ.emergency_approval_revision,
        "first_force_suspension_revision": occ.first_force_suspension_revision,
        "last_force_suspension_revision": occ.last_force_suspension_revision,
        "force_suspended_application_ids": forced_ids,
        "force_suspensions": [
            {
                "revision": ev.revision,
                "application_id": ev.application_id,
                "reason": ev.reason,
            }
            for ev in forced_rows
        ],
        "still_force_suspended_application_ids": still_forced,
        "created_at": iso(occ.created_at),
        "application": (
            serialize_application(emergency_app)
            if emergency_app is not None
            else None
        ),
    }
    if occ.released_revision is not None:
        body["released_revision"] = occ.released_revision
        body["release_reason"] = occ.release_reason
        body["released_at"] = iso(occ.released_at)
    return body


def emergency_preview_body(
    session,
    payload: EmergencyOccupationRequest,
    *,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Shared read-only rehearsal for a burst emergency occupation.

    Adjudicates the emergency application (which must describe the spectrum
    being grabbed) against the current effective set, split into ordinary
    licenses (which the confirmation would force-suspend) and active
    emergency licenses (which reject the whole grab, because one emergency
    never suspends another), and against every active restriction window.
    Never mutates state; status problems come back as non-fatal ``blockers``.
    """
    meta = session.get(RevisionMeta, 1)
    current = meta.revision
    guard_band = meta.guard_band

    row = session.get(Application, payload.application_id)
    blockers: list[dict[str, Any]] = []
    if row is None:
        blockers.append(
            {
                "code": E_NOT_FOUND,
                "field": "application_id",
                "message": (
                    f"application {payload.application_id} does not exist"
                ),
            }
        )
    elif row.status != "pending":
        blockers.append(
            {
                "code": E_NOT_PENDING,
                "field": "application_id",
                "message": (
                    f"emergency application {row.id} is {row.status}; only a "
                    "pending application can occupy spectrum in an emergency"
                ),
                "status": row.status,
            }
        )

    active_occupations = load_active_emergencies(session)
    emergency_ids = emergency_application_ids(active_occupations)
    all_window_ids = load_active_window_ids(session)

    ordinary_conflicts: list[Conflict] = []
    emergency_conflicts: list[Conflict] = []
    window_hits: list[WindowHit] = []
    waitlist_position = None
    if row is not None:
        candidate = to_license(row)
        # Narrow the effective set in the database by time, coverage and
        # guard-widened frequency before the exact predicates run.
        effective = load_candidate_effective_licenses(
            session, _candidate_envelope(candidate, guard_band)
        )
        ordinary_effective = [
            lic for lic in effective if lic.application_id not in emergency_ids
        ]
        emergency_effective = [
            lic for lic in effective if lic.application_id in emergency_ids
        ]
        # The guard band never extends a window: no frequency widening.
        window_rows = load_candidate_windows(
            session, _window_hit_envelope(candidate)
        )
        ordinary_conflicts = adjudication.evaluate(
            candidate, ordinary_effective, guard_band
        )
        emergency_conflicts = adjudication.evaluate(
            candidate, emergency_effective, guard_band
        )
        window_hits = window_hits_for(candidate, window_rows)
        waitlist_position = _waitlist_position(session, row.id)

    if emergency_conflicts:
        blockers.append(
            {
                "code": E_EMERGENCY_CONFLICT,
                "field": "application_id",
                "message": (
                    f"emergency application would interfere with "
                    f"{len(emergency_conflicts)} active emergency license(s); "
                    "an emergency never force-suspends another emergency"
                ),
            }
        )
    if window_hits:
        blockers.append(
            {
                "code": E_RESTRICTION_WINDOW,
                "field": "application_id",
                "message": (
                    f"emergency application hits {len(window_hits)} active "
                    "restriction window(s); the occupation cannot make it "
                    "effective"
                ),
            }
        )

    return {
        "application_id": payload.application_id,
        "reason": payload.reason,
        "current_revision": current,
        "expected_revision": expected_revision,
        "guard_band": guard_band,
        "ready": not blockers,
        "blockers": blockers,
        "application": serialize_application(row) if row is not None else None,
        "waitlist_position": waitlist_position,
        # Ordinary effective licenses the confirmation would force out of the
        # effective set (one forced suspension revision each, in this order).
        "force_suspend_application_ids": [
            c.other.application_id for c in ordinary_conflicts
        ],
        "force_suspend_licenses": [
            serialize_conflict(c, guard_band) for c in ordinary_conflicts
        ],
        # Active emergency licenses that clash and reject the whole grab.
        "emergency_conflict_application_ids": [
            c.other.application_id for c in emergency_conflicts
        ],
        "emergency_conflicts": [
            serialize_conflict(c, guard_band) for c in emergency_conflicts
        ],
        "has_restriction_window_conflicts": bool(window_hits),
        "restriction_window_conflicts": [
            serialize_window_hit(h) for h in window_hits
        ],
        "active_restriction_window_ids": all_window_ids,
        # Revisions the confirm would consume: one per forced suspension,
        # then the emergency approval.
        "planned_first_force_suspension_revision": (
            current + 1 if ordinary_conflicts else None
        ),
        "planned_approval_revision": current + len(ordinary_conflicts) + 1,
        "planned_final_revision": current + len(ordinary_conflicts) + 1,
    }


# --------------------------------------------------------------------------- #
# Temporary restriction windows
# --------------------------------------------------------------------------- #
def serialize_window(
    window: RestrictionWindow | RestrictionWindowRow,
    *,
    status: str = "active",
    activated_at: datetime | None = None,
) -> dict[str, Any]:
    """Public shape of a restriction window (row or adjudicator dataclass)."""
    is_row = isinstance(window, RestrictionWindowRow)
    window_id = window.id if is_row else window.window_id
    cx = window.cx if is_row else window.circle.cx
    cy = window.cy if is_row else window.circle.cy
    radius = window.radius if is_row else window.circle.radius
    body = {
        "id": window_id,
        "window_id": window_id,
        "status": status,
        "circle": {"cx": cx, "cy": cy, "radius": radius},
        "frequency": {"low": window.freq_low, "high": window.freq_high},
        "time_window": {
            "start": iso(window.start_time),
            "end": iso(window.end_time),
        },
        "reason": window.reason,
        "revision": window.revision,
    }
    if activated_at is not None:
        body["activated_at"] = iso(activated_at)
    return body


def serialize_window_row(row: RestrictionWindowRow) -> dict[str, Any]:
    return serialize_window(row, activated_at=row.created_at)


def serialize_window_hit(hit: WindowHit) -> dict[str, Any]:
    """One hit window with its trigger conditions (mirrors a conflict)."""
    return {
        "window": serialize_window(hit.window),
        "trigger": {
            "time_intersect": hit.time_intersect,
            "area_intersect": hit.area_intersect,
            "frequency_intersect": hit.frequency_intersect,
        },
        "reason": hit.reason,
    }


def to_window(row: RestrictionWindowRow) -> RestrictionWindow:
    return RestrictionWindow(
        window_id=row.id,
        circle=Circle(row.cx, row.cy, row.radius),
        freq_low=row.freq_low,
        freq_high=row.freq_high,
        start_time=_utc(row.start_time),
        end_time=_utc(row.end_time),
        reason=row.reason,
        revision=row.revision,
    )


def load_active_windows(session) -> list[RestrictionWindowRow]:
    """Windows confirmed and not yet released, in confirmation order."""
    return (
        session.query(RestrictionWindowRow)
        .order_by(RestrictionWindowRow.id)
        .all()
    )


def window_hits_for(
    candidate: License, window_rows: list[RestrictionWindowRow]
) -> list[WindowHit]:
    """Hit windows for one candidate against the active window rows."""
    return adjudication.evaluate_restriction_windows(
        candidate, [to_window(row) for row in window_rows]
    )


def proposed_window(payload: RestrictionWindowRequest) -> RestrictionWindow:
    """Build an unsaved adjudicator window from a preview/confirm payload."""
    return RestrictionWindow(
        window_id=0,
        circle=Circle(
            payload.circle.cx, payload.circle.cy, payload.circle.radius
        ),
        freq_low=payload.frequency.low,
        freq_high=payload.frequency.high,
        start_time=payload.time_window.start.astimezone(timezone.utc),
        end_time=payload.time_window.end.astimezone(timezone.utc),
        reason=payload.reason,
    )


# --------------------------------------------------------------------------- #
# Waitlist (standby queue for pending applications while the band is busy)
# --------------------------------------------------------------------------- #
def serialize_waitlist_entry(
    session, entry: WaitlistEntry, position: int
) -> dict[str, Any]:
    """Public shape of one queue position; the application stays ``pending``."""
    app = session.get(Application, entry.application_id)
    return {
        "position": position,
        "application_id": entry.application_id,
        "reason": entry.reason,
        "registered_at": iso(entry.created_at),
        "application": serialize_application(app) if app else None,
    }


def load_waitlist(session) -> list[WaitlistEntry]:
    """The current standby queue in registration (FIFO) order."""
    return (
        session.query(WaitlistEntry)
        .order_by(WaitlistEntry.id)
        .all()
    )


def drain_waitlist(
    session,
    meta: RevisionMeta,
    release_revision: int,
) -> dict[str, Any]:
    """Re-adjudicate the standby queue after an effective license was released.

    Runs inside the suspend/revoke transaction, after that operation has
    already consumed its single revision (``release_revision``).  Entries are
    visited strictly in registration order and re-adjudicated against the
    current guard band and the licenses effective *so far*:

    * an entry with no conflicts and no restriction-window hits is approved
      immediately, consumes the next consecutive revision, leaves the queue
      and joins the effective set, so later entries are also checked against
      it;
    * an entry that still interferes with a license or hits an active
      restriction window keeps its place in the queue and the drain
      continues with the following entries.

    Each entry narrows the database candidate set by its own time window,
    coverage and (guard-widened) frequency span before the exact predicates
    run, so effective licenses disjoint in every dimension are never
    loaded; licenses approved earlier in this same drain are appended in
    memory in approval order, preserving the exact conflict ordering of a
    full effective-set scan.  Restriction windows are enforced with the
    live set of active window rows (the guard band never extends a
    window).  Releasing a window itself never calls this function, so
    lifting a control only affects later adjudications and never
    auto-approves standbys.

    Manually approved applications have had their queue entries removed by
    the approve/batch handlers, so a queued application is never
    auto-approved twice.  Returns the structured result embedded in the
    suspend/revoke response; every created event is a normal
    ``ApprovalEvent`` flagged ``auto_approved`` with ``release_revision`` so
    historical revision snapshots restore the automatic decisions.
    """
    guard_band = meta.guard_band

    approved: list[dict[str, Any]] = []
    pending_details: list[dict[str, Any]] = []
    # Licenses made effective earlier in this same drain: re-adjudicated
    # in-memory (and excluded from the per-entry narrowed query) so later
    # entries see them in exactly the old full-scan append order.
    newly_effective: list[License] = []
    newly_effective_ids: set[int] = set()
    for entry in load_waitlist(session):
        row = session.get(Application, entry.application_id)
        # Defensive: only genuinely pending applications can be queued; skip
        # anything that has since changed state without its entry being
        # removed.
        if row is None or row.status != "pending":
            continue
        candidate = to_license(row)
        effective = load_candidate_effective_licenses(
            session,
            _candidate_envelope(candidate, guard_band),
            exclude_ids=newly_effective_ids,
            extra_licenses=newly_effective,
        )
        conflicts = adjudication.evaluate(candidate, effective, guard_band)
        window_rows = load_candidate_windows(
            session, _window_hit_envelope(candidate)
        )
        hits = window_hits_for(candidate, window_rows)
        if conflicts or hits:
            # Still blocked (by a license or by a restriction window): keep
            # the queue entry and carry on with the rest.
            detail = {
                "application_id": row.id,
                "conflicts": [
                    serialize_conflict(c, guard_band) for c in conflicts
                ],
                "restriction_window_conflicts": [
                    serialize_window_hit(h) for h in hits
                ],
            }
            pending_details.append(detail)
            continue

        # Passes now: approve immediately, one more revision, same band.
        meta.revision += 1
        session.add(
            ApprovalEvent(
                revision=meta.revision,
                application_id=row.id,
                guard_band=guard_band,
                auto_approved=True,
                release_revision=release_revision,
            )
        )
        row.status = "approved"
        row.approval_revision = meta.revision
        session.delete(entry)
        # A waitlist auto-approval decides the application: any booked
        # scheduled appointment is invalidated in the same transaction so it
        # can never fire afterwards; the invalidation is visible only from
        # the auto-approval's own revision.
        invalidate_scheduled_approvals(
            session, row.id, "waitlist_auto_approval", meta.revision
        )
        newly_effective.append(candidate)
        newly_effective_ids.add(row.id)
        approved.append(
            {
                "application_id": row.id,
                "revision": meta.revision,
                "guard_band": guard_band,
                "release_revision": release_revision,
                "application": serialize_application(row),
            }
        )

    return {
        "auto_approved_application_ids": [a["application_id"] for a in approved],
        "auto_approvals": approved,
        "remaining_waitlist_application_ids": [
            d["application_id"] for d in pending_details
        ],
        "waitlist_pending": pending_details,
        "final_revision": meta.revision,
    }


# --------------------------------------------------------------------------- #
# License handover (hand one effective license to a pending application)
# --------------------------------------------------------------------------- #
def _waitlist_position(session, application_id: int) -> int | None:
    """1-based FIFO position of an application in the standby queue, if any."""
    for position, entry in enumerate(load_waitlist(session), start=1):
        if entry.application_id == application_id:
            return position
    return None


# --------------------------------------------------------------------------- #
# Scheduled auto-approvals (operator-booked UTC appointment per application)
# --------------------------------------------------------------------------- #
# Terminal states of an appointment; only "scheduled" rows ever fire and only
# one of them may exist per application (partial unique index).
SCHEDULED_STATUS = "scheduled"
SCHEDULED_APPROVED = "approved"
SCHEDULED_BLOCKED = "blocked"
SCHEDULED_CANCELLED = "cancelled"
SCHEDULED_INVALIDATED = "invalidated"
SCHEDULED_TERMINAL_STATES = (
    SCHEDULED_APPROVED,
    SCHEDULED_BLOCKED,
    SCHEDULED_CANCELLED,
    SCHEDULED_INVALIDATED,
)


def invalidate_scheduled_approvals(
    session, application_id: int, reason: str, decision_revision: int
) -> list[ScheduledApproval]:
    """Invalidate every still-waiting appointment of a decided application.

    Called inside the same transaction that decides the application through
    another path (manual single/batch approval, waitlist auto-approval during
    a suspend/revoke/emergency-release drain, handover successor approval or
    an emergency occupation).  Only ``scheduled`` rows move; already terminal
    appointments are left untouched.  ``decision_revision`` is the revision of
    the deciding event: the invalidation becomes visible only from that
    revision on, so as-of snapshots at earlier revisions still show the
    waiting appointment.  Returns the rows invalidated.
    """
    rows = (
        session.query(ScheduledApproval)
        .filter(
            ScheduledApproval.application_id == application_id,
            ScheduledApproval.status == SCHEDULED_STATUS,
        )
        .all()
    )
    now = datetime.now(timezone.utc)
    for row in rows:
        row.status = SCHEDULED_INVALIDATED
        row.outcome_reason = reason
        row.outcome_revision = decision_revision
        row.outcome_at = now
        row.updated_at = now
    return rows


def serialize_scheduled_approval(
    session, booking: ScheduledApproval
) -> dict[str, Any]:
    """Public shape of one appointment (waiting, processed or cancelled)."""
    app_row = session.get(Application, booking.application_id)
    body: dict[str, Any] = {
        "id": booking.id,
        "application_id": booking.application_id,
        "scheduled_at": iso(booking.scheduled_at),
        "status": booking.status,
        "reason": booking.reason,
        "created_at": iso(booking.created_at),
        "updated_at": iso(booking.updated_at),
        "waitlist_position": _waitlist_position(
            session, booking.application_id
        ),
        "application": (
            serialize_application(app_row) if app_row is not None else None
        ),
    }
    if booking.processed_at is not None:
        # The UTC instant the appointment was actually re-adjudicated.
        body["processed_at"] = iso(booking.processed_at)
    if booking.processed_guard_band is not None:
        body["processed_guard_band"] = booking.processed_guard_band
    if booking.approval_revision is not None:
        body["approval_revision"] = booking.approval_revision
    if booking.blocked_details is not None:
        body["blocked_details"] = booking.blocked_details
    if booking.outcome_reason is not None:
        body["outcome_reason"] = booking.outcome_reason
    # Revision-visibility metadata: first revision the appointment existed
    # at, and first revision its terminal outcome shows up at (null while
    # still waiting).  Additive fields; live appointments booked at the
    # current revision normally carry ``booked_revision == current_revision``.
    body["booked_revision"] = (
        booking.booked_revision if booking.booked_revision is not None else 0
    )
    body["outcome_revision"] = booking.outcome_revision
    return body


def get_active_schedule(session, application_id: int) -> ScheduledApproval | None:
    """The application's one waiting appointment, if any."""
    return (
        session.query(ScheduledApproval)
        .filter(
            ScheduledApproval.application_id == application_id,
            ScheduledApproval.status == SCHEDULED_STATUS,
        )
        .one_or_none()
    )


def record_schedule_change(
    session,
    booking: ScheduledApproval,
    change_type: str,
    revision: int,
    *,
    scheduled_at: datetime,
    reason: str,
) -> ScheduledApprovalChange:
    """Append one booking/reschedule journal entry (no revision consumed).

    The entry captures the revision in force at move time so as-of snapshots
    can restore the appointment instant/reason visible then.
    """
    entry = ScheduledApprovalChange(
        scheduled_approval_id=booking.id,
        application_id=booking.application_id,
        change_type=change_type,
        revision=revision,
        scheduled_at=scheduled_at,
        reason=reason,
    )
    session.add(entry)
    return entry


def process_due_scheduled_approvals(
    session,
    meta: RevisionMeta,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Re-adjudicate every due appointment inside the shared arbitration.

    Runs in one transaction while holding the singleton revision lock (so it
    cannot interleave with a manual approval, a suspend/revoke drain, a
    cancellation or a reschedule).  Due appointments are visited strictly in
    ``(scheduled_at, id)`` order and each is decided against the state at the
    *actual processing instant*:

    * the application is still pending, has no interference and hits no
      window -> one ordinary ``ApprovalEvent`` flagged ``scheduled`` consumes
      the next revision; a queued application leaves the waitlist first; the
      appointment becomes ``approved`` and the new license joins the
      effective set for the following appointments;
    * it clashes with an effective license or hits a window -> the blockers
      are recorded, the appointment becomes ``blocked``, the application stays
      pending and **no** revision is consumed; there is no automatic retry;
    * the application is no longer pending (a concurrent decision committed
      earlier under the lock invalidated the appointment) -> the row is
      skipped, it never fires.

    A scheduled approval never triggers a waitlist re-adjudication: other
    standbys keep their place.  Returns one result dict per processed
    appointment (approved or blocked).
    """
    current = now if now is not None else datetime.now(timezone.utc)
    current = current.astimezone(timezone.utc)

    due = (
        session.query(ScheduledApproval)
        .filter(
            ScheduledApproval.status == SCHEDULED_STATUS,
            ScheduledApproval.scheduled_at <= _stored_instant(current),
        )
        .order_by(ScheduledApproval.scheduled_at, ScheduledApproval.id)
        .all()
    )
    results: list[dict[str, Any]] = []
    for booking in due:
        # The booking may have been invalidated by an earlier decision in the
        # same transaction (defensive: the query predates those writes).
        if booking.status != SCHEDULED_STATUS:
            continue
        row = session.get(Application, booking.application_id)
        booking.processed_at = current
        booking.processed_guard_band = meta.guard_band

        if row is None or row.status != "pending":
            # Decided by another path first; that path invalidated the row.
            # Defensive backstop (e.g. an older row): derive the deciding
            # revision from the application's last approval event so the
            # invalidation is visible from the correct revision onward.
            if row is not None:
                decided = (
                    session.query(ApprovalEvent)
                    .filter(ApprovalEvent.application_id == row.id)
                    .order_by(ApprovalEvent.revision.asc())
                    .first()
                )
                invalidate_scheduled_approvals(
                    session,
                    row.id,
                    f"application_{row.status}",
                    decided.revision if decided is not None else meta.revision,
                )
            continue

        candidate = to_license(row)
        effective = load_candidate_effective_licenses(
            session, _candidate_envelope(candidate, meta.guard_band)
        )
        conflicts = adjudication.evaluate(
            candidate, effective, meta.guard_band
        )
        window_rows = load_candidate_windows(
            session, _window_hit_envelope(candidate)
        )
        window_hits = window_hits_for(candidate, window_rows)

        if conflicts or window_hits:
            # Blocked at processing time: stay pending, consume no revision,
            # record the blockers once and never retry automatically.  The
            # block is visible only from the revision currently in force
            # (blocking itself consumes none).
            booking.status = SCHEDULED_BLOCKED
            booking.outcome_revision = meta.revision
            booking.outcome_at = current
            booking.blocked_details = {
                "conflicts": [
                    serialize_conflict(c, meta.guard_band) for c in conflicts
                ],
                "restriction_window_conflicts": [
                    serialize_window_hit(h) for h in window_hits
                ],
            }
            results.append(
                {
                    "scheduled_approval_id": booking.id,
                    "application_id": row.id,
                    "outcome": SCHEDULED_BLOCKED,
                    "processed_at": iso(current),
                    "guard_band": meta.guard_band,
                    "blocked_details": booking.blocked_details,
                }
            )
            continue

        # Passes: a queued application leaves the standby queue first.
        entry = (
            session.query(WaitlistEntry)
            .filter(WaitlistEntry.application_id == row.id)
            .one_or_none()
        )
        was_waitlisted = entry is not None
        if entry is not None:
            session.delete(entry)

        meta.revision += 1
        session.add(
            ApprovalEvent(
                revision=meta.revision,
                application_id=row.id,
                guard_band=meta.guard_band,
                auto_approved=False,
                release_revision=None,
                scheduled=True,
            )
        )
        row.status = "approved"
        row.approval_revision = meta.revision
        booking.status = SCHEDULED_APPROVED
        booking.approval_revision = meta.revision
        # The approved outcome (and its revision) is visible only from the
        # revision the due approval just consumed.
        booking.outcome_revision = meta.revision
        booking.outcome_at = current
        results.append(
            {
                "scheduled_approval_id": booking.id,
                "application_id": row.id,
                "outcome": SCHEDULED_APPROVED,
                "processed_at": iso(current),
                "revision": meta.revision,
                "guard_band": meta.guard_band,
                "was_waitlisted": was_waitlisted,
            }
        )
        # The new license participates in the adjudication of the later due
        # appointments processed by this same sweep: each appointment runs
        # its own narrowed effective-set query, which auto-flushes the
        # approved status before executing.
    return results


def run_scheduled_approval_sweep(*, now: datetime | None = None) -> dict[str, Any]:
    """Process every due appointment in one revision-arbitrated transaction.

    Each due appointment gets its own outcome; approved ones consume one
    revision each (a blocked appointment consumes none).  Safe to call from
    the background loop, the startup catch-up pass or the explicit
    administration trigger.  Returns a summary; on conflict the transaction
    is rolled back and the next sweep retries (an unprocessed appointment is
    still ``scheduled``, so it is never lost or double-approved).
    """
    processed: list[dict[str, Any]] = []
    from_revision: int | None = None
    final_revision: int | None = None
    with SessionLocal() as session:
        try:
            with session.begin():
                meta = session.get(RevisionMeta, 1, with_for_update=True)
                from_revision = meta.revision
                processed = process_due_scheduled_approvals(
                    session, meta, now=now
                )
                final_revision = meta.revision
        except IntegrityError:
            session.rollback()
            log.warning(
                "scheduled approval sweep lost the revision arbitration; "
                "the next sweep retries the still-scheduled appointments"
            )
            return {
                "processed_count": 0,
                "approved_application_ids": [],
                "blocked_application_ids": [],
                "results": [],
                "from_revision": None,
                "final_revision": None,
                "retried": True,
            }
    return {
        "processed_count": len(processed),
        "approved_application_ids": [
            r["application_id"]
            for r in processed
            if r["outcome"] == SCHEDULED_APPROVED
        ],
        "blocked_application_ids": [
            r["application_id"]
            for r in processed
            if r["outcome"] == SCHEDULED_BLOCKED
        ],
        "results": processed,
        "from_revision": from_revision,
        "final_revision": final_revision,
    }


def transfer_preview_body(
    session,
    payload: LicenseTransferRequest,
    *,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Shared read-only rehearsal for an effective->pending license handover.

    The successor is adjudicated against the current effective set with the
    source license excluded (the whole point of the swap is that the source
    gives up its spectrum) and against every active restriction window (the
    guard band never extends a window).  The computation never mutates
    state.  Status problems are reported as non-fatal ``blockers`` rather
    than raised, so an operator can rehearse at any time and see exactly why
    a handover would not commit; the confirm endpoint enforces them hard.

    An approved source that still holds an unreleased emergency occupation
    is reported as an ``emergency_license_protected`` blocker: an emergency
    license ends only through its dedicated release, never through a swap.
    """
    meta = session.get(RevisionMeta, 1)
    current = meta.revision
    guard_band = meta.guard_band

    source = session.get(Application, payload.source_application_id)
    successor = session.get(Application, payload.successor_application_id)

    blockers: list[dict[str, Any]] = []
    if source is None:
        blockers.append(
            {
                "code": E_NOT_FOUND,
                "field": "source_application_id",
                "message": (
                    f"application {payload.source_application_id} does not exist"
                ),
            }
        )
    elif source.status != "approved":
        blockers.append(
            {
                "code": E_SOURCE_NOT_EFFECTIVE,
                "field": "source_application_id",
                "message": (
                    f"source application {source.id} is {source.status}; only an "
                    "approved, non-suspended, non-revoked license can be handed over"
                ),
                "status": source.status,
            }
        )
    elif (
        # An approved license that still holds an unreleased emergency
        # occupation is protected exactly as against ordinary suspend/revoke:
        # the emergency ends only via its dedicated release, so it can never
        # be the source of an equipment-swap handover.  A released occupation
        # leaves the application revoked (or otherwise non-approved), which
        # the source_not_effective branch above already reports.
        session.query(EmergencyOccupation)
        .filter(
            EmergencyOccupation.emergency_application_id == source.id,
            EmergencyOccupation.released_revision.is_(None),
        )
        .one_or_none()
        is not None
    ):
        blockers.append(
            {
                "code": E_EMERGENCY_LICENSE_PROTECTED,
                "field": "source_application_id",
                "message": (
                    f"source application {source.id} holds an active emergency "
                    "occupation; it can only be ended by releasing the emergency "
                    "occupation, so the license cannot be handed over"
                ),
                "status": source.status,
            }
        )
    if successor is None:
        blockers.append(
            {
                "code": E_NOT_FOUND,
                "field": "successor_application_id",
                "message": (
                    f"application {payload.successor_application_id} does not exist"
                ),
            }
        )
    elif successor.status != "pending":
        blockers.append(
            {
                "code": E_SUCCESSOR_NOT_PENDING,
                "field": "successor_application_id",
                "message": (
                    f"successor application {successor.id} is "
                    f"{successor.status}; only a pending application can take over"
                ),
                "status": successor.status,
            }
        )

    # Adjudicate the successor with the source license removed from the
    # effective set.  The successor is pending so it is never itself in that
    # set; the id filter also keeps the exclusion meaningful in every state.
    # The effective set is narrowed in the database by the successor's time
    # window, coverage and guard-widened frequency envelope first.
    excluded_ids = {
        payload.source_application_id,
        payload.successor_application_id,
    }
    conflicts: list = []
    window_hits: list = []
    if successor is not None:
        candidate = to_license(successor)
        effective = load_candidate_effective_licenses(
            session,
            _candidate_envelope(candidate, guard_band),
            exclude_ids=excluded_ids,
        )
        window_rows = load_candidate_windows(
            session, _window_hit_envelope(candidate)
        )
        conflicts = adjudication.evaluate(candidate, effective, guard_band)
        window_hits = window_hits_for(candidate, window_rows)
    else:
        effective = []
        window_rows = []
    all_window_ids = load_active_window_ids(session)

    if conflicts:
        blockers.append(
            {
                "code": E_INTERFERENCE,
                "field": "successor_application_id",
                "message": (
                    f"successor would interfere with {len(conflicts)} effective "
                    "license(s) even after the source license is released"
                ),
            }
        )
    if window_hits:
        blockers.append(
            {
                "code": E_RESTRICTION_WINDOW,
                "field": "successor_application_id",
                "message": (
                    f"successor hits {len(window_hits)} active restriction "
                    "window(s); the handover cannot make it effective"
                ),
            }
        )

    return {
        "source_application_id": payload.source_application_id,
        "successor_application_id": payload.successor_application_id,
        "reason": payload.reason,
        "current_revision": current,
        "expected_revision": expected_revision,
        "guard_band": guard_band,
        "ready": not blockers,
        "blockers": blockers,
        "source_application": (
            serialize_application(source) if source is not None else None
        ),
        "successor_application": (
            serialize_application(successor) if successor is not None else None
        ),
        # Whether the successor currently occupies a standby slot (it leaves
        # the queue before its approval on a successful handover).
        "successor_waitlist_position": (
            _waitlist_position(session, payload.successor_application_id)
            if successor is not None
            else None
        ),
        # Effective set the successor is adjudicated against: the live set
        # with the source license excluded per the current guard band.
        "effective_application_ids": [lic.application_id for lic in effective],
        "has_conflicts": bool(conflicts),
        "conflicts": [serialize_conflict(c, guard_band) for c in conflicts],
        "has_restriction_window_conflicts": bool(window_hits),
        "restriction_window_conflicts": [
            serialize_window_hit(h) for h in window_hits
        ],
        "active_restriction_window_ids": all_window_ids,
        # On a successful confirm the two revisions the handover consumes.
        "planned_revoc_revision": current + 1,
        "planned_successor_revision": current + 2,
    }


# --------------------------------------------------------------------------- #
# Revision snapshots
# --------------------------------------------------------------------------- #
def guard_band_at(session, revision: int) -> float:
    """Guard band in force at ``revision`` (latest change at or before it)."""
    event = (
        session.query(GuardBandEvent)
        .filter(GuardBandEvent.revision <= revision)
        .order_by(GuardBandEvent.revision.desc())
        .first()
    )
    if event is not None:
        return event.guard_band
    # Defensive: the revision-0 seed guarantees an event; fall back to the
    # live setting if it is somehow missing.
    meta = session.get(RevisionMeta, 1)
    return meta.guard_band if meta is not None else settings.guard_band


def build_snapshot(session, revision: int) -> dict[str, Any]:
    """Reconstruct the state of the world *at* ``revision``.

    Returns every approval event with revision <= the target and the
    application snapshot, replays the approval/suspension/resumption/
    revocation timeline of the period to flag each grant's status as of the
    target revision, and includes all suspension, resumption and revocation
    events of the period, plus the guard band in force at that revision and
    the guard-band records behind it.  Original approval events are never
    rewritten: a resumption is a separate event carrying its own band
    snapshot.
    """
    approvals = (
        session.query(ApprovalEvent)
        .filter(ApprovalEvent.revision <= revision)
        .order_by(ApprovalEvent.revision)
        .all()
    )
    revocations = (
        session.query(RevocationEvent)
        .filter(RevocationEvent.revision <= revision)
        .order_by(RevocationEvent.revision)
        .all()
    )
    transfers = (
        session.query(LicenseTransferEvent)
        .filter(LicenseTransferEvent.revision <= revision)
        .order_by(LicenseTransferEvent.revision)
        .all()
    )
    suspensions = (
        session.query(SuspensionEvent)
        .filter(SuspensionEvent.revision <= revision)
        .order_by(SuspensionEvent.revision)
        .all()
    )
    resumptions = (
        session.query(ResumptionEvent)
        .filter(ResumptionEvent.revision <= revision)
        .order_by(ResumptionEvent.revision)
        .all()
    )
    guard_events = (
        session.query(GuardBandEvent)
        .filter(GuardBandEvent.revision <= revision)
        .order_by(GuardBandEvent.revision)
        .all()
    )
    window_events = (
        session.query(RestrictionWindowEvent)
        .filter(RestrictionWindowEvent.revision <= revision)
        .order_by(RestrictionWindowEvent.revision)
        .all()
    )
    occupations = (
        session.query(EmergencyOccupation)
        .filter(EmergencyOccupation.emergency_approval_revision <= revision)
        .order_by(EmergencyOccupation.id)
        .all()
    )
    scheduled_rows = (
        session.query(ScheduledApproval)
        .order_by(ScheduledApproval.id)
        .all()
    )
    schedule_changes = (
        session.query(ScheduledApprovalChange)
        .order_by(ScheduledApprovalChange.id)
        .all()
    )

    # Replay every lifecycle event up to the target revision in revision
    # order; the last event of each application decides its as-of state.
    # (revision, kind, application_id, event); every revision has exactly
    # one event, so the revision key alone fully determines the order.
    lifecycle: list[tuple[int, str, int, Any]] = []
    lifecycle.extend(
        (ev.revision, "approval", ev.application_id, ev) for ev in approvals
    )
    lifecycle.extend(
        (ev.revision, "suspension", ev.application_id, ev) for ev in suspensions
    )
    lifecycle.extend(
        (ev.revision, "resumption", ev.application_id, ev) for ev in resumptions
    )
    lifecycle.extend(
        (ev.revision, "revocation", ev.application_id, ev) for ev in revocations
    )
    lifecycle.sort(key=lambda item: item[0])
    # state[app_id] = (state, latest marker revision): marker is the
    # suspension revision while suspended, the revocation revision once
    # revoked, and irrelevant while approved.
    state: dict[int, tuple[str, int]] = {}
    # as_of[app_id] = the mutable Application fields (status, approval and
    # suspension revisions, emergency force-suspension source) replayed to
    # the target revision, mirroring exactly how the live row is written by
    # the approve/suspend/resume/revoke/emergency-release handlers.  The
    # embedded applications of the approval records below are serialised
    # from these replayed values, so a historical view stays consistent
    # with the event replay and never leaks later writes.
    as_of: dict[int, dict[str, Any]] = {}
    for rev, kind, app_id, event in lifecycle:
        fields = as_of.setdefault(
            app_id,
            {
                "status": "approved",
                "approval_revision": None,
                "suspended_revision": None,
                "force_suspended_by": None,
            },
        )
        if kind == "suspension":
            state[app_id] = ("suspended", rev)
            fields["status"] = "suspended"
            fields["suspended_revision"] = rev
            # A forced suspension (emergency occupation) names the emergency
            # application that forced the license out; an ordinary outage
            # clears the marker.
            fields["force_suspended_by"] = (
                event.forced_by_emergency_application_id
                if event.forced
                else None
            )
        elif kind == "revocation":
            state[app_id] = ("revoked", rev)
            fields["status"] = "revoked"
            # The suspension revision stays as a historical marker (the live
            # row keeps it too); the emergency marker is dropped on revoke.
            fields["force_suspended_by"] = None
            if event.emergency_release and (
                event.emergency_application_id is not None
            ):
                # Ending the occupation lifts the forced marker of every
                # license it still holds: from the release revision on they
                # are ordinary outages, whether their restoration lands on a
                # later revision or they stay suspended.
                for other in as_of.values():
                    if (
                        other["force_suspended_by"]
                        == event.emergency_application_id
                    ):
                        other["force_suspended_by"] = None
        elif kind == "resumption":
            state[app_id] = ("approved", rev)
            fields["status"] = "approved"
            fields["suspended_revision"] = None
            fields["force_suspended_by"] = None
        else:  # approval
            state[app_id] = ("approved", rev)
            fields["status"] = "approved"
            fields["approval_revision"] = rev
            fields["suspended_revision"] = None
            fields["force_suspended_by"] = None

    approval_records: list[dict[str, Any]] = []
    effective_ids: list[int] = []
    suspended_ids: list[int] = []
    for ev in approvals:
        app = session.get(Application, ev.application_id)
        status_as_of, marker = state.get(
            ev.application_id, ("approved", ev.revision)
        )
        # The embedded application is rewound to the replayed as-of state:
        # its status, approval/suspension revisions and emergency
        # force-suspension source are exactly what the event stream shows at
        # the queried revision, never the live row's later values.
        replayed = as_of.get(ev.application_id)
        if app is None:
            application_view = None
        elif replayed is not None:
            application_view = serialize_application_as_of(app, replayed)
        else:  # defensive: an approval always replays its own event first
            application_view = serialize_application(app)
        record = {
            "revision": ev.revision,
            "application_id": ev.application_id,
            "guard_band": ev.guard_band,
            # Automatic approvals from a waitlist release share the ordinary
            # approval event stream; the flags let history restore which
            # decisions were automatic and which release triggered them.
            "auto_approved": bool(ev.auto_approved),
            "release_revision": ev.release_revision,
            # An approval granting the license of an active/released
            # burst-communication emergency occupation.
            "emergency": bool(ev.emergency),
            # An approval granted by a due operator-booked scheduled
            # auto-approval appointment (keeps the ordinary approval event
            # semantics; auto_approved stays false for these).
            "scheduled": bool(ev.scheduled),
            "created_at": iso(ev.created_at),
            # Status of this grant *as of the queried revision*.
            "status_as_of": status_as_of,
            "application": application_view,
        }
        if status_as_of == "revoked":
            record["revoked_at_revision"] = marker
        elif status_as_of == "suspended":
            record["suspended_at_revision"] = marker
            suspended_ids.append(ev.application_id)
        else:
            effective_ids.append(ev.application_id)
        approval_records.append(record)

    revocation_records = [
        {
            "revision": ev.revision,
            "application_id": ev.application_id,
            "reason": ev.reason,
            "emergency_release": bool(ev.emergency_release),
            "emergency_application_id": ev.emergency_application_id,
            "created_at": iso(ev.created_at),
        }
        for ev in revocations
    ]
    transfer_records = [
        {
            "revision": ev.revision,
            "source_application_id": ev.source_application_id,
            "successor_application_id": ev.successor_application_id,
            "successor_approval_revision": ev.successor_approval_revision,
            "reason": ev.reason,
            "created_at": iso(ev.created_at),
        }
        for ev in transfers
    ]
    suspension_records = [
        {
            "revision": ev.revision,
            "application_id": ev.application_id,
            "reason": ev.reason,
            "forced": bool(ev.forced),
            "forced_by_emergency_application_id": (
                ev.forced_by_emergency_application_id
            ),
            "created_at": iso(ev.created_at),
        }
        for ev in suspensions
    ]
    resumption_records = [
        {
            "revision": ev.revision,
            "application_id": ev.application_id,
            "guard_band": ev.guard_band,
            "reason": ev.reason,
            "forced_recovery": bool(ev.forced_recovery),
            "emergency_application_id": ev.emergency_application_id,
            "created_at": iso(ev.created_at),
        }
        for ev in resumptions
    ]

    # Replay restriction-window lifecycle rows: an activation inserts the
    # window (keyed by its stable window id), a release removes it.  The
    # result is exactly the set of windows active *at* the target revision;
    # expired-but-not-released windows remain active as adjudication rules
    # (a window is only lifted by an explicit release).
    active_windows: dict[int, RestrictionWindow] = {}
    window_activated_revision: dict[int, int] = {}
    window_records: list[dict[str, Any]] = []
    for ev in window_events:
        record = {
            "revision": ev.revision,
            "event_type": ev.event_type,
            "window_id": ev.window_id,
            "reason": ev.reason,
            "created_at": iso(ev.created_at),
        }
        if ev.event_type == "activated":
            window = RestrictionWindow(
                window_id=ev.window_id,
                circle=Circle(ev.cx, ev.cy, ev.radius),
                freq_low=ev.freq_low,
                freq_high=ev.freq_high,
                start_time=_utc(ev.start_time),
                end_time=_utc(ev.end_time),
                reason=ev.reason,
                revision=ev.revision,
            )
            active_windows[ev.window_id] = window
            window_activated_revision[ev.window_id] = ev.revision
            record["window"] = serialize_window(window)
        elif ev.event_type == "released":
            active_windows.pop(ev.window_id, None)
        window_records.append(record)

    active_window_records = [
        serialize_window(
            active_windows[wid],
            activated_at=None,
        )
        for wid in sorted(active_windows)
    ]

    # Replay emergency occupations: an occupation exists at revisions at or
    # after its emergency approval revision and stays active until its release
    # revision (exclusive).  The forced suspensions / forced recoveries /
    # emergency approval / release revocation are already ordinary events in
    # the lifecycle replay above; these records only link them for audit.
    last_suspension: dict[int, SuspensionEvent] = {}
    for ev in suspensions:
        prev = last_suspension.get(ev.application_id)
        if prev is None or ev.revision >= prev.revision:
            last_suspension[ev.application_id] = ev

    active_emergency_ids: list[int] = []
    emergency_records: list[dict[str, Any]] = []
    for occ in occupations:
        is_active = (
            occ.released_revision is None or occ.released_revision > revision
        )
        forced_events_as_of = [
            ev
            for ev in suspensions
            if ev.forced
            and ev.forced_by_emergency_application_id
            == occ.emergency_application_id
        ]
        recovered_as_of = {
            ev.application_id
            for ev in resumptions
            if ev.forced_recovery
            and ev.emergency_application_id == occ.emergency_application_id
        }
        revoked_as_of = {
            ev.application_id
            for ev in revocations
            if ev.revision <= revision
        }
        still_forced: list[int] = []
        if is_active:
            # While the occupation is active every forced license is still
            # forced out, unless it was revoked in the meantime.
            for ev in forced_events_as_of:
                if ev.application_id not in revoked_as_of:
                    still_forced.append(ev.application_id)
        record = {
            "id": occ.id,
            "application_id": occ.emergency_application_id,
            "emergency_application_id": occ.emergency_application_id,
            "reason": occ.reason,
            "emergency_approval_revision": occ.emergency_approval_revision,
            "first_force_suspension_revision": (
                occ.first_force_suspension_revision
            ),
            "last_force_suspension_revision": (
                occ.last_force_suspension_revision
            ),
            "force_suspended_application_ids": [
                ev.application_id for ev in forced_events_as_of
            ],
            "force_suspensions": [
                {
                    "revision": ev.revision,
                    "application_id": ev.application_id,
                    "reason": ev.reason,
                }
                for ev in forced_events_as_of
            ],
            "recovered_application_ids": sorted(recovered_as_of),
            "released_revision": occ.released_revision,
            "release_reason": occ.release_reason,
            "status": "active" if is_active else "released",
        }
        emergency_records.append(record)
        if is_active:
            active_emergency_ids.append(occ.emergency_application_id)
            still_forced_set = set(still_forced)
            # Only genuinely still-suspended licenses count as force
            # suspended at the revision.
            for app_id in list(still_forced):
                status_as_of, _ = state.get(app_id, ("approved", 0))
                if status_as_of != "suspended":
                    still_forced_set.discard(app_id)
            record["still_force_suspended_application_ids"] = sorted(
                still_forced_set
            )
        else:
            record["still_force_suspended_application_ids"] = []

    # Force-suspended licenses as of the revision: suspended whose latest
    # suspension is a forced one belonging to an emergency occupation that
    # had started (the forced suspension itself already consumes a revision
    # before the emergency approval, so intermediate revisions restore
    # correctly) and had not yet been released at or before the target
    # revision (once the emergency release begins, the license is no longer
    # held by an active emergency, even if its restoration lands on the next
    # revision).
    released_emergency_ids = {
        occ.emergency_application_id
        for occ in occupations
        if occ.released_revision is not None
        and occ.released_revision <= revision
    }
    active_emergency_set = set(active_emergency_ids)
    force_suspended_ids: list[int] = []
    for app_id, (status_as_of, _marker) in state.items():
        if status_as_of != "suspended":
            continue
        susp = last_suspension.get(app_id)
        if (
            susp is not None
            and susp.forced
            and (susp.forced_by_emergency_application_id or 0)
            not in released_emergency_ids
        ):
            force_suspended_ids.append(app_id)
    force_suspended_ids.sort()

    emergency_occupation_events: list[dict[str, Any]] = []
    for occ in occupations:
        emergency_occupation_events.append(
            {
                "revision": occ.emergency_approval_revision,
                "event_type": "activated",
                "application_id": occ.emergency_application_id,
                "emergency_application_id": occ.emergency_application_id,
                "reason": occ.reason,
            }
        )
        if occ.released_revision is not None and occ.released_revision <= revision:
            emergency_occupation_events.append(
                {
                    "revision": occ.released_revision,
                    "event_type": "released",
                    "application_id": occ.emergency_application_id,
                    "emergency_application_id": occ.emergency_application_id,
                    "reason": occ.release_reason,
                }
            )
    emergency_occupation_events.sort(key=lambda ev: ev["revision"])

    # Operator-booked appointments reconstructed as of the target revision.
    # Bookings/reschedules/cancellations consume no revision, so visibility
    # follows the revision in force at the time:
    # * an appointment booked at revision B is absent from every revision < B
    #   (it did not exist yet);
    # * a terminal outcome visible only from revision O rewinds to the
    #   still-waiting shape at every revision < O -- the later approval,
    #   block, cancellation or invalidation must never leak into earlier
    #   revisions (no audit time travel);
    # * the booked instant/reason come from the latest journal move with
    #   revision <= target, so a later reschedule does not move the
    #   appointment in older revisions; several moves within the same
    #   revision collapse to the latest one.
    changes_by_booking: dict[int, list[ScheduledApprovalChange]] = {}
    for change in schedule_changes:
        changes_by_booking.setdefault(change.scheduled_approval_id, []).append(
            change
        )

    scheduled_records: list[dict[str, Any]] = []
    for booking in scheduled_rows:
        booked_revision = (
            booking.booked_revision
            if booking.booked_revision is not None
            else 0
        )
        if booked_revision > revision:
            # Booked in a later revision: not visible yet.
            continue

        visible_moves = [
            change
            for change in changes_by_booking.get(booking.id, [])
            if change.revision <= revision
        ]
        if visible_moves:
            latest_move = visible_moves[-1]
            scheduled_at = latest_move.scheduled_at
            reason = latest_move.reason
        else:
            # Defensive fallback (rows predating the journal): the live row
            # is the only shape available.
            scheduled_at = booking.scheduled_at
            reason = booking.reason

        outcome_revision = booking.outcome_revision
        outcome_visible = (
            booking.status != SCHEDULED_STATUS
            and outcome_revision is not None
            and outcome_revision <= revision
        )
        if outcome_visible:
            status = booking.status
            processed_at = (
                iso(booking.outcome_at)
                if booking.outcome_at is not None
                else (
                    iso(booking.processed_at)
                    if booking.processed_at is not None
                    else None
                )
            )
            # The approval revision is itself the decision revision: it must
            # not appear in earlier revisions, so it is only populated once
            # the outcome is visible.
            approval_revision = booking.approval_revision
            processed_guard_band = booking.processed_guard_band
            blocked_details = booking.blocked_details
            outcome_reason = booking.outcome_reason
            updated_at = booking.outcome_at or booking.updated_at
        else:
            # Earlier revision (or an outcome on the live row not decided
            # yet): present the appointment as still waiting.
            status = SCHEDULED_STATUS
            processed_at = None
            approval_revision = None
            processed_guard_band = None
            blocked_details = None
            outcome_reason = None
            updated_at = (
                latest_move.created_at
                if visible_moves
                else booking.updated_at
            )

        record = {
            "id": booking.id,
            "application_id": booking.application_id,
            "scheduled_at": iso(scheduled_at),
            "status": status,
            "reason": reason,
            "processed_at": processed_at,
            "approval_revision": approval_revision,
            "processed_guard_band": processed_guard_band,
            "blocked_details": blocked_details,
            "outcome_reason": outcome_reason,
            "created_at": iso(booking.created_at),
            "updated_at": iso(updated_at),
            # Revision-visibility metadata: first revision the appointment
            # existed at, and first revision its terminal outcome (if any)
            # shows up at (null while still waiting or before visibility).
            "booked_revision": booked_revision,
            "outcome_revision": (
                outcome_revision if outcome_visible else None
            ),
        }
        scheduled_records.append(record)

    return {
        "revision": revision,
        "guard_band": guard_band_at(session, revision),
        "guard_band_changes": [
            {
                "revision": ev.revision,
                "previous_guard_band": ev.previous_guard_band,
                "guard_band": ev.guard_band,
                "created_at": iso(ev.created_at),
            }
            for ev in guard_events
        ],
        "effective_application_ids": effective_ids,
        "suspended_application_ids": suspended_ids,
        # Ordinary licenses forced out by an emergency occupation still
        # active at the revision (a subset of suspended_application_ids).
        "force_suspended_application_ids": force_suspended_ids,
        # Applications holding an emergency license at the revision (a subset
        # of effective_application_ids).
        "active_emergency_application_ids": sorted(active_emergency_set),
        "emergency_occupations": emergency_records,
        "emergency_occupation_events": emergency_occupation_events,
        "active_restriction_window_ids": sorted(active_windows),
        "active_restriction_windows": active_window_records,
        "restriction_window_events": window_records,
        "approvals": approval_records,
        "suspensions": suspension_records,
        "resumptions": resumption_records,
        "revocations": revocation_records,
        "license_transfers": transfer_records,
        # Operator-booked auto-approval appointments reconstructed as of the
        # target revision.  Bookings consume no revision, so an appointment
        # appears from its ``booked_revision`` on; a scheduled *approval* is
        # also one of the ordinary approval records above, flagged
        # ``scheduled: true``.  Terminal outcomes (approval/block/cancel/
        # invalidation) show only from their deciding revision -- older
        # snapshots still show the waiting appointment and never leak the
        # later approval revision.
        "scheduled_approvals": scheduled_records,
    }


def _snapshot_status_map(snapshot: dict[str, Any]) -> dict[int, str]:
    """Latest grant status represented by an as-of snapshot.

    Applications without a lifecycle event at or before the revision are not
    grants yet and are treated by callers as ``pending`` where relevant.
    """
    statuses: dict[int, str] = {}
    for approval in snapshot["approvals"]:
        statuses[approval["application_id"]] = approval["status_as_of"]
    return statuses


def build_revision_comparison(
    session,
    base_revision: int,
    target_revision: int,
    application_id: int | None = None,
) -> dict[str, Any]:
    """Compare two immutable as-of revision snapshots without mutating state.

    The two revisions may be supplied in either chronological direction.  The
    event interval is the half-open span ``(older, newer]`` (matching the
    existing rule that snapshot N includes every event with revision <= N);
    the ``base``/``target`` views retain the caller's labels, while
    ``status_changes`` and ``differences`` follow chronological event order.
    """
    older_revision = min(base_revision, target_revision)
    newer_revision = max(base_revision, target_revision)
    older = build_snapshot(session, older_revision)
    newer = build_snapshot(session, newer_revision)

    # When base is the older revision, these references are already in the
    # caller's base/target order; otherwise swap them while retaining the
    # caller's revision labels in the returned base/target views.
    if base_revision == older_revision:
        base_snapshot, target_snapshot = older, newer
    else:
        base_snapshot, target_snapshot = newer, older

    base_effective = set(base_snapshot["effective_application_ids"])
    target_effective = set(target_snapshot["effective_application_ids"])
    base_suspended = set(base_snapshot["suspended_application_ids"])
    target_suspended = set(target_snapshot["suspended_application_ids"])
    base_windows = set(base_snapshot["active_restriction_window_ids"])
    target_windows = set(target_snapshot["active_restriction_window_ids"])
    base_emergencies = set(
        base_snapshot["active_emergency_application_ids"]
    )
    target_emergencies = set(
        target_snapshot["active_emergency_application_ids"]
    )
    base_force_suspended = set(
        base_snapshot["force_suspended_application_ids"]
    )
    target_force_suspended = set(
        target_snapshot["force_suspended_application_ids"]
    )

    def in_scope(app_id: int) -> bool:
        return application_id is None or app_id == application_id

    events: list[dict[str, Any]] = []

    approvals: list[dict[str, Any]] = []
    for event in newer["approvals"]:
        if event["revision"] <= older_revision or not in_scope(
            event["application_id"]
        ):
            continue
        record = dict(event)
        record["type"] = "approval"
        approvals.append(record)
        events.append(record)

    suspensions: list[dict[str, Any]] = []
    for event in newer["suspensions"]:
        if event["revision"] <= older_revision or not in_scope(
            event["application_id"]
        ):
            continue
        record = dict(event)
        record["type"] = "suspension"
        suspensions.append(record)
        events.append(record)

    resumptions: list[dict[str, Any]] = []
    for event in newer["resumptions"]:
        if event["revision"] <= older_revision or not in_scope(
            event["application_id"]
        ):
            continue
        record = dict(event)
        record["type"] = "resumption"
        resumptions.append(record)
        events.append(record)

    revocations: list[dict[str, Any]] = []
    for event in newer["revocations"]:
        if event["revision"] <= older_revision or not in_scope(
            event["application_id"]
        ):
            continue
        record = dict(event)
        record["type"] = "revocation"
        revocations.append(record)
        events.append(record)

    # Handover links are not license lifecycle events themselves (the
    # revocation and successor approval already appear above), so they are
    # surfaced in their own list and in the unified difference stream but
    # never fed into the per-application status replay.
    license_transfers: list[dict[str, Any]] = []
    for event in newer.get("license_transfers", []):
        if event["revision"] <= older_revision:
            continue
        if application_id is not None and application_id not in (
            event["source_application_id"],
            event["successor_application_id"],
        ):
            continue
        record = dict(event)
        record["type"] = "license_transfer"
        license_transfers.append(record)
        events.append(record)

    guard_band_changes: list[dict[str, Any]] = []
    for event in newer["guard_band_changes"]:
        if event["revision"] <= older_revision:
            continue
        record = dict(event)
        record["type"] = "guard_band_change"
        guard_band_changes.append(record)
        events.append(record)

    restriction_window_events: list[dict[str, Any]] = []
    for event in newer["restriction_window_events"]:
        if event["revision"] <= older_revision:
            continue
        record = dict(event)
        # Normalised event type across the unified difference stream.
        record["type"] = (
            "restriction_window_activated"
            if event["event_type"] == "activated"
            else "restriction_window_released"
        )
        restriction_window_events.append(record)
        events.append(record)

    # Emergency occupation lifecycle links.  The forced suspensions, forced
    # recoveries, emergency approval and release revocation already appear in
    # the ordinary streams above; these records only signal the occupation
    # boundary and are never fed into the status replay.
    emergency_occupation_events: list[dict[str, Any]] = []
    for event in newer.get("emergency_occupation_events", []):
        if event["revision"] <= older_revision:
            continue
        app_id = event["emergency_application_id"]
        if application_id is not None and application_id != app_id:
            continue
        record = dict(event)
        record["type"] = (
            "emergency_occupation_activated"
            if event["event_type"] == "activated"
            else "emergency_occupation_released"
        )
        emergency_occupation_events.append(record)
        events.append(record)

    events.sort(key=lambda event: event["revision"])

    interval_statuses = _snapshot_status_map(older)
    status_changes = []
    for event in events:
        app_id = event.get("application_id")
        if app_id is None:
            continue
        if event["type"] == "license_transfer":
            # A handover link carries two application ids and no single
            # application_id; its status changes are already represented by
            # the linked revocation/approval events.
            continue
        before = interval_statuses.get(app_id, "pending")
        if event["type"] == "approval" or event["type"] == "resumption":
            after = "approved"
        elif event["type"] == "suspension":
            after = "suspended"
        elif event["type"] == "revocation":
            after = "revoked"
        else:
            continue
        if before != after:
            status_changes.append(
                {
                    "revision": event["revision"],
                    "application_id": app_id,
                    "from_status": before,
                    "to_status": after,
                }
            )
            interval_statuses[app_id] = after

    base_statuses = _snapshot_status_map(base_snapshot)
    target_statuses = _snapshot_status_map(target_snapshot)

    base_effective_added = sorted(target_effective - base_effective)
    base_effective_removed = sorted(base_effective - target_effective)
    base_suspended_added = sorted(target_suspended - base_suspended)
    base_suspended_removed = sorted(base_suspended - target_suspended)
    base_windows_added = sorted(target_windows - base_windows)
    base_windows_removed = sorted(base_windows - target_windows)
    base_emergencies_added = sorted(target_emergencies - base_emergencies)
    base_emergencies_removed = sorted(base_emergencies - target_emergencies)
    base_force_added = sorted(target_force_suspended - base_force_suspended)
    base_force_removed = sorted(
        base_force_suspended - target_force_suspended
    )

    def revision_view(
        snapshot: dict[str, Any], statuses: dict[int, str]
    ) -> dict[str, Any]:
        view = {
            "revision": snapshot["revision"],
            "guard_band": snapshot["guard_band"],
            "effective_application_ids": snapshot["effective_application_ids"],
            "suspended_application_ids": snapshot[
                "suspended_application_ids"
            ],
            "active_restriction_window_ids": snapshot[
                "active_restriction_window_ids"
            ],
            "active_emergency_application_ids": snapshot[
                "active_emergency_application_ids"
            ],
            "force_suspended_application_ids": snapshot[
                "force_suspended_application_ids"
            ],
        }
        if application_id is not None:
            view["application_id"] = application_id
            view["application_status"] = statuses.get(
                application_id, "pending"
            )
            view["is_effective"] = application_id in set(
                snapshot["effective_application_ids"]
            )
            view["is_suspended"] = application_id in set(
                snapshot["suspended_application_ids"]
            )
        return view

    return {
        "base_revision": base_revision,
        "target_revision": target_revision,
        "older_revision": older_revision,
        "newer_revision": newer_revision,
        "direction": (
            "forward" if target_revision >= base_revision else "backward"
        ),
        "application_id": application_id,
        "base": revision_view(base_snapshot, base_statuses),
        "target": revision_view(target_snapshot, target_statuses),
        "guard_band": {
            "base": base_snapshot["guard_band"],
            "target": target_snapshot["guard_band"],
            "changed": (
                base_snapshot["guard_band"]
                != target_snapshot["guard_band"]
            ),
        },
        "effective_application_ids": {
            "base": sorted(base_effective),
            "target": sorted(target_effective),
            "added": base_effective_added,
            "removed": base_effective_removed,
            "added_to_target": base_effective_added,
            "removed_from_base": base_effective_removed,
        },
        "suspended_application_ids": {
            "base": sorted(base_suspended),
            "target": sorted(target_suspended),
            "added": base_suspended_added,
            "removed": base_suspended_removed,
            "added_to_target": base_suspended_added,
            "removed_from_base": base_suspended_removed,
        },
        "active_restriction_window_ids": {
            "base": sorted(base_windows),
            "target": sorted(target_windows),
            "added": base_windows_added,
            "removed": base_windows_removed,
            "added_to_target": base_windows_added,
            "removed_from_base": base_windows_removed,
        },
        "active_emergency_application_ids": {
            "base": sorted(base_emergencies),
            "target": sorted(target_emergencies),
            "added": base_emergencies_added,
            "removed": base_emergencies_removed,
            "added_to_target": base_emergencies_added,
            "removed_from_base": base_emergencies_removed,
        },
        "force_suspended_application_ids": {
            "base": sorted(base_force_suspended),
            "target": sorted(target_force_suspended),
            "added": base_force_added,
            "removed": base_force_removed,
            "added_to_target": base_force_added,
            "removed_from_base": base_force_removed,
        },
        "status_changes": status_changes,
        "approvals": approvals,
        "suspensions": suspensions,
        "resumptions": resumptions,
        "revocations": revocations,
        "license_transfers": license_transfers,
        "guard_band_changes": guard_band_changes,
        "restriction_window_events": restriction_window_events,
        "emergency_occupation_events": emergency_occupation_events,
        "differences": events,
        "events": events,
    }


# --------------------------------------------------------------------------- #
# Application factory / lifespan
# --------------------------------------------------------------------------- #
_sweep_task: asyncio.Task | None = None


def _background_sweep_enabled() -> bool:
    """Whether this process runs the scheduled-approval sweeper loop.

    Default on; set SCHEDULED_APPROVAL_BACKGROUND=0/false to disable it (the
    deterministic ``process-due`` endpoint still works, which is what the
    SQLite tests rely on).
    """
    return os.getenv("SCHEDULED_APPROVAL_BACKGROUND", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _sweep_interval_seconds() -> float:
    """Poll interval of the background sweeper (seconds, clamped to >= 0.05)."""
    try:
        return max(0.05, float(os.getenv("SCHEDULED_APPROVAL_POLL_SECONDS", "0.5")))
    except ValueError:
        return 0.5


async def _scheduled_approval_sweep_loop() -> None:
    """Poll for due appointments and process them off the event loop.

    Due processing is synchronous DB work, so it runs in a worker thread; the
    singleton revision lock serialises it against manual decisions, cancels
    and reschedules.  A lost arbitration just leaves the appointment
    ``scheduled`` for the next tick.
    """
    interval = _sweep_interval_seconds()
    while True:
        await asyncio.sleep(interval)
        try:
            summary = await asyncio.to_thread(run_scheduled_approval_sweep)
        except Exception:  # pragma: no cover - a sweep must never kill the app
            log.exception("scheduled approval sweep failed")
            continue
        if summary.get("processed_count"):
            log.info(
                "scheduled approval sweep processed %d appointment(s): "
                "approved=%s blocked=%s final_revision=%s",
                summary["processed_count"],
                summary["approved_application_ids"],
                summary["blocked_application_ids"],
                summary["final_revision"],
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    wait_for_database()
    init_db()
    with SessionLocal() as session:
        meta = session.get(RevisionMeta, 1)
        live_band = meta.guard_band if meta is not None else settings.guard_band
    # Startup catch-up: appointments that came due while the process was down
    # are re-adjudicated at the restart instant inside the shared
    # arbitration, so a restart never silently skips or double-approves one.
    try:
        catch_up = await asyncio.to_thread(run_scheduled_approval_sweep)
        if catch_up.get("processed_count"):
            log.info(
                "startup catch-up processed %d due scheduled approval(s): "
                "approved=%s blocked=%s",
                catch_up["processed_count"],
                catch_up["approved_application_ids"],
                catch_up["blocked_application_ids"],
            )
    except Exception:  # pragma: no cover - readiness must survive a sweep error
        log.exception("startup scheduled approval catch-up failed")
    global _sweep_task
    if _background_sweep_enabled():
        _sweep_task = asyncio.create_task(
            _scheduled_approval_sweep_loop(), name="scheduled-approval-sweep"
        )
    log.info(
        "startup complete; guard_band=%s (GUARD_BAND seed=%s) listening on %s:%s",
        live_band,
        settings.guard_band,
        settings.app_host,
        settings.app_port,
    )
    yield
    if _sweep_task is not None:
        _sweep_task.cancel()
    engine.dispose()


app = FastAPI(
    title="Temporary Event Radio Spectrum API",
    version="1.0.0",
    description=(
        "Submit, preview, approve and revoke temporary radio applications. "
        "Frequencies are closed intervals, UTC times are half-open intervals, "
        "coverage is a planar circle."
    ),
    lifespan=lifespan,
)
app.add_exception_handler(APIError, api_error_handler)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Normalise FastAPI structural errors into the standard error envelope."""
    details = [
        {
            "field": ".".join(str(p) for p in err["loc"] if p != "body"),
            "code": err["type"],
            "message": err["msg"],
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": E_VALIDATION,
                "message": "request validation failed; the whole request was rejected",
                "details": details,
            }
        },
    )


# --------------------------------------------------------------------------- #
# Health / revisions
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/revision/current", tags=["revision"])
def current_revision() -> dict[str, Any]:
    with SessionLocal() as session:
        meta = session.get(RevisionMeta, 1)
        revision = meta.revision if meta else 0
        guard_band = meta.guard_band if meta else settings.guard_band
        snapshot = build_snapshot(session, revision)
        effective_applications = [
            serialize_application(app)
            for app_id in snapshot["effective_application_ids"]
            if (app := session.get(Application, app_id)) is not None
        ]
        suspended_applications = [
            serialize_application(app)
            for app_id in snapshot["suspended_application_ids"]
            if (app := session.get(Application, app_id)) is not None
        ]
        waitlist_entries = [
            serialize_waitlist_entry(session, entry, position)
            for position, entry in enumerate(load_waitlist(session), start=1)
        ]
        waiting_schedules = (
            session.query(ScheduledApproval)
            .filter(ScheduledApproval.status == SCHEDULED_STATUS)
            .order_by(ScheduledApproval.scheduled_at, ScheduledApproval.id)
            .all()
        )
        active_window_rows = load_active_windows(session)
        active_emergencies = load_active_emergencies(session)
    return {
        "current_revision": revision,
        "guard_band": guard_band,
        "effective_application_ids": snapshot["effective_application_ids"],
        "effective_applications": effective_applications,
        "suspended_application_ids": snapshot["suspended_application_ids"],
        "suspended_applications": suspended_applications,
        "force_suspended_application_ids": snapshot[
            "force_suspended_application_ids"
        ],
        "active_emergency_application_ids": snapshot[
            "active_emergency_application_ids"
        ],
        "active_emergencies": [
            serialize_emergency_occupation(session, occ)
            for occ in active_emergencies
        ],
        "emergency_occupations": snapshot["emergency_occupations"],
        "emergency_occupation_events": snapshot["emergency_occupation_events"],
        "active_restriction_window_ids": [row.id for row in active_window_rows],
        "active_restriction_windows": [
            serialize_window_row(row) for row in active_window_rows
        ],
        "restriction_window_events": snapshot["restriction_window_events"],
        "waitlist_application_ids": [e["application_id"] for e in waitlist_entries],
        "waitlist_entries": waitlist_entries,
        "scheduled_approval_ids": [b.id for b in waiting_schedules],
        "scheduled_approvals": [
            serialize_scheduled_approval(session, b) for b in waiting_schedules
        ],
        "approvals": snapshot["approvals"],
        "suspensions": snapshot["suspensions"],
        "resumptions": snapshot["resumptions"],
        "revocations": snapshot["revocations"],
        "license_transfers": snapshot["license_transfers"],
        "guard_band_changes": snapshot["guard_band_changes"],
    }


@app.get("/api/revision/compare", tags=["revision"])
def compare_revisions(
    base_revision: int,
    target_revision: int,
    application_id: int | None = None,
) -> dict[str, Any]:
    """Compare two existing, as-of revision snapshots.

    This is a read-only audit view: it reconstructs both sides with
    :func:`build_snapshot` and never writes applications, events or revision
    metadata.  Both revision numbers must exist (zero through the current
    revision), and the optional application filter must identify a known
    application.
    """
    with SessionLocal() as session:
        current = session.get(RevisionMeta, 1).revision
        negative = [
            revision
            for revision in (base_revision, target_revision)
            if revision < 0
        ]
        if negative:
            raise APIError(
                E_REVISION_OUT_OF_RANGE,
                "revision must be >= 0",
                status_code=422,
                details={"invalid_revisions": negative},
            )
        if application_id is not None and application_id < 1:
            raise APIError(
                E_VALIDATION,
                "application_id must be >= 1",
                status_code=422,
            )
        invalid = [
            revision
            for revision in (base_revision, target_revision)
            if revision > current
        ]
        if invalid:
            raise APIError(
                E_REVISION_OUT_OF_RANGE,
                f"revision(s) {invalid} are in the future; current revision "
                f"is {current}",
                status_code=404,
                details={
                    "current_revision": current,
                    "out_of_range_revisions": invalid,
                },
            )
        if application_id is not None:
            row = session.get(Application, application_id)
            if row is None:
                raise APIError(
                    E_NOT_FOUND,
                    f"application {application_id} does not exist",
                    status_code=404,
                )
        return build_revision_comparison(
            session, base_revision, target_revision, application_id
        )


@app.get("/api/revision/{revision}", tags=["revision"])
def revision_snapshot(revision: int) -> dict[str, Any]:
    if revision < 0:
        raise APIError(
            E_REVISION_OUT_OF_RANGE,
            "revision must be >= 0",
            status_code=422,
        )
    with SessionLocal() as session:
        current = session.get(RevisionMeta, 1).revision
        if revision > current:
            raise APIError(
                E_REVISION_OUT_OF_RANGE,
                f"revision {revision} is in the future; current revision is {current}",
                status_code=404,
                details={"current_revision": current},
            )
        return build_snapshot(session, revision)


# --------------------------------------------------------------------------- #
# Applications: create / list / get
# --------------------------------------------------------------------------- #
@app.post("/api/applications", status_code=201, tags=["applications"])
def create_application(payload: ApplicationCreate) -> dict[str, Any]:
    errors = validate_application(payload)
    if errors:
        raise APIError(
            E_VALIDATION,
            "the whole application was rejected due to invalid fields",
            status_code=422,
            details=errors,
        )

    app_row = Application(
        name=payload.name,
        cx=payload.circle.cx,
        cy=payload.circle.cy,
        radius=payload.circle.radius,
        freq_low=payload.frequency.low,
        freq_high=payload.frequency.high,
        start_time=payload.time_window.start.astimezone(timezone.utc),
        end_time=payload.time_window.end.astimezone(timezone.utc),
        status="pending",
    )
    with SessionLocal() as session, session.begin():
        session.add(app_row)
        session.flush()
        result = serialize_application(app_row)
    return result


@app.get("/api/applications", tags=["applications"])
def list_applications(status: str | None = None) -> dict[str, Any]:
    with SessionLocal() as session:
        query = session.query(Application).order_by(Application.id)
        if status is not None:
            if status not in {"pending", "approved", "suspended", "revoked"}:
                raise APIError(
                    E_VALIDATION,
                    "status filter must be one of pending, approved, "
                    "suspended, revoked",
                    status_code=422,
                )
            query = query.filter(Application.status == status)
        rows = query.all()
        return {"applications": [serialize_application(row) for row in rows]}


@app.get("/api/applications/{application_id}", tags=["applications"])
def get_application(application_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        row = session.get(Application, application_id)
        if row is None:
            raise APIError(
                E_NOT_FOUND,
                f"application {application_id} does not exist",
                status_code=404,
            )
        return serialize_application(row)


# --------------------------------------------------------------------------- #
# Preview (non-mutating)
# --------------------------------------------------------------------------- #
@app.post("/api/applications/{application_id}/preview", tags=["applications"])
def preview_application(application_id: int) -> dict[str, Any]:
    with SessionLocal() as session:
        row = session.get(Application, application_id)
        if row is None:
            raise APIError(
                E_NOT_FOUND,
                f"application {application_id} does not exist",
                status_code=404,
            )
        if row.status != "pending":
            raise APIError(
                E_NOT_PENDING,
                f"application {application_id} is {row.status}, only pending "
                "applications can be previewed",
                status_code=409,
                details={"status": row.status},
            )

        meta = session.get(RevisionMeta, 1)
        current = meta.revision
        guard_band = meta.guard_band
        candidate = to_license(row)
        # Narrow the database candidate set by time, coverage and the
        # guard-widened frequency span before the exact predicates run.
        effective = load_candidate_effective_licenses(
            session, _candidate_envelope(candidate, guard_band)
        )
        conflicts = adjudication.evaluate(candidate, effective, guard_band)
        window_rows = load_candidate_windows(
            session, _window_hit_envelope(candidate)
        )
        window_hits = window_hits_for(candidate, window_rows)
        active_window_ids = load_active_window_ids(session)

    return {
        "application_id": application_id,
        "current_revision": current,
        "guard_band": guard_band,
        "has_conflicts": bool(conflicts),
        "conflicts": [serialize_conflict(c, guard_band) for c in conflicts],
        # Active temporary restriction windows hit by the application.
        # Additive field: ``has_conflicts``/``conflicts`` keep their
        # license-interference meaning; a hit blocks writes regardless.
        "has_restriction_window_conflicts": bool(window_hits),
        "restriction_window_conflicts": [
            serialize_window_hit(h) for h in window_hits
        ],
        "active_restriction_window_ids": active_window_ids,
    }


# --------------------------------------------------------------------------- #
# Available ranges (non-mutating search over candidate band lower bounds)
# --------------------------------------------------------------------------- #
@app.post(
    "/api/applications/{application_id}/available-ranges",
    tags=["applications"],
)
def find_available_ranges(
    application_id: int, payload: AvailableRangesRequest
) -> dict[str, Any]:
    """Every maximal interference-free range of candidate band lower bounds.

    The application keeps its coverage circle, UTC time window and band
    *width* (possibly zero for a single frequency); only the lower bound of
    its band varies over the closed candidate interval
    ``[candidate_low_min, candidate_low_max]``.  Adjudication uses the
    effective licenses and the live guard band of the current revision,
    exactly like ``preview``: only grants intersecting in space and time
    block, touching band endpoints interfere, and a separation strictly
    smaller than the guard band interferes.  Each returned range flags
    whether its endpoints are attainable; the list is empty when no
    candidate lower bound is usable.  The query never mutates applications,
    licenses or the revision.
    """
    errors = validate_candidate_low_range(payload)
    if errors:
        raise APIError(
            E_VALIDATION,
            "the available-ranges query was rejected due to invalid fields",
            status_code=422,
            details=errors,
        )
    with SessionLocal() as session:
        row = session.get(Application, application_id)
        if row is None:
            raise APIError(
                E_NOT_FOUND,
                f"application {application_id} does not exist",
                status_code=404,
            )
        if row.status != "pending":
            raise APIError(
                E_NOT_PENDING,
                f"application {application_id} is {row.status}, only pending "
                "applications can be searched for available ranges",
                status_code=409,
                details={"status": row.status},
            )

        meta = session.get(RevisionMeta, 1)
        current = meta.revision
        guard_band = meta.guard_band
        bandwidth = row.freq_high - row.freq_low
        candidate = to_license(row)
        # The lower bound sweeps the whole [candidate_low_min,
        # candidate_low_max] interval, so the envelope spans the whole
        # swept band (plus guard band); licenses/windows outside it can
        # forbid no attainable lower bound and are never loaded.
        license_envelope = _available_ranges_envelope(
            candidate,
            guard_band,
            payload.candidate_low_min,
            payload.candidate_low_max,
        )
        effective = load_candidate_effective_licenses(
            session, license_envelope
        )
        # Windows use closed-band intersection with no guard widening;
        # cover the same swept lower-bound interval.
        window_envelope = _available_ranges_envelope(
            candidate, 0.0, payload.candidate_low_min, payload.candidate_low_max
        )
        window_rows = load_candidate_windows(session, window_envelope)
        active_window_ids = load_active_window_ids(session)
        ranges = adjudication.available_ranges(
            candidate,
            effective,
            guard_band,
            payload.candidate_low_min,
            payload.candidate_low_max,
            windows=[to_window(w) for w in window_rows],
        )

    return {
        "application_id": application_id,
        "current_revision": current,
        "guard_band": guard_band,
        "bandwidth": bandwidth,
        "candidate_low_min": payload.candidate_low_min,
        "candidate_low_max": payload.candidate_low_max,
        "active_restriction_window_ids": active_window_ids,
        "available_ranges": [
            {
                "min": r.min,
                "max": r.max,
                "min_inclusive": r.min_inclusive,
                "max_inclusive": r.max_inclusive,
            }
            for r in ranges
        ],
    }


# --------------------------------------------------------------------------- #
# Approve (conditional on expected revision, row-lock serialised)
# --------------------------------------------------------------------------- #
@app.post("/api/applications/{application_id}/approve", tags=["applications"])
def approve_application(
    application_id: int, payload: ApprovalRequest
) -> dict[str, Any]:
    with SessionLocal() as session:
        try:
            with session.begin():
                # Serialise against every other approve/revoke/guard-band
                # change on the singleton row (a real row lock on
                # PostgreSQL).
                meta = session.get(RevisionMeta, 1, with_for_update=True)
                current = meta.revision
                guard_band = meta.guard_band

                row = session.get(Application, application_id)
                if row is None:
                    raise APIError(
                        E_NOT_FOUND,
                        f"application {application_id} does not exist",
                        status_code=404,
                    )
                if row.status == "revoked":
                    raise APIError(
                        E_ALREADY_REVOKED,
                        f"application {application_id} has been revoked",
                        status_code=409,
                    )
                if row.status == "approved":
                    raise APIError(
                        E_NOT_PENDING,
                        f"application {application_id} is already approved at "
                        f"revision {row.approval_revision}",
                        status_code=409,
                        details={"approval_revision": row.approval_revision},
                    )

                # Optimistic revision check against the locked, latest revision.
                if payload.expected_revision != current:
                    raise APIError(
                        E_STALE_REVISION,
                        f"expected revision {payload.expected_revision} but "
                        f"current revision is {current}; re-preview and re-decide",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                        },
                    )

                # Re-adjudicate under the lock against the latest effective
                # set and the live guard band, after narrowing the database
                # candidate set by time, coverage and frequency.
                candidate = to_license(row)
                effective = load_candidate_effective_licenses(
                    session, _candidate_envelope(candidate, guard_band)
                )
                conflicts = adjudication.evaluate(
                    candidate, effective, guard_band
                )
                window_rows = load_candidate_windows(
                    session, _window_hit_envelope(candidate)
                )
                window_hits = window_hits_for(candidate, window_rows)
                if window_hits:
                    # A hit window forbids the grant even when no license
                    # interferes; the application stays pending and no
                    # revision is consumed.
                    raise APIError(
                        E_RESTRICTION_WINDOW,
                        f"application {application_id} hits "
                        f"{len(window_hits)} active restriction window(s); "
                        "the license was not granted",
                        status_code=409,
                        details={
                            "current_revision": current,
                            "restriction_window_conflicts": [
                                serialize_window_hit(h) for h in window_hits
                            ],
                            "conflicts": [
                                serialize_conflict(c, guard_band)
                                for c in conflicts
                            ],
                        },
                    )
                if conflicts:
                    raise APIError(
                        E_INTERFERENCE,
                        f"application {application_id} interferes with "
                        f"{len(conflicts)} effective grant(s); frequency not granted",
                        status_code=409,
                        details={
                            "current_revision": current,
                            "conflicts": [
                                serialize_conflict(c, guard_band)
                                for c in conflicts
                            ],
                        },
                    )

                meta.revision = current + 1
                session.add(
                    ApprovalEvent(
                        revision=meta.revision,
                        application_id=application_id,
                        guard_band=guard_band,
                    )
                )
                row.status = "approved"
                row.approval_revision = meta.revision
                # A manually approved standby leaves the queue so it can
                # never be auto-approved afterwards.
                entry = (
                    session.query(WaitlistEntry)
                    .filter(WaitlistEntry.application_id == application_id)
                    .one_or_none()
                )
                if entry is not None:
                    session.delete(entry)
                # A manual decision supersedes any booked appointment.
                invalidate_scheduled_approvals(
                    session,
                    application_id,
                    "manual_approval",
                    meta.revision,
                )
                session.flush()
                response = {
                    "approved": True,
                    "application_id": application_id,
                    "revision": meta.revision,
                    "guard_band": guard_band,
                    "application": serialize_application(row),
                }
        except IntegrityError:
            # Last line of defence (e.g. backends without row locks such as
            # SQLite's tests): the revision slot was taken concurrently.
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); "
                "re-preview and re-decide",
                status_code=409,
                details={"current_revision": latest},
            )
    return response


# --------------------------------------------------------------------------- #
# Batch approve (all-or-nothing group approval under one revision lock)
# --------------------------------------------------------------------------- #
@app.post("/api/applications/batch-approve", tags=["applications"])
def batch_approve_applications(payload: BatchApprovalRequest) -> dict[str, Any]:
    # Structural shape (non-empty list, non-negative revision) is enforced by
    # Pydantic; the uniqueness rule goes through the standard error envelope.
    seen: set[int] = set()
    duplicates: list[int] = []
    for app_id in payload.application_ids:
        if app_id in seen:
            duplicates.append(app_id)
        seen.add(app_id)
    if duplicates:
        raise APIError(
            E_VALIDATION,
            "the whole batch was rejected: application_ids must be distinct",
            status_code=422,
            details=[
                {
                    "field": "application_ids",
                    "code": E_DUPLICATE_APPLICATION,
                    "message": (
                        "application_ids must not contain duplicates; "
                        f"repeated: {duplicates}"
                    ),
                    "duplicates": duplicates,
                }
            ],
        )

    with SessionLocal() as session:
        try:
            with session.begin():
                # Same singleton lock as single approve/revoke/guard-band
                # change: all writers share one revision arbitration and
                # serialise here.
                meta = session.get(RevisionMeta, 1, with_for_update=True)
                current = meta.revision
                guard_band = meta.guard_band

                rows_by_id = {
                    row.id: row
                    for row in (
                        session.query(Application)
                        .filter(Application.id.in_(payload.application_ids))
                        .all()
                    )
                }

                # Failure type 1: unknown IDs (batch is not approved at all).
                missing = [
                    app_id
                    for app_id in payload.application_ids
                    if app_id not in rows_by_id
                ]
                if missing:
                    raise APIError(
                        E_NOT_FOUND,
                        f"{len(missing)} application(s) do not exist; the "
                        "whole batch was rejected and no revision was consumed",
                        status_code=404,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                            "missing_application_ids": missing,
                        },
                    )

                # Failure type 2: any member no longer pending (same precedence
                # as single approval: revoked outranks already-approved).
                revoked = [
                    app_id
                    for app_id in payload.application_ids
                    if rows_by_id[app_id].status == "revoked"
                ]
                if revoked:
                    raise APIError(
                        E_ALREADY_REVOKED,
                        f"application(s) {revoked} have been revoked; the "
                        "whole batch was rejected and no revision was consumed",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                            "applications": [
                                {
                                    "application_id": app_id,
                                    "status": rows_by_id[app_id].status,
                                    "approval_revision": rows_by_id[
                                        app_id
                                    ].approval_revision,
                                }
                                for app_id in payload.application_ids
                                if rows_by_id[app_id].status != "pending"
                            ],
                        },
                    )
                not_pending = [
                    app_id
                    for app_id in payload.application_ids
                    if rows_by_id[app_id].status != "pending"
                ]
                if not_pending:
                    raise APIError(
                        E_NOT_PENDING,
                        f"application(s) {not_pending} are not pending; the "
                        "whole batch was rejected and no revision was consumed",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                            "applications": [
                                {
                                    "application_id": app_id,
                                    "status": rows_by_id[app_id].status,
                                    "approval_revision": rows_by_id[
                                        app_id
                                    ].approval_revision,
                                }
                                for app_id in not_pending
                            ],
                        },
                    )

                # Failure type 3: stale revision (a concurrent decision won).
                if payload.expected_revision != current:
                    raise APIError(
                        E_STALE_REVISION,
                        f"expected revision {payload.expected_revision} but "
                        f"current revision is {current}; re-preview and "
                        "re-decide; the whole batch was rejected",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                        },
                    )

                # Failure type 4: interference or restriction-window hits,
                # re-adjudicated under the lock.  Every member is checked
                # against the current effective set, every other member of
                # the group, and every active restriction window.  The
                # effective/window candidate sets are narrowed in the
                # database by the union envelope of the group members.
                ordered_rows = [rows_by_id[i] for i in payload.application_ids]
                candidates = [to_license(row) for row in ordered_rows]
                effective = load_candidate_effective_licenses(
                    session, _group_envelope(candidates, guard_band)
                )
                against_effective, within_group = adjudication.evaluate_group(
                    candidates, effective, guard_band
                )
                window_rows = load_candidate_windows(
                    session,
                    _union_envelopes(
                        [_window_hit_envelope(c) for c in candidates]
                    ),
                )
                window_hit_ids: set[int] = set()
                interference_details = []
                for candidate in candidates:
                    eff_conflicts = [
                        serialize_group_conflict(c, "effective", guard_band)
                        for c in against_effective[candidate.application_id]
                    ]
                    group_conflicts = [
                        serialize_group_conflict(c, "group", guard_band)
                        for c in within_group[candidate.application_id]
                    ]
                    hits = window_hits_for(candidate, window_rows)
                    if hits:
                        window_hit_ids.add(candidate.application_id)
                    if eff_conflicts or group_conflicts or hits:
                        interference_details.append(
                            {
                                "application_id": candidate.application_id,
                                "conflicts": eff_conflicts + group_conflicts,
                                "restriction_window_conflicts": [
                                    serialize_window_hit(h) for h in hits
                                ],
                            }
                        )
                if interference_details:
                    # Window-only blockers get the dedicated code; a batch
                    # with any ordinary interference keeps ``interference``.
                    code = (
                        E_RESTRICTION_WINDOW
                        if window_hit_ids
                        and not any(
                            item["conflicts"] for item in interference_details
                        )
                        else E_INTERFERENCE
                    )
                    message = (
                        f"{len(interference_details)} application(s) in the "
                        "batch interfere with effective grants, with each "
                        "other, or hit active restriction windows; the whole "
                        "batch was rejected and no frequency was granted"
                    )
                    raise APIError(
                        code,
                        message,
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                            "applications": interference_details,
                        },
                    )

                # All clear: one ApprovalEvent per member, consecutive
                # revisions assigned in the submitted order, one commit.
                start_revision = current
                results = []
                for row in ordered_rows:
                    meta.revision += 1
                    session.add(
                        ApprovalEvent(
                            revision=meta.revision,
                            application_id=row.id,
                            guard_band=guard_band,
                        )
                    )
                    row.status = "approved"
                    row.approval_revision = meta.revision
                    # Manually approved standbys leave the queue: they must
                    # not be auto-approved a second time during a later
                    # release.
                    entry = (
                        session.query(WaitlistEntry)
                        .filter(WaitlistEntry.application_id == row.id)
                        .one_or_none()
                    )
                    if entry is not None:
                        session.delete(entry)
                    # A manual decision supersedes any booked appointment.
                    invalidate_scheduled_approvals(
                        session,
                        row.id,
                        "batch_approval",
                        meta.revision,
                    )
                    results.append(
                        {
                            "application_id": row.id,
                            "revision": meta.revision,
                            "application": serialize_application(row),
                        }
                    )
                session.flush()
                response = {
                    "approved": True,
                    "expected_revision": payload.expected_revision,
                    "from_revision": start_revision,
                    "to_revision": meta.revision,
                    # Headline final revision, same field name as single
                    # approve/revoke responses.
                    "revision": meta.revision,
                    "guard_band": guard_band,
                    "application_ids": payload.application_ids,
                    "results": results,
                }
        except IntegrityError:
            # Backends without row locks (SQLite in tests) still have the
            # unique revision constraint: a concurrent writer won, so the
            # whole batch is rejected and nothing was partially approved.
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); "
                "re-preview and re-decide; the whole batch was rejected",
                status_code=409,
                details={"current_revision": latest},
            )
    return response


# --------------------------------------------------------------------------- #
# Revoke (only affects the effective set used by later adjudications)
# --------------------------------------------------------------------------- #
@app.post("/api/applications/{application_id}/revoke", tags=["applications"])
def revoke_application(
    application_id: int, payload: RevocationRequest
) -> dict[str, Any]:
    """Permanently revoke a license.

    Works on both effective licenses and licenses temporarily suspended for
    an equipment outage; a revoked license can never be resumed.  Only the
    effective set used by later adjudications changes; history is kept.
    """
    with SessionLocal() as session:
        try:
            with session.begin():
                meta = session.get(RevisionMeta, 1, with_for_update=True)

                row = session.get(Application, application_id)
                if row is None:
                    raise APIError(
                        E_NOT_FOUND,
                        f"application {application_id} does not exist",
                        status_code=404,
                    )
                if row.status == "revoked":
                    raise APIError(
                        E_ALREADY_REVOKED,
                        f"application {application_id} is already revoked",
                        status_code=409,
                    )
                if row.status not in ("approved", "suspended"):
                    raise APIError(
                        E_NOT_PENDING,
                        f"application {application_id} is {row.status} and "
                        "cannot be revoked",
                        status_code=409,
                        details={"status": row.status},
                    )
                if row.status == "approved":
                    active_emergency = (
                        session.query(EmergencyOccupation)
                        .filter(
                            EmergencyOccupation.emergency_application_id
                            == application_id,
                            EmergencyOccupation.released_revision.is_(None),
                        )
                        .one_or_none()
                    )
                    if active_emergency is not None:
                        # Emergency licenses end exclusively through the
                        # dedicated release.  A force-suspended *ordinary*
                        # license, by contrast, may still be revoked as usual.
                        raise APIError(
                            E_EMERGENCY_LICENSE_PROTECTED,
                            f"application {application_id} holds an active "
                            "emergency occupation; it can only be ended by "
                            "releasing the emergency occupation",
                            status_code=409,
                            details={"status": row.status},
                        )

                was_suspended = row.status == "suspended"
                meta.revision += 1
                release_revision = meta.revision
                session.add(
                    RevocationEvent(
                        revision=meta.revision,
                        application_id=application_id,
                        reason=payload.reason,
                    )
                )
                row.status = "revoked"
                # A force-suspended license that is revoked drops the live
                # emergency marker (it stays revoked; the event history keeps
                # the link for snapshots).
                row.force_suspended_by = None

                # A released license may free spectrum: re-adjudicate the
                # standby queue in registration order, in this same
                # transaction and revision sequence.  The revoke keeps its
                # one revision; each automatic approval consumes the next.
                waitlist_result = drain_waitlist(
                    session, meta, release_revision
                )
                session.flush()
                response = {
                    "revoked": True,
                    "application_id": application_id,
                    # The revoke's own revision (compatibility field).
                    "revision": release_revision,
                    "previous_approval_revision": row.approval_revision,
                    "was_suspended": was_suspended,
                    "reason": payload.reason,
                    **waitlist_result,
                }
        except IntegrityError:
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); retry",
                status_code=409,
                details={"current_revision": latest},
            )
    return response


# --------------------------------------------------------------------------- #
# Suspend / resume (temporary equipment outage, revision-arbitrated)
# --------------------------------------------------------------------------- #
@app.post("/api/applications/{application_id}/suspend", tags=["applications"])
def suspend_application(
    application_id: int, payload: SuspensionRequest
) -> dict[str, Any]:
    """Temporarily take an approved license out of the effective set.

    The original approval is kept and no re-application is needed; the
    license immediately stops participating in later adjudications.  Shares
    the singleton revision lock with every other writer: a stale expected
    revision returns the current revision and consumes nothing.  Pending
    applications must be approved first; a suspended application must not
    be suspended again; revoked licenses are terminal.
    """
    with SessionLocal() as session:
        try:
            with session.begin():
                # Same singleton lock as approve/batch/revoke/guard-band
                # change: all writers share one revision arbitration.
                meta = session.get(RevisionMeta, 1, with_for_update=True)
                current = meta.revision

                row = session.get(Application, application_id)
                if row is None:
                    raise APIError(
                        E_NOT_FOUND,
                        f"application {application_id} does not exist",
                        status_code=404,
                    )
                if row.status == "revoked":
                    raise APIError(
                        E_ALREADY_REVOKED,
                        f"application {application_id} has been revoked and "
                        "cannot be suspended",
                        status_code=409,
                    )
                if (
                    row.status == "approved"
                    and session.query(EmergencyOccupation)
                    .filter(
                        EmergencyOccupation.emergency_application_id
                        == application_id,
                        EmergencyOccupation.released_revision.is_(None),
                    )
                    .one_or_none()
                    is not None
                ):
                    # An emergency license can only end through the dedicated
                    # emergency release; it can never be ordinarily suspended.
                    raise APIError(
                        E_EMERGENCY_LICENSE_PROTECTED,
                        f"application {application_id} holds an active "
                        "emergency occupation; it cannot be ordinarily "
                        "suspended or revoked, end the emergency instead",
                        status_code=409,
                        details={"status": row.status},
                    )
                if row.status == "suspended":
                    if row.force_suspended_by is not None:
                        raise APIError(
                            E_FORCE_SUSPENDED,
                            f"application {application_id} is force-suspended "
                            f"by active emergency application "
                            f"{row.force_suspended_by} since revision "
                            f"{row.suspended_revision}; it cannot be "
                            "ordinarily suspended while that emergency is "
                            "active",
                            status_code=409,
                            details={
                                "suspended_at_revision": row.suspended_revision,
                                "force_suspended_by": row.force_suspended_by,
                            },
                        )
                    raise APIError(
                        E_ALREADY_SUSPENDED,
                        f"application {application_id} is already suspended "
                        f"since revision {row.suspended_revision}",
                        status_code=409,
                        details={
                            "suspended_at_revision": row.suspended_revision
                        },
                    )
                if row.status != "approved":
                    raise APIError(
                        E_NOT_APPROVED,
                        f"application {application_id} is {row.status}; only "
                        "approved licenses can be suspended",
                        status_code=409,
                        details={"status": row.status},
                    )

                if payload.expected_revision != current:
                    raise APIError(
                        E_STALE_REVISION,
                        f"expected revision {payload.expected_revision} but "
                        f"current revision is {current}; re-decide; the "
                        "license was not suspended",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                        },
                    )

                meta.revision = current + 1
                release_revision = meta.revision
                session.add(
                    SuspensionEvent(
                        revision=meta.revision,
                        application_id=application_id,
                        reason=payload.reason,
                    )
                )
                row.status = "suspended"
                row.suspended_revision = meta.revision

                # The license just left the effective set: re-adjudicate the
                # standby queue in registration order within the same
                # transaction and revision sequence.  The suspension keeps
                # its one revision; every automatic approval consumes the
                # next consecutive one.
                waitlist_result = drain_waitlist(
                    session, meta, release_revision
                )
                session.flush()
                response = {
                    "suspended": True,
                    "application_id": application_id,
                    # The suspension's own revision (compatibility field).
                    "revision": release_revision,
                    "previous_approval_revision": row.approval_revision,
                    "reason": payload.reason,
                    "application": serialize_application(row),
                    **waitlist_result,
                }
        except IntegrityError:
            # Backends without row locks (SQLite in tests): a concurrent
            # writer took the revision slot; nothing was changed.
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); "
                "re-decide; the license was not suspended",
                status_code=409,
                details={"current_revision": latest},
            )
    return response


@app.post("/api/applications/{application_id}/resume", tags=["applications"])
def resume_application(
    application_id: int, payload: ResumptionRequest
) -> dict[str, Any]:
    """Restore a suspended license after re-adjudication.

    The license is re-checked under the revision lock against the live guard
    band and every currently effective license.  A stale expected revision
    returns the current revision and a renewed interference returns the
    conflicting licenses; in both cases the license stays suspended and no
    revision is consumed.  A successful resume consumes exactly one
    revision; the original approval event is left untouched.
    """
    with SessionLocal() as session:
        try:
            with session.begin():
                # Same singleton lock as every other writer.
                meta = session.get(RevisionMeta, 1, with_for_update=True)
                current = meta.revision
                guard_band = meta.guard_band

                row = session.get(Application, application_id)
                if row is None:
                    raise APIError(
                        E_NOT_FOUND,
                        f"application {application_id} does not exist",
                        status_code=404,
                    )
                if row.status == "revoked":
                    raise APIError(
                        E_ALREADY_REVOKED,
                        f"application {application_id} has been revoked and "
                        "cannot be resumed",
                        status_code=409,
                    )
                if row.status != "suspended":
                    raise APIError(
                        E_NOT_SUSPENDED,
                        f"application {application_id} is {row.status}; only "
                        "suspended licenses can be resumed",
                        status_code=409,
                        details={"status": row.status},
                    )
                if row.force_suspended_by is not None:
                    # The license was force-suspended by an emergency that is
                    # still active: ordinary resumes are refused; the
                    # dedicated emergency release restores it automatically.
                    raise APIError(
                        E_FORCE_SUSPENDED,
                        f"application {application_id} is force-suspended by "
                        f"active emergency application "
                        f"{row.force_suspended_by}; an ordinary resume is "
                        "refused while that emergency is active, release the "
                        "emergency occupation to restore it",
                        status_code=409,
                        details={
                            "status": row.status,
                            "suspended_at_revision": row.suspended_revision,
                            "force_suspended_by": row.force_suspended_by,
                        },
                    )

                if payload.expected_revision != current:
                    raise APIError(
                        E_STALE_REVISION,
                        f"expected revision {payload.expected_revision} but "
                        f"current revision is {current}; re-decide; the "
                        "license stays suspended",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                        },
                    )

                # Re-adjudicate under the lock against the latest effective
                # set and the live guard band, after narrowing the database
                # candidate set by time, coverage and frequency.  The
                # resuming license itself is still suspended, so it is not
                # part of that set.
                candidate = to_license(row)
                effective = load_candidate_effective_licenses(
                    session, _candidate_envelope(candidate, guard_band)
                )
                conflicts = adjudication.evaluate(
                    candidate, effective, guard_band
                )
                window_rows = load_candidate_windows(
                    session, _window_hit_envelope(candidate)
                )
                window_hits = window_hits_for(candidate, window_rows)
                if window_hits:
                    raise APIError(
                        E_RESTRICTION_WINDOW,
                        f"application {application_id} hits "
                        f"{len(window_hits)} active restriction window(s); "
                        "the license stays suspended and no revision was consumed",
                        status_code=409,
                        details={
                            "current_revision": current,
                            "restriction_window_conflicts": [
                                serialize_window_hit(h) for h in window_hits
                            ],
                            "conflicts": [
                                serialize_conflict(c, guard_band)
                                for c in conflicts
                            ],
                        },
                    )
                if conflicts:
                    raise APIError(
                        E_INTERFERENCE,
                        f"application {application_id} would interfere with "
                        f"{len(conflicts)} effective grant(s); the license "
                        "stays suspended and no revision was consumed",
                        status_code=409,
                        details={
                            "current_revision": current,
                            "conflicts": [
                                serialize_conflict(c, guard_band)
                                for c in conflicts
                            ],
                        },
                    )

                meta.revision = current + 1
                session.add(
                    ResumptionEvent(
                        revision=meta.revision,
                        application_id=application_id,
                        guard_band=guard_band,
                        reason=payload.reason,
                    )
                )
                row.status = "approved"
                row.suspended_revision = None
                session.flush()
                response = {
                    "resumed": True,
                    "application_id": application_id,
                    "revision": meta.revision,
                    "original_approval_revision": row.approval_revision,
                    "guard_band": guard_band,
                    "reason": payload.reason,
                    "application": serialize_application(row),
                }
        except IntegrityError:
            # Backends without row locks (SQLite in tests): a concurrent
            # writer took the revision slot; the license stays suspended.
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); "
                "re-decide; the license stays suspended",
                status_code=409,
                details={"current_revision": latest},
            )
    return response


# --------------------------------------------------------------------------- #
# Waitlist (standby queue; registration/cancellation consume no revision)
# --------------------------------------------------------------------------- #
@app.post(
    "/api/applications/{application_id}/waitlist",
    tags=["waitlist"],
)
def register_waitlist(
    application_id: int,
    payload: WaitlistRegistrationRequest | None = None,
) -> dict[str, Any]:
    """Register a pending application as a standby for the busy band.

    The application must exist and still be ``pending``; each application can
    be queued at most once.  Registration consumes **no** revision and never
    changes the application's status; the response gives the FIFO queue
    position (1-based) and the current revision.  When a later suspension or
    revoke releases an effective license, the queue is re-adjudicated in
    registration order inside that transaction; a manually approved standby
    leaves the queue first.
    """
    reason = payload.reason if payload is not None else ""
    with SessionLocal() as session:
        try:
            with session.begin():
                # Take the singleton lock so a concurrent suspend/revoke
                # drain cannot race the registration; registration itself
                # does not touch the revision number.
                session.get(RevisionMeta, 1, with_for_update=True)

                row = session.get(Application, application_id)
                if row is None:
                    raise APIError(
                        E_NOT_FOUND,
                        f"application {application_id} does not exist",
                        status_code=404,
                    )
                existing = (
                    session.query(WaitlistEntry)
                    .filter(WaitlistEntry.application_id == application_id)
                    .one_or_none()
                )
                if existing is not None:
                    raise APIError(
                        E_ALREADY_WAITLISTED,
                        f"application {application_id} is already registered in "
                        "the waitlist; the same application cannot be "
                        "registered twice",
                        status_code=409,
                        details={
                            "position": load_waitlist(session).index(existing)
                            + 1
                        },
                    )
                if row.status != "pending":
                    raise APIError(
                        E_NOT_PENDING,
                        f"application {application_id} is {row.status}; only "
                        "pending applications can be registered in the waitlist",
                        status_code=409,
                        details={"status": row.status},
                    )

                entry = WaitlistEntry(
                    application_id=application_id, reason=reason
                )
                session.add(entry)
                session.flush()
                current = session.get(RevisionMeta, 1).revision
                # Position equals the insertion sequence: every earlier entry
                # has a smaller id, and the lock guarantees no concurrent
                # insert.
                position = (
                    session.query(WaitlistEntry)
                    .filter(WaitlistEntry.id <= entry.id)
                    .count()
                )
                response = {
                    "waitlisted": True,
                    "application_id": application_id,
                    "queue_position": position,
                    "current_revision": current,
                    "reason": reason,
                    "registered_at": iso(entry.created_at),
                }
        except IntegrityError:
            # Backends without row locks (SQLite): a concurrent registration
            # of the same application won the unique constraint.
            session.rollback()
            raise APIError(
                E_ALREADY_WAITLISTED,
                f"application {application_id} is already registered in the "
                "waitlist; the same application cannot be registered twice",
                status_code=409,
            )
    return response


@app.post(
    "/api/applications/{application_id}/waitlist/cancel",
    tags=["waitlist"],
)
def cancel_waitlist(application_id: int) -> dict[str, Any]:
    """Withdraw a pending application from the standby queue.

    Cancellation consumes no revision and does not change the application's
    status (it stays ``pending``).  Unknown applications are 404; applications
    not currently queued are 409 ``not_waitlisted``.
    """
    with SessionLocal() as session, session.begin():
        session.get(RevisionMeta, 1, with_for_update=True)

        row = session.get(Application, application_id)
        if row is None:
            raise APIError(
                E_NOT_FOUND,
                f"application {application_id} does not exist",
                status_code=404,
            )
        entry = (
            session.query(WaitlistEntry)
            .filter(WaitlistEntry.application_id == application_id)
            .one_or_none()
        )
        if entry is None:
            raise APIError(
                E_NOT_WAITLISTED,
                f"application {application_id} is not registered in the "
                "waitlist",
                status_code=409,
                details={"status": row.status},
            )
        session.delete(entry)
        session.flush()
        current = session.get(RevisionMeta, 1).revision
        response = {
            "cancelled": True,
            "application_id": application_id,
            "current_revision": current,
            "application": serialize_application(row),
        }
    return response


@app.get("/api/waitlist", tags=["waitlist"])
def list_waitlist() -> dict[str, Any]:
    """The whole standby queue in registration (FIFO) order."""
    with SessionLocal() as session:
        meta = session.get(RevisionMeta, 1)
        entries = load_waitlist(session)
        return {
            "current_revision": meta.revision,
            "queue_length": len(entries),
            "waitlist_application_ids": [e.application_id for e in entries],
            "entries": [
                serialize_waitlist_entry(session, entry, position)
                for position, entry in enumerate(entries, start=1)
            ],
        }


@app.get(
    "/api/applications/{application_id}/waitlist",
    tags=["waitlist"],
)
def get_waitlist_entry(application_id: int) -> dict[str, Any]:
    """Look up one application's standby position (404 when not queued)."""
    with SessionLocal() as session:
        row = session.get(Application, application_id)
        if row is None:
            raise APIError(
                E_NOT_FOUND,
                f"application {application_id} does not exist",
                status_code=404,
            )
        entries = load_waitlist(session)
        for position, entry in enumerate(entries, start=1):
            if entry.application_id == application_id:
                meta = session.get(RevisionMeta, 1)
                body = serialize_waitlist_entry(session, entry, position)
                body["current_revision"] = meta.revision
                return body
        raise APIError(
            E_NOT_WAITLISTED,
            f"application {application_id} is not registered in the waitlist",
            status_code=404,
            details={"status": row.status},
        )


# --------------------------------------------------------------------------- #
# Scheduled auto-approvals (book / reschedule / cancel / query / sweep)
# --------------------------------------------------------------------------- #
def _validate_schedule_time(
    payload: ScheduledApprovalRequest,
    app_end_time: datetime,
    now: datetime,
) -> None:
    """Reject naive / non-future / not-before-window-end booking instants.

    The instant must be timezone-aware, strictly later than ``now`` and
    strictly earlier than the application's own half-open window end.  Every
    violation uses the standard error envelope and leaves no appointment.
    """
    errors: list[dict[str, Any]] = []
    if payload.scheduled_at.tzinfo is None:
        errors.append(
            {
                "field": "scheduled_at",
                "code": "naive_datetime",
                "message": (
                    "scheduled_at must carry a timezone (use UTC, e.g. ...Z)"
                ),
            }
        )
    else:
        scheduled_at = payload.scheduled_at.astimezone(timezone.utc)
        if scheduled_at <= now:
            errors.append(
                {
                    "field": "scheduled_at",
                    "code": "scheduled_at_not_in_future",
                    "message": (
                        "scheduled_at must be strictly later than the current "
                        "UTC time"
                    ),
                    "scheduled_at": iso(scheduled_at),
                    "now": iso(now),
                }
            )
        if scheduled_at >= _utc(app_end_time):
            errors.append(
                {
                    "field": "scheduled_at",
                    "code": "scheduled_at_not_before_window_end",
                    "message": (
                        "scheduled_at must be strictly earlier than the "
                        "application time window end"
                    ),
                    "scheduled_at": iso(scheduled_at),
                    "application_end_time": iso(app_end_time),
                }
            )
    if errors:
        raise APIError(
            E_SCHEDULE_TIME_INVALID,
            "the scheduled auto-approval time is invalid; no appointment was "
            "booked",
            status_code=422,
            details=errors,
        )


def _book_or_reschedule_schedule(
    application_id: int, payload: ScheduledApprovalRequest
) -> tuple[dict[str, Any], int]:
    """Shared create/reschedule logic; returns (body, http_status)."""
    with SessionLocal() as session, session.begin():
        # Serialise against due processing and every revision writer so a
        # booking/reschedule and a firing appointment cannot interleave.
        session.get(RevisionMeta, 1, with_for_update=True)

        row = session.get(Application, application_id)
        if row is None:
            raise APIError(
                E_NOT_FOUND,
                f"application {application_id} does not exist",
                status_code=404,
            )
        if row.status != "pending":
            raise APIError(
                E_NOT_PENDING,
                f"application {application_id} is {row.status}; only pending "
                "applications can have a scheduled auto-approval",
                status_code=409,
                details={"status": row.status},
            )

        now = datetime.now(timezone.utc)
        _validate_schedule_time(payload, row.end_time, now)

        existing = get_active_schedule(session, application_id)
        scheduled_at = payload.scheduled_at.astimezone(timezone.utc)
        current = session.get(RevisionMeta, 1).revision
        if existing is None:
            booking = ScheduledApproval(
                application_id=application_id,
                scheduled_at=scheduled_at,
                status=SCHEDULED_STATUS,
                reason=payload.reason,
                booked_revision=current,
            )
            session.add(booking)
            session.flush()
            # Booking consumes no revision; the journal marks the first
            # revision from which the appointment is visible.
            record_schedule_change(
                session,
                booking,
                "booked",
                current,
                scheduled_at=scheduled_at,
                reason=payload.reason,
            )
            action = "booked"
            http_status = 201
        else:
            existing.scheduled_at = scheduled_at
            existing.reason = payload.reason
            existing.updated_at = now
            booking = existing
            session.flush()
            record_schedule_change(
                session,
                booking,
                "rescheduled",
                current,
                scheduled_at=scheduled_at,
                reason=payload.reason,
            )
            action = "rescheduled"
            http_status = 200
        body = {
            action: True,
            "action": action,
            "id": booking.id,
            "scheduled_approval_id": booking.id,
            "application_id": application_id,
            "scheduled_at": iso(booking.scheduled_at),
            "status": booking.status,
            "reason": booking.reason,
            "current_revision": current,
            "application_end_time": iso(row.end_time),
            "booked_at": iso(booking.created_at),
            "updated_at": iso(booking.updated_at),
        }
        return body, http_status


@app.post(
    "/api/applications/{application_id}/scheduled-approvals",
    tags=["scheduled-approvals"],
)
def book_scheduled_approval(
    application_id: int, payload: ScheduledApprovalRequest
) -> Response:
    """Book a UTC auto-approval appointment for a pending application.

    The instant must be strictly later than now and strictly earlier than the
    application's own window end; otherwise the request is rejected and **no
    appointment is left behind**.  Booking consumes no revision and does not
    change the application (a queued application keeps its queue place).  One
    application has at most one waiting appointment; calling this endpoint
    again before it fires *reschedules* the same appointment (200, action
    ``rescheduled``) instead of creating a second one.  Unknown application
    -> 404; non-pending -> 409 ``not_pending``; invalid instant -> 422
    ``scheduled_time_invalid``.
    """
    body, http_status = _book_or_reschedule_schedule(application_id, payload)
    return JSONResponse(status_code=http_status, content=body)


@app.post(
    "/api/applications/{application_id}/scheduled-approvals/reschedule",
    tags=["scheduled-approvals"],
)
def reschedule_scheduled_approval(
    application_id: int, payload: ScheduledApprovalRequest
) -> dict[str, Any]:
    """Move the waiting appointment to a new UTC instant before it fires.

    The new instant obeys the same validity rules as booking.  Unknown
    application -> 404; no waiting appointment -> 409
    ``scheduled_approval_not_active``; invalid instant -> 422
    ``scheduled_time_invalid`` (the existing appointment is kept unchanged).
    """
    with SessionLocal() as session, session.begin():
        session.get(RevisionMeta, 1, with_for_update=True)
        row = session.get(Application, application_id)
        if row is None:
            raise APIError(
                E_NOT_FOUND,
                f"application {application_id} does not exist",
                status_code=404,
            )
        existing = get_active_schedule(session, application_id)
        if existing is None:
            latest = (
                session.query(ScheduledApproval)
                .filter(ScheduledApproval.application_id == application_id)
                .order_by(ScheduledApproval.id.desc())
                .first()
            )
            raise APIError(
                E_SCHEDULE_NOT_ACTIVE,
                f"application {application_id} has no waiting scheduled "
                "auto-approval; it can only be rescheduled before it fires",
                status_code=409,
                details=(
                    {"status": latest.status} if latest is not None else None
                ),
            )
        now = datetime.now(timezone.utc)
        _validate_schedule_time(payload, row.end_time, now)
        scheduled_at = payload.scheduled_at.astimezone(timezone.utc)
        existing.scheduled_at = scheduled_at
        existing.reason = payload.reason
        existing.updated_at = now
        session.flush()
        current = session.get(RevisionMeta, 1).revision
        record_schedule_change(
            session,
            existing,
            "rescheduled",
            current,
            scheduled_at=scheduled_at,
            reason=payload.reason,
        )
        return {
            "rescheduled": True,
            "id": existing.id,
            "scheduled_approval_id": existing.id,
            "application_id": application_id,
            "scheduled_at": iso(existing.scheduled_at),
            "status": existing.status,
            "reason": existing.reason,
            "current_revision": current,
            "updated_at": iso(existing.updated_at),
            "scheduled_approval": serialize_scheduled_approval(
                session, existing
            ),
        }


@app.post(
    "/api/applications/{application_id}/scheduled-approvals/cancel",
    tags=["scheduled-approvals"],
)
def cancel_scheduled_approval(
    application_id: int,
    payload: ScheduledApprovalCancelRequest | None = None,
) -> dict[str, Any]:
    """Withdraw the waiting appointment before it fires (no revision used).

    Unknown application -> 404; no waiting appointment -> 409
    ``scheduled_approval_not_active``.  A cancelled appointment stays in
    history with status ``cancelled`` and a new one can be booked afterwards.
    """
    reason = payload.reason if payload is not None else ""
    with SessionLocal() as session, session.begin():
        session.get(RevisionMeta, 1, with_for_update=True)
        row = session.get(Application, application_id)
        if row is None:
            raise APIError(
                E_NOT_FOUND,
                f"application {application_id} does not exist",
                status_code=404,
            )
        existing = get_active_schedule(session, application_id)
        if existing is None:
            latest = (
                session.query(ScheduledApproval)
                .filter(ScheduledApproval.application_id == application_id)
                .order_by(ScheduledApproval.id.desc())
                .first()
            )
            raise APIError(
                E_SCHEDULE_NOT_ACTIVE,
                f"application {application_id} has no waiting scheduled "
                "auto-approval to cancel",
                status_code=409,
                details=(
                    {"status": latest.status} if latest is not None else None
                ),
            )
        # Cancellation consumes no revision: the outcome is visible from the
        # revision currently in force, and older snapshots still show the
        # waiting appointment.
        current = session.get(RevisionMeta, 1).revision
        cancelled_at = datetime.now(timezone.utc)
        existing.status = SCHEDULED_CANCELLED
        existing.outcome_reason = reason
        existing.outcome_revision = current
        existing.outcome_at = cancelled_at
        existing.updated_at = cancelled_at
        session.flush()
        return {
            "cancelled": True,
            "id": existing.id,
            "scheduled_approval_id": existing.id,
            "application_id": application_id,
            "scheduled_at": iso(existing.scheduled_at),
            "status": existing.status,
            "outcome_reason": reason,
            "current_revision": current,
            "scheduled_approval": serialize_scheduled_approval(
                session, existing
            ),
        }


@app.get(
    "/api/applications/{application_id}/scheduled-approvals",
    tags=["scheduled-approvals"],
)
def list_application_scheduled_approvals(
    application_id: int,
) -> dict[str, Any]:
    """All appointments of one application (waiting and historical)."""
    with SessionLocal() as session:
        row = session.get(Application, application_id)
        if row is None:
            raise APIError(
                E_NOT_FOUND,
                f"application {application_id} does not exist",
                status_code=404,
            )
        bookings = (
            session.query(ScheduledApproval)
            .filter(ScheduledApproval.application_id == application_id)
            .order_by(ScheduledApproval.id)
            .all()
        )
        current = session.get(RevisionMeta, 1).revision
        return {
            "application_id": application_id,
            "current_revision": current,
            "count": len(bookings),
            "scheduled_approvals": [
                serialize_scheduled_approval(session, b) for b in bookings
            ],
        }


@app.get("/api/scheduled-approvals", tags=["scheduled-approvals"])
def list_scheduled_approvals(
    status: str | None = None,
    application_id: int | None = None,
) -> dict[str, Any]:
    """List appointments, optionally filtered by status and/or application.

    ``status`` is one of ``scheduled``/``approved``/``blocked``/``cancelled``/
    ``invalidated`` (default: every state, newest first within each
    application history).
    """
    valid = {SCHEDULED_STATUS, *SCHEDULED_TERMINAL_STATES}
    with SessionLocal() as session:
        query = session.query(ScheduledApproval)
        if status is not None:
            if status not in valid:
                raise APIError(
                    E_VALIDATION,
                    "status filter must be one of " + ", ".join(sorted(valid)),
                    status_code=422,
                )
            query = query.filter(ScheduledApproval.status == status)
        if application_id is not None:
            query = query.filter(
                ScheduledApproval.application_id == application_id
            )
        bookings = query.order_by(ScheduledApproval.id).all()
        current = session.get(RevisionMeta, 1).revision
        return {
            "current_revision": current,
            "count": len(bookings),
            "scheduled_approval_ids": [b.id for b in bookings],
            "scheduled_approvals": [
                serialize_scheduled_approval(session, b) for b in bookings
            ],
        }


@app.get(
    "/api/scheduled-approvals/{scheduled_approval_id}",
    tags=["scheduled-approvals"],
)
def get_scheduled_approval(scheduled_approval_id: int) -> dict[str, Any]:
    """Fetch one appointment: status, actual processing time and outcome.

    An approved appointment carries ``approval_revision`` and
    ``processed_guard_band``; a blocked one carries ``blocked_details``
    (conflicts and restriction-window hits captured at processing time).
    """
    with SessionLocal() as session:
        booking = session.get(ScheduledApproval, scheduled_approval_id)
        if booking is None:
            raise APIError(
                E_SCHEDULE_NOT_FOUND,
                f"scheduled approval {scheduled_approval_id} does not exist",
                status_code=404,
            )
        current = session.get(RevisionMeta, 1).revision
        body = serialize_scheduled_approval(session, booking)
        body["current_revision"] = current
        return body


@app.post(
    "/api/scheduled-approvals/process-due",
    tags=["scheduled-approvals"],
)
def process_due_scheduled_approvals_endpoint() -> dict[str, Any]:
    """Administration/test trigger: process every due appointment now.

    Does exactly what the background sweeper (and the startup catch-up pass)
    does: lock the singleton revision row and adjudicate each due appointment
    at the current instant.  Idempotent in the sense that only still-
    ``scheduled`` due rows fire, so a repeated call after a restart never
    approves the same appointment twice.  No body/expected revision is
    required: due processing is itself a revision writer and serialises
    against every other writer on the shared lock.
    """
    return run_scheduled_approval_sweep()


# --------------------------------------------------------------------------- #
# Temporary restriction windows (preview/activate/release, revision-arbitrated)
# --------------------------------------------------------------------------- #
def _window_preview_body(
    session, payload: RestrictionWindowRequest
) -> dict[str, Any]:
    """Shared read-only rehearsal computation for a proposed window.

    Lists the currently effective licenses and standby (waitlisted)
    applications the proposed window hits, together with the revision each
    listing is based on.  Never mutates state.  A hit requires the coverage
    circle, half-open UTC interval and closed frequency band all to
    intersect (touching band endpoints hit); the guard band never extends a
    window.
    """
    meta = session.get(RevisionMeta, 1)
    current = meta.revision
    window = proposed_window(payload)
    envelope = _proposed_window_envelope(window)

    # Narrow the effective set by the window's own time/coverage/closed-band
    # envelope first; the guard band never extends a window.
    effective_hits: list[dict[str, Any]] = []
    for app_row in (
        _apply_application_filters(
            session.query(Application), envelope, status="approved"
        )
        .order_by(Application.id)
        .all()
    ):
        lic = to_license(app_row)
        hit = adjudication.restriction_window_hit(lic, window)
        if hit is not None:
            effective_hits.append(
                {
                    "application_id": lic.application_id,
                    "application": _license_view(lic),
                    "trigger": {
                        "time_intersect": hit.time_intersect,
                        "area_intersect": hit.area_intersect,
                        "frequency_intersect": hit.frequency_intersect,
                    },
                }
            )

    # The queue position is defined against the full FIFO queue, so
    # positions are numbered from it; only envelope-matching pending
    # applications are fetched for the exact hit test.
    all_entries = load_waitlist(session)
    candidate_ids = {entry.application_id for entry in all_entries}
    candidate_rows: dict[int, Application] = {}
    if candidate_ids:
        for app_row in (
            _apply_application_filters(
                session.query(Application).filter(
                    Application.id.in_(candidate_ids)
                ),
                envelope,
                status="pending",
            )
            .order_by(Application.id)
            .all()
        ):
            candidate_rows[app_row.id] = app_row

    waitlist_hits: list[dict[str, Any]] = []
    for position, entry in enumerate(all_entries, start=1):
        row = candidate_rows.get(entry.application_id)
        if row is None:
            continue
        lic = to_license(row)
        hit = adjudication.restriction_window_hit(lic, window)
        if hit is not None:
            waitlist_hits.append(
                {
                    "application_id": row.id,
                    "queue_position": position,
                    "application": serialize_application(row),
                    "trigger": {
                        "time_intersect": hit.time_intersect,
                        "area_intersect": hit.area_intersect,
                        "frequency_intersect": hit.frequency_intersect,
                    },
                }
            )

    return {
        "window": {
            "circle": {
                "cx": payload.circle.cx,
                "cy": payload.circle.cy,
                "radius": payload.circle.radius,
            },
            "frequency": {
                "low": payload.frequency.low,
                "high": payload.frequency.high,
            },
            "time_window": {
                "start": iso(window.start_time),
                "end": iso(window.end_time),
            },
            "reason": payload.reason,
        },
        "current_revision": current,
        "hit_effective_application_ids": [h["application_id"] for h in effective_hits],
        "hit_effective": effective_hits,
        "hit_waitlist_application_ids": [h["application_id"] for h in waitlist_hits],
        "hit_waitlist": waitlist_hits,
        "hit_count": len(effective_hits) + len(waitlist_hits),
    }


def _validate_window_payload(payload: RestrictionWindowRequest) -> None:
    errors = validate_restriction_window(payload)
    if errors:
        raise APIError(
            E_VALIDATION,
            "the restriction window was rejected due to invalid fields",
            status_code=422,
            details=errors,
        )


@app.post("/api/restriction-windows/preview", tags=["restriction-windows"])
def preview_restriction_window(payload: RestrictionWindowRequest) -> dict[str, Any]:
    """Rehearse a temporary restriction window without changing any state.

    Returns the current revision (carry it into the confirmation as
    ``expected_revision``) and every currently effective license and
    standby application the proposed window would hit, with the triggering
    dimensions.  Illegal shapes (mirroring application validation) and
    already-expired windows are rejected as 422 and consume no revision.
    """
    _validate_window_payload(payload)
    with SessionLocal() as session:
        return _window_preview_body(session, payload)


@app.post("/api/restriction-windows", status_code=201, tags=["restriction-windows"])
def activate_restriction_window(
    payload: RestrictionWindowConfirmRequest,
) -> dict[str, Any]:
    """Confirm (activate) a rehearsed restriction window.

    Shares the singleton revision lock with every other writer and consumes
    exactly one revision.  A stale expected revision is refused as
    ``stale_revision`` and nothing changes.  Existing effective licenses
    that the window hits stay effective; from this revision onwards pending
    previews, single/batch approvals, suspended-license resumptions and
    post-release waitlist auto-approvals report hit windows and no write may
    make a hit application effective.  The response repeats the rehearsal
    hits based on the locked state.
    """
    _validate_window_payload(payload)
    with SessionLocal() as session:
        try:
            with session.begin():
                meta = session.get(RevisionMeta, 1, with_for_update=True)
                current = meta.revision

                if payload.expected_revision != current:
                    raise APIError(
                        E_STALE_REVISION,
                        f"expected revision {payload.expected_revision} but "
                        f"current revision is {current}; re-preview and "
                        "re-decide; the restriction window was not activated",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                        },
                    )

                preview_body = _window_preview_body(session, payload)

                start = payload.time_window.start.astimezone(timezone.utc)
                end = payload.time_window.end.astimezone(timezone.utc)
                row = RestrictionWindowRow(
                    revision=current + 1,
                    reason=payload.reason,
                    cx=payload.circle.cx,
                    cy=payload.circle.cy,
                    radius=payload.circle.radius,
                    freq_low=payload.frequency.low,
                    freq_high=payload.frequency.high,
                    start_time=start,
                    end_time=end,
                )
                session.add(row)
                session.flush()  # assign the stable window id

                meta.revision = current + 1
                session.add(
                    RestrictionWindowEvent(
                        revision=meta.revision,
                        window_id=row.id,
                        event_type="activated",
                        reason=payload.reason,
                        cx=row.cx,
                        cy=row.cy,
                        radius=row.radius,
                        freq_low=row.freq_low,
                        freq_high=row.freq_high,
                        start_time=start,
                        end_time=end,
                    )
                )
                session.flush()
                response = {
                    **preview_body,
                    "activated": True,
                    "window_id": row.id,
                    "id": row.id,
                    "revision": meta.revision,
                    "reason": payload.reason,
                    # Full activated window (replaces the preview's proposed
                    # shape) with the assigned id and activation revision.
                    "window": serialize_window_row(row),
                }
        except IntegrityError:
            # Backends without row locks (SQLite in tests): a concurrent
            # writer took the revision slot; nothing was changed.
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); "
                "re-preview and re-decide; the restriction window was not activated",
                status_code=409,
                details={"current_revision": latest},
            )
    return response


@app.post(
    "/api/restriction-windows/{window_id}/release",
    tags=["restriction-windows"],
)
def release_restriction_window(
    window_id: int, payload: RestrictionWindowReleaseRequest
) -> dict[str, Any]:
    """Lift a restriction window by its id (conditional on the revision).

    Shares revision arbitration with every other writer and consumes exactly
    one revision.  Releasing only affects later adjudication: no waitlist
    auto-approval is triggered and existing licenses are untouched.  An
    unknown window id is 404; releasing an already-released window is 409
    ``restriction_window_not_active``; a stale revision is 409 and nothing
    changes.
    """
    with SessionLocal() as session:
        try:
            with session.begin():
                meta = session.get(RevisionMeta, 1, with_for_update=True)
                current = meta.revision

                row = session.get(RestrictionWindowRow, window_id)
                if row is None:
                    # Distinguish "never existed" from "already released"
                    # via the immutable event stream.
                    activated = (
                        session.query(RestrictionWindowEvent)
                        .filter(
                            RestrictionWindowEvent.window_id == window_id,
                            RestrictionWindowEvent.event_type == "activated",
                        )
                        .one_or_none()
                    )
                    if activated is None:
                        raise APIError(
                            E_WINDOW_NOT_FOUND,
                            f"restriction window {window_id} does not exist",
                            status_code=404,
                        )
                    released = (
                        session.query(RestrictionWindowEvent)
                        .filter(
                            RestrictionWindowEvent.window_id == window_id,
                            RestrictionWindowEvent.event_type == "released",
                        )
                        .first()
                    )
                    raise APIError(
                        E_WINDOW_NOT_ACTIVE,
                        f"restriction window {window_id} is no longer active "
                        f"(released at revision {released.revision})",
                        status_code=409,
                        details={
                            "window_id": window_id,
                            "released_at_revision": released.revision,
                            "activated_at_revision": activated.revision,
                        },
                    )

                if payload.expected_revision != current:
                    raise APIError(
                        E_STALE_REVISION,
                        f"expected revision {payload.expected_revision} but "
                        f"current revision is {current}; re-decide; the "
                        "restriction window was not released",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                        },
                    )

                activated_revision = row.revision
                window_view = to_window(row)
                meta.revision = current + 1
                session.add(
                    RestrictionWindowEvent(
                        revision=meta.revision,
                        window_id=window_id,
                        event_type="released",
                        reason=payload.reason,
                    )
                )
                session.delete(row)
                session.flush()
                response = {
                    "released": True,
                    "window_id": window_id,
                    "revision": meta.revision,
                    "activated_at_revision": activated_revision,
                    "reason": payload.reason,
                    # A window release only changes later adjudication; the
                    # standby queue is intentionally not drained here.
                    "window": serialize_window(window_view),
                }
        except IntegrityError:
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); "
                "re-decide; the restriction window was not released",
                status_code=409,
                details={"current_revision": latest},
            )
    return response


@app.get("/api/restriction-windows", tags=["restriction-windows"])
def list_restriction_windows(
    status: str = "active", include_inactive: bool = False
) -> dict[str, Any]:
    """List restriction windows.

    By default only currently active windows are returned; pass
    ``include_inactive=true`` (or ``status=all``) to also reconstruct
    released windows from the immutable event stream.  ``status=released``
    returns only the released windows.
    """
    if status not in ("active", "released", "all"):
        raise APIError(
            E_VALIDATION,
            "status filter must be one of active, released, all",
            status_code=422,
        )
    want_active = include_inactive or status in ("active", "all")
    want_released = include_inactive or status in ("released", "all")
    with SessionLocal() as session:
        meta = session.get(RevisionMeta, 1)
        active_rows = load_active_windows(session)
        active_ids = {row.id for row in active_rows}

        active_bodies = (
            [serialize_window_row(row) for row in active_rows]
            if want_active
            else []
        )
        released_bodies: list[dict[str, Any]] = []
        if want_released:
            release_events = (
                session.query(RestrictionWindowEvent)
                .filter(RestrictionWindowEvent.event_type == "released")
                .order_by(RestrictionWindowEvent.revision)
                .all()
            )
            for ev in release_events:
                activation = (
                    session.query(RestrictionWindowEvent)
                    .filter(
                        RestrictionWindowEvent.window_id == ev.window_id,
                        RestrictionWindowEvent.event_type == "activated",
                    )
                    .one()
                )
                window = RestrictionWindow(
                    window_id=ev.window_id,
                    circle=Circle(
                        activation.cx, activation.cy, activation.radius
                    ),
                    freq_low=activation.freq_low,
                    freq_high=activation.freq_high,
                    start_time=_utc(activation.start_time),
                    end_time=_utc(activation.end_time),
                    reason=activation.reason,
                    revision=activation.revision,
                )
                body = serialize_window(window, status="released")
                body["activated_at_revision"] = activation.revision
                body["released_at_revision"] = ev.revision
                body["release_reason"] = ev.reason
                released_bodies.append(body)

        return {
            "current_revision": meta.revision,
            "active_restriction_window_ids": sorted(active_ids),
            "active_windows": active_bodies,
            "released_windows": released_bodies,
            "windows": active_bodies + released_bodies,
        }


@app.get("/api/restriction-windows/{window_id}", tags=["restriction-windows"])
def get_restriction_window(window_id: int) -> dict[str, Any]:
    """Fetch one restriction window (active or previously released)."""
    with SessionLocal() as session:
        row = session.get(RestrictionWindowRow, window_id)
        if row is not None:
            return {"window": serialize_window_row(row)}
        activation = (
            session.query(RestrictionWindowEvent)
            .filter(
                RestrictionWindowEvent.window_id == window_id,
                RestrictionWindowEvent.event_type == "activated",
            )
            .one_or_none()
        )
        if activation is None:
            raise APIError(
                E_WINDOW_NOT_FOUND,
                f"restriction window {window_id} does not exist",
                status_code=404,
            )
        release = (
            session.query(RestrictionWindowEvent)
            .filter(
                RestrictionWindowEvent.window_id == window_id,
                RestrictionWindowEvent.event_type == "released",
            )
            .one_or_none()
        )
        window = RestrictionWindow(
            window_id=window_id,
            circle=Circle(activation.cx, activation.cy, activation.radius),
            freq_low=activation.freq_low,
            freq_high=activation.freq_high,
            start_time=_utc(activation.start_time),
            end_time=_utc(activation.end_time),
            reason=activation.reason,
            revision=activation.revision,
        )
        body = serialize_window(
            window, status="released" if release else "active"
        )
        body["activated_at_revision"] = activation.revision
        if release is not None:
            body["released_at_revision"] = release.revision
            body["release_reason"] = release.reason
        return {"window": body}


# --------------------------------------------------------------------------- #
# Guard-band configuration (online, revision-arbitrated)
# --------------------------------------------------------------------------- #
@app.get("/api/config/guard-band", tags=["config"])
def get_guard_band() -> dict[str, Any]:
    """Live guard band, the initial seeded value and the current revision."""
    with SessionLocal() as session:
        meta = session.get(RevisionMeta, 1)
        seed = (
            session.query(GuardBandEvent)
            .filter(GuardBandEvent.revision == 0)
            .first()
        )
        guard_band = meta.guard_band if meta else settings.guard_band
        return {
            "current_revision": meta.revision if meta else 0,
            "guard_band": guard_band,
            "initial_guard_band": seed.guard_band if seed else guard_band,
        }


@app.post("/api/config/guard-band/preview", tags=["config"])
def preview_guard_band(payload: GuardBandPreviewRequest) -> dict[str, Any]:
    """Dry-run a proposed guard band against the current effective set.

    Returns the current revision (to be carried into the confirm call as
    ``expected_revision``) and every pair of currently effective licenses
    that would interfere under the proposed band.  Never mutates state.
    """
    errors = validate_guard_band(payload.guard_band)
    if errors:
        raise APIError(
            E_VALIDATION,
            "the guard band preview was rejected due to an invalid value",
            status_code=422,
            details=errors,
        )
    with SessionLocal() as session:
        meta = session.get(RevisionMeta, 1)
        effective = load_effective_licenses(session)
        pairs = adjudication.find_interfering_pairs(
            effective, payload.guard_band
        )
        return {
            "current_revision": meta.revision,
            "current_guard_band": meta.guard_band,
            "proposed_guard_band": payload.guard_band,
            "effective_application_ids": [
                lic.application_id for lic in effective
            ],
            "has_conflicts": bool(pairs),
            "interfering_pair_count": len(pairs),
            "interfering_pairs": [
                serialize_pair_conflict(p, payload.guard_band) for p in pairs
            ],
        }


@app.post("/api/config/guard-band", tags=["config"])
def update_guard_band(payload: GuardBandUpdateRequest) -> dict[str, Any]:
    """Confirm a new guard band (conditional on the expected revision).

    Shares the singleton revision lock with single/batch approvals and
    revocations.  The change is refused when the revision is stale or when
    the proposed band would make any pair of currently effective licenses
    interfere; in both cases the band, the effective set and the revision
    are left untouched.  On success exactly one revision is consumed and
    every later adjudication uses the new band.
    """
    errors = validate_guard_band(payload.guard_band)
    if errors:
        raise APIError(
            E_VALIDATION,
            "the guard band change was rejected due to an invalid value",
            status_code=422,
            details=errors,
        )
    with SessionLocal() as session:
        try:
            with session.begin():
                # Same singleton lock as approve/batch/revoke: one revision
                # arbitration for every writer.
                meta = session.get(RevisionMeta, 1, with_for_update=True)
                current = meta.revision

                if payload.expected_revision != current:
                    raise APIError(
                        E_STALE_REVISION,
                        f"expected revision {payload.expected_revision} but "
                        f"current revision is {current}; re-preview and "
                        "re-decide; the guard band was not changed",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                        },
                    )

                # Re-check under the lock: the new band must not make any
                # pair of currently effective licenses interfere.
                effective = load_effective_licenses(session)
                pairs = adjudication.find_interfering_pairs(
                    effective, payload.guard_band
                )
                if pairs:
                    raise APIError(
                        E_INTERFERENCE,
                        f"proposed guard band {payload.guard_band} would make "
                        f"{len(pairs)} effective license pair(s) interfere; "
                        "the guard band was not changed",
                        status_code=409,
                        details={
                            "current_revision": current,
                            "current_guard_band": meta.guard_band,
                            "proposed_guard_band": payload.guard_band,
                            "interfering_pairs": [
                                serialize_pair_conflict(p, payload.guard_band)
                                for p in pairs
                            ],
                        },
                    )

                previous = meta.guard_band
                meta.revision = current + 1
                meta.guard_band = payload.guard_band
                session.add(
                    GuardBandEvent(
                        revision=meta.revision,
                        previous_guard_band=previous,
                        guard_band=payload.guard_band,
                    )
                )
                session.flush()
                response = {
                    "updated": True,
                    "revision": meta.revision,
                    "previous_guard_band": previous,
                    "guard_band": payload.guard_band,
                }
        except IntegrityError:
            # Backends without row locks (SQLite in tests): a concurrent
            # writer took the revision slot; nothing was changed.
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); "
                "re-preview and re-decide; the guard band was not changed",
                status_code=409,
                details={"current_revision": latest},
            )
    return response


# --------------------------------------------------------------------------- #
# License handover (equipment swap: effective license -> pending application)
# --------------------------------------------------------------------------- #
def _validate_transfer_ids(payload: LicenseTransferRequest) -> None:
    """A handover must name two distinct applications."""
    if payload.source_application_id == payload.successor_application_id:
        raise APIError(
            E_VALIDATION,
            "the handover was rejected: the source license and the successor "
            "application must be distinct",
            status_code=422,
            details=[
                {
                    "field": "successor_application_id",
                    "code": E_SAME_APPLICATION,
                    "message": (
                        "successor_application_id must differ from "
                        "source_application_id"
                    ),
                    "application_id": payload.source_application_id,
                }
            ],
        )


@app.post("/api/license-transfers/preview", tags=["license-transfers"])
def preview_license_transfer(payload: LicenseTransferRequest) -> dict[str, Any]:
    """Rehearse handing an effective license to a pending application.

    Read-only: returns the current revision (carry it into the confirmation
    as ``expected_revision``), the successor's interference against the
    effective set with the source license excluded, and the active
    restriction windows it would hit.  Status problems (missing/non-effective
    source, a source that still holds an unreleased emergency occupation,
    non-pending successor) are listed under ``blockers`` rather than raised,
    so the rehearsal always shows the current picture; nothing is mutated
    and no revision is consumed.
    """
    _validate_transfer_ids(payload)
    with SessionLocal() as session:
        return transfer_preview_body(session, payload)


@app.post("/api/license-transfers", tags=["license-transfers"])
def confirm_license_transfer(
    payload: LicenseTransferConfirmRequest,
) -> dict[str, Any]:
    """Atomically revoke one effective license and approve its successor.

    Runs in the shared singleton revision arbitration.  Inside the lock the
    source must still be effective, must not hold an unreleased emergency
    occupation (such a license ends only through the dedicated emergency
    release), the successor must still be pending, the expected revision
    must be current, and the successor (adjudicated with the source removed
    from the effective set, under the live guard band and against every
    active restriction window) must be free of conflicts and window hits.
    Otherwise the whole handover is rejected with no change to licenses,
    the waitlist, the emergency occupation records or the revision.

    On success the ordinary source revocation consumes the first revision
    and the ordinary successor approval the next consecutive one; a queued
    successor leaves the waitlist first (its approval is manual, never
    ``auto_approved``), after which the remaining standbys are
    re-adjudicated by the existing FIFO drain and each passer consumes the
    following revision.  One immutable ``LicenseTransferEvent`` links the two
    handover revisions; existing revocation/approval semantics are kept.
    """
    _validate_transfer_ids(payload)
    with SessionLocal() as session:
        try:
            with session.begin():
                # Same singleton lock as every other writer.
                meta = session.get(RevisionMeta, 1, with_for_update=True)
                current = meta.revision
                guard_band = meta.guard_band

                source = session.get(Application, payload.source_application_id)
                if source is None:
                    raise APIError(
                        E_NOT_FOUND,
                        f"source application {payload.source_application_id} "
                        "does not exist",
                        status_code=404,
                    )
                successor = session.get(
                    Application, payload.successor_application_id
                )
                if successor is None:
                    raise APIError(
                        E_NOT_FOUND,
                        "successor application "
                        f"{payload.successor_application_id} does not exist",
                        status_code=404,
                    )

                # Status precedes the revision check (same precedence as the
                # single/batch approve handlers).
                if source.status != "approved":
                    raise APIError(
                        E_SOURCE_NOT_EFFECTIVE,
                        f"source application {source.id} is {source.status}; "
                        "only an approved, non-suspended, non-revoked license "
                        "can be handed over",
                        status_code=409,
                        details={
                            "application_id": source.id,
                            "status": source.status,
                        },
                    )
                active_emergency = (
                    session.query(EmergencyOccupation)
                    .filter(
                        EmergencyOccupation.emergency_application_id == source.id,
                        EmergencyOccupation.released_revision.is_(None),
                    )
                    .one_or_none()
                )
                if active_emergency is not None:
                    # Emergency licenses end exclusively through the dedicated
                    # emergency release; they can never be handed over as the
                    # source of a swap.  Rejected inside the shared arbitration
                    # before anything is written, so the source license, the
                    # successor application, the waitlist, the occupation
                    # record and the revision are all left untouched.
                    raise APIError(
                        E_EMERGENCY_LICENSE_PROTECTED,
                        f"source application {source.id} holds an active "
                        "emergency occupation; release the emergency occupation "
                        "before any handover; the handover was not performed",
                        status_code=409,
                        details={
                            "application_id": source.id,
                            "status": source.status,
                            "current_revision": current,
                            "emergency_occupation_id": active_emergency.id,
                            "emergency_approval_revision": (
                                active_emergency.emergency_approval_revision
                            ),
                        },
                    )
                if successor.status == "revoked":
                    raise APIError(
                        E_ALREADY_REVOKED,
                        f"successor application {successor.id} has been revoked",
                        status_code=409,
                    )
                if successor.status != "pending":
                    raise APIError(
                        E_SUCCESSOR_NOT_PENDING,
                        f"successor application {successor.id} is "
                        f"{successor.status}; only a pending application can "
                        "take over a license",
                        status_code=409,
                        details={
                            "application_id": successor.id,
                            "status": successor.status,
                            "approval_revision": successor.approval_revision,
                        },
                    )

                if payload.expected_revision != current:
                    raise APIError(
                        E_STALE_REVISION,
                        f"expected revision {payload.expected_revision} but "
                        f"current revision is {current}; re-preview and "
                        "re-decide; the handover was not performed",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                        },
                    )

                # Re-adjudicate under the lock with the source license
                # removed from the effective set, after narrowing the
                # database candidate set by the successor's envelope.
                candidate = to_license(successor)
                effective = load_candidate_effective_licenses(
                    session,
                    _candidate_envelope(candidate, guard_band),
                    exclude_ids={source.id, successor.id},
                )
                conflicts = adjudication.evaluate(
                    candidate, effective, guard_band
                )
                window_rows = load_candidate_windows(
                    session, _window_hit_envelope(candidate)
                )
                window_hits = window_hits_for(candidate, window_rows)
                if window_hits:
                    raise APIError(
                        E_RESTRICTION_WINDOW,
                        f"successor application {successor.id} hits "
                        f"{len(window_hits)} active restriction window(s); "
                        "the handover was not performed",
                        status_code=409,
                        details={
                            "current_revision": current,
                            "restriction_window_conflicts": [
                                serialize_window_hit(h) for h in window_hits
                            ],
                            "conflicts": [
                                serialize_conflict(c, guard_band)
                                for c in conflicts
                            ],
                        },
                    )
                if conflicts:
                    raise APIError(
                        E_INTERFERENCE,
                        f"successor application {successor.id} would interfere "
                        f"with {len(conflicts)} effective license(s) even after "
                        "the source license is released; the handover was not "
                        "performed",
                        status_code=409,
                        details={
                            "current_revision": current,
                            "conflicts": [
                                serialize_conflict(c, guard_band)
                                for c in conflicts
                            ],
                        },
                    )

                # A queued successor leaves the standby queue first, so it is
                # approved exactly once (manually) and never auto-approved.
                successor_entry = (
                    session.query(WaitlistEntry)
                    .filter(
                        WaitlistEntry.application_id == successor.id
                    )
                    .one_or_none()
                )
                successor_was_waitlisted = successor_entry is not None
                if successor_entry is not None:
                    session.delete(successor_entry)

                # Step 1: the ordinary source revocation.
                previous_approval_revision = source.approval_revision
                meta.revision = current + 1
                revocation_revision = meta.revision
                session.add(
                    RevocationEvent(
                        revision=revocation_revision,
                        application_id=source.id,
                        reason=payload.reason,
                    )
                )
                source.status = "revoked"

                # Step 2: the ordinary (manual) successor approval on the next
                # consecutive revision.
                meta.revision = revocation_revision + 1
                successor_revision = meta.revision
                session.add(
                    ApprovalEvent(
                        revision=successor_revision,
                        application_id=successor.id,
                        guard_band=guard_band,
                        auto_approved=False,
                        release_revision=None,
                    )
                )
                successor.status = "approved"
                successor.approval_revision = successor_revision

                # The handover decides the successor: its booked appointment
                # (if any) is superseded in the same transaction.
                invalidate_scheduled_approvals(
                    session,
                    successor.id,
                    "license_transfer",
                    successor_revision,
                )

                # The audit link between the two revisions of the handover.
                session.add(
                    LicenseTransferEvent(
                        revision=revocation_revision,
                        source_application_id=source.id,
                        successor_application_id=successor.id,
                        successor_approval_revision=successor_revision,
                        reason=payload.reason,
                    )
                )

                # Re-adjudicate the remaining standbys with the normal FIFO
                # rules; auto approvals keep the handover's revocation
                # revision as their release revision.
                waitlist_result = drain_waitlist(
                    session, meta, revocation_revision
                )
                session.flush()
                response = {
                    "transferred": True,
                    "source_application_id": source.id,
                    "successor_application_id": successor.id,
                    "expected_revision": payload.expected_revision,
                    "from_revision": current,
                    # Headline revision: the successor's approval revision.
                    "revision": successor_revision,
                    "revocation_revision": revocation_revision,
                    "successor_approval_revision": successor_revision,
                    "to_revision": meta.revision,
                    "guard_band": guard_band,
                    "reason": payload.reason,
                    "successor_was_waitlisted": successor_was_waitlisted,
                    "source_application": serialize_application(source),
                    "successor_application": serialize_application(successor),
                    "revocation": {
                        "revision": revocation_revision,
                        "application_id": source.id,
                        "previous_approval_revision": previous_approval_revision,
                        "reason": payload.reason,
                    },
                    "approval": {
                        "revision": successor_revision,
                        "application_id": successor.id,
                        "guard_band": guard_band,
                        "auto_approved": False,
                    },
                    **waitlist_result,
                }
        except IntegrityError:
            # Backends without row locks (SQLite in tests): a concurrent
            # writer took a revision slot; nothing was changed.
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); "
                "re-preview and re-decide; the handover was not performed",
                status_code=409,
                details={"current_revision": latest},
            )
    return response


# --------------------------------------------------------------------------- #
# Burst-communication emergency frequency occupations (preview / grab / release)
# --------------------------------------------------------------------------- #
@app.post("/api/emergency-occupations/preview", tags=["emergency-occupations"])
def preview_emergency_occupation(
    payload: EmergencyOccupationRequest,
) -> dict[str, Any]:
    """Rehearse an emergency occupation without changing any state.

    Read-only: returns the current revision (carry it into the confirm as
    ``expected_revision``), the ordinary effective licenses the emergency
    application would force-suspend (``force_suspend_licenses``), the active
    emergency licenses it would interfere with (``emergency_conflicts``), and
    the active restriction windows it hits.  Status problems are listed under
    ``blockers`` and ``ready`` is false rather than raising, so the operator
    always sees the current picture; nothing is mutated and no revision is
    consumed.
    """
    with SessionLocal() as session:
        return emergency_preview_body(session, payload)


@app.post("/api/emergency-occupations", status_code=201, tags=["emergency-occupations"])
def confirm_emergency_occupation(
    payload: EmergencyOccupationConfirmRequest,
) -> dict[str, Any]:
    """Atomically grab spectrum for a pending emergency application.

    Runs inside the shared singleton revision arbitration.  Under the lock
    the emergency application must still exist and be pending, the expected
    revision must be current, the application must hit no active restriction
    window and must not interfere with another *emergency* license (one
    emergency never force-suspends another).  Otherwise the whole occupation
    is rejected and licenses, the waitlist and the revision are untouched.

    On success the interfering *ordinary* effective licenses are force-
    suspended first, one consecutive revision each in a fixed order, each an
    ordinary suspension event flagged ``forced`` (no waitlist re-adjudication
    is triggered); a queued emergency application leaves the queue; the
    emergency license is then approved (an ordinary approval event flagged
    ``emergency``) at the next consecutive revision.  Original approval
    records are never rewritten.
    """
    with SessionLocal() as session:
        try:
            with session.begin():
                # Same singleton lock as every other writer.
                meta = session.get(RevisionMeta, 1, with_for_update=True)
                current = meta.revision
                guard_band = meta.guard_band

                row = session.get(Application, payload.application_id)
                if row is None:
                    raise APIError(
                        E_NOT_FOUND,
                        f"application {payload.application_id} does not exist",
                        status_code=404,
                    )
                if row.status == "revoked":
                    raise APIError(
                        E_ALREADY_REVOKED,
                        f"emergency application {row.id} has been revoked",
                        status_code=409,
                    )
                if row.status != "pending":
                    raise APIError(
                        E_NOT_PENDING,
                        f"emergency application {row.id} is {row.status}; "
                        "only a pending application can occupy spectrum in an "
                        "emergency",
                        status_code=409,
                        details={"status": row.status},
                    )

                if payload.expected_revision != current:
                    raise APIError(
                        E_STALE_REVISION,
                        f"expected revision {payload.expected_revision} but "
                        f"current revision is {current}; re-preview and "
                        "re-decide; the emergency occupation was not performed",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                        },
                    )

                # Re-adjudicate under the lock, splitting the narrowed
                # effective set into ordinary licenses (force-suspend
                # candidates) and emergency licenses (hard blockers).
                active_occupations = load_active_emergencies(session)
                emergency_ids = emergency_application_ids(active_occupations)
                candidate = to_license(row)
                effective = load_candidate_effective_licenses(
                    session, _candidate_envelope(candidate, guard_band)
                )
                ordinary_effective = [
                    lic
                    for lic in effective
                    if lic.application_id not in emergency_ids
                ]
                emergency_effective = [
                    lic
                    for lic in effective
                    if lic.application_id in emergency_ids
                ]
                ordinary_conflicts = adjudication.evaluate(
                    candidate, ordinary_effective, guard_band
                )
                emergency_conflicts = adjudication.evaluate(
                    candidate, emergency_effective, guard_band
                )
                window_rows = load_candidate_windows(
                    session, _window_hit_envelope(candidate)
                )
                window_hits = window_hits_for(candidate, window_rows)
                if window_hits:
                    raise APIError(
                        E_RESTRICTION_WINDOW,
                        f"emergency application {row.id} hits "
                        f"{len(window_hits)} active restriction window(s); "
                        "the emergency occupation was not performed",
                        status_code=409,
                        details={
                            "current_revision": current,
                            "restriction_window_conflicts": [
                                serialize_window_hit(h) for h in window_hits
                            ],
                            "conflicts": [
                                serialize_conflict(c, guard_band)
                                for c in ordinary_conflicts
                                + emergency_conflicts
                            ],
                        },
                    )
                if emergency_conflicts:
                    raise APIError(
                        E_EMERGENCY_CONFLICT,
                        f"emergency application {row.id} would interfere with "
                        f"{len(emergency_conflicts)} active emergency "
                        "license(s); an emergency never force-suspends another "
                        "emergency, so the occupation was not performed",
                        status_code=409,
                        details={
                            "current_revision": current,
                            "conflicts": [
                                serialize_conflict(c, guard_band)
                                for c in emergency_conflicts
                            ],
                        },
                    )

                # Step 1: force-suspend every interfering ordinary license,
                # one consecutive revision each in deterministic order.  This
                # deliberately does NOT drain the waitlist: no standby is
                # re-adjudicated while the spectrum is being grabbed.
                forced_rows = [
                    session.get(
                        Application, conflict.other.application_id
                    )
                    for conflict in ordinary_conflicts
                ]
                force_suspensions: list[dict[str, Any]] = []
                first_forced_revision: int | None = None
                for forced_row in forced_rows:
                    meta.revision += 1
                    if first_forced_revision is None:
                        first_forced_revision = meta.revision
                    session.add(
                        SuspensionEvent(
                            revision=meta.revision,
                            application_id=forced_row.id,
                            reason=payload.reason,
                            forced=True,
                            forced_by_emergency_application_id=row.id,
                        )
                    )
                    forced_row.status = "suspended"
                    forced_row.suspended_revision = meta.revision
                    forced_row.force_suspended_by = row.id
                    force_suspensions.append(
                        {
                            "revision": meta.revision,
                            "application_id": forced_row.id,
                            "application": serialize_application(forced_row),
                            "reason": payload.reason,
                        }
                    )
                last_forced_revision = meta.revision if forced_rows else None

                # Step 2: a queued emergency application leaves the standby
                # queue before its (manual) approval.
                entry = (
                    session.query(WaitlistEntry)
                    .filter(WaitlistEntry.application_id == row.id)
                    .one_or_none()
                )
                was_waitlisted = entry is not None
                if entry is not None:
                    session.delete(entry)

                # Step 3: approve the emergency license at the next
                # consecutive revision (an ordinary approval event, flagged).
                meta.revision += 1
                approval_revision = meta.revision
                session.add(
                    ApprovalEvent(
                        revision=approval_revision,
                        application_id=row.id,
                        guard_band=guard_band,
                        auto_approved=False,
                        release_revision=None,
                        emergency=True,
                    )
                )
                row.status = "approved"
                row.approval_revision = approval_revision

                # The emergency occupation decides the application: any
                # booked appointment is superseded in the same transaction.
                invalidate_scheduled_approvals(
                    session,
                    row.id,
                    "emergency_occupation",
                    approval_revision,
                )

                occupation = EmergencyOccupation(
                    emergency_application_id=row.id,
                    emergency_approval_revision=approval_revision,
                    first_force_suspension_revision=first_forced_revision,
                    last_force_suspension_revision=last_forced_revision,
                    reason=payload.reason,
                )
                session.add(occupation)
                session.flush()

                response = {
                    "occupied": True,
                    "id": occupation.id,
                    "application_id": row.id,
                    "emergency_application_id": row.id,
                    "expected_revision": payload.expected_revision,
                    "from_revision": current,
                    # The emergency approval revision is the headline one.
                    "revision": approval_revision,
                    "emergency_approval_revision": approval_revision,
                    "first_force_suspension_revision": first_forced_revision,
                    "last_force_suspension_revision": last_forced_revision,
                    "to_revision": meta.revision,
                    "guard_band": guard_band,
                    "reason": payload.reason,
                    "was_waitlisted": was_waitlisted,
                    "force_suspended_application_ids": [
                        f["application_id"] for f in force_suspensions
                    ],
                    "force_suspensions": force_suspensions,
                    "emergency_application": serialize_application(row),
                }
        except IntegrityError:
            # Backends without row locks (SQLite in tests): a concurrent
            # writer took a revision slot; nothing was changed.
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); "
                "re-preview and re-decide; the emergency occupation was not "
                "performed",
                status_code=409,
                details={"current_revision": latest},
            )
    return response


@app.post(
    "/api/emergency-occupations/{application_id}/release",
    tags=["emergency-occupations"],
)
def release_emergency_occupation(
    application_id: int, payload: EmergencyOccupationReleaseRequest
) -> dict[str, Any]:
    """End an emergency occupation: revoke, restore, then FIFO re-adjudicate.

    The whole sequence runs atomically inside the shared revision
    arbitration:

    1. the emergency license is revoked (one revision, an ordinary revocation
       event flagged ``emergency_release``);
    2. licenses still force-suspended *by this* occupation are
       re-adjudicated, in their original suspension order, against the live
       guard band and the effective set so far (which includes every license
       already restored) and every active restriction window.  Passers
       resume, one consecutive revision each (ordinary resumption events
       flagged ``forced_recovery``); blocked ones stay suspended, lose the
       emergency marker (they are ordinary outages again) and their blocking
       conflicts/window hits are returned;
    3. the standby queue is finally re-adjudicated by the normal FIFO drain.

    A stale expected revision, an unknown emergency id or a repeated release
    changes nothing.
    """
    with SessionLocal() as session:
        try:
            with session.begin():
                meta = session.get(RevisionMeta, 1, with_for_update=True)
                current = meta.revision
                guard_band = meta.guard_band

                occupation = (
                    session.query(EmergencyOccupation)
                    .filter(
                        EmergencyOccupation.emergency_application_id
                        == application_id
                    )
                    .one_or_none()
                )
                if occupation is None:
                    raise APIError(
                        E_EMERGENCY_NOT_FOUND,
                        f"application {application_id} has no emergency "
                        "occupation",
                        status_code=404,
                    )
                if occupation.released_revision is not None:
                    raise APIError(
                        E_EMERGENCY_NOT_ACTIVE,
                        f"emergency occupation of application "
                        f"{application_id} was already released at revision "
                        f"{occupation.released_revision}; a repeated release "
                        "changes nothing",
                        status_code=409,
                        details={
                            "released_revision": occupation.released_revision,
                            "emergency_approval_revision": (
                                occupation.emergency_approval_revision
                            ),
                        },
                    )

                if payload.expected_revision != current:
                    raise APIError(
                        E_STALE_REVISION,
                        f"expected revision {payload.expected_revision} but "
                        f"current revision is {current}; re-decide; the "
                        "emergency occupation was not released",
                        status_code=409,
                        details={
                            "expected_revision": payload.expected_revision,
                            "current_revision": current,
                        },
                    )

                emergency_row = session.get(Application, application_id)

                # Step 1: revoke the emergency license.
                meta.revision += 1
                release_revision = meta.revision
                session.add(
                    RevocationEvent(
                        revision=release_revision,
                        application_id=application_id,
                        reason=payload.reason,
                        emergency_release=True,
                        emergency_application_id=application_id,
                    )
                )
                if emergency_row is not None:
                    emergency_row.status = "revoked"

                # Step 2: restore the still-force-suspended licenses in their
                # original suspension order.
                forced_events = (
                    session.query(SuspensionEvent)
                    .filter(
                        SuspensionEvent.forced.is_(True),
                        SuspensionEvent.forced_by_emergency_application_id
                        == application_id,
                    )
                    .order_by(SuspensionEvent.revision)
                    .all()
                )
                restored: list[dict[str, Any]] = []
                blocked: list[dict[str, Any]] = []
                # Licenses restored earlier in this same release are
                # re-adjudicated in-memory in restoration order, matching
                # the ordering of the previous full-scan append.
                restored_licenses: list[License] = []
                restored_ids: set[int] = set()
                for susp_event in forced_events:
                    lic_row = session.get(
                        Application, susp_event.application_id
                    )
                    if lic_row is None:
                        continue
                    # Only licenses still suspended *by this* emergency right
                    # now are restored: ones revoked in the meantime stay
                    # revoked and nothing is replayed for them.
                    if (
                        lic_row.status != "suspended"
                        or lic_row.force_suspended_by != application_id
                    ):
                        continue
                    candidate = to_license(lic_row)
                    effective = load_candidate_effective_licenses(
                        session,
                        _candidate_envelope(candidate, guard_band),
                        exclude_ids=restored_ids,
                        extra_licenses=restored_licenses,
                    )
                    conflicts = adjudication.evaluate(
                        candidate, effective, guard_band
                    )
                    hits = window_hits_for(
                        candidate,
                        load_candidate_windows(
                            session, _window_hit_envelope(candidate)
                        ),
                    )
                    if conflicts or hits:
                        # Stays suspended: the emergency is over, so it is an
                        # ordinary suspension again and a normal resume is
                        # possible later.
                        lic_row.force_suspended_by = None
                        blocked.append(
                            {
                                "application_id": lic_row.id,
                                "application": serialize_application(lic_row),
                                "conflicts": [
                                    serialize_conflict(c, guard_band)
                                    for c in conflicts
                                ],
                                "restriction_window_conflicts": [
                                    serialize_window_hit(h) for h in hits
                                ],
                            }
                        )
                        continue

                    meta.revision += 1
                    session.add(
                        ResumptionEvent(
                            revision=meta.revision,
                            application_id=lic_row.id,
                            guard_band=guard_band,
                            reason=payload.reason,
                            forced_recovery=True,
                            emergency_application_id=application_id,
                        )
                    )
                    lic_row.status = "approved"
                    lic_row.suspended_revision = None
                    lic_row.force_suspended_by = None
                    restored_licenses.append(candidate)
                    restored_ids.add(lic_row.id)
                    restored.append(
                        {
                            "revision": meta.revision,
                            "application_id": lic_row.id,
                            "application": serialize_application(lic_row),
                            "guard_band": guard_band,
                        }
                    )

                # Step 3: FIFO re-adjudicate the standby queue.
                waitlist_result = drain_waitlist(
                    session, meta, release_revision
                )

                occupation.released_revision = release_revision
                occupation.release_reason = payload.reason
                occupation.released_at = datetime.now(timezone.utc)
                session.flush()

                response = {
                    "released": True,
                    "id": occupation.id,
                    "application_id": application_id,
                    "emergency_application_id": application_id,
                    "expected_revision": payload.expected_revision,
                    # The emergency license revocation revision (headline).
                    "revision": release_revision,
                    "emergency_revocation_revision": release_revision,
                    "restored_application_ids": [r["application_id"] for r in restored],
                    "restorations": restored,
                    "blocked_application_ids": [b["application_id"] for b in blocked],
                    "blocked": blocked,
                    "to_revision": meta.revision,
                    "final_revision": meta.revision,
                    "guard_band": guard_band,
                    "reason": payload.reason,
                    **waitlist_result,
                }
        except IntegrityError:
            session.rollback()
            latest = session.get(RevisionMeta, 1).revision
            raise APIError(
                E_STALE_REVISION,
                f"the revision advanced concurrently (now {latest}); "
                "re-decide; the emergency occupation was not released",
                status_code=409,
                details={"current_revision": latest},
            )
    return response


@app.get("/api/emergency-occupations", tags=["emergency-occupations"])
def list_emergency_occupations(
    status: str = "active", include_inactive: bool = False
) -> dict[str, Any]:
    """List emergency occupations.

    By default only currently active occupations are returned; pass
    ``include_inactive=true`` (or ``status=all``) to also include released
    ones.  ``status=released`` returns only released occupations.
    """
    if status not in ("active", "released", "all"):
        raise APIError(
            E_VALIDATION,
            "status filter must be one of active, released, all",
            status_code=422,
        )
    want_active = include_inactive or status in ("active", "all")
    want_released = include_inactive or status in ("released", "all")
    with SessionLocal() as session:
        meta = session.get(RevisionMeta, 1)
        rows = (
            session.query(EmergencyOccupation)
            .order_by(EmergencyOccupation.id)
            .all()
        )
        bodies: list[dict[str, Any]] = []
        active_bodies: list[dict[str, Any]] = []
        released_bodies: list[dict[str, Any]] = []
        for occ in rows:
            body = serialize_emergency_occupation(session, occ)
            if occ.released_revision is None:
                if want_active:
                    active_bodies.append(body)
                    bodies.append(body)
            elif want_released:
                released_bodies.append(body)
                bodies.append(body)
        return {
            "current_revision": meta.revision,
            "active_emergency_application_ids": [
                b["application_id"] for b in active_bodies
            ],
            "active_occupations": active_bodies,
            "released_occupations": released_bodies,
            # Rows in occupation id order across active + released.
            "occupations": bodies,
        }


@app.get(
    "/api/emergency-occupations/{application_id}",
    tags=["emergency-occupations"],
)
def get_emergency_occupation(application_id: int) -> dict[str, Any]:
    """Fetch one emergency occupation (active or previously released)."""
    with SessionLocal() as session:
        occ = (
            session.query(EmergencyOccupation)
            .filter(
                EmergencyOccupation.emergency_application_id == application_id
            )
            .one_or_none()
        )
        if occ is None:
            raise APIError(
                E_EMERGENCY_NOT_FOUND,
                f"application {application_id} has no emergency occupation",
                status_code=404,
            )
        return {"occupation": serialize_emergency_occupation(session, occ)}
