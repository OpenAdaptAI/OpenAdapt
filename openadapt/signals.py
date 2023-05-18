import requests

from loguru import logger


class Signals:
    def __init__(self):
        self.signals = []


    def return_signals(self):
        """
        Return the list of signals.
        """
        return self.signals



    def __add_database_signal(self, db_url):
        """
        Add a signal from a database.
        """
        # Get the signal from the database.
        return


    def __add_file_signal(self, file_path):
        """
        Add a signal from a file.
        """
        # Get the signal from the file.
        try:
            with open(file_path, 'r') as file:
                content = file.read()
        except FileNotFoundError:
            logger.info(f"Error: File not found.")
            return "Error: File not found."
        except PermissionError:
            logger.info(f"Error: Permission denied.")
            return None
        except IOError:
            logger.info(f"Error: I/O error.")
            return None
        return content


    def __add_url_signal(self, http_url):
        """
        Add a signal from an HTTP URL.
        """
        # Get the signal from the URL.
        response = requests.get(http_url)
        if response.status_code != 200:
            logger.info(f"Error: HTTP request failed.")
        else:
            logger.info(f"Success: HTTP request succeeded.")
            signal_data = response.content
        return signal_data


    def __add_function_signal(self, function_name):
        """
        Add a signal from a Python function.
        """
        # Get the signal from the function.
        return

    def add_signal(self, signal_address):
        signal_data = None
        if isinstance(signal_address, str):
            # If signal is a string, it could be a file path, a database URL,
            # an HTTP URL, or a Python function name.
            if signal_address.startswith("pgsql://"):
                # If the string starts with "pgsql://", treat it as a database URL.
                signal_data = self.__add_database_signal(signal_address)
            elif signal_address.startswith("http://") or signal_address.startswith("https://"):
                # If the string starts with "http://" or "https://", treat it as an HTTP URL.
                signal_data = self.__add_url_signal(signal_address)
            elif signal_address.endswith(('.json', '.csv', '.txt')):
                # If the string ends with a known file extension, treat it as a file path.
                signal_data = self.__add_file_signal(signal_address)
            else:
                # Otherwise, treat it as a Python function name.
                signal_data = self.__add_function_signal(signal_address)
        else:
            # If signal is not a string, raise an error.
            raise ValueError("Signal must be a string.")
        
        self.signals.append(signal_data)

