"""create_new_recording_fks

Revision ID: c24abb5455d3
Revises: 30a5ba9d6453
Create Date: 2024-04-24 19:32:59.011079

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "c24abb5455d3"
down_revision = "8495f5471e23"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table("action_event", schema=None) as batch_op:
        batch_op.add_column(sa.Column("recording_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("screenshot_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("window_event_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_action_event_recording_id_recording"),
            "recording",
            ["recording_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_action_event_screenshot_id_screenshot"),
            "screenshot",
            ["screenshot_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_action_event_window_event_id_window_event"),
            "window_event",
            ["window_event_id"],
            ["id"],
        )

    with op.batch_alter_table("performance_stat", schema=None) as batch_op:
        batch_op.add_column(sa.Column("recording_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_performance_stat_recording_id_recording"),
            "recording",
            ["recording_id"],
            ["id"],
        )

    with op.batch_alter_table("memory_stat", schema=None) as batch_op:
        batch_op.add_column(sa.Column("recording_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_memory_stat_recording_id_recording"),
            "recording",
            ["recording_id"],
            ["id"],
        )

    with op.batch_alter_table("screenshot", schema=None) as batch_op:
        batch_op.add_column(sa.Column("recording_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_screenshot_recording_id_recording"),
            "recording",
            ["recording_id"],
            ["id"],
        )

    with op.batch_alter_table("window_event", schema=None) as batch_op:
        batch_op.add_column(sa.Column("recording_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_window_event_recording_id_recording"),
            "recording",
            ["recording_id"],
            ["id"],
        )

    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table("window_event", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_window_event_recording_id_recording"), type_="foreignkey"
        )
        batch_op.drop_column("recording_id")

    with op.batch_alter_table("screenshot", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_screenshot_recording_id_recording"), type_="foreignkey"
        )
        batch_op.drop_column("recording_id")

    with op.batch_alter_table("memory_stat", schema=None) as batch_op:
        batch_op.drop_column("recording_id")

    with op.batch_alter_table("performance_stat", schema=None) as batch_op:
        batch_op.drop_column("recording_id")

    with op.batch_alter_table("action_event", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_action_event_window_event_id_window_event"),
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            batch_op.f("fk_action_event_screenshot_id_screenshot"), type_="foreignkey"
        )
        batch_op.drop_constraint(
            batch_op.f("fk_action_event_recording_id_recording"), type_="foreignkey"
        )
        batch_op.drop_column("window_event_id")
        batch_op.drop_column("screenshot_id")
        batch_op.drop_column("recording_id")

    # ### end Alembic commands ###
