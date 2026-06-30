"""add_case_set_pass_results

Revision ID: b7c9d1e2f3a4
Revises: a1b2c3d4e5f6
Create Date: 2026-06-25
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b7c9d1e2f3a4"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "eval_runs",
        sa.Column("attempt_index", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.create_index(
        "idx_eval_runs_task_case_attempt",
        "eval_runs",
        ["task_id", "eval_case_id", "attempt_index"],
        unique=False,
    )

    op.create_table(
        "case_set_eval_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_set_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_version", sa.String(length=100), nullable=False),
        sa.Column("formula", sa.String(length=30), nullable=False),
        sa.Column("k", sa.Integer(), nullable=False),
        sa.Column("score_threshold", sa.NUMERIC(precision=5, scale=2), nullable=False),
        sa.Column("power_threshold", sa.NUMERIC(precision=5, scale=4), nullable=False),
        sa.Column("min_case_pass_rate", sa.NUMERIC(precision=5, scale=4), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("total_cases", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("passed_cases", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed_cases", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("insufficient_cases", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("case_pass_rate", sa.NUMERIC(precision=7, scale=4), nullable=False, server_default=sa.text("0")),
        sa.Column("attempt_pass_rate", sa.NUMERIC(precision=7, scale=4), nullable=False, server_default=sa.text("0")),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["case_set_id"], ["case_sets.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["eval_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id"),
    )
    op.create_index("idx_case_set_eval_results_case_set", "case_set_eval_results", ["case_set_id"])
    op.create_index("idx_case_set_eval_results_passed", "case_set_eval_results", ["passed"])
    op.create_index("idx_case_set_eval_results_status", "case_set_eval_results", ["status"])
    op.create_index("idx_case_set_eval_results_task", "case_set_eval_results", ["task_id"])

    op.create_table(
        "case_set_eval_case_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("result_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("eval_case_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("completed_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("passed_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("required_passes", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("best_score", sa.NUMERIC(precision=5, scale=2), nullable=True),
        sa.Column("avg_score", sa.NUMERIC(precision=5, scale=2), nullable=True),
        sa.Column("attempts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["result_id"], ["case_set_eval_results.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("result_id", "eval_case_id", name="uq_case_set_eval_case_result"),
    )
    op.create_index("idx_case_set_eval_case_results_case", "case_set_eval_case_results", ["eval_case_id"])
    op.create_index("idx_case_set_eval_case_results_passed", "case_set_eval_case_results", ["passed"])
    op.create_index("idx_case_set_eval_case_results_result", "case_set_eval_case_results", ["result_id"])


def downgrade() -> None:
    op.drop_index("idx_case_set_eval_case_results_result", table_name="case_set_eval_case_results")
    op.drop_index("idx_case_set_eval_case_results_passed", table_name="case_set_eval_case_results")
    op.drop_index("idx_case_set_eval_case_results_case", table_name="case_set_eval_case_results")
    op.drop_table("case_set_eval_case_results")

    op.drop_index("idx_case_set_eval_results_task", table_name="case_set_eval_results")
    op.drop_index("idx_case_set_eval_results_status", table_name="case_set_eval_results")
    op.drop_index("idx_case_set_eval_results_passed", table_name="case_set_eval_results")
    op.drop_index("idx_case_set_eval_results_case_set", table_name="case_set_eval_results")
    op.drop_table("case_set_eval_results")

    op.drop_index("idx_eval_runs_task_case_attempt", table_name="eval_runs")
    op.drop_column("eval_runs", "attempt_index")
