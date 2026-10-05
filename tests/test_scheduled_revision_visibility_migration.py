"""Upgrade path: legacy databases without appointment revision metadata.

Older deployments stored only the mutable, always-current appointment row,
so a later invalidation leaked into every revision snapshot.  Startup adds
``booked_revision`` / ``outcome_revision`` / ``outcome_at`` and the
``scheduled_approval_changes`` journal, then backfills best-effort values
from the immutable event stream.  These tests pin that behaviour and its
idempotency.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text


NOW = datetime.now(timezone.utc).replace(microsecond=0)


def test_legacy_approved_appointment_rewinds_in_old_revision(client):
    from app.db import SessionLocal, engine, init_db
    from app import models

    # Simulate a database from before the visibility columns existed.
    models.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE scheduled_approvals DROP COLUMN booked_revision")
        )
        conn.execute(
            text("ALTER TABLE scheduled_approvals DROP COLUMN outcome_revision")
        )
        conn.execute(text("ALTER TABLE scheduled_approvals DROP COLUMN outcome_at"))
        conn.execute(text("DROP TABLE scheduled_approval_changes"))
        # The client fixture already seeded revision 0; advance it to 1.
        conn.execute(
            text("UPDATE revision_meta SET revision = 1 WHERE id = 1")
        )
        conn.execute(
            text(
                "INSERT INTO applications (id,name,cx,cy,radius,freq_low,"
                "freq_high,start_time,end_time,status,approval_revision,"
                "created_at) VALUES (1,'A',0,0,1000,100,110,:s,:e,"
                "'approved',1,:now)"
            ),
            {"s": NOW + timedelta(days=2), "e": NOW + timedelta(days=3),
             "now": NOW},
        )
        conn.execute(
            text(
                "INSERT INTO approval_events (revision,application_id,"
                "guard_band,auto_approved,release_revision,emergency,"
                "scheduled,created_at) VALUES (1,1,2.0,0,NULL,0,1,:now)"
            ),
            {"now": NOW},
        )
        conn.execute(
            text(
                "INSERT INTO scheduled_approvals (id,application_id,"
                "scheduled_at,status,reason,processed_at,approval_revision,"
                "processed_guard_band,created_at,updated_at) VALUES (1,1,"
                ":at,'approved','r',:now,1,2.0,:now,:now)"
            ),
            {"at": NOW, "now": NOW},
        )

    init_db()  # the startup upgrade + backfill

    r0 = client.get("/api/revision/0").json()["scheduled_approvals"]
    r1 = client.get("/api/revision/1").json()["scheduled_approvals"]
    assert r0[0]["status"] == "scheduled"
    assert r0[0]["approval_revision"] is None
    assert r0[0]["booked_revision"] == 0
    assert r1[0]["status"] == "approved"
    assert r1[0]["approval_revision"] == 1
    assert r1[0]["outcome_revision"] == 1

    with SessionLocal() as session:
        rows = session.query(models.ScheduledApprovalChange).all()
        assert len(rows) == 1
        assert rows[0].change_type == "booked"
        assert rows[0].revision == 0

    # A second startup must not duplicate the journal or fail.
    init_db()
    with SessionLocal() as session:
        assert session.query(models.ScheduledApprovalChange).count() == 1
