"""migrate_data_to_new_fks

Revision ID: f9586c10a561
Revises: c24abb5455d3
Create Date: 2024-04-24 19:34:00.000152

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "f9586c10a561"
down_revision = "c24abb5455d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    session = sa.orm.Session(bind=bind)

    for table in [
        "action_event",
        "window_event",
        "screenshot",
        "memory_stat",
        "performance_stat",
    ]:
        session.execute(
            f"UPDATE {table} SET recording_id = (SELECT id FROM recording WHERE"
            f" recording.timestamp = {table}.recording_timestamp)"
        )

    session.execute(
        "UPDATE action_event SET window_event_id = (SELECT id FROM window_event WHERE"
        " window_event.timestamp = action_event.window_event_timestamp)"
    )
    session.execute(
        "UPDATE action_event SET screenshot_id = (SELECT id FROM screenshot WHERE"
        " screenshot.timestamp = action_event.screenshot_timestamp)"
    )


def downgrade() -> None:
    pass
