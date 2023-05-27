"""ActionEvent.element_state

Revision ID: d63569e4fb90
Revises: ec337f277666
Create Date: 2023-05-16 21:43:00.120143

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd63569e4fb90'
down_revision = 'ec337f277666'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('action_event', schema=None) as batch_op:
        batch_op.add_column(sa.Column('element_state', sa.JSON(), nullable=True))

    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('action_event', schema=None) as batch_op:
        batch_op.drop_column('element_state')

    # ### end Alembic commands ###
