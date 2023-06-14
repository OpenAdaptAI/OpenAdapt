import sqlite3
import tempfile
import os
import sys

from loguru import logger
from openadapt.signals import Signals


def test_setup_database_signal():
    signals = Signals()
    signals.add_signal("tests/resources/test_database.db")

    signal_description = signals.return_signals()[0]["description"]
    assert "Table: table, Schema: test_table" in signal_description


def test_access_database_signal():
    signals = Signals()
    signals.add_signal('tests/resources/test_database.db')  # assuming you've a SQLite database file named 'test.db' in your working directory

    # Assuming you have a table named 'test_table' in your database
    query = 'SELECT * FROM test_table'  

    signal_data = signals.return_signal_data(1, query=query)
    assert signal_data == [(1, 'Test data')]
    # Add more specific asserts based on what data you expect to retrieve from the database


def test_add_file_signal():
    signals = Signals()
    signals.add_signal("tests/resources/test_signal_data.txt")
    signal_data = signals.return_signal_data(1)
    assert signal_data == "test_data_success"


def test_add_url_signal():
    signals = Signals()
    signals.add_signal("https://www.accuweather.com/")
    signal_data = signals.return_signal_data(1)
    #TODO: change to a more specific url to see if gathered data is correct and usable
    assert signal_data != None


def test_add_function_signal():
    signals = Signals()
    sys.path.append("tests/resources")
    signals.add_signal("sample_package.sample_module.sample_function")
    signal = signals.return_signals()[0]
    signal_data = signals.return_signal_data(1)
    assert signal_data == "Sample function success"
    assert signal["description"] == "Function: sample_function, Module: sample_package.sample_module, Description: \n    This function is used to test the openadapt.signals module\n    "


def test_add_function_signal():
    signals = Signals()
    sys.path.append(".venv/lib/site-packages")
    signals.add_signal("openai.Completion.create")
    signal_data = signals.return_signal_data(1, engine="davinci", prompt="Translate the following English word to French: 'Hello'", max_tokens=10)
    signal_data = signal_data["choices"][0]["text"]
    logger.info(signal_data)
    assert signal_data != None


def test_setup_xslx_signal():
    signals = Signals()
    signals.add_signal("tests/resources/test_data.xlsx")
    signal_description = signals.return_signals()[0]["description"]
    assert "test_value" in signal_description


def test_access_xslx_signal():
    signals = Signals()
    signals.add_signal("tests/resources/test_data.xlsx")
    signal_data = signals.return_signal_data(1)
    assert "test_value" in signal_data


def test_non_existent_file():
    signals = Signals()
    signals.add_signal("tests/resources/test_signal_data_non_existent.txt")
    signal_data = signals.return_signal_data(1)
    assert signal_data == None


def test_access_private_members():
    signals = Signals()
    try:
        signals.__add_file_signal("tests/resources/test_signal_data.txt")
    except AttributeError:
        assert True


def test_remove_signal():
    signals = Signals()
    signals.add_signal("tests/resources/test_signal_data.txt")
    signals.add_signal("https://platform.openai.com/")
    signals.remove_signal(1)
    signals_list = signals.return_signals()
    assert len(signals_list) == 1
    assert signals_list[0]["number"] == 1


def test_add_signal_with_title():
    signals = Signals()
    signals.add_signal("tests/resources/test_signal_data.txt", "test title")
    signals_list = signals.return_signals()
    assert signals_list[0]["title"] == "test title"


def test_add_invalid_signal():
    signals = Signals()
    try:
        signals.add_signal(12345)
    except ValueError:
        assert True


def test_return_signal_data_unsupported_type():
    signals = Signals()
    try:
        signals.add_signal("unsupported_signal_type")
    except ValueError:
        assert True


