import os
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from openadapt.db.db import Base
from openadapt.config import DATA_DIR_PATH, PARENT_DIR_PATH, RECORDING_DIR_PATH
import openadapt.db.crud as crud
from loguru import logger


def get_session():
    db_url = RECORDING_DIR_PATH / "recording.db"
    print(f"Database URL: {db_url}")
    engine = create_engine(f"sqlite:///{db_url}")
    # SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = crud.get_new_session(read_only=True)
    print("Database connection established.")
    return session, engine


def check_tables_exist(engine):
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
    # get the most recent three recordings
    recordings = crud.get_recordings(session, max_rows=3)
    recording_ids = [recording.id for recording in recordings]

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
    print(f"Recordings: {len(data['recordings'])} found.")
    print(f"Action Events: {len(data['action_events'])} found.")
    print(f"Screenshots: {len(data['screenshots'])} found.")
    print(f"Window Events: {len(data['window_events'])} found.")
    print(f"Performance Stats: {len(data['performance_stats'])} found.")
    print(f"Memory Stats: {len(data['memory_stats'])} found.")

    return data


def format_sql_insert(table_name, rows):
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
    session, engine = get_session()
    check_tables_exist(engine)
    data = fetch_data(session)

    with open(filepath, "a", encoding="utf-8") as file:
        if data["recordings"]:
            file.write("-- Insert sample recordings\n")
            file.write(format_sql_insert("recording", data["recordings"]))

        if data["action_events"]:
            file.write("-- Insert sample action_events\n")
            file.write(format_sql_insert("action_event", data["action_events"]))

        if data["screenshots"]:
            file.write("-- Insert sample screenshots\n")
            file.write(format_sql_insert("screenshot", data["screenshots"]))

        if data["window_events"]:
            file.write("-- Insert sample window_events\n")
            file.write(format_sql_insert("window_event", data["window_events"]))

        if data["performance_stats"]:
            file.write("-- Insert sample performance_stats\n")
            file.write(format_sql_insert("performance_stat", data["performance_stats"]))

        if data["memory_stats"]:
            file.write("-- Insert sample memory_stats\n")
            file.write(format_sql_insert("memory_stat", data["memory_stats"]))
            print(f"Data appended to {filepath}")


if __name__ == "__main__":

    fixtures_path = PARENT_DIR_PATH / "tests/assets/fixtures.sql"

    dump_to_fixtures(fixtures_path)
