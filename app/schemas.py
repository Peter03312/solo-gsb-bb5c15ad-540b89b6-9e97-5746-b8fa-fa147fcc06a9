"""Pydantic request schemas.

Structural problems (missing / wrong-typed fields) are reported by FastAPI
and normalised in main.py.  Cross-field semantic rules are checked in
semantic_validation so the whole request is rejected with explicit codes:

* ``radius`` must be positive;
* ``freq_low`` may not exceed ``freq_high`` (equal is allowed, a single
  frequency);
* ``start_time`` must be strictly earlier than ``end_time``.

``BatchApprovalRequest`` additionally requires a non-empty ID list; duplicate
IDs inside the list are checked in the handler (with the standard envelope),
not by Pydantic.

``AvailableRangesRequest`` carries the closed interval of candidate band
lower bounds for the available-ranges search (a non-mutating query).

``WaitlistRegistrationRequest`` is the optional body (``{"reason": ...}``)
when registering a pending application into the FIFO standby queue; the
waitlist cancel endpoint takes no body.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CircleIn(BaseModel):
    cx: float
    cy: float
    radius: float


class FrequencyBandIn(BaseModel):
    low: float
    high: float


class TimeWindowIn(BaseModel):
    # Timezone-aware ISO-8601 instants (UTC recommended, e.g. 2026-10-01T08:00:00Z).
    start: datetime
    end: datetime


class ApplicationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    circle: CircleIn
    frequency: FrequencyBandIn
    time_window: TimeWindowIn


class ApprovalRequest(BaseModel):
    # Revision the client based its preview decision on; required.
    expected_revision: int = Field(ge=0)


class BatchApprovalRequest(BaseModel):
    # Whole-group conditional approval: every listed application must still be
    # pending and every one must pass adjudication (against the current
    # effective set *and* against the rest of the group), or nothing is
    # approved.
    expected_revision: int = Field(ge=0)
    # Non-empty list of distinct application IDs, approved in this order.
    application_ids: list[int] = Field(min_length=1)


class RevocationRequest(BaseModel):
    reason: str = Field(default="", max_length=2000)


class SuspensionRequest(BaseModel):
    # Temporary equipment downtime: take an approved, non-revoked license out
    # of the effective set.  Shares revision arbitration with every other
    # writer; the expected revision is mandatory.
    expected_revision: int = Field(ge=0)
    reason: str = Field(default="", max_length=2000)


class ResumptionRequest(BaseModel):
    # Restore a suspended license after re-adjudication against the current
    # guard band and all effective licenses.  Shares revision arbitration;
    # a stale revision or renewed interference leaves it suspended and
    # consumes no revision.
    expected_revision: int = Field(ge=0)
    reason: str = Field(default="", max_length=2000)


class GuardBandPreviewRequest(BaseModel):
    # Proposed guard band to dry-run against the current effective set.
    # Finite/non-negative checks run in semantic_validation so they use the
    # standard error envelope.
    guard_band: float


class GuardBandUpdateRequest(BaseModel):
    # Confirm a previously previewed value: the same proposed band plus the
    # revision the decision was based on.  Shares revision arbitration with
    # single/batch approvals and revocations.
    guard_band: float
    expected_revision: int = Field(ge=0)


class AvailableRangesRequest(BaseModel):
    # Closed interval of candidate lower bounds for the application's band;
    # the band width, coverage circle and time window stay as submitted.
    # Equal bounds query a single candidate position.  Finite/ordering
    # checks run in semantic_validation so they use the standard envelope.
    candidate_low_min: float
    candidate_low_max: float


class WaitlistRegistrationRequest(BaseModel):
    # Register a pending application into the FIFO standby queue while the
    # band is busy.  The body is optional; ``reason`` is purely informational.
    reason: str = Field(default="", max_length=2000)


class RestrictionWindowRequest(BaseModel):
    # A temporary spectrum-control window proposal (used by both the
    # read-only rehearsal preview and the confirming activation): coverage
    # circle, closed frequency band and half-open UTC interval, plus an
    # operator reason.  Geometry/band/time validation (including the
    # not-already-expired rule) runs in semantic_validation so failures use
    # the standard error envelope.
    circle: CircleIn
    frequency: FrequencyBandIn
    time_window: TimeWindowIn
    reason: str = Field(default="", max_length=2000)


class RestrictionWindowConfirmRequest(RestrictionWindowRequest):
    # Activate a rehearsed window: the same proposal plus the expected
    # revision (from the preview/current revision).  Shares revision
    # arbitration with every other writer and consumes exactly one revision.
    expected_revision: int = Field(ge=0)


class RestrictionWindowReleaseRequest(BaseModel):
    # Release a previously confirmed window by its id and the expected
    # revision.  Only later adjudication is affected; the waitlist is not
    # auto-approved.  Consumes exactly one revision.
    expected_revision: int = Field(ge=0)
    reason: str = Field(default="", max_length=2000)


class LicenseTransferRequest(BaseModel):
    """Hand over one effective license to a pending application (device swap).

    Used by both the read-only rehearsal preview and the confirming call.
    ``source_application_id`` is the currently effective license to revoke;
    ``successor_application_id`` is the still-pending application that
    replaces it.  The preview needs no revision; the confirm request adds the
    mandatory ``expected_revision`` (see :class:`LicenseTransferConfirmRequest`).
    """

    source_application_id: int = Field(ge=1)
    successor_application_id: int = Field(ge=1)
    reason: str = Field(default="", max_length=2000)


class LicenseTransferConfirmRequest(LicenseTransferRequest):
    """Commit a rehearsed handover under the shared revision arbitration.

    On success the source revocation and the successor approval consume two
    consecutive revisions; a queued successor leaves the waitlist first and
    the remaining standbys are re-adjudicated by the normal FIFO rules.  A
    stale revision, a status change, renewed interference or a restriction
    window hit rejects the whole transaction without touching licenses, the
    queue or the revision.
    """

    expected_revision: int = Field(ge=0)


class EmergencyOccupationRequest(BaseModel):
    """Rehearse a burst-communication emergency frequency occupation.

    Names the still-pending emergency application (whose own circle, band and
    time window describe the spectrum being grabbed) and an operator reason.
    The read-only preview lists the current revision, the ordinary effective
    licenses that would be force-suspended, any already-emergency licenses
    that would clash, and the active restriction windows the emergency
    application hits.  Nothing is mutated and no revision is consumed.
    """

    application_id: int = Field(ge=1)
    reason: str = Field(default="", max_length=2000)


class EmergencyOccupationConfirmRequest(EmergencyOccupationRequest):
    """Commit a rehearsed emergency occupation under revision arbitration.

    On success the interfering ordinary licenses are force-suspended (one
    consecutive revision each, without a waitlist drain), a queued emergency
    application leaves the queue, and the emergency license is approved at
    the next consecutive revision.  A stale revision, a status change, a
    restriction-window hit or a clash with another emergency license rejects
    the whole occupation without touching licenses, the queue or the
    revision.
    """

    expected_revision: int = Field(ge=0)


class EmergencyOccupationReleaseRequest(BaseModel):
    """End an emergency occupation by its emergency application id.

    Carries the expected revision and an optional reason.  The emergency
    license is revoked first, the licenses still force-suspended by this
    occupation are re-adjudicated in their suspension order against the
    current guard band and active windows and restored one revision each
    (blocked ones stay suspended with the reason returned), and the standby
    queue is finally re-adjudicated FIFO; the whole sequence commits
    atomically.  A stale revision or a repeated release changes nothing.
    """

    expected_revision: int = Field(ge=0)
    reason: str = Field(default="", max_length=2000)


class ScheduledApprovalRequest(BaseModel):
    """Book (or reschedule) a UTC auto-approval appointment.

    ``scheduled_at`` must be a timezone-aware UTC instant strictly later than
    the booking moment and strictly earlier than the application's own time
    window end.  Booking consumes no revision; the time-validity checks run in
    the handler (they need the application row and the current clock) and
    failures use the standard error envelope, leaving no appointment behind.
    """

    scheduled_at: datetime
    reason: str = Field(default="", max_length=2000)


class ScheduledApprovalCancelRequest(BaseModel):
    """Withdraw a booked appointment before it fires (optional reason)."""

    reason: str = Field(default="", max_length=2000)
