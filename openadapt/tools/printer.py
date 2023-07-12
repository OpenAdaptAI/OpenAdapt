from PyQt6.QtPrintSupport import QPrinter, QPrinterInfo
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPainter, QImage,QPixmap
from PyQt6.QtCore import QPoint,QByteArray,QCoreApplication
from loguru import logger
from PyPDF2 import PdfReader
from pdf2image import convert_from_path
from PIL.ImageQt import ImageQt
import sys


def get_available_printers():
    printer_info_list = QPrinterInfo.availablePrinters()
    printer_names = [printer.printerName() for printer in printer_info_list]
    return printer_names

def print_document(document_path, printer_name):
    printer = QPrinter()
    printer.setOutputFileName(document_path)
    printer.setPrinterName(printer_name)

    file_extension = document_path.split('.')[-1]
    if file_extension == 'pdf':
        logger.info("printing a pdf file")
        #printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        print_pdf(document_path, printer)
    # TODO : Add more conditions for other document formats and corresponding print functions


def print_pdf(pdf_file_path, printer):
    pdf_file = open(pdf_file_path, 'rb')
    pdf_reader = PdfReader(pdf_file)
    total_pages = len(pdf_reader.pages)
    logger.info(f"{total_pages}")

    painter = QPainter()
    if not painter.begin(printer):
        print("Failed to start printing")
        return

    for page_num in range(total_pages):
        printer.newPage()
        page = pdf_reader.pages[page_num]
        text = page.extract_text()
        logger.info(f"{text=}")
        image_data = QByteArray(text.encode())

        painter.drawImage(QPoint(0, 0), QImage.fromData(image_data))

    painter.end()
    pdf_file.close()
    logger.info("finish printing")

# def print_pdf(pdf_file_path, printer):
#     images = convert_from_path(pdf_file_path)
#     painter = QPainter()
#     if not painter.begin(printer):
#         print("Failed to start printing")
#         return

#     for i, image in enumerate(images):
#         if i > 0:
#             printer.newPage()
#         rect = painter.viewport()
#         qt_image = ImageQt(image)
#         qt_image_scaled = qt_image.scaled(rect.size(), aspectRatioMode=Qt.AspectRatioMode.KeepAspectRatio, transformMode=Qt.TransformationMode.SmoothTransformation)
#         painter.drawImage(rect, qt_image_scaled)

#     painter.end()
#     logger.info("Finish printing")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    document_path = 'C:/Users/jesic/Documents/test1.pdf'

    if document_path:
        printer_names = get_available_printers()
        logger.info(f"{printer_names=}")
        if printer_names:
            selected_printer = printer_names[0]  # Select the first available printer automatically
            logger.info(f"{selected_printer}")
            print_document(document_path, selected_printer)
        else:
            print("No printers available.")
    else:
        print("No document selected.")
    logger.info("quitting app")
    sys.exit(app.exec())
