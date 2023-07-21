from anonympy.pdf import pdfAnonymizer


# need to specify paths, since I don't have them in system variables
anonym = pdfAnonymizer(
    path_to_pdf="embedded_text.pdf",
    pytesseract_path=r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    poppler_path=r"C:\Users\Krish Patel\Downloads\poppler-23.07.0\Library\bin",
)

# Calling the generic function
anonym.anonymize(
    output_path="output.pdf", remove_metadata=True, fill="red", outline="black"
)
