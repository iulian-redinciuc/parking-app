"""weather_hour and forecast_model: the forecast model's weather and its nightly result (P9.6)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-10 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "weather_hour",
        sa.Column("lot_id", sa.String(), nullable=False),
        sa.Column("bucket_ts", sa.String(length=32), nullable=False),
        sa.Column("temp_c", sa.Float(), nullable=True),
        sa.Column("precip_mm", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("lot_id", "bucket_ts"),
    )
    op.create_table(
        "forecast_model",
        sa.Column("zone_id", sa.String(), nullable=False),
        sa.Column("trained_at", sa.String(length=32), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=False),
        sa.Column("holdout_rows", sa.Integer(), nullable=False),
        sa.Column("mae_model", sa.Float(), nullable=True),
        sa.Column("mae_median", sa.Float(), nullable=True),
        sa.Column("note", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("zone_id"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("forecast_model")
    op.drop_table("weather_hour")
