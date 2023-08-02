import platform
import subprocess


def get_available_printers():
    """
    Retrieves a list of available printers on the system.

    Returns:
        list: A list of available printer names.
    """
    printers = []
    if platform.system() == "Windows":
        # Define the wmic command with findstr to exclude the "Name" header
        command = 'wmic printer where "PrinterState=0" get Name'

        # Execute the command and capture the output
        result = subprocess.run(command, capture_output=True, text=True)
        # Get the printer names as a list of strings
        printer_names = result.stdout.strip().split("\n")
        # Remove any leading or trailing whitespaces from the printer names
        printer_names = [
            name.strip() for name in printer_names[1:] if name.strip() != ""
        ]
        return printer_names
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
        # Print the document using Windows API
        job_id = win32api.ShellExecute(
            0, "print", file_path, f'/d:"{printer_name}"', ".", 0
        )
        return job_id
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
        # Define the wmic command to get print job IDs for the specified printer
        # Open a handle to the printer
        import ipdb
        import win32print

        ipdb.set_trace()
        printer_handle = win32print.OpenPrinter(printer_name)
        import ipdb

        ipdb.set_trace
        # Enumerate the print jobs for the printer
        job_entries = win32print.EnumJobs(printer_handle, 0, -1, 1)

        # Get the job IDs as a list
        job_ids = [job["JobId"] for job in job_entries]

        # Close the printer handle
        win32print.ClosePrinter(printer_handle)
        return job_ids
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
