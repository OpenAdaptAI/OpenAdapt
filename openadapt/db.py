import os

from dictalchemy import DictableModel
from loguru import logger
from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import MetaData
import sqlalchemy as sa

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
        src_metadata = MetaData()
        tgt_metadata = MetaData()

        @event.listens_for(src_metadata, "column_reflect")
        def genericize_datatypes(inspector, tablename, column_dict):
            column_dict["type"] = column_dict["type"].as_generic(allow_nulltype=True)

        src_conn = source_engine.connect()
        tgt_conn = target_engine.connect()
        tgt_metadata.reflect(bind=target_engine)
        src_metadata.reflect(bind=source_engine)

        # Drop all tables in target database
        for table in reversed(tgt_metadata.sorted_tables):
            if table.name not in exclude_tables:
                print("Dropping table =", table.name)
                table.drop(bind=target_engine)

        tgt_metadata.clear()
        tgt_metadata.reflect(bind=target_engine)
        src_metadata.reflect(bind=source_engine)

        # create all tables in target database
        for table in src_metadata.sorted_tables:
            if table.name not in exclude_tables:
                table.create(bind=target_engine)

        # refresh metadata before you can copy data
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
        tgt_insert = tgt_recording_table.insert().values(src_recording)
        tgt_conn.execute(tgt_insert)

        tgt_conn.commit()
        src_conn.close()
        tgt_conn.close()

    except Exception as exc:
        # Perform cleanup
        tgt_conn.close()
        db_file_path = target_engine.url.database
        if db_file_path and os.path.exists(db_file_path):
            os.remove(db_file_path)
        logger.exception(exc)
        raise

    return target_engine.url.database


def export_recording(recording_id: int) -> str:
    """Export a recording by creating a new database, importing the recording, and then restoring the previous state.

    Args:
        recording_id (int): The ID of the recording to export.

    Returns:
        str: The file path of the new database.
    """
    db_fname = f"recording_{recording_id}.db"
    target_path = config.ROOT_DIRPATH / db_fname
    target_db_url = f"sqlite:///{target_path}"

    target_engine = create_engine(target_db_url, future=True)

    db_file_path = copy_recording_data(engine, target_engine, recording_id)
    return db_file_path
