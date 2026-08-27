"""Package for interacting with the openadapt-capture database.

Copied from legacy OpenAdapt db/db.py, adapted for per-capture databases.
"""

import sqlite3
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import MetaData

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class BaseModel:
    """The base model for database tables."""

    __abstract__ = True

    def __repr__(self) -> str:
        """Return a string representation of the model object."""
        params = ", ".join(
            f"{k}={v!r}"
            for k, v in {
                c.name: getattr(self, c.name)
                for c in self.__table__.columns
            }.items()
            if v is not None
        )
        return f"{self.__class__.__name__}({params})"


def get_base() -> sa.engine:
    """Create and return the base model.

    Returns:
        The base model object.
    """
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    Base = declarative_base(
        cls=BaseModel,
        metadata=metadata,
    )
    return Base


Base = get_base()


def get_engine(db_url: str, echo: bool = False) -> sa.engine:
    """Create and return a database engine.

    Args:
        db_url: SQLAlchemy database URL (e.g. sqlite:///path/to/db).
        echo: Whether to echo SQL statements.
    """
    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False},
        echo=echo,
    )
    return engine


def get_session_maker(engine: sa.engine) -> sessionmaker:
    """Create a session maker bound to the given engine."""
    return sessionmaker(bind=engine)


def migrate_missing_columns(engine: sa.engine) -> None:
    """Add columns present in the models but missing from an existing DB.

    Per-capture SQLite databases have no migration framework: each capture
    gets a fresh schema via ``create_all``. When the models gain a new
    column, older ``recording.db`` files predate it, so loading them would
    fail with ``no such column``. SQLite supports cheap
    ``ALTER TABLE ... ADD COLUMN``, so we reconcile missing columns here.

    Strictly additive: it never drops or alters existing columns, and adds
    new columns without a DEFAULT so pre-existing rows read back as NULL
    (letting callers fall back to legacy sources like the config JSON).

    Args:
        engine: Engine bound to the per-capture SQLite database.
    """
    # Import models to ensure the tables are registered with Base.
    from openadapt_capture.db import models  # noqa: F401

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                # create_all handles brand-new tables.
                continue
            existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing_cols:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                conn.execute(
                    text(
                        f'ALTER TABLE "{table.name}" '
                        f'ADD COLUMN "{column.name}" {col_type}'
                    )
                )


def create_db(db_path: str, echo: bool = False) -> tuple:
    """Create a new database at the given path, returning (engine, Session).

    Creates all tables defined in the models.

    Args:
        db_path: Path to the SQLite database file.
        echo: Whether to echo SQL statements.

    Returns:
        tuple of (engine, Session class).
    """
    db_url = f"sqlite:///{db_path}"
    engine = get_engine(db_url, echo=echo)

    # Import models to ensure they are registered with Base
    from openadapt_capture.db import models  # noqa: F401

    Base.metadata.create_all(engine)
    # Reconcile schemas of pre-existing DBs that predate newer columns.
    migrate_missing_columns(engine)
    Session = get_session_maker(engine)
    return engine, Session


def get_session_for_path(db_path: str, echo: bool = False):
    """Create and return a new session for the given database path.

    This is used by worker processes to get their own session to the
    per-capture database.

    Args:
        db_path: Path to the SQLite database file.
        echo: Whether to echo SQL statements.

    Returns:
        A SQLAlchemy Session instance.
    """
    db_url = f"sqlite:///{db_path}"
    engine = get_engine(db_url, echo=echo)
    try:
        # Older recording.db files may predate columns the models now expect;
        # add any missing ones so loading them does not fail with 'no such
        # column'. Safe/no-op when the schema is already current.
        migrate_missing_columns(engine)
        Session = get_session_maker(engine)
        return Session()
    except Exception:
        # A corrupt/unreadable db raises above; dispose the engine so its
        # pooled connection does not keep the file handle open (on Windows
        # a lingering handle makes the capture directory undeletable).
        engine.dispose()
        raise


def get_immutable_session_for_path(db_path: str, echo: bool = False):
    """Open an already-verified SQLite snapshot without schema migration."""

    immutable_uri = f"{Path(db_path).resolve().as_uri()}?mode=ro&immutable=1"

    def _connect() -> sqlite3.Connection:
        return sqlite3.connect(
            immutable_uri,
            uri=True,
            check_same_thread=False,
        )

    engine = create_engine("sqlite://", creator=_connect, echo=echo)
    Session = get_session_maker(engine)
    try:
        return Session()
    except Exception:
        engine.dispose()
        raise
