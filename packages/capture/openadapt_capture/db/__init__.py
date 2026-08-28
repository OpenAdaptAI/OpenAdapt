"""Package for interacting with the openadapt-capture database.

Copied from legacy OpenAdapt db/db.py, adapted for per-capture databases.
"""

import sqlite3
from pathlib import Path

import sqlalchemy as sa
from loguru import logger
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

# How long ONE statement waits for the write lock before it reports
# "database is locked".
#
# This is deliberately short. It is not the whole wait: crud spends a declared
# budget on many short attempts rather than a few long ones, and this value is
# the cost of one of those attempts. A long value spends the whole budget on a
# handful of samples of a lock that is busy most of the time, which is how a
# writer used to reach its last retry ~22 seconds after its first.
SQLITE_BUSY_TIMEOUT_SECONDS = 0.5

# Journal mode for a live per-capture database.
#
# Every recorder writer process commits to the one capture file, and under the
# default rollback journal each commit creates, syncs and deletes a journal
# file beside it. On Windows that file churn is scanned by the filesystem
# filter driver, one screenshot row costs about half a second to commit, and a
# writer draining a backlog holds the single write lock at essentially full
# duty cycle. Other writers then starve for tens of seconds.
#
# A write-ahead log appends instead, so a commit is cheap, readers never block
# a writer, and the lock is free far more often. The mode is recorded in the
# database header, so every later connection to the same file inherits it
# without setting it. A capture is returned to the rollback journal when it is
# finalized (see finalize_capture_database), so a sealed capture keeps the
# on-disk shape it has always had and carries no sidecar files.
SQLITE_CAPTURE_JOURNAL_MODE = "WAL"


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
        connect_args={
            "check_same_thread": False,
            "timeout": SQLITE_BUSY_TIMEOUT_SECONDS,
        },
        echo=echo,
    )

    @sa.event.listens_for(engine, "connect")
    def _match_synchronous_to_the_journal_mode(dbapi_connection, _record) -> None:
        """Relax the sync only for a database that already keeps a write log.

        A write-ahead log makes NORMAL safe against a process crash: only a
        power loss can cost the last commits, and a capture interrupted by a
        power loss is incomplete anyway. A rollback-journal database keeps the
        default FULL, so an existing capture's durability is unchanged.
        """
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode")
            row = cursor.fetchone()
            if row and str(row[0]).lower() == "wal":
                cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

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


def create_db(db_path: str, echo: bool = False, journal_mode: str | None = None) -> tuple:
    """Create a new database at the given path, returning (engine, Session).

    Creates all tables defined in the models.

    Args:
        db_path: Path to the SQLite database file.
        echo: Whether to echo SQL statements.
        journal_mode: Journal mode to record in the new database header. Pass
            ``SQLITE_CAPTURE_JOURNAL_MODE`` for a database several writer
            processes will share; leave it unset for a database with one
            writer, whose exact bytes are then unchanged by this argument.

    Returns:
        tuple of (engine, Session class).
    """
    db_url = f"sqlite:///{db_path}"
    engine = get_engine(db_url, echo=echo)

    # Import models to ensure they are registered with Base
    from openadapt_capture.db import models  # noqa: F401

    if journal_mode is not None:
        with engine.connect() as connection:
            selected = connection.exec_driver_sql(
                f"PRAGMA journal_mode={journal_mode}"
            ).scalar()
        # A journal mode is a request, not a guarantee: a database on a
        # filesystem without shared memory keeps the rollback journal. The
        # capture still works there, only with the contention this mode is here
        # to remove, so say so once rather than failing a recording over it.
        if str(selected).lower() != journal_mode.lower():
            logger.warning(
                f"{db_path} kept journal mode {selected!r} rather than "
                f"{journal_mode!r}; expect slower commits and more "
                "writer-lock contention"
            )
        engine.dispose()

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


def close_capture_session(session) -> None:
    """Close a session and release the connection its engine pooled.

    ``get_session_for_path`` builds an engine per call, so closing the session
    alone returns its connection to that engine's pool and leaves the file
    open. An open connection stops a capture being finalized, and on Windows it
    also makes the capture directory undeletable.

    Args:
        session: A session from ``get_session_for_path``.
    """
    bind = session.get_bind()
    session.close()
    bind.dispose()


def finalize_capture_database(db_path: str) -> None:
    """Fold the write log back into the capture file and drop the sidecars.

    A live capture uses a write-ahead log, which keeps ``recording.db-wal`` and
    ``recording.db-shm`` beside the database. A finalized capture must not: the
    seal inventories every regular file under the capture directory, and the
    shared-memory file is created and removed by whoever opens the database
    next, so a sealed capture that listed one would fail its own validation.

    Call this once, after every writer process has exited and before the
    capture is verified and sealed. It fails loud rather than sealing a capture
    whose sidecars are still there.

    Args:
        db_path: Path to the per-capture SQLite database file.

    Raises:
        RuntimeError: The write log did not fold back into the database.
    """
    database = sqlite3.connect(db_path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
    try:
        database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        database.execute("PRAGMA journal_mode=DELETE")
        database.commit()
    finally:
        database.close()

    surviving = [
        suffix
        for suffix in ("-wal", "-shm")
        if Path(f"{db_path}{suffix}").exists()
    ]
    if surviving:
        raise RuntimeError(
            "The finalized Capture database still has a write log: "
            f"{', '.join(surviving)}. A writer is still holding it open."
        )


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
