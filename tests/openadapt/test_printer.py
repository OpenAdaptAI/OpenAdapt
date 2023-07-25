import pytest
import os
import platform
from openadapt.tools import printer
import subprocess

# Get the directory of the current test script
current_dir = os.path.dirname(os.path.abspath(__file__))


@pytest.mark.skipif(
    not printer.get_available_printers(), reason="No printers available"
)
def test_print_document():
    printer_name = printer.get_available_printers()[0]
    file_path = os.path.join(current_dir, "resources", "test_print_document.pdf")
    printer.print_document(printer_name, file_path)
    printer_jobs = printer.get_printer_jobs(printer_name)
    if platform == "Windows" :
        pass
    else :
        # Split the output into lines
        lines = printer_jobs.strip().split('\n')

        # Extract job files and store them in a list
        job_files = [line.split()[3] for line in lines[1:]]
        assert file_path in job_files
        subprocess.run(
            ["cancel", printer_name]
        )
