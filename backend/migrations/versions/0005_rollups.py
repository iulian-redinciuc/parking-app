"""Phase 7 zone_minute / zone_hour rollups (data-model.md §1, §4, P7.5)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-09 14:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("zone_minute", "zone_hour")


def upgrade() -> None:
    """Upgrade schema."""
    for name in TABLES:
        op.create_table(
            name,
            sa.Column("zone_id", sa.String(), nullable=False),
            sa.Column("bucket_ts", sa.String(length=32), nullable=False),
            sa.Column("free_avg", sa.Float(), nullable=False),
            sa.Column("free_min", sa.Integer(), nullable=False),
            sa.Column("free_max", sa.Integer(), nullable=False),
            sa.Column("occupied_avg", sa.Float(), nullable=False),
            sa.Column("samples", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("zone_id", "bucket_ts"),
        )


def downgrade() -> None:
    """Downgrade schema."""
    for name in reversed(TABLES):
        op.drop_table(name)
