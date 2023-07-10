import sys
from PyQt5.QtWidgets import QApplication, QFileDialog, QMessageBox, QComboBox
from PyQt5.QtPrintSupport import QPrinter, QPrintDialog

class PrinterIntegration:
    def __init__(self):
        self.app = QApplication(sys.argv)
        
    def get_available_printers(self):
        printer_info = QPrinter()
        printer_names = printer_info.supportedPrinterNames()
        return printer_names

    def print_document(self, document_path, printer_name):
        printer = QPrinter()
        printer.setOutputFormat(QPrinter.NativeFormat)
        printer.setOutputFileName(document_path)
        printer.setPrinterName(printer_name)
        
        print_dialog = QPrintDialog(printer)
        if print_dialog.exec_() == QPrintDialog.Accepted:
            printer.printFile(document_path)
            
        sys.exit(self.app.exec())

if __name__ == "__main__":
    printer_integration = PrinterIntegration()
    
    document_path, _ = QFileDialog.getOpenFileName(None, "Select Document to Print", "", "PDF Files (*.pdf);;All Files (*.*)")
    
    if document_path:
        printer_names = printer_integration.get_available_printers()
        
        if printer_names:
            printer_combo = QComboBox()
            printer_combo.addItems(printer_names)
            printer_combo.setCurrentIndex(0)
            printer_combo_dialog = QMessageBox()
            printer_combo_dialog.setWindowTitle("Select Printer")
            printer_combo_dialog.setText("Select the printer:")
            printer_combo_dialog.setIcon(QMessageBox.Question)
            printer_combo_dialog.addButton(QMessageBox.Cancel)
            printer_combo_dialog.addButton(QMessageBox.Ok)
            printer_combo_dialog.setDefaultButton(QMessageBox.Ok)
            printer_combo_dialog.setEscapeButton(QMessageBox.Cancel)
            printer_combo_dialog.setFixedWidth(300)
            printer_combo_dialog.layout().addWidget(printer_combo)
            
            if printer_combo_dialog.exec_() == QMessageBox.Ok:
                selected_printer = printer_combo.currentText()
                printer_integration.print_document(document_path, selected_printer)
        else:
            QMessageBox.warning(None, "Error", "No printers available.")
    else:
        QMessageBox.warning(None, "Error", "No document selected.")
