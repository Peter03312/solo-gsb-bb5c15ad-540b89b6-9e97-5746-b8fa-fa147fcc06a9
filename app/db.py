"""Database engine, startup readiness wait and schema initialisation."""
from __future__ import annotations

import logging
import time

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from .config import settings

log = logging.getLogger("radio.db")

engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def wait_for_database(timeout_seconds: float = 60.0) -> None:
    """Block until the database accepts connections (Compose startup race)."""
    deadline = time.monotonic() + timeout_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            log.info("database is ready after %d attempt(s)", attempt)
            return
        except OperationalError:
            if time.monotonic() >= deadline:
                raise
            log.info("waiting for database (attempt %d) ...", attempt)
            time.sleep(1.0)


def _ensure_guard_band_column() -> None:
    """Add ``revision_meta.guard_band`` to databases created before it existed.

    ``create_all`` never alters existing tables, so the column is added with
    a plain idempotent ``ALTER TABLE``; its value is backfilled in
    ``init_db`` from the ``GUARD_BAND`` environment variable, which was the
    operative setting before online configuration existed.
    """
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("revision_meta")}
    if "guard_band" in columns:
        return
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE revision_meta ADD COLUMN guard_band FLOAT")
        )


def _ensure_suspended_revision_column() -> None:
    """Add ``applications.suspended_revision`` to older databases.

    Mirrors :func:`_ensure_guard_band_column`: ``create_all`` only creates
    missing tables, so the nullable column marking a temporary outage is
    added idempotently; existing rows simply start as NULL (not suspended).
    """
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("applications")}
    if "suspended_revision" in columns:
        return
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE applications ADD COLUMN suspended_revision INTEGER")
        )


def _ensure_approval_event_waitlist_columns() -> None:
    """Add waitlist-era columns to ``approval_events`` on older databases.

    ``auto_approved`` marks approvals granted automatically while draining
    the standby queue on a suspend/revoke; ``release_revision`` records which
    release revision triggered that auto approval.  Both are added
    idempotently; historical manual approvals start as ``0`` / NULL.
    """
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("approval_events")}
    with engine.begin() as conn:
        if "auto_approved" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE approval_events "
                    "ADD COLUMN auto_approved BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )
        if "release_revision" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE approval_events "
                    "ADD COLUMN release_revision INTEGER"
                )
            )
        # Burst-communication emergency occupations: marks the ordinary
        # approval event that grants an emergency license.
        if "emergency" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE approval_events "
                    "ADD COLUMN emergency BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )
        # Operator-booked scheduled auto-approvals: marks the ordinary
        # approval event the due appointment granted.
        if "scheduled" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE approval_events "
                    "ADD COLUMN scheduled BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )


def _ensure_narrowing_indexes() -> None:
    """Create the candidate-narrowing indexes on older databases.

    ``create_all`` only creates missing tables/indexes for a fresh
    database, so databases created before adjudication narrowed its
    candidate set get these indexes added idempotently (by name).  They
    back the status/time/frequency/coverage predicates used to shrink the
    effective-license and active-window candidate sets before the exact
    in-process interference rules run.
    """
    wanted = {
        "applications": (
            ("ix_applications_status_time", "status", "start_time", "end_time"),
            ("ix_applications_status_freq", "status", "freq_low", "freq_high"),
            ("ix_applications_status_space", "status", "cx", "cy"),
        ),
        "restriction_windows": (
            ("ix_restriction_windows_time", "start_time", "end_time"),
            ("ix_restriction_windows_freq", "freq_low", "freq_high"),
            ("ix_restriction_windows_space", "cx", "cy"),
        ),
    }
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, indexes in wanted.items():
            if table not in existing_tables:
                continue
            present = {ix["name"] for ix in inspector.get_indexes(table)}
            for name, *columns in indexes:
                if name in present:
                    continue
                column_list = ", ".join(columns)
                conn.execute(
                    text(
                        f"CREATE INDEX IF NOT EXISTS {name} "
                        f"ON {table} ({column_list})"
                    )
                )


def _ensure_emergency_columns() -> None:
    """Add emergency-occupation-era columns to older databases.

    Mirrors :func:`_ensure_guard_band_column`: ``create_all`` only creates
    missing tables, so every nullable/Boolean column the emergency feature
    adds to an existing table is added idempotently with a compatible
    default; historical rows read back as ordinary events / ordinary
    effective licenses.
    """
    inspector = inspect(engine)

    def columns_of(table: str) -> set[str]:
        return {col["name"] for col in inspector.get_columns(table)}

    with engine.begin() as conn:
        app_cols = columns_of("applications")
        if "force_suspended_by" not in app_cols:
            conn.execute(
                text(
                    "ALTER TABLE applications "
                    "ADD COLUMN force_suspended_by INTEGER"
                )
            )

        rev_cols = columns_of("revocation_events")
        if "emergency_release" not in rev_cols:
            conn.execute(
                text(
                    "ALTER TABLE revocation_events "
                    "ADD COLUMN emergency_release BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )
        if "emergency_application_id" not in rev_cols:
            conn.execute(
                text(
                    "ALTER TABLE revocation_events "
                    "ADD COLUMN emergency_application_id INTEGER"
                )
            )

        susp_cols = columns_of("suspension_events")
        if "forced" not in susp_cols:
            conn.execute(
                text(
                    "ALTER TABLE suspension_events "
                    "ADD COLUMN forced BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )
        if "forced_by_emergency_application_id" not in susp_cols:
            conn.execute(
                text(
                    "ALTER TABLE suspension_events "
                    "ADD COLUMN forced_by_emergency_application_id INTEGER"
                )
            )

        res_cols = columns_of("resumption_events")
        if "forced_recovery" not in res_cols:
            conn.execute(
                text(
                    "ALTER TABLE resumption_events "
                    "ADD COLUMN forced_recovery BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )
        if "emergency_application_id" not in res_cols:
            conn.execute(
                text(
                    "ALTER TABLE resumption_events "
                    "ADD COLUMN emergency_application_id INTEGER"
                )
            )


def _ensure_scheduled_approval_events() -> None:
    """Backfill lifecycle events for appointments predating the event stream.

    ``create_all`` creates the ``scheduled_approval_events`` table itself;
    this seeds the immutable events for every legacy ``scheduled_approvals``
    row that has none yet, so historical revision snapshots keep listing it.
    Booking, rescheduling, cancellation and processing-time blocks consumed
    no revision, so their exact decision revision is unknowable for legacy
    rows: their events pin at revision 0 (visible in every snapshot, exactly
    how those rows were presented before the event stream existed).  A due
    approval pins at its recorded ``approval_revision`` and an invalidation
    at the revision of the application's approval event (the decision that
    superseded the appointment).  Rows that already have events are skipped,
    so the backfill runs once and never touches post-upgrade history.
    """
    from .models import (
        ApprovalEvent,
        ScheduledApproval,
        ScheduledApprovalEvent,
    )

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if (
        "scheduled_approvals" not in tables
        or "scheduled_approval_events" not in tables
    ):
        return
    with SessionLocal() as session, session.begin():
        known = {
            row_id
            for (row_id,) in session.query(
                ScheduledApprovalEvent.scheduled_approval_id
            ).distinct()
        }
        rows = (
            session.query(ScheduledApproval)
            .order_by(ScheduledApproval.id)
            .all()
        )
        for row in rows:
            if row.id in known:
                continue
            session.add(
                ScheduledApprovalEvent(
                    scheduled_approval_id=row.id,
                    application_id=row.application_id,
                    revision=0,
                    event_type="booked",
                    scheduled_at=row.scheduled_at,
                    reason=row.reason,
                    created_at=row.created_at,
                )
            )
            if row.status == "approved":
                session.add(
                    ScheduledApprovalEvent(
                        scheduled_approval_id=row.id,
                        application_id=row.application_id,
                        revision=row.approval_revision or 0,
                        event_type="approved",
                        processed_at=row.processed_at,
                        processed_guard_band=row.processed_guard_band,
                        approval_revision=row.approval_revision,
                        created_at=row.updated_at,
                    )
                )
            elif row.status == "invalidated":
                # The application was decided by another path; its (single)
                # approval event carries the deciding revision.
                deciding = (
                    session.query(ApprovalEvent)
                    .filter(
                        ApprovalEvent.application_id == row.application_id
                    )
                    .first()
                )
                session.add(
                    ScheduledApprovalEvent(
                        scheduled_approval_id=row.id,
                        application_id=row.application_id,
                        revision=deciding.revision if deciding else 0,
                        event_type="invalidated",
                        outcome_reason=row.outcome_reason,
                        created_at=row.updated_at,
                    )
                )
            elif row.status == "blocked":
                session.add(
                    ScheduledApprovalEvent(
                        scheduled_approval_id=row.id,
                        application_id=row.application_id,
                        revision=0,
                        event_type="blocked",
                        processed_at=row.processed_at,
                        processed_guard_band=row.processed_guard_band,
                        blocked_details=row.blocked_details,
                        created_at=row.updated_at,
                    )
                )
            elif row.status == "cancelled":
                session.add(
                    ScheduledApprovalEvent(
                        scheduled_approval_id=row.id,
                        application_id=row.application_id,
                        revision=0,
                        event_type="cancelled",
                        outcome_reason=row.outcome_reason,
                        created_at=row.updated_at,
                    )
                )


def init_db() -> None:
    """Create tables and seed the singleton revision row (revision 0).

    The guard band is seeded from ``GUARD_BAND`` only on a brand-new
    database (or backfilled once into databases created before online
    guard-band configuration existed).  An already-effective setting is
    never overwritten at startup: restarts keep the live value.
    """
    # Imported here so models register on Base.metadata before create_all().
    from . import models  # noqa: F401
    from .models import Base, GuardBandEvent, RevisionMeta

    Base.metadata.create_all(bind=engine)
    _ensure_guard_band_column()
    _ensure_suspended_revision_column()
    _ensure_approval_event_waitlist_columns()
    _ensure_emergency_columns()
    _ensure_narrowing_indexes()
    _ensure_scheduled_approval_events()
    with SessionLocal() as session, session.begin():
        meta = session.get(RevisionMeta, 1)
        if meta is None:
            meta = RevisionMeta(
                id=1, revision=0, guard_band=settings.guard_band
            )
            session.add(meta)
            session.flush()
        elif meta.guard_band is None:
            # Database predates online guard-band configuration: the
            # GUARD_BAND env var was the operative setting, adopt it once.
            meta.guard_band = settings.guard_band
        seed = (
            session.query(GuardBandEvent)
            .filter(GuardBandEvent.revision == 0)
            .first()
        )
        if seed is None:
            # Initial-value record so every historical revision snapshot can
            # restore the band in force at that time.
            session.add(
                GuardBandEvent(
                    revision=0,
                    previous_guard_band=None,
                    guard_band=meta.guard_band,
                )
            )
