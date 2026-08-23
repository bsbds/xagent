"""add minimal actor OAuth flow nonce table

Revision ID: 20260821_actor_oauth_flow_states
Revises: 20260818_user_oauth_resource_owner
Create Date: 2026-08-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260821_actor_oauth_flow_states"
down_revision: Union[str, None] = "20260818_user_oauth_resource_owner"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "actor_oauth_flow_states"


def upgrade() -> None:
    """Create the nonce-only state consumed by trusted actor callbacks."""
    op.create_table(
        TABLE,
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("nonce", name="pk_actor_oauth_flow_states"),
    )


def downgrade() -> None:
    """Remove actor OAuth flow state after entry points have been disabled."""
    op.drop_table(TABLE)
