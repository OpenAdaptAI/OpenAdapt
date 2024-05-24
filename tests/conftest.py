"""This module contains fixtures and setup for testing."""

from pathlib import Path
import os

from PIL import Image
from sqlalchemy import create_engine, engine, text
import pytest

from openadapt.config import DATA_DIR_PATH, RECORDING_DIR_PATH, ROOT_DIR_PATH
from openadapt.db.db import Base


@pytest.fixture(scope="session")
def setup_database(request: pytest.FixtureRequest) -> engine:
    """Set up a database for testing."""
    # Create a new database or connect to an existing one
    db_url = RECORDING_DIR_PATH / "recording.db"
    engine = create_engine(f"sqlite:///{db_url}")

    # Create the database tables (if necessary)
    Base.metadata.create_all(bind=engine)

    # Read the SQL file and split the content into individual statements
    with open(DATA_DIR_PATH / "assets/fixtures.sql", "r") as file:
        statements = file.read().split(";")

    # Remove any empty statements from the list
    statements = [stmt.strip() for stmt in statements if stmt.strip()]

    # Execute each statement one at a time
    with engine.connect() as connection:
        for statement in statements:
            connection.execute(text(statement))

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


@pytest.fixture(scope="session")
def db_engine(request: pytest.FixtureRequest) -> engine:
    """Return the database engine for testing."""
    db_url = DATA_DIR_PATH / "test.db"
    engine = create_engine(f"sqlite:///{db_url}")

    # Create the database tables (if necessary)
    Base.metadata.create_all(bind=engine)

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


def load_image(filename: str) -> Image.Image:
    """Load an image from a path"""
    image_file_path = DATA_DIR_PATH / "tests" / "assets" / filename
    image = Image.open(image_file_path)
    return image


@pytest.fixture
def calculator_image() -> Image.Image:
    """Get an image of a calculator screenshot"""
    return load_image("calculator.png")


@pytest.fixture
def excel_image() -> Image.Image:
    """Get an image of an Excel screenshot"""
    return load_image("excel.png")
