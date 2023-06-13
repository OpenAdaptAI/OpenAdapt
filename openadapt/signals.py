import requests
import importlib
import os
import mimetypes
import sys #for debugging
import sqlite3
import pandas as pd

from loguru import logger


class Signals:
    """
    Create Signal structure as dictionary
    signals = [{},{},{}]

    {
        "number": signal number in list,
        "title": given signal title,
        "type": type of signal (file, database, etc.)
        "address": given signal address,
        "description": description of signal (file type, length, etc.),
    }
    """
    def __init__(self):
        self.signals = []

    def return_signals(self):
        """
        Return the list of signals.
        """
        return self.signals
    

    def __setup_database_signal(self, db_url):
        """
        Read a description of a signal from a database.
        """
        # Get the signal from the database.
        #TODO: implement database signal

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
            table_info.append(f"Table: {row[0]}, Schema: {row[1]}")
        
        description = "\n".join(table_info)
        return description


    def __setup_file_signal(self, file_path):
        """
        Read a description of a signal from a file.
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

            if (file_path.endswith(('.xls','.xlsx'))):
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
            logger.info(f"Error: Pandas could not parse the excel file, possible formatting issue.")
        return description
        


    def __setup_url_signal(self, http_url):
        """
        Read a description of a signal from an HTTP URL.
        """
        # Get the signal from the URL.
        response = requests.get(http_url, allow_redirects=True)
        if response.status_code != 200:
            logger.info(f"Error: HTTP request failed.")
        else:
            logger.info(f"Success: HTTP request succeeded.")

            # Get the signal's length and type.
            length = response.headers.get('Content-Length')
            type = response.headers.get('Content-Type')

            description = (
                f"Length: {length if length else 'Content-Length not provided'}, "
                f"Type: {type if type else 'Content-Type not provided'}"
            )
        return description


    def __setup_function_signal(self, function_name):
        """
        Return a description of a Python function signal.
        """
        module_name, func_name = function_name.rsplit('.', 1)
        module = importlib.import_module(module_name)
        func = getattr(module, func_name)

        # Get the function's docstring, or 'No description provided' if it doesn't have one
        docstring = func.__doc__ if func.__doc__ else 'No description provided'

        # Get the function's name and module
        description = (
            f"Function: {func.__name__}, "
            f"Module: {module.__name__}, "
            f"Description: {docstring}"
        )

        return description


    def __access_database_signal(self, db_url, query):
        """
        Read signal data from a database.
        """
        # Get the signal from the database.
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


    def __access_file_signal(self, file_path):
        """
        Read signal data from a file.
        """
        # Get the signal from the file.
        try:
            if file_path.endswith(('.xls','.xlsx')):
                data_frame = pd.read_excel(file_path)
                content = data_frame.to_string()
            with open(file_path, 'r') as file:
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


    def __access_url_signal(self, http_url):
        """
        Read signal data from an HTTP URL.
        """
        # Get the signal from the URL.
        response = requests.get(http_url, allow_redirects=True)
        if response.status_code != 200:
            logger.info(f"Error: HTTP request failed.")
        else:
            logger.info(f"Success: HTTP request succeeded.")
            signal_data = response.content
        return signal_data


    def __access_function_signal(self, function_name):
        """
        Read signal data from a Python function.
        """
        module_name, func_name = function_name.rsplit('.', 1)
        module = importlib.import_module(module_name)
        func = getattr(module, func_name)

        # Call the function and get its result
        result = func()

        return result


    def add_signal(self, signal_address, signal_title="None"):
        signal_description = None
        signal_type = None
        if isinstance(signal_address, str):
            # If signal is a string, it could be a file path, a database URL, an HTTP URL, or a Python function name.
            if signal_address.startswith("pgsql://") or signal_address.endswith(".db"):
                # If the string starts with "pgsql://", treat it as a database URL.
                signal_description = self.__setup_database_signal(signal_address)
                signal_type = "database"
            elif signal_address.startswith("http://") or signal_address.startswith("https://"):
                # If the string starts with "http://" or "https://", treat it as an HTTP URL.
                signal_description = self.__setup_url_signal(signal_address)
                signal_type = "web_url"
            elif signal_address.endswith(('.json', '.csv', '.txt', '.xlsx', '.xls')):
                # If the string ends with a known file extension, treat it as a file path.
                signal_description = self.__setup_file_signal(signal_address)
                signal_type = "file"
            elif signal_address.count(".") >= 1:
                # Otherwise, treat it as a Python function name.
                signal_description = self.__setup_function_signal(signal_address)
                signal_type = "function"
            else:
                raise ValueError("Invalid signal address.")
        else:
            # If signal is not a string, raise an error.
            raise ValueError("Signal must be a string.")
            
        
        signal_number = len(self.signals) + 1

        signal = {
            "number": signal_number, 
            "title": signal_title, 
            "type": signal_type, 
            "address": signal_address, 
            "description": signal_description
        }
        self.signals.append(signal)


    def remove_signal(self, signal_number):
        """
        Remove a signal from the list.
        """
        self.signals.pop(signal_number - 1)
        for i in range(signal_number - 1, len(self.signals)):
            # Decrement the signal numbers of all signals after the removed signal.
            self.signals[i]["number"] -= 1


    def return_signal_data(self, signal_number, query=None):
        """
        Return the data of a signal.
        """
        signal = self.signals[signal_number - 1]
        if len(signal) == 0:
            return None
        elif signal["type"] == "database":
            return(self.__access_database_signal(signal["address"], query))
        elif signal["type"] == "web_url":
            return(self.__access_url_signal(signal["address"]))
        elif signal["type"] == "file":
            return(self.__access_file_signal(signal["address"]))
        elif signal["type"] == "function":
            return(self.__access_function_signal(signal["address"]))
        else:
            return None

#Demonstration test code
if __name__ == "__main__":
    r = requests.head("https://en.wikipedia.org/wiki/HTTP#Request_methods", allow_redirects=True)
    length = r.headers.get('Content-Length') 
    type = r.headers.get('Content-Type') 
    print(length if length else 'Content-Length not provided')
    print(type if type else 'Content-Type not provided')

    signals = Signals()
    signals.add_signal("tests/openadapt/test_signal_data.txt", "test data file")
    signals.add_signal("https://en.wikipedia.org/wiki/HTTP#Request_methods", "wikipedia request methods page")
    sys.path.append("tests/openadapt")
    signals.add_signal("sample_package.sample_module.sample_function", "test function")


    signal_info = signals.return_signals()
    for signal in signal_info:
        print(signal)

