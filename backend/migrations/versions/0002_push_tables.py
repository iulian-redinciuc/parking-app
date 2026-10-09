"""Phase 6 tables: push_subscription, notification_log (data-model.md §1)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-09 06:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "push_subscription",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("p256dh", sa.String(), nullable=False),
        sa.Column("auth", sa.String(), nullable=False),
        sa.Column("prefs", sa.JSON(), nullable=False),
        sa.Column("tz", sa.String(), nullable=False),
        sa.Column("lang", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("last_seen_at", sa.String(length=32), nullable=False),
        sa.Column("on_my_way_until", sa.String(length=32), nullable=True),
        sa.Column("last_sent_at", sa.String(length=32), nullable=True),
        sa.Column("last_sent_free", sa.Integer(), nullable=True),
        sa.Column("last_sent_level", sa.String(), nullable=True),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("endpoint"),
    )
    op.create_table(
        "notification_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.String(length=32), nullable=False),
        sa.Column("subscription_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("notification_log", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_notification_log_ts"), ["ts"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("notification_log", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_notification_log_ts"))
    op.drop_table("notification_log")
    op.drop_table("push_subscription")
