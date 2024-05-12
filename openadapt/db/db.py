"""Implements functionality for connecting to and interacting with the database.

Module: db.py
"""

from typing import Any
import os
import time

from dictalchemy import DictableModel
from loguru import logger
from sqlalchemy import create_engine, event
from sqlalchemy.engine import reflection
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import MetaData
import sqlalchemy as sa

from openadapt.config import RECORDING_DIR_PATH, config

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class BaseModel(DictableModel):
    """The base model for database tables."""

    __abstract__ = True

    def __repr__(self) -> str:
        """Return a string representation of the model object."""
        # avoid circular import
        from openadapt.utils import EMPTY, row2dict

        params = ", ".join(
            f"{k}={v!r}"  # !r converts value to string using repr (adds quotes)
            for k, v in row2dict(self, follow=False).items()
            if v not in EMPTY
        )
        return f"{self.__class__.__name__}({params})"


def get_engine() -> sa.engine:
    """Create and return a database engine."""
    engine = sa.create_engine(
        config.DB_URL,
        connect_args={"check_same_thread": False},
        echo=config.DB_ECHO,
    )
    return engine


def get_base(engine: sa.engine) -> sa.engine:
    """Create and return the base model with the provided engine.

    Args:
        engine (sa.engine): The database engine to bind to the base model.

    Returns:
        sa.engine: The base model object.
    """
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    Base = declarative_base(
        cls=BaseModel,
        bind=engine,
        metadata=metadata,
    )
    return Base


engine = get_engine()
Base = get_base(engine)
Session = sessionmaker(bind=engine)


def copy_recording_data(
    source_engine: sa.engine,
    target_engine: sa.engine,
    recording_id: int,
    exclude_tables: tuple = (),
) -> str:
    """Copy a specific recording from the source database to the target database.

    Args:
        source_engine (create_engine): SQLAlchemy engine for the source database.
        target_engine (create_engine): SQLAlchemy engine for the target database.
        recording_id (int): The ID of the recording to copy.
        exclude_tables (tuple, optional): Tables excluded from copying. Defaults to ().

    Returns:
        str: The URL or path of the target database.
    """
    try:
        with source_engine.connect() as src_conn, target_engine.connect() as tgt_conn:
            src_metadata = MetaData()
            tgt_metadata = MetaData()

            @event.listens_for(src_metadata, "column_reflect")
            def genericize_datatypes(
                inspector: reflection.Inspector,
                tablename: str,
                column_dict: dict[str, Any],
            ) -> None:
                column_dict["type"] = column_dict["type"].as_generic(
                    allow_nulltype=True
                )

            tgt_metadata.reflect(bind=target_engine)
            src_metadata.reflect(bind=source_engine)

            # Drop all tables in target database (except excluded tables)
            for table in reversed(tgt_metadata.sorted_tables):
                if table.name not in exclude_tables:
                    logger.info("Dropping table =", table.name)
                    table.drop(bind=target_engine)

            tgt_metadata.clear()
            tgt_metadata.reflect(bind=target_engine)
            src_metadata.reflect(bind=source_engine)

            # Create all tables in target database (except excluded tables)
            for table in src_metadata.sorted_tables:
                if table.name not in exclude_tables:
                    table.create(bind=target_engine)

            # Refresh metadata before copying data
            tgt_metadata.clear()
            tgt_metadata.reflect(bind=target_engine)

            # Get the source recording table
            src_recording_table = src_metadata.tables["recording"]
            tgt_recording_table = tgt_metadata.tables["recording"]

            # Select the recording with the given recording_id from the source
            src_select = src_recording_table.select().where(
                src_recording_table.c.id == recording_id
            )
            src_recording = src_conn.execute(src_select).fetchone()

            # Insert the recording into the target recording table
            tgt_conn.execute(tgt_recording_table.insert().values(src_recording))

            # Get the timestamp from the source recording
            src_timestamp = src_recording["timestamp"]

            # Copy data from tables with the same timestamp
            for table in src_metadata.sorted_tables:
                if (
                    table.name not in exclude_tables
                    and "recording_timestamp" in table.columns.keys()
                ):
                    # Select data from source table with the same timestamp
                    src_select = table.select().where(
                        table.c.recording_timestamp == src_timestamp
                    )
                    src_rows = src_conn.execute(src_select).fetchall()

                    # Insert data into target table
                    tgt_table = tgt_metadata.tables[table.name]
                    for row in src_rows:
                        tgt_insert = tgt_table.insert().values(**row._asdict())
                        tgt_conn.execute(tgt_insert)

            # Copy data from alembic_version table
            src_alembic_version_table = src_metadata.tables["alembic_version"]
            tgt_alembic_version_table = tgt_metadata.tables["alembic_version"]
            src_alembic_version_select = src_alembic_version_table.select()
            src_alembic_version_data = src_conn.execute(
                src_alembic_version_select
            ).fetchall()
            for row in src_alembic_version_data:
                tgt_alembic_version_insert = tgt_alembic_version_table.insert().values(
                    row
                )
                tgt_conn.execute(tgt_alembic_version_insert)

            # Commit the transaction
            tgt_conn.commit()

    except Exception as exc:
        # Perform cleanup
        db_file_path = target_engine.url.database
        if db_file_path and os.path.exists(db_file_path):
            os.remove(db_file_path)
        logger.exception(exc)
        return ""

    return target_engine.url.database


def export_recording(recording_id: int) -> str:
    """Export a recording by its ID to a new SQLite database.

    Args:
        recording_id (int): The ID of the recording to export.

    Returns:
        str: The file path of the new database with timestamp.
    """
    timestamp = int(time.time())
    db_fname = f"recording_{recording_id}_{timestamp}.db"
    target_path = RECORDING_DIR_PATH / db_fname
    target_db_url = f"sqlite:///{target_path}"

    target_engine = create_engine(target_db_url, future=True)

    db_file_path = copy_recording_data(engine, target_engine, recording_id)
    return db_file_path
