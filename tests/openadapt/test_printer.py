import pytest
import os
from openadapt.tools import printer
# Get the directory of the current test script
current_dir = os.path.dirname(os.path.abspath(__file__))

@pytest.mark.skipif(not printer.get_available_printers(), reason="No printers available")
def test_print_document():
    printer_name = printer.get_available_printers()[0]  # Use the first available printer
    file_path = os.path.join(current_dir, "resources", "test_print_document.pdf")
    printer.print_document(printer_name, file_path)