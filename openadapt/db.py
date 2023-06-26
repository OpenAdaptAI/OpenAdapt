import os
import time
import shutil

import sqlalchemy as sa

from loguru import logger
from dictalchemy import DictableModel
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import MetaData
from sqlalchemy.ext.declarative import declarative_base

from openadapt import config, utils


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class BaseModel(DictableModel):

    __abstract__ = True

    def __repr__(self):
        params = ", ".join(
            f"{k}={v!r}"  # !r converts value to string using repr (adds quotes)
            for k, v in utils.row2dict(self, follow=False).items()
            if v not in utils.EMPTY
        )
        return f"{self.__class__.__name__}({params})"


def get_engine():
    engine = sa.create_engine(
        config.DB_URL,
        echo=config.DB_ECHO,
    )
    return engine


def get_base(engine):
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

def export_sql(recording_id):
    from openadapt.crud import get_recording_by_id  # to avoid circular import
    """Export the recording data as SQL statements.

    Args:
        recording_id (int): The ID of the recording.

    Returns:
        str: The SQL statement to insert the recording into the output file.
    """
    engine = sa.create_engine(config.DB_URL)
    Session = sessionmaker(bind=engine)
    session = Session()

    recording = get_recording_by_id(recording_id)

    if recording:
        sql = """
            INSERT INTO recording
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        values = (
            recording.id,
            recording.timestamp,
            recording.monitor_width,
            recording.monitor_height,
            recording.double_click_interval_seconds,
            recording.double_click_distance_pixels,
            recording.platform,
            recording.task_description,
        )

        logger.info(f"Recording with ID {recording_id} exported successfully.")
    else:
        sql = ""
        logger.info(f"No recording found with ID {recording_id}.")

    return sql, values


def create_db(recording_id, sql, values):
    """Create a new database and import the recording data.

    Args:
        recording_id (int): The ID of the recording.
        sql (str): The SQL statements to import the recording.

    Returns:
        tuple: A tuple containing the timestamp and the file path of the new database.
    """
    # engine.close()
    db_fname = f"recording_{recording_id}.db"

    timestamp = time.time()
    source_file_path = config.ENV_FILE_PATH
    target_file_path = f"{config.ENV_FILE_PATH}-{timestamp}"
    logger.info(
        f"source_file_path={source_file_path}, target_file_path={target_file_path}"
    )
    shutil.copyfile(source_file_path, target_file_path)
    config.set_db_url(db_fname)

    with open(config.ENV_FILE_PATH, "r") as env_file:
        env_file_lines = [
            f"DB_FNAME={db_fname}\n"
            if env_file_line.startswith("DB_FNAME")
            else env_file_line
            for env_file_line in env_file.readlines()
        ]

    with open(config.ENV_FILE_PATH, "w") as env_file:
        env_file.writelines(env_file_lines)

    engine = sa.create_engine(config.DB_URL)
    Session = sessionmaker(bind=engine)
    session = Session()
    os.system("alembic upgrade head")
    # db.engine = engine

    with engine.begin() as connection:
        connection.execute(sql, values)

    db_file_path = config.DB_FPATH.resolve()

    return timestamp, db_file_path


def restore_db(timestamp):
    """Restore the database to a previous state.

    Args:
        timestamp (float): The timestamp associated with the backup file.
    """
    backup_file = f"{config.ENV_FILE_PATH}-{timestamp}"
    shutil.copyfile(backup_file, config.ENV_FILE_PATH)
    config.set_db_url("openadapt.db")
    engine = get_engine()


def export_recording(recording_id):
    """Export a recording by creating a new database, importing the recording, and then restoring the previous state.

    Args:
        recording_id (int): The ID of the recording to export.

    Returns:
        str: The file path of the new database.
    """
    sql, values = export_sql(recording_id)
    timestamp, db_file_path = create_db(recording_id, sql, values)
    restore_db(timestamp)
    return db_file_path
