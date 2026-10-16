"""agent fleet briefings

Revision ID: c4a1f9b73e02
Revises: 767531c6565e
Create Date: 2026-08-16 14:20:11.482913
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from decisionflow.db.rls import disable_rls, enable_rls

revision: str = "c4a1f9b73e02"
down_revision: str | None = "767531c6565e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Pinned to the tables this revision creates. See the ingestion-core migration
# for why this is never imported from application code.
RLS_TABLES = ("briefing_runs", "briefing_findings")


def upgrade() -> None:
    op.create_table(
        "briefing_runs",
        sa.Column("dataset_id", sa.UUID(), nullable=False),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("narrative", sa.Text(), nullable=False),
        sa.Column(
            "recommendations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("plan_reasoning", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=80), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_by_id", sa.UUID(), nullable=True),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_briefing_runs_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name=op.f("fk_briefing_runs_dataset_id_datasets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_briefing_runs_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_briefing_runs")),
    )
    op.create_index(
        op.f("ix_briefing_runs_dataset_id"), "briefing_runs", ["dataset_id"], unique=False
    )
    op.create_index(op.f("ix_briefing_runs_org_id"), "briefing_runs", ["org_id"], unique=False)
    op.create_index(
        "ix_briefing_runs_dataset_id_created_at",
        "briefing_runs",
        ["dataset_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "briefing_findings",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("specialist", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("sql", sa.Text(), nullable=True),
        sa.Column("method", sa.String(length=80), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_briefing_findings_org_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["briefing_runs.id"],
            name=op.f("fk_briefing_findings_run_id_briefing_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_briefing_findings")),
    )
    op.create_index(
        op.f("ix_briefing_findings_run_id"), "briefing_findings", ["run_id"], unique=False
    )
    op.create_index(
        op.f("ix_briefing_findings_org_id"), "briefing_findings", ["org_id"], unique=False
    )

    for table in RLS_TABLES:
        for statement in enable_rls(table):
            op.execute(statement)


def downgrade() -> None:
    for table in RLS_TABLES:
        for statement in disable_rls(table):
            op.execute(statement)

    op.drop_index(op.f("ix_briefing_findings_org_id"), table_name="briefing_findings")
    op.drop_index(op.f("ix_briefing_findings_run_id"), table_name="briefing_findings")
    op.drop_table("briefing_findings")
    op.drop_index("ix_briefing_runs_dataset_id_created_at", table_name="briefing_runs")
    op.drop_index(op.f("ix_briefing_runs_org_id"), table_name="briefing_runs")
    op.drop_index(op.f("ix_briefing_runs_dataset_id"), table_name="briefing_runs")
    op.drop_table("briefing_runs")
