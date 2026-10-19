"""Discovery tables: runs, candidates, sightings, the publishing outbox and the audit log.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-08 15:32:42.180235
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("actor", sa.String(length=120), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("target_type", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_table(
        "discovery_runs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("flow", sa.String(length=8), nullable=False),
        sa.Column("trigger", sa.String(length=16), server_default="manual", nullable=False),
        sa.Column("requested_by", sa.String(length=120), nullable=True),
        sa.Column("filter", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("filter_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column(
            "stats", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("cost_usd", sa.Numeric(precision=10, scale=4), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("flow IN ('venue', 'event')", name=op.f("ck_discovery_runs_flow")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovery_runs")),
        sa.UniqueConstraint("code", name=op.f("uq_discovery_runs_code")),
    )
    op.create_index(op.f("ix_discovery_runs_filter_hash"), "discovery_runs", ["filter_hash"], unique=False)
    op.create_index(
        "uq_runs_active",
        "discovery_runs",
        ["flow", "filter_hash"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_table(
        "event_candidates",
        sa.Column("occurrences", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("first_start", sa.Date(), nullable=True),
        sa.Column("last_end", sa.Date(), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("fingerprint", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("city", sa.String(length=120), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("tg_city_id", sa.Integer(), nullable=True),
        sa.Column("score", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("scores", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("found_by", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("fast_approve", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("safety_flag", sa.String(length=32), nullable=True),
        sa.Column("rejection_reason", sa.String(length=200), nullable=True),
        sa.Column("first_run_id", sa.Uuid(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_changed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("seen_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("reviewed_by", sa.String(length=120), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_candidates")),
        sa.UniqueConstraint("fingerprint", name=op.f("uq_event_candidates_fingerprint")),
    )
    op.create_index(op.f("ix_event_candidates_city"), "event_candidates", ["city"], unique=False)
    op.create_index(op.f("ix_event_candidates_first_start"), "event_candidates", ["first_start"], unique=False)
    op.create_index(op.f("ix_event_candidates_status"), "event_candidates", ["status"], unique=False)
    op.create_table(
        "publish_outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("flow", sa.String(length=8), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("target", sa.String(length=40), server_default="travelgay", nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publish_outbox")),
    )
    op.create_index(op.f("ix_publish_outbox_candidate_id"), "publish_outbox", ["candidate_id"], unique=False)
    op.create_table(
        "sightings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("flow", sa.String(length=8), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("providers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("score", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sightings")),
    )
    op.create_index(op.f("ix_sightings_run_id"), "sightings", ["run_id"], unique=False)
    op.create_index("uq_sightings_run_candidate", "sightings", ["run_id", "flow", "candidate_id"], unique=True)
    op.create_table(
        "venue_candidates",
        sa.Column("address", sa.String(length=400), nullable=True),
        sa.Column("lat", sa.Double(), nullable=True),
        sa.Column("lng", sa.Double(), nullable=True),
        sa.Column(
            "external_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("fingerprint", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("city", sa.String(length=120), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("tg_city_id", sa.Integer(), nullable=True),
        sa.Column("score", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("scores", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("found_by", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("fast_approve", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("safety_flag", sa.String(length=32), nullable=True),
        sa.Column("rejection_reason", sa.String(length=200), nullable=True),
        sa.Column("first_run_id", sa.Uuid(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_changed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("seen_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("reviewed_by", sa.String(length=120), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_venue_candidates")),
        sa.UniqueConstraint("fingerprint", name=op.f("uq_venue_candidates_fingerprint")),
    )
    op.create_index(op.f("ix_venue_candidates_city"), "venue_candidates", ["city"], unique=False)
    op.create_index(op.f("ix_venue_candidates_status"), "venue_candidates", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_venue_candidates_status"), table_name="venue_candidates")
    op.drop_index(op.f("ix_venue_candidates_city"), table_name="venue_candidates")
    op.drop_table("venue_candidates")
    op.drop_index("uq_sightings_run_candidate", table_name="sightings")
    op.drop_index(op.f("ix_sightings_run_id"), table_name="sightings")
    op.drop_table("sightings")
    op.drop_index(op.f("ix_publish_outbox_candidate_id"), table_name="publish_outbox")
    op.drop_table("publish_outbox")
    op.drop_index(op.f("ix_event_candidates_status"), table_name="event_candidates")
    op.drop_index(op.f("ix_event_candidates_first_start"), table_name="event_candidates")
    op.drop_index(op.f("ix_event_candidates_city"), table_name="event_candidates")
    op.drop_table("event_candidates")
    op.drop_index(
        "uq_runs_active", table_name="discovery_runs", postgresql_where=sa.text("status IN ('queued', 'running')")
    )
    op.drop_index(op.f("ix_discovery_runs_filter_hash"), table_name="discovery_runs")
    op.drop_table("discovery_runs")
    op.drop_table("audit_log")
