"""rename input_event to action_event

Revision ID: 20f9c2afb42c
Revises: 5139d7df38f6
Create Date: 2023-05-10 11:22:37.266810

"""
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20f9c2afb42c"
down_revision = "5139d7df38f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("input_event", "action_event")


def downgrade() -> None:
    op.rename_table("action_event", "input_event")
