import sys
from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtPrintSupport import QPrinterInfo, QPrintDialog

def get_available_printers():
    printer_info_list = QPrinterInfo.availablePrinters()
    printer_names = [printer.printerName() for printer in printer_info_list]
    return printer_names

def print_document(document_path, printer_name):
    app = QCoreApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    
    printer_info = QPrinterInfo.printerInfo(printer_name)
    if printer_info is not None:
        printer = QPrinter(printer_info)
        printer.setOutputFormat(QPrinter.NativeFormat)
        printer.setOutputFileName(document_path)
        
        print_dialog = QPrintDialog(printer)
        print_dialog.setWindowTitle("Print Document")
        print_dialog.setOptions(QPrintDialog.PrintToFile | QPrintDialog.PrintPageRange)
        
        if print_dialog.exec() == QPrintDialog.Accepted:
            painter = printer.paintEngine().painter()
            
            painter = QtGui.QPainter(printer)
            painter.begin(printer)
            
            painter.end()
        
        if isinstance(app, QApplication):
            sys.exit(app.exec())

if __name__ == "__main__":
    app = QApplication(sys.argv)
    sys.exit(app.exec())
