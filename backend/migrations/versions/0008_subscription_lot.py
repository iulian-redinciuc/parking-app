"""push_subscription.lot_id: the lot a subscription follows (P9.5); NULL = the default lot

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-10 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("push_subscription", schema=None) as batch_op:
        batch_op.add_column(sa.Column("lot_id", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("push_subscription", schema=None) as batch_op:
        batch_op.drop_column("lot_id")
