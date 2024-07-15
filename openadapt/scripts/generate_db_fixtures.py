from sqlalchemy import create_engine, inspect
from openadapt.db.db import Base
from openadapt.config import PARENT_DIR_PATH, RECORDING_DIR_PATH
import openadapt.db.crud as crud
from loguru import logger


def get_session():
    """
    Establishes a database connection and returns a session and engine.

    Returns:
        tuple: A tuple containing the SQLAlchemy session and engine.
    """
    db_url = RECORDING_DIR_PATH / "recording.db"
    logger.info(f"Database URL: {db_url}")
    engine = create_engine(f"sqlite:///{db_url}")
    Base.metadata.create_all(bind=engine)
    session = crud.get_new_session(read_only=True)
    logger.info("Database connection established.")
    return session, engine


def check_tables_exist(engine):
    """
    Checks if the expected tables exist in the database.

    Args:
        engine: SQLAlchemy engine object.

    Returns:
        list: A list of table names in the database.
    """
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    expected_tables = [
        "recording",
        "action_event",
        "screenshot",
        "window_event",
        "performance_stat",
        "memory_stat",
    ]
    for table_name in expected_tables:
        table_exists = table_name in tables
        logger.info(f"{table_name=} {table_exists=}")
    return tables


def fetch_data(session):
    """
    Fetches the most recent recordings and related data from the database.

    Args:
        session: SQLAlchemy session object.

    Returns:
        dict: A dictionary containing fetched data.
    """
    # get the most recent three recordings
    recordings = crud.get_recordings(session, max_rows=3)

    action_events = []
    screenshots = []
    window_events = []
    performance_stats = []
    memory_stats = []

    for recording in recordings:
        action_events.extend(crud.get_action_events(session, recording))
        screenshots.extend(crud.get_screenshots(session, recording))
        window_events.extend(crud.get_window_events(session, recording))
        performance_stats.extend(crud.get_perf_stats(session, recording))
        memory_stats.extend(crud.get_memory_stats(session, recording))

    data = {
        "recordings": recordings,
        "action_events": action_events,
        "screenshots": screenshots,
        "window_events": window_events,
        "performance_stats": performance_stats,
        "memory_stats": memory_stats,
    }

    # Debug prints to verify data fetching
    logger.info(f"Recordings: {len(data['recordings'])} found.")
    logger.info(f"Action Events: {len(data['action_events'])} found.")
    logger.info(f"Screenshots: {len(data['screenshots'])} found.")
    logger.info(f"Window Events: {len(data['window_events'])} found.")
    logger.info(f"Performance Stats: {len(data['performance_stats'])} found.")
    logger.info(f"Memory Stats: {len(data['memory_stats'])} found.")

    return data


def format_sql_insert(table_name, rows):
    """
    Formats SQL insert statements for a given table and rows.

    Args:
        table_name (str): The name of the table.
        rows (list): A list of SQLAlchemy ORM objects representing the rows.

    Returns:
        str: A string containing the SQL insert statements.
    """
    if not rows:
        return ""

    columns = rows[0].__table__.columns.keys()
    sql = f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES\n"
    values = []

    for row in rows:
        row_values = [getattr(row, col) for col in columns]
        row_values = [
            f"'{value}'" if isinstance(value, str) else str(value)
            for value in row_values
        ]
        values.append(f"({', '.join(row_values)})")

    sql += ",\n".join(values) + ";\n"
    return sql


def dump_to_fixtures(filepath):
    """
    Dumps the fetched data into an SQL file.

    Args:
        filepath (str): The path to the SQL file.
    """
    session, engine = get_session()
    check_tables_exist(engine)
    rows_by_table_name = fetch_data(session)

    for table_name, rows in rows_by_table_name.items():
        if not rows:
            logger.warning(f"No rows for {table_name=}")
            continue
        with open(filepath, "a", encoding="utf-8") as file:
            logger.info(f"Writing {len(rows)=} to {filepath=} for {table_name=}")
            file.write(f"-- Insert sample rows for {table_name}\n")
            file.write(format_sql_insert(table_name, rows))


if __name__ == "__main__":

    fixtures_path = PARENT_DIR_PATH / "tests/assets/fixtures.sql"

    dump_to_fixtures(fixtures_path)
