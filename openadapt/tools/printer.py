import subprocess
import platform
import os

def get_available_printers():
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
        lines = result.stdout.strip().split('\n')
        # Extract printer names and store them in a list
        if result.stderr == "lpstat: No destinations added.\n":
            return None
        else :
            printer_names = [line.split()[0] for line in lines]
            return printer_names


def print_document(printer_name, file_path):
    if platform.system() == "Windows":
        win32api.ShellExecute(0, "print", file_path, f'/d:"{printer_name}"', ".", 0)
    else:
        result = subprocess.run(["lp", "-d", printer_name, file_path], capture_output=True, text=True)
        result = result.stdout
        job_id = result.strip().split()[3]
        return job_id

def get_printer_jobs(printer_name):
    if platform.system() == "Windows":
        # Get printer jobs on Windows (Same as the previous implementation)
        import win32api
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
        result = result.stdout.decode('utf-8')
        #import ipdb; ipdb.set_trace()
        lines = result.strip().split('\n')
        job_ids= [line.split()[0] for line in lines]
        return job_ids
if __name__ == "__main__":
    # Get the directory of the current test script
    current_dir = os.path.dirname(os.path.abspath(__file__))
    printer_name = get_available_printers()[0]
    file_path = '/Users/jesicasusanto/Desktop/MLDSAI/PAT/tests/openadapt/resources/test_print_document.pdf'
    job_id = print_document(printer_name, file_path) 
    job_ids = get_printer_jobs(printer_name)
    import ipdb; ipdb.set_trace()
    if platform == "Windows" :
        pass
    else :
        assert job_id in job_ids
