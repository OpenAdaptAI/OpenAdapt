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


def export_sql(recording_id: int) -> tuple:
    from openadapt.crud import get_recording_by_id  # to avoid circular import

    """Export the recording data as SQL statements.

    Args:
        recording_id (int): The ID of the recording.

    Returns:
        sql (str): The SQL statement to insert the recording into the output file.
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


def update_db_fname_in_env_file(db_fname: str) -> None:
    with open(config.ENV_FILE_PATH, "r") as env_file:
        env_file_lines = [
            f"DB_FNAME={db_fname}\n"
            if env_file_line.startswith("DB_FNAME")
            else env_file_line
            for env_file_line in env_file.readlines()
        ]

    with open(config.ENV_FILE_PATH, "w") as env_file:
        env_file.writelines(env_file_lines)


def create_db(recording_id: int, sql: str, values) -> tuple[float, str]:
    """Create a new database and import the recording data.

    Args:
        recording_id (int): The ID of the recording.
        sql (str): The SQL statements to import the recording.

    Returns:
        tuple: A tuple containing the timestamp and the file path of the new database.
    """
    db_file_path = None  # Initialize with a default value
    try:
        db_fname = f"recording_{recording_id}.db"
        original_db_fname = config.DB_FNAME  # Store the original value

        timestamp = time.time()
        source_file_path = config.ENV_FILE_PATH
        target_file_path = f"{config.ENV_FILE_PATH}-{timestamp}"
        logger.info(
            f"source_file_path={source_file_path}, target_file_path={target_file_path}"
        )
        shutil.copyfile(source_file_path, target_file_path)
        config.set_db_url(db_fname)
        update_db_fname_in_env_file(db_fname)
        db_file_path = config.DB_FPATH.resolve()

        engine = sa.create_engine(config.DB_URL)
        Session = sessionmaker(bind=engine)
        session = Session()
        os.system("alembic upgrade head")

        with engine.begin() as connection:
            connection.execute(sql, values)

        return timestamp, db_file_path

    except KeyboardInterrupt:
        # Perform cleanup if Ctrl+C is pressed during execution
        if db_file_path and os.path.exists(db_file_path):
            os.remove(db_file_path)
        update_db_fname_in_env_file(original_db_fname)
        raise

    except Exception as e:
        # Handle other exceptions
        logger.error(f"Error occurred in create_db: {e}")
        # Perform necessary cleanup or error handling actions
        raise


def restore_db(timestamp: float, original_db: str) -> None:
    """Restore the database to a previous state.

    Args:
        timestamp (float): The timestamp associated with the backup file.
        original_db (string): Original database name before overwriting.
    """
    backup_file = f"{config.ENV_FILE_PATH}-{timestamp}"
    shutil.copyfile(backup_file, config.ENV_FILE_PATH)
    config.set_db_url(original_db)
    engine = get_engine()


def export_recording(recording_id: int) -> str:
    """Export a recording by creating a new database, importing the recording, and then restoring the previous state.

    Args:
        recording_id (int): The ID of the recording to export.

    Returns:
        str: The file path of the new database.
    """
    try:
        original_db = os.environ["DB_FNAME"]
    except KeyError:
        logger.warning("Warning: The 'DB_FNAME' environment variable is not set in .env. Using default value: openadapt.db.")
        original_db = "openadapt.db"
    sql, values = export_sql(recording_id)
    timestamp, db_file_path = create_db(recording_id, sql, values)
    restore_db(timestamp, original_db)
    return db_file_path
