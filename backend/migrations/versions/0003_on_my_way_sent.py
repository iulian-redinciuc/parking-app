"""push_subscription.on_my_way_sent: pushes in the current on-my-way window (P6.6)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-09 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("push_subscription", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("on_my_way_sent", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("push_subscription", schema=None) as batch_op:
        batch_op.drop_column("on_my_way_sent")
