import subprocess
import platform

def get_available_printers():
    """
    Retrieves a list of available printers on the system.

    Returns:
        list: A list of available printer names.
    """
    printers = []
    if platform.system() == "Windows":
        import win32print

        printers = [
            printer[2]
            for printer in win32print.EnumPrinters(
                win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            )
        ]
        return printers
    else:
        # Display only those printers that are currently accepting print requests.
        result = subprocess.run(["lpstat", "-a"], capture_output=True, text=True)
        # Split the output into lines
        lines = result.stdout.strip().split("\n")
        # Extract printer names and store them in a list
        if result.stderr == "lpstat: No destinations added.\n":
            return None
        else:
            printer_names = [line.split()[0] for line in lines]
            return printer_names


def print_document(printer_name, file_path):
    """
    Prints a document to the specified printer.

    Args:
        printer_name (str): The name of the printer to use for printing.
        file_path (str): The path to the document file to be printed.

    Returns:
        str or None: On Windows, returns None. On non-Windows platforms, returns the job ID of the print job.
    """
    if platform.system() == "Windows":
        import win32api

        # Print the document using Windows API
        win32api.ShellExecute(0, "print", file_path, f'/d:"{printer_name}"', ".", 0)
    else:
        # Print the document using the lp command on non-Windows platforms
        result = subprocess.run(
            ["lp", "-d", printer_name, file_path], capture_output=True, text=True
        )
        result = result.stdout
        job_id = result.strip().split()[3]
        return job_id


def get_printer_jobs(printer_name):
    """
    Retrieves a list of print jobs for a specific printer.

    Args:
        printer_name (str): The name of the printer.

    Returns:
        list: A list of print job IDs for the specified printer.
    """
    if platform.system() == "Windows":
        # Get printer jobs on Windows (Same as the previous implementation)
        import win32api
        import win32print

        phandle = win32print.OpenPrinter(printer_name)
        print_jobs = win32print.EnumJobs(phandle, 0, -1, 1)
        win32print.ClosePrinter(phandle)
        return print_jobs
    else:
        # Get printer jobs on non-Windows platforms using lpstat
        result = subprocess.run(
            ["lpstat", "-o", printer_name],
            capture_output=True,
        )
        result = result.stdout.decode("utf-8")
        lines = result.strip().split("\n")
        job_ids = [line.split()[0] for line in lines]
        return job_ids
