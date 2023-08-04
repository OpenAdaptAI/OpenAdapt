import importlib
import mimetypes
import os
import sqlite3
import sys

from loguru import logger
import openpyxl
import pandas as pd
import psutil
import requests

from openadapt import config


class Signals:
    """
    Create Signal structure as dictionary
    signals = [{},{},{}]

    {
        "id": signal id in list,
        "title": given signal title,
        "type": type of signal (file, database, etc.)
        "address": given signal address,
        "description": description of signal (file type, length, etc.),
    }
    """

    def __init__(self):
        self.signals = []

    def return_signals(self) -> list[dict]:
        """Return the list of signals."""
        return self.signals

    def __setup_database_signal(self, db_url) -> str:
        """Read a description of a signal from a database.

        Args:
            db_url (str): The path to the database file.

        Returns:
            str: A description of the signal.
        """

        # Get the signal from the database.
        conn = sqlite3.connect(db_url)
        cur = conn.cursor()
        cur.execute("SELECT * FROM sqlite_master WHERE type='table';")

        rows = cur.fetchall()
        conn.close()

        result = {}

        for row in rows:
            result[row[0]] = row[1]

        # Prepare the result
        table_info = []

        for row in rows:
            # The first element is the table name, the second element is the SQL schema
            table_info.append({"Table": row[0], "Schema": row[1]})

        description = table_info
        return description

    def __setup_file_signal(self, file_path) -> str:
        """Read a description of a signal from a file.

        Args:
            file_path (str): The path to the file.

        Returns:
            str: A description of the signal.
        """
        # Get the signal from the file.
        if not os.path.isfile(file_path):
            logger.info(f"Error: File not found.")
            return None

        try:
            # Get the file's size and type.
            size = os.path.getsize(file_path)
            type, _ = mimetypes.guess_type(file_path)

            description = (
                f"Size: {size if size else 'File size not provided'}, "
                f"Type: {type if type else 'File type not provided'}"
            )

            if file_path.endswith((".xls", ".xlsx")):
                data_frame = pd.read_excel(file_path)
                data_frame_description = data_frame.describe().to_string()
                description += f", Data_Frame_Description: {data_frame_description}"

        except PermissionError:
            logger.info(f"Error: Permission denied.")
            return None
        except IOError:
            logger.info(f"Error: I/O error.")
            return None
        except pd.errors.ParserError:
            logger.info(
                f"Error: Pandas could not parse the excel file, possible formatting"
                f" issue."
            )
        return description

    def __setup_url_signal(self, http_url) -> str:
        """Read a description of a signal from an HTTP URL.

        Args:
            http_url (str): The URL to the signal.

        Returns:
            str: A description of the signal.
        """
        # Get the signal from the URL.
        HEADERS = {
            "User-Agent": (
                "Mozilla/5.0 (iPad; CPU OS 12_2 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Mobile/15E148"
            )
        }
        response = requests.get(http_url, headers=HEADERS, allow_redirects=True)
        if response.status_code != 200:
            logger.info(f"Error: HTTP request failed.")
            raise ValueError(
                f"HTTP request failed with status code {response.status_code}."
            )
        else:
            logger.info(f"Success: HTTP request succeeded.")

            # Get the signal's length and type.
            length = response.headers.get("Content-Length")
            type = response.headers.get("Content-Type")

            description = {
                "Length": length if length else "Content-Length not provided",
                "Type": type if type else "Content-Type not provided",
            }
        return description

    def __setup_function_signal(self, function_address, function_path=None) -> str:
        """Read a description of a signal from a Python function.

        Args:
            function_address (str): The address of the function within the package.
            function_path (str): The path to the package.

        Returns:
            str: A description of the signal.
        """
        if function_path:
            sys.path.append(function_path)
        parts = function_address.rsplit(".", 2)

        try:
            module_name, class_name, func_name = parts
            module = importlib.import_module(module_name)
            func_class = getattr(module, class_name)
            func = getattr(func_class, func_name)
        except AttributeError:  # We don't have a class
            module_name, func_name = function_address.rsplit(".", 1)
            module = importlib.import_module(module_name)
            func = getattr(module, func_name)
            class_name = "N/A"

        # Get the function's docstring, or 'No description provided' if it doesn't have one
        docstring = func.__doc__ if func.__doc__ else "No docstring provided"

        # Describe the function
        description = {
            "Module": module_name,
            "Class": class_name,
            "Function": func_name,
            "Docstring": docstring,
        }

        return description

    def __access_function_signal(self, function_address, **kwargs) -> any:
        """Read signal data from a Python function.

        Args:
            function_address (str): The address of the function within the package.

        Returns:
            any: The data returned by the function.
        """
        if isinstance(function_address, tuple):
            assert len(function_address) == 2, function_address
            function_path, function_address = function_address

            sys.path.append(function_path)

        module_name, class_name, func_name = function_address.rsplit(".", 2)
        module = importlib.import_module(module_name)
        func_class = getattr(module, class_name)
        func = getattr(func_class, func_name)

        # Call the function and get its result
        result = func(**kwargs)

        return result

    def __access_database_signal(self, db_url, **kwargs) -> any:
        """Read signal data from a database.

        Args:
            db_url (str): The path to the database file.

        Returns:
            any: The data returned by the database query.
        """
        # Get the signal from the database.
        query = kwargs.get("query")

        try:
            conn = sqlite3.connect(db_url)
            cur = conn.cursor()
            cur.execute(query)
            data = cur.fetchall()
        except sqlite3.OperationalError:
            logger.info(f"Error: Database query failed.")
            return None
        finally:
            if conn:
                conn.close()
        return data

    def __access_file_signal(self, file_path) -> str:
        """Read signal data from a file.

        Args:
            file_path (str): The path to the file.

        Returns:
            str: The data read from the file.
        """
        # Get the signal from the file.
        pandas_engines = [None, "xlrd", "openpyxl", "odf", "pyxlsb"]
        # When engine=None, pandas will try to automatically infer the engine
        try:
            if file_path.endswith((".xls", ".xlsx")):
                content = None
                for engine in pandas_engines:
                    try:
                        data_frame = pd.read_excel(file_path, engine=engine)
                        content = data_frame.to_string()
                        break
                    except Exception as e:
                        logger.info(f"Error Message from pandas read: {engine}, {e}")
                        continue
                if content is None:
                    logger.info(f"Error: Pandas could not read the excel file.")
                    return None
            else:
                with open(file_path, "r") as file:
                    content = file.read()
        except FileNotFoundError:
            logger.info(f"Error: File not found.")
            return None
        except PermissionError:
            logger.info(f"Error: Permission denied.")
            return None
        except IOError:
            logger.info(f"Error: I/O error.")
            return None
        return content

    def __access_url_signal(self, http_url) -> str:
        """Read signal data from an HTTP URL.

        Args:
            http_url (str): The URL to the signal.

        Returns:
            str: The data read from the URL.
        """
        # Get the signal from the URL.
        HEADERS = {
            "User-Agent": (
                "Mozilla/5.0 (iPad; CPU OS 12_2 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Mobile/15E148"
            )
        }
        response = requests.get(http_url, headers=HEADERS, allow_redirects=True)
        if response.status_code != 200:
            logger.info(f"Error: HTTP request failed.")
        else:
            logger.info(f"Success: HTTP request succeeded.")
            signal_data = response.content
        return signal_data

    def add_signal(
        self, signal_address, signal_path=None, signal_title=None, signal_type=None
    ) -> None:
        """Add a signal to the list.

        Args:
            signal_address (str): The address of the signal.
            signal_path (str): The path to the signal (only used when dealing with function signals where a path and address are needed).
            signal_title (str): The title assigned to the signal.
            signal_type (str): The type of the signal (optional but helps when determining how to process the signal).

        Returns:
            None
        """
        signal_description = None
        if isinstance(signal_address, str):
            # If signal is a string, it could be a file path, a database URL, an HTTP URL, or a Python function name.
            if (
                signal_address.startswith("pgsql://")
                or signal_address.endswith(".db")
                or signal_type == "database"
            ):
                # If the string starts with "pgsql://", treat it as a database URL.
                signal_description = self.__setup_database_signal(signal_address)
                signal_type = "database"
            elif (
                signal_address.startswith("http://")
                or signal_address.startswith("https://")
                or signal_type == "web_url"
            ):
                # If the string starts with "http://" or "https://", treat it as an HTTP URL.
                signal_description = self.__setup_url_signal(signal_address)
                signal_type = "web_url"
            elif (
                signal_address.endswith((".json", ".csv", ".txt", ".xlsx", ".xls"))
                or signal_type == "file"
            ):
                # If the string ends with a known file extension, treat it as a file path.
                signal_description = self.__setup_file_signal(signal_address)
                signal_type = "file"
            elif signal_address.count(".") >= 1 or signal_type == "function":
                # Otherwise, treat it as a Python function name.
                signal_description = self.__setup_function_signal(
                    signal_address, function_path=signal_path
                )
                signal_type = "function"
            else:
                raise ValueError("Invalid signal address.")
        else:
            # If signal is not a string, raise an error.
            raise ValueError("Signal must be a string.")

        signal_id = len(self.signals) + 1

        if signal_type == "function" and signal_path != None:
            signal_address = (signal_path, signal_address)

        signal = {
            "id": signal_id,
            "title": signal_title,
            "type": signal_type,
            "address": signal_address,
            "description": signal_description,
        }
        self.signals.append(signal)

    def remove_signal(self, signal_id) -> None:
        """Remove a signal from the list.

        Args:
            signal_id (int): The id of the signal to remove.

        Returns:
            None
        """
        self.signals.pop(signal_id - 1)
        for i in range(signal_id - 1, len(self.signals)):
            # Decrement the signal ids of all signals after the removed signal.
            self.signals[i]["id"] -= 1

    def return_signal_data(self, signal_id, **kwargs) -> any:
        """Return the data of a signal.

        Args:
            signal_id (int): The id of the signal to return data for.

        Returns:
            any: The data of the signal.
        """
        signal = self.signals[signal_id - 1]
        if len(signal) == 0:
            return None
        elif signal["type"] == "database":
            return self.__access_database_signal(signal["address"], **kwargs)
        elif signal["type"] == "web_url":
            return self.__access_url_signal(signal["address"])
        elif signal["type"] == "file":
            return self.__access_file_signal(signal["address"])
        elif signal["type"] == "function":
            return self.__access_function_signal(signal["address"], **kwargs)
        else:
            return None


def initialize_default_signals() -> Signals:
    """Initialize the default signals."""
    signals = Signals()
    for signal_address in config.DEFAULT_SIGNALS:
        if isinstance(signal_address, tuple):
            signal_path = signal_address[0]
            signal_address = signal_address[1]
            sys.path.append(signal_path)

        signals.add_signal(signal_address, signal_path=signal_path)
    return signals


# class Signal:
#     def __init__(self, id, address, description, type, title="None"):
#         self.id = id
#         self.address = address
#         self.title = title
#         self.description = description
#         self.type = type


# class DBTableSignal(Signal):
#     def __init__(self, id, address, description, type, title="None"):
#         super().__init__(id, address, title, description, type)


def add_files_from_pid(current_signals: Signals, pid: int) -> None:
    """Add all open files from a process to the current signals.

    Args:
        current_signals (Signals): The current signals to add to.
        pid (int): The process id to retrieve open files from.

    Returns:
        None
    """
    directories_to_avoid = config.DIRECTORIES_TO_AVOID
    try:
        process = psutil.Process(pid)
        open_files = process.open_files()
        for file in open_files:
            if not any(
                directory in str(file.path) for directory in directories_to_avoid
            ):
                logger.info(f"Open file: {file.path}")
                current_signals.add_signal(file.path, signal_type="file")
    except psutil.NoSuchProcess:
        print(f"No process with pid {pid} exists")


# Demonstration test code
if __name__ == "__main__":
    print(None)
    # r = requests.head("https://en.wikipedia.org/wiki/HTTP#Request_methods", allow_redirects=True)
    # length = r.headers.get('Content-Length')
    # type = r.headers.get('Content-Type')
    # print(length if length else 'Content-Length not provided')
    # print(type if type else 'Content-Type not provided')

    # signals = Signals()
    # signals.add_signal("tests/resources/test_signal_data.txt", "test data file")
    # signals.add_signal("https://en.wikipedia.org/wiki/HTTP#Request_methods", "wikipedia request methods page")
    # sys.path.append("tests/resources")
    # signals.add_signal("sample_package.sample_module.sample_function", "test function")
    # signals = Signals()
    # signals.add_signal("tests/resources/test_data.xlsx")
    # print(signals.return_signals())
    # signal_info = signals.return_signals()
    # for signal in signal_info:
    #     print(signal)
