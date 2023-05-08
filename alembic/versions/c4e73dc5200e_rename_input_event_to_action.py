"""rename input_event to action

Revision ID: c4e73dc5200e
Revises: 5139d7df38f6
Create Date: 2023-05-07 22:28:25.667936

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4e73dc5200e'
down_revision = '5139d7df38f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table('input_event', 'action')


def downgrade() -> None:
    op.rename_table('action', 'input_event')
