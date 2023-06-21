import sqlite3
import tempfile
import os
import sys
import subprocess

from loguru import logger
from openadapt.signals import Signals, initialize_default_signals, add_files_from_pid
from openadapt import config


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


def test_add_other_url_signal():
    signals = Signals()
    signals.add_signal("https://alerts.ttc.ca/api/alerts/list")
    signal_data = signals.return_signal_data(1)
    print(signal_data)
    assert signal_data != None


def test_add_function_signal():
    signals = Signals()
    sys.path.append("tests/resources")
    signals.add_signal("sample_package.sample_module.sample_function")
    signal = signals.return_signals()[0]
    signal_data = signals.return_signal_data(1)
    assert signal_data == "Sample function success"
    assert signal["description"] == "Module: sample_package.sample_module, Class: N/A, Function: sample_function, Doctstring: \n    This function is used to test the openadapt.signals module\n    "


if config.TEST_OPENAI == True:
    def test_add_openai_function_signal():
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
    assert "test_value_1" in signal_description and "test_value_2" in signal_description


def test_access_xslx_signal():
    signals = Signals()
    signals.add_signal("tests/resources/test_data.xlsx")
    signal_data = signals.return_signal_data(1)
    assert "test_value_1" in signal_data and "test_value_2" in signal_data


def test_intialize_default_signals():
    signals = initialize_default_signals()
    signal_list = signals.return_signals()
    assert signal_list[0]["address"] == (".venv/lib/site-packages","openai.Completion.create")
    assert signals

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
    TEST_TITLE = "test title"
    signals.add_signal("tests/resources/test_signal_data.txt", signal_title=TEST_TITLE)
    signals_list = signals.return_signals()
    assert signals_list[0]["title"] == TEST_TITLE


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


def test_add_signal_from_pid():
    signals = Signals()

    with open('test_file.txt', 'w') as f:
        f.write('Test')
    process = subprocess.Popen(['python', '-c', 
        'import time; f = open("test_file.txt", "r"); time.sleep(5)'])

    add_files_from_pid(signals,process.pid)
    process.wait()
    process.terminate()
    os.remove('test_file.txt')

    #signal_data = signals.return_signal_data(1)
    signal_data = signals.return_signals()
    assert "test_file" in str(signal_data)
