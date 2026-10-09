"""push_subscription.admin_alerts: the subscription receives admin alerts (P7.8)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-09 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("push_subscription", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("admin_alerts", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("push_subscription", schema=None) as batch_op:
        batch_op.drop_column("admin_alerts")
