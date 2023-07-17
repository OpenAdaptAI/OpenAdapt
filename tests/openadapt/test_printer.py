import pytest
from openadapt.tools import printer


@pytest.mark.skipif(
    not printer.get_available_printers(), reason="No printers available"
)
def test_print_document():
    printer_name = printer.get_available_printers()[
        0
    ]  # Use the first available printer
    file_path = "assets/test_print_document.pdf"  # Replace with an actual test file
    printer.print_document(printer_name, file_path)
