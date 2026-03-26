"""empty message

Revision ID: b8107f37e6f7
Revises: 62ee04b26702, 20260309_add_provider_oauth_tables
Create Date: 2026-03-25 19:04:18.229645

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8107f37e6f7'
down_revision: Union[str, None] = ('62ee04b26702', '20260309_add_provider_oauth_tables')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
