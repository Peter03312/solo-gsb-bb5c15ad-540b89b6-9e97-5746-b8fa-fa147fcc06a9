"""SQLAlchemy ORM models.

History is event-sourced:

* ``applications``       - every submitted request, rejected submissions are
                           never persisted (see whole-request validation);
* ``approval_events``    - append-only approval record, one per revision
                           (``auto_approved``/``release_revision`` mark
                           approvals granted automatically from the waitlist
                           during a suspend/revoke release);
* ``suspension_events``  - append-only record of a temporary outage: an
                           approved license is taken out of the effective set,
                           one per revision;
* ``resumption_events``  - append-only record restoring a suspended license
                           after re-adjudication, one per revision (keeps the
                           guard band snapshot the re-decision used);
* ``revocation_events``  - append-only revocation record, one per revision
                           (a suspended license may be permanently revoked);
* ``license_transfer_events`` - append-only equipment-change handover record,
                           one per handover, keyed by the revocation revision;
                           links the ordinary revocation of the source license
                           and the ordinary (manual) approval of its pending
                           successor, which keep two consecutive revisions;
* ``guard_band_events``  - append-only guard-band record, one per revision
                           (revision 0 is the initial value seeded at database
                           creation, every later row is one confirmed online
                           change);
* ``waitlist_entries``    - FIFO queue of pending applications registered as
                           standbys while the band is busy; registration and
                           cancellation consume no revision and the entry is
                           removed on approval (manual or automatic);
* ``restriction_windows`` - currently active temporary spectrum-control
                           windows (confirmed but not yet released); each
                           confirmation/release consumes one revision;
* ``restriction_window_events`` - append-only activation/release records for
                           restriction windows, one per revision, so snapshots
                           reconstruct the windows active at any revision;
* ``emergency_occupations`` - one row per burst-communication emergency
                           frequency grab: the emergency application, its
                           approval revision, the spanned force-suspension
                           revisions and the release revision (NULL while
                           active).  The forced suspensions are ordinary
                           ``suspension_events`` rows flagged ``forced``, the
                           release-time restorations are ordinary
                           ``resumption_events`` rows flagged
                           ``forced_recovery``, and the emergency approval /
                           release revocation are ordinary approval /
                           revocation events flagged ``emergency`` /
                           ``emergency_release``;
* ``scheduled_approvals`` - one row per operator-booked UTC auto-approval
                           appointment for a pending application.  Exactly one
                           ``scheduled`` row may exist per application; it is
                           either cancelled/rescheduled before firing,
                           invalidated when the application is decided by
                           another path, or processed exactly once at its due
                           time (re-adjudicated then: approved on one new
                           revision, or recorded blocked while the
                           application stays pending and no revision is used).
                           ``booked_revision`` marks the revision in force when
                           the appointment became visible and ``outcome_revision``
                           marks the first revision showing each terminal
                           outcome, so as-of snapshots never leak later
                           outcomes into earlier revisions.  The approval
                           itself is an ordinary ``approval_events`` row
                           flagged ``scheduled``;
* ``scheduled_approval_changes`` - append-only booking/reschedule ledger for
                           appointments.  Booking and rescheduling consume no
                           revision; every row records the revision in force at
                           the time and the booked instant/reason then chosen,
                           so an as-of snapshot restores the exact appointment
                           shape visible at that revision;
* ``revision_meta``      - singleton row holding the current revision number
                           and the guard band currently in force.

A revocation never deletes history: it only removes an application from the
effective set used by later adjudications.  A suspension does the same
temporarily until a resumption puts the license back.  Snapshots for any past
revision are reconstructed from the event tables, including the guard band in
force at that revision.  Approval events keep their own immutable copy of the
band they were decided under and are never rewritten; resumption events keep
the band of their own re-adjudication.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Coverage circle in planar coordinates.
    cx: Mapped[float] = mapped_column(Float, nullable=False)
    cy: Mapped[float] = mapped_column(Float, nullable=False)
    radius: Mapped[float] = mapped_column(Float, nullable=False)

    # Closed frequency interval [freq_low, freq_high].
    freq_low: Mapped[float] = mapped_column(Float, nullable=False)
    freq_high: Mapped[float] = mapped_column(Float, nullable=False)

    # UTC half-open interval [start_time, end_time).
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    end_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # pending -> approved -> suspended -> approved -> ... -> revoked
    # (suspension is temporary equipment downtime; revoked is terminal)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    approval_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Revision of the latest suspension while status == "suspended"; cleared
    # again on every resumption and left set as a historical marker on revoke.
    suspended_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # While an emergency occupation is active, an ordinary license forced out
    # of the effective set by that occupation carries the emergency
    # application's id here; NULL for ordinary suspensions and effective
    # licenses.  Cleared on the emergency release (the license either resumes
    # or turns into an ordinary suspension) and on revoke.  Used by the
    # live-state guards (ordinary resume/suspend refusal); historical as-of
    # state is replayed from the suspension/resumption events instead.
    force_suspended_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    # The adjudication chains narrow their database candidate set by
    # status + time window / frequency band / coverage before the exact
    # in-process interference predicates run, so the leading columns are
    # indexed; SQLite/PostgreSQL also use the trailing columns for the
    # corresponding range predicates.
    __table_args__ = (
        Index("ix_applications_status_time", "status", "start_time", "end_time"),
        Index("ix_applications_status_freq", "status", "freq_low", "freq_high"),
        Index("ix_applications_status_space", "status", "cx", "cy"),
    )


class ApprovalEvent(Base):
    __tablename__ = "approval_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), nullable=False
    )
    # Snapshot of the guard band in force when the decision was made.
    guard_band: Mapped[float] = mapped_column(Float, nullable=False)
    # True when the approval was granted automatically from the waitlist
    # while a suspension/revoke transaction was draining the queue.  Manual
    # approvals leave this false; original approval events are otherwise
    # identical and never rewritten.
    auto_approved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # For an auto approval: the single revision consumed by the suspend/
    # revoke release that triggered the queue drain (NULL for manual and
    # batch approvals).  Lets historical snapshots restore which automatic
    # decision each revision belonged to.
    release_revision: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    # True when this approval is the (manual, never auto-approved) approval
    # of a burst-communication emergency occupation.  The emergency audit row
    # (EmergencyOccupation) links this revision; the approval event itself is
    # otherwise an ordinary, immutable approval record.
    emergency: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # True when this approval was granted by a due scheduled (booked)
    # auto-approval appointment.  The ScheduledApproval audit row links this
    # revision; the approval event keeps the ordinary approval semantics and
    # is never rewritten (its ``auto_approved`` flag stays false: a scheduled
    # approval is not a waitlist drain decision).
    scheduled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (Index("ix_approval_events_application", "application_id"),)


class RevocationEvent(Base):
    __tablename__ = "revocation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # True when this revocation is the end of an emergency occupation
    # (releasing the emergency license during the emergency release).  The
    # event is otherwise an ordinary, immutable revocation record.
    emergency_release: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # For an emergency-release revocation: the emergency application id whose
    # occupation is ending (NULL for ordinary and handover revocations).
    emergency_application_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (Index("ix_revocation_events_application", "application_id"),)


class LicenseTransferEvent(Base):
    """Append-only record of an equipment-change license handover.

    A handover permanently revokes one currently effective license (the
    active-device license being handed in) and approves one still-pending
    application (the replacement device) as one atomic, conditional
    transaction.  The source revocation and the successor approval are the
    ordinary :class:`RevocationEvent` / :class:`ApprovalEvent` rows on two
    consecutive revisions (existing revoke/approve semantics are kept); this
    row only links them for audit.

    ``revision`` is the revocation revision; ``successor_approval_revision``
    is the next consecutive revision (both unique).  ``reason`` is the
    operator reason given for the handover.  A successor that was queued as a
    standby leaves the waitlist before its approval; afterwards the queue is
    re-adjudicated by the normal FIFO rules, exactly like after an ordinary
    revoke.  History snapshots replay this row alongside the ordinary
    events, so every revision of a handover (and the auto approvals it may
    trigger) reconstructs exactly as it was; original approval events are
    never rewritten.
    """

    __tablename__ = "license_transfer_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The handover's first revision: the source license revocation.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    source_application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), nullable=False
    )
    successor_application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), nullable=False
    )
    # The handover's second revision: the successor's (manual) approval.
    successor_approval_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, unique=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        Index("ix_license_transfer_events_source", "source_application_id"),
        Index("ix_license_transfer_events_successor", "successor_application_id"),
    )


class SuspensionEvent(Base):
    """Append-only suspension record, one per revision.

    A suspension marks a temporary equipment outage: the license leaves the
    effective set immediately, keeps its original approval, and may later be
    resumed or permanently revoked.  Shares the revision sequence with
    approvals, resumptions, revocations and guard-band changes.
    """

    __tablename__ = "suspension_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # True when this suspension was *forced* by a burst-communication
    # emergency occupation (the ordinary license had to clear the spectrum),
    # rather than an equipment outage.  A forced suspension never triggers a
    # waitlist re-adjudication; while its emergency is active the license can
    # be revoked but not ordinarily suspended or resumed.
    forced: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # The emergency application id that forced this suspension (NULL for an
    # ordinary equipment-outage suspension).
    forced_by_emergency_application_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (Index("ix_suspension_events_application", "application_id"),)


class ResumptionEvent(Base):
    """Append-only resumption record, one per revision.

    Restores a suspended license after re-adjudicating it against the current
    guard band and every effective license.  Carries the band snapshot the
    re-decision used, mirroring ``ApprovalEvent``; the original approval event
    is never rewritten.
    """

    __tablename__ = "resumption_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), nullable=False
    )
    # Snapshot of the guard band in force when the license was resumed.
    guard_band: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # True when this resumption is the automatic restoration during an
    # emergency-occupation release (the forced suspension is lifted), as
    # opposed to an operator-driven ordinary resume.
    forced_recovery: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # For a forced recovery: the emergency application id whose occupation is
    # being released (NULL for ordinary resumptions).
    emergency_application_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (Index("ix_resumption_events_application", "application_id"),)


class GuardBandEvent(Base):
    """Append-only guard-band record, one per revision.

    The row at revision 0 is the initial value seeded when the database was
    created (``previous_guard_band`` is NULL there); every later row is one
    confirmed online change and consumes exactly one revision, sharing the
    revision sequence with approvals and revocations.
    """

    __tablename__ = "guard_band_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    previous_guard_band: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    guard_band: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class RestrictionWindow(Base):
    """A currently active temporary spectrum-control window.

    Operators preview (a read-only rehearsal) and then confirm a window:
    coverage circle, closed frequency band, half-open UTC time interval,
    reason and the expected revision.  Confirmation consumes exactly one
    revision and inserts the window; releasing it with its window id and the
    expected revision consumes another.  Releasing only affects later
    adjudication and never auto-approves waitlist entries.

    Existing effective licenses stay effective when a window appears; from
    the confirmation revision onwards every pending preview, single/batch
    approval, suspended-license resumption and post-release waitlist
    auto-approval reports hit windows and no write may make a hit
    application effective.  A hit requires the circle, the half-open time
    interval and the closed frequency band all to intersect (touching band
    endpoints hit); the guard band never extends a window.

    This table mirrors the mutable current state; the append-only
    :class:`RestrictionWindowEvent` rows are the source of truth for
    historical revision snapshots.  ``start_time``/``end_time`` and the
    geometry are NULL on a release row (which only carries window/revision).
    """

    __tablename__ = "restriction_windows"

    # Stable window id assigned at confirmation; reused by the release call.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Revision at which the window was confirmed (activated).
    revision: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")

    cx: Mapped[float] = mapped_column(Float, nullable=False)
    cy: Mapped[float] = mapped_column(Float, nullable=False)
    radius: Mapped[float] = mapped_column(Float, nullable=False)
    freq_low: Mapped[float] = mapped_column(Float, nullable=False)
    freq_high: Mapped[float] = mapped_column(Float, nullable=False)
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    end_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    # Window-hit adjudication narrows by the same three dimensions (with
    # closed-band frequency intersection; the guard band never widens a
    # window), so the active-window table carries matching indexes.
    __table_args__ = (
        Index("ix_restriction_windows_time", "start_time", "end_time"),
        Index("ix_restriction_windows_freq", "freq_low", "freq_high"),
        Index("ix_restriction_windows_space", "cx", "cy"),
    )


class RestrictionWindowEvent(Base):
    """Append-only lifecycle record of a restriction window, one per revision.

    ``event_type`` is ``"activated"`` (confirmation) or ``"released"``.
    Activation rows carry the full geometry, band, half-open interval and
    reason; release rows carry only the window id.  Historical revision
    snapshots replay these rows to reconstruct the windows active at any
    revision; original approval events are never rewritten.
    """

    __tablename__ = "restriction_window_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    # NULL on release rows; the activated window's id otherwise.
    window_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")

    cx: Mapped[float | None] = mapped_column(Float, nullable=True)
    cy: Mapped[float | None] = mapped_column(Float, nullable=True)
    radius: Mapped[float | None] = mapped_column(Float, nullable=True)
    freq_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    freq_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    end_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class WaitlistEntry(Base):
    """A pending application registered as a standby (FIFO queue).

    Only ``pending`` applications can be queued and each application can be
    queued at most once (unique constraint).  Registration and cancellation
    consume **no** revision: the queue is ordered strictly by insertion (the
    primary key doubles as the queue sequence number).  A successful manual
    or batch approval removes the entry (a manually approved application must
    never be auto-approved afterwards).  A suspension or revoke that releases
    an effective license drains the queue in registration order inside the
    same transaction: entries that pass re-adjudication under the live guard
    band are approved automatically, the rest keep their place and the drain
    continues with the following entries.  An entry hit by an active
    restriction window is blocked just like a license conflict: it keeps its
    place and the drain continues with the following entries.
    """

    __tablename__ = "waitlist_entries"

    # Monotonic queue sequence: registration order is the primary key order.
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), nullable=False, unique=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class RevisionMeta(Base):
    """Singleton row (id == 1): current revision + live guard band."""

    __tablename__ = "revision_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Live guard band used by every new adjudication.  Seeded once from the
    # GUARD_BAND environment variable; afterwards only changed through the
    # online config endpoint and never overwritten at startup.
    guard_band: Mapped[float] = mapped_column(Float, nullable=False)


class EmergencyOccupation(Base):
    """Audit + current-state row for one burst-communication emergency grab.

    An emergency occupation lets an operator take spectrum ahead of normal
    queued decisions: a read-only rehearsal (the emergency preview) lists the
    current revision, the ordinary effective licenses the emergency candidate
    would interfere with (these get *force-suspended* on confirmation), the
    already-emergency licenses it would interfere with (these reject the
    whole grab) and the active restriction windows it hits.  The confirmation
    carries the expected revision and, inside the shared revision
    arbitration, re-checks that the emergency application is still pending,
    hits no window and interferes with no other emergency license; otherwise
    the whole request is rejected without touching the revision.

    On success the interfering ordinary licenses are force-suspended first,
    one consecutive revision each, in a fixed order (each is an ordinary
    ``SuspensionEvent`` flagged ``forced`` and **does not** trigger a waitlist
    re-adjudication); a queued emergency application leaves the waitlist; the
    emergency license is then approved (an ordinary ``ApprovalEvent`` flagged
    ``emergency``) at the next consecutive revision.  The original approval
    records of the force-suspended licenses are never rewritten.

    An emergency license can neither be ordinarily suspended nor ordinarily
    revoked; the occupation ends only through the dedicated release (which
    revokes the emergency license, restores the still-suspended licenses one
    by one after re-adjudication and finally drains the waitlist, all
    atomically).  A force-suspended license can be revoked ordinarily but an
    ordinary resume is refused while its emergency is active.  A stale
    expected revision or a repeated release leaves the state untouched.

    The row is the mutable current-state mirror (``released_revision`` is
    NULL while active) and the audit link between the revisions; the append-
    only approval/suspension/resumption/revocation events are the source of
    truth for historical revision snapshots.
    """

    __tablename__ = "emergency_occupations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # The emergency application whose approval is the occupation.
    emergency_application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), nullable=False, unique=True, index=True
    )
    # Revision at which the emergency license was approved (the occupation's
    # last confirmation revision, after the force-suspension revisions).
    emergency_approval_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, unique=True
    )
    # First/last force-suspension revisions bookkeeping; NULL only when the
    # emergency interfered with no ordinary license (both stay NULL).
    first_force_suspension_revision: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    last_force_suspension_revision: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # NULL while the occupation is active; set to the emergency license
    # revocation revision when the dedicated release ends the occupation.
    released_revision: Mapped[int | None] = mapped_column(
        Integer, nullable=True, unique=True
    )
    release_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ScheduledApproval(Base):
    """An operator-booked UTC auto-approval appointment for one application.

    Operators book an appointment for a ``pending`` application at a UTC
    instant strictly after the booking time and strictly before the
    application's own time window ends.  Booking consumes no revision; at most
    one ``scheduled`` row exists per application (partial unique index), and
    it may be cancelled or rescheduled before it fires.

    A background sweeper (and a startup catch-up pass) processes due rows
    inside the shared singleton revision arbitration.  At the actual
    processing instant the application is re-adjudicated against the live
    guard band, the current effective set and every active restriction
    window:

    * it passes  -> one ordinary ``ApprovalEvent`` flagged ``scheduled``
      consumes exactly one new revision, a queued application leaves the
      waitlist first, and the appointment becomes ``approved``;
    * it clashes or hits a window -> the blockers are recorded on this row,
      the appointment becomes ``blocked``, the application stays ``pending``
      and no revision is consumed; nothing is retried automatically.

    An application decided first by another path -- a manual single/batch
    approval, a license-handover successor approval, an emergency occupation
    or a waitlist auto-approval during a suspend/revoke/emergency-release
    drain -- leaves its appointment ``invalidated`` inside the same
    transaction, so it can never fire afterwards.  Cancellation,
    rescheduling, due processing and the other decisions therefore compete
    under one arbitration and exactly one outcome wins; a restart never
    approves the same appointment twice (only ``scheduled`` rows fire).

    Revision visibility (bookings consume no revision): ``booked_revision``
    is the revision in force when the appointment was booked (or rebooked),
    and ``outcome_revision`` is the first revision from which each terminal
    outcome is visible -- the approval revision when approved, the revision
    in force when it was blocked/cancelled, or the deciding revision when
    invalidated.  An as-of snapshot at revision R hides rows booked after R
    and rewinds outcomes decided after R back to the still-waiting shape, so
    a later decision (e.g. an invalidation at revision 1) never shows up at
    an earlier revision.  Reschedules are journaled in
    ``scheduled_approval_changes`` with the revision in force at each move.
    """

    __tablename__ = "scheduled_approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), nullable=False
    )
    # The booked UTC auto-approval instant; rescheduling overwrites it and
    # bumps ``updated_at`` (every earlier instant is kept in
    # ``scheduled_approval_changes`` for revision-accurate audit).
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # "scheduled"  - waiting for ``scheduled_at`` (the only actionable state);
    # "approved"   - processed: an ordinary scheduled approval was granted;
    # "blocked"    - processed: interference/window hit, still pending, no
    #                automatic retry (blockers in ``blocked_details``);
    # "cancelled"  - withdrawn by an operator before it fired;
    # "invalidated"- the application was decided by another path first.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="scheduled", server_default="scheduled"
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Processing outcome fields (NULL while the appointment is still
    # scheduled/cancelled-before-processing).
    # The UTC instant the sweeper actually re-adjudicated the appointment.
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Revision consumed by the scheduled approval (NULL when blocked).
    approval_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Guard band snapshot used by the processing-time re-adjudication.
    processed_guard_band: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Blocking detail captured when processing found interference/window hits:
    # {"conflicts": [...], "restriction_window_conflicts": [...]} in the same
    # shape as a pending preview.
    blocked_details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Why a terminal non-approval state was reached (operator reason on
    # cancel; short machine reason on invalidation, e.g.
    # "manual_approval" / "waitlist_auto_approval" / "license_transfer" /
    # "emergency_occupation").
    outcome_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    # Revision in force when the appointment became visible (booking/rebooking
    # consumes no revision, so it is visible from this revision on).  Used by
    # as-of snapshots to hide appointments booked in later revisions.
    booked_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    # First revision from which the terminal status (approved/blocked/
    # cancelled/invalidated) is visible.  NULL while still waiting: a
    # cancellation/block uses the revision in force at that moment (the
    # operation consumes no revision), an approval uses the revision it
    # consumed and an invalidation uses the revision of the deciding event.
    # Snapshots at smaller revisions rewind the row to ``scheduled``.
    outcome_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # UTC instant the terminal status took effect (NULL while waiting); lets
    # historical snapshots report the decision time without leaking later
    # outcome fields.
    outcome_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index(
            "uq_scheduled_approvals_active_application",
            "application_id",
            unique=True,
            sqlite_where=text("status = 'scheduled'"),
            postgresql_where=text("status = 'scheduled'"),
        ),
        Index(
            "ix_scheduled_approvals_due",
            "status",
            "scheduled_at",
        ),
    )


class ScheduledApprovalChange(Base):
    """Append-only booking/reschedule journal of one appointment.

    Neither booking nor rescheduling consumes a revision, but every entry
    records the revision in force at that moment together with the booked
    instant and reason then chosen.  An as-of snapshot at revision R
    restores the latest entry with ``revision <= R`` (the booking entry is
    the fallback), so a reschedule performed at a later revision never moves
    the appointment in earlier revisions.  Same-revision moves overwrite one
    another in the snapshot because the revision window includes the whole
    revision: the latest state within a revision is what callers see.
    """

    __tablename__ = "scheduled_approval_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scheduled_approval_id: Mapped[int] = mapped_column(
        ForeignKey("scheduled_approvals.id"), nullable=False
    )
    application_id: Mapped[int] = mapped_column(
        ForeignKey("applications.id"), nullable=False
    )
    # "booked" or "rescheduled".
    change_type: Mapped[str] = mapped_column(String(20), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        Index(
            "ix_scheduled_approval_changes_lookup",
            "scheduled_approval_id",
            "revision",
            "id",
        ),
    )
