from PyQt6.QtPrintSupport import QPrinter, QPrinterInfo
from PyQt6.QtWidgets import QApplication
import PyPDF2
import sys


def get_available_printers():
    printer_info_list = QPrinterInfo.availablePrinters()
    printer_names = [printer.printerName() for printer in printer_info_list]
    return printer_names


def print_document(document_path, printer_name):
    printer = QPrinter()
    printer.setOutputFormat(QPrinter.NativeFormat)
    printer.setOutputFileName(document_path)
    printer.setPrinterName(printer_name)

    file_extension = document_path.split('.')[-1]
    if file_extension == 'pdf':
        print_pdf(document_path, printer)
    # TODO : Add more conditions for other document formats and corresponding print functions


def print_pdf(pdf_file_path, printer):
    pdf_file = open(pdf_file_path, 'rb')
    pdf_reader = PyPDF2.PdfFileReader(pdf_file)
    total_pages = pdf_reader.numPages

    painter = QtGui.QPainter()
    if not painter.begin(printer):
        print("Failed to start printing")
        return

    for page_num in range(total_pages):
        printer.newPage()
        painter.drawImage(QtCore.QPoint(0, 0), QtGui.QImage(pdf_reader.getPage(page_num).to_image()))

    painter.end()
    pdf_file.close()


if __name__ == "__main__":
    app = QApplication(sys.argv)

    document_path = 'path/to/your/document'

    if document_path:
        printer_names = get_available_printers()

        if printer_names:
            selected_printer = printer_names[0]  # Select the first available printer automatically
            print_document(document_path, selected_printer)
        else:
            print("No printers available.")
    else:
        print("No document selected.")

    sys.exit(app.exec())
