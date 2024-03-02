import pytest
import os
from openadapt.tools import printer
import subprocess

# Get the directory of the current test script
current_dir = os.path.dirname(os.path.abspath(__file__))


# Skip this test if there are no available printers
@pytest.mark.skipif(
    not printer.get_available_printers(), reason="No printers available"
)
def test_print_document():
    # Get the name of the first available printer
    printer_name = printer.get_available_printers()[0]

    # Get the file path of the test document to be printed
    file_path = os.path.join(current_dir, "resources", "test_print_document.pdf")

    # Print the document and get the job ID
    job_id = printer.print_document(printer_name, file_path)

    # Get the list of job IDs for the specified printer
    job_ids = printer.get_printer_jobs(printer_name)

    # Assert that the job ID returned by print_document is in the list of printer jobs
    assert job_id in job_ids

    # Cancel the print job to clean up after the test
    subprocess.run(["cancel", job_id])
