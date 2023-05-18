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



    def add_database_signal(self, db_url):
        """
        Add a signal from a database.
        """
        # Get the signal from the database.
        return


    def add_file_signal(self, file_path):
        """
        Add a signal from a file.
        """
        # Get the signal from the file.
        return


    def add_url_signal(self, http_url):
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


    def add_function_signal(self, function_name):
        """
        Add a signal from a Python function.
        """
        # Get the signal from the function.
        return

    def add_signal(self, signal_address):
        if isinstance(signal_address, str):
            # If signal is a string, it could be a file path, a database URL,
            # an HTTP URL, or a Python function name.
            if signal_address.startswith("pgsql://"):
                # If the string starts with "pgsql://", treat it as a database URL.
                self.add_database_signal(signal_address)
            elif signal_address.startswith("http://") or signal_address.startswith("https://"):
                # If the string starts with "http://" or "https://", treat it as an HTTP URL.
                self.add_url_signal(signal_address)
            elif signal_address.endswith(('.json', '.csv')):
                # If the string ends with a known file extension, treat it as a file path.
                self.add_file_signal(signal_address)
            else:
                # Otherwise, treat it as a Python function name.
                self.add_function_signal(signal_address)
        else:
            # If signal is not a string, raise an error.
            raise ValueError("Signal must be a string.")

