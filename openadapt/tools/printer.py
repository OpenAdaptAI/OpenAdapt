import subprocess
import platform
import win32print
import win32api

def get_available_printers():
    printers = []
    if platform.system() == "Windows":
        printers = [printer[2] for printer in win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)]
    else:
        result = subprocess.run(["lpstat", "-p"], capture_output=True, text=True)
        output = result.stdout.strip().split('\n')
        printers = [line.split(' ')[1] for line in output if line.startswith('printer ')]

    return printers

def print_document(printer_name, file_path):
    if platform.system() == "Windows":
        win32api.ShellExecute(0, "print", file_path, f'/d:"{printer_name}"', ".", 0)
    else:
        subprocess.run(['lp', '-d', printer_name, file_path], capture_output=True)

# Get available printers
available_printers = get_available_printers()
print("Available Printers:")
for printer in available_printers:
    print(printer)

# Example usage: Print a document
printer_name = "OneNote (Desktop)"
file_path = "C:/Users/jesic/Documents/test1.pdf"
print_document(printer_name, file_path)
