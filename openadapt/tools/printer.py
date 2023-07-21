import subprocess
import platform
import win32print
import win32api


def get_available_printers():
    printers = []
    if platform.system() == "Windows":
        printers = [
            printer[2]
            for printer in win32print.EnumPrinters(
                win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            )
        ]
    else:
        result = subprocess.run(["lpstat", "-p"], capture_output=True, text=True)
        output = result.stdout.strip().split("\n")
        printers = [
            line.split(" ")[1] for line in output if line.startswith("printer ")
        ]

    return printers


def print_document(printer_name, file_path):
    if platform.system() == "Windows":
        win32api.ShellExecute(0, "print", file_path, f'/d:"{printer_name}"', ".", 0)
    else:
        subprocess.run(["lp", "-d", printer_name, file_path], capture_output=True)


def get_printer_jobs(printer_name):
    if platform.system() == "Windows":
        # Get printer jobs on Windows (Same as the previous implementation)
        phandle = win32print.OpenPrinter(printer_name)
        print_jobs = win32print.EnumJobs(phandle, 0, -1, 1)
        win32print.ClosePrinter(phandle)
        return print_jobs
    else:
        # Get printer jobs on non-Windows platforms using lpstat
        result = subprocess.run(
            ["lpstat", "-W", "completed", "-o", printer_name],
            capture_output=True,
            text=True,
        )
        output = result.stdout.strip().split("\n")
        jobs = []
        for line in output:
            job_info = line.split()
            job = {
                "job_id": int(job_info[0]),
                "username": job_info[1],
                "total_pages": int(job_info[2]),
                "pages_printed": int(job_info[3]),
                "status": job_info[4],
            }
            jobs.append(job)
        return jobs


if __name__ == "__main__":
    import ipdb

    ipdb.set_trace()
    printer_name = get_available_printers()[0]
    get_printer_jobs(printer_name)
