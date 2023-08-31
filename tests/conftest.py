"""This module contains fixtures and setup for testing."""

import os

from sqlalchemy import create_engine, engine, text
import pytest

from openadapt.config import ROOT_DIRPATH
from openadapt.db.db import Base


@pytest.fixture(scope="session")
def setup_database(request: pytest.FixtureRequest) -> engine:
    """Set up a database for testing."""
    # Create a new database or connect to an existing one
    db_url = ROOT_DIRPATH / "temp.db"
    engine = create_engine(f"sqlite:///{db_url}")

    # Create the database tables (if necessary)
    Base.metadata.create_all(bind=engine)

    # Read the SQL file and execute the statements to seed the database
    with open(ROOT_DIRPATH / "assets/fixtures.sql", "r") as file:
        statements = file.read()

    with engine.connect() as connection:
        connection.execute(text(statements))

    def teardown() -> None:
        """Teardown function to clean up resources after testing."""
        # Add code here to drop tables, clean up resources, etc.
        # This code will be executed after the tests complete (whether or not they pass)
        # Replace it with the appropriate cleanup operations for your project
        # Example: db.Base.metadata.drop_all(bind=engine)

        # Close the database connection (if necessary)
        engine.dispose()
        os.remove(db_url)

    # Register the teardown function to be called after the tests complete
    request.addfinalizer(teardown)

    # Return the database connection object or engine for the tests to use
    return engine
