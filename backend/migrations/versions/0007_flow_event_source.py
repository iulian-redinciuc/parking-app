"""flow_event.source and flow_event.counted: barrier events and a zone's second source (P9.3)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-10 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("flow_event", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("source", sa.String(), nullable=False, server_default="camera")
        )
        batch_op.add_column(
            sa.Column("counted", sa.Boolean(), nullable=False, server_default=sa.true())
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("flow_event", schema=None) as batch_op:
        batch_op.drop_column("counted")
        batch_op.drop_column("source")
