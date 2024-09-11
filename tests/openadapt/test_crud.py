"""Tests for the CRUD operations in the openadapt.db.crud module."""


from unittest.mock import patch

import pytest
import sqlalchemy as sa

from openadapt.db import crud, db
from openadapt.models import Recording


def test_get_new_session_read_only(db_engine: sa.engine.Engine) -> None:
    """Test that get_new_session returns a read-only session when read_only=True.

    Args:
        db_engine (sa.engine.Engine): The test database engine.
    """
    # patch the ReadOnlySession class to return a mock object
    with patch(
        "openadapt.db.db.get_read_only_session_maker",
        return_value=db.get_read_only_session_maker(db_engine),
    ):
        session = crud.get_new_session(read_only=True)
        recording = Recording(
            timestamp=0,
            monitor_width=1920,
            monitor_height=1080,
            double_click_interval_seconds=0,
            double_click_distance_pixels=0,
            platform="Windows",
            task_description="Task description",
        )
        # with pytest.raises(PermissionError):
        #    session.add(recording)
        with pytest.raises(PermissionError):
            session.commit()
        with pytest.raises(PermissionError):
            session.flush()
        with pytest.raises(PermissionError):
            session.delete(recording)
