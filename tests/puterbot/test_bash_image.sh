#!/bin/sh

pytest -k test_scrub_image_data test_scrub.py > /dev/null 2>&1

# Set path to the scrubbed image file
SCRUBBED_IMAGE_PATH="scrubbed_image.png"

# Run pytesseract on the scrubbed image file and save the output to a variable
OCR_TEXT=$(pytesseract "$SCRUBBED_IMAGE_PATH")

# Check if the OCR text contains the email
if echo "$OCR_TEXT" | grep -q "john@deo.gmail.com"; then
    echo "Error: OCR text contains original email"
    exit 1
fi

# Check if the OCR text does not contain the scrubbed email
if ! echo "$OCR_TEXT" | grep -q "my email is"; then
    echo "Error: OCR text does not contain the string \"my email is\""
    exit 1
fi

rm -f "$SCRUBBED_IMAGE_PATH"

echo "Test Passed!"
exit 0
