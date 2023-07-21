import pytest
import os
import platform
from openadapt.tools import printer


# Get the directory of the current test script
current_dir = os.path.dirname(os.path.abspath(__file__))
printer_list = printer.get_available_printers()[0]


@pytest.mark.skipif(
    not printer.get_available_printers(), reason="No printers available"
)
def test_print_document():
    printer_name = printer.get_available_printers()[0]
    file_path = os.path.join(current_dir, "resources", "test_print_document.pdf")
    printer.print_document(printer_name, file_path)


@pytest.mark.skipif(
    not printer.get_available_printers(), reason="No printers available"
)
def test_get_printer_jobs_on_windows():
    # Mock the win32print.EnumJobs function for Windows platform
    printer_name = printer.get_available_printers()[0]
    printer_jobs = printer.get_printer_jobs(printer_name)


if __name__ == "__main__":
    pytest.main()
