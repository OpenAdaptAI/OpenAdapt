"""Package for interacting with the openadapt-capture database.

Copied from legacy OpenAdapt db/db.py, adapted for per-capture databases.
"""

import sqlalchemy as sa
from sqlalchemy import create_engine
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
    Session = get_session_maker(engine)
    return Session()
