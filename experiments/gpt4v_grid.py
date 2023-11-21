prompt = """
Provide an exhaustive list of the UI elements and their locations in this
image. Provide this information in a hierarchical JSON representation.
Include bounding box coordinates.
Note that x0/x1/y0/y1 should be specified in grid coordinates, e.g. B3.
Respond in raw JSON only.
Every object should have the following properties:
- type: <button | input | label | ...>
- content: ...
- position: [x0 y0 x1 y1]
- label: <e.g. footer, trial_info, upgrade_button, etc.>
- children: [ { ... }, { ... }, ... ]
"""

from openadapt import cache, config

from joblib import memory
import base64
import requests
import io
import json
import os

# Replace with your OpenAI API key
api_key = config.OPENAI_API_KEY

from PIL import Image, ImageDraw, ImageFont

GRID_SPACING = 20

# Function to get a font object with the specified size, using Arial if available, or a default font otherwise
def get_font(font_size):
    try:
        # Try to use Arial font
        return ImageFont.truetype("Arial.ttf", font_size)
    except IOError:
        # If Arial is not available, fall back to the default font
        return ImageFont.load_default()


def overlay_grid(image_path, grid_spacing):
    # Open the image
    img = Image.open(image_path)
    img = img.convert('RGBA')  # Ensure image is in RGBA format to handle transparency

    # Add padding to the image
    padded_width = img.width + grid_spacing
    padded_height = img.height + grid_spacing
    padded_img = Image.new('RGBA', (padded_width, padded_height), (255, 255, 255, 255))  # White background
    padded_img.paste(img, (grid_spacing, grid_spacing))

    draw = ImageDraw.Draw(padded_img)

    # Function to convert column number to Excel-style column name
    def get_column_letter(n):
        name = ""
        while n > 0:
            n, remainder = divmod(n - 1, 26)
            name = chr(65 + remainder) + name
        return name

    # Estimate the maximum label width based on the number of columns
    max_label_length = len(get_column_letter(padded_width // grid_spacing))

    # Find the font size that fits the widest label in the grid spacing
    font_size = grid_spacing
    font = get_font(font_size)
    while font.getsize("W" * max_label_length)[0] > grid_spacing and font_size > 0:
        font_size -= 1
        font = get_font(font_size)

    # Draw the vertical grid lines and column letters with a consistent font size
    for i in range(1, padded_width // grid_spacing):
        x = i * grid_spacing
        line = [(x, 0), (x, padded_height)]
        draw.line(line, fill="black", width=1)
        # Get Excel-style column letter
        if i >= 1:  # Start annotations from the second column
            column_letter = get_column_letter(i)
            text_width, text_height = font.getsize(column_letter)
            # Center the text within the grid cell
            text_x = x + (grid_spacing - text_width) // 2
            text_y = (grid_spacing - text_height) // 2
            draw.text((text_x, text_y), column_letter, fill="black", font=font)


    # Draw the horizontal grid lines and row numbers
    for j in range(1, padded_height // grid_spacing):
        y = j * grid_spacing
        line = [(0, y), (padded_width, y)]
        draw.line(line, fill="black", width=1)
        if j > 0:  # Skip the first line to start annotations from the second row
            draw.text((0, y + 2), str(j), fill="black", font=font)

    # Return the in-memory image
    return padded_img

def encode_image(image_path, grid_spacing=GRID_SPACING):
    """Encodes an image to base64 in memory after overlaying a grid and determines the correct MIME type."""
    # Overlay grid and get the in-memory image
    img_with_grid = overlay_grid(image_path, grid_spacing)
    
    # Convert image to byte array in memory
    byte_arr = io.BytesIO()
    img_with_grid.save(byte_arr, format='PNG')  # Save as PNG to byte array to preserve transparency
    byte_arr = byte_arr.getvalue()
    
    # Encode byte array to base64
    encoded_string = base64.b64encode(byte_arr).decode('utf-8')
    
    # Determine the MIME type
    mime_type = "image/png"  # Grid overlay function always returns a PNG image
    return f"data:{mime_type};base64,{encoded_string}"

def create_payload(images, prompt, model="gpt-4-vision-preview", max_tokens=4000, detail="high"):
    """Creates the payload for the API request."""
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": prompt
                },
                {
                    "type": "text",
                    "text": "Please provide the complete contents of the object with label 'spreadsheet_content', including all children.",
                },
            ]
        }
    ]

    for image in images:
        base64_image = encode_image(image)
        messages[0]["content"].append({
            "type": "image_url",
            "image_url": {
                "url": base64_image,
                "detail": detail
            }
        })

    return {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens
    }

@cache.cache()
def query_openai(payload):
    """Sends a request to the OpenAI API and prints the response."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
    return response.json()

# Example usage
image_paths = [
    "./experiments/spreadsheet.png",
    #"path_to_image2.png",
]

payload = create_payload(image_paths, prompt)
response = query_openai(payload)
from pprint import pprint
pprint(response)
if response and response.get("choices"):
    json_content = response["choices"][0]["message"]["content"]
    json_content = json_content.strip('```json').strip('```').strip()
    # Now, parse the JSON string
    try:
        parsed_json = json.loads(json_content)
        print(json.dumps(parsed_json, indent=4))
    except json.JSONDecodeError as e:
        print("Error decoding JSON:", e)
        print("Original content:", json_content)

from PIL import Image, ImageDraw, ImageFont


# Load the image
img = Image.open(image_paths[0])
draw = ImageDraw.Draw(img)
width, height = img.size

def column_letter_to_number(column_letter):
    """Converts a column letter (e.g., 'A', 'B', ..., 'Z', 'AA', ...) to a number (e.g., 0, 1, ..., 25, 26, ...)."""
    number = 0
    for char in column_letter:
        number = number * 26 + (ord(char.upper()) - ord('A') + 1)
    return number - 1

def draw_box_and_label(data, draw, grid_spacing, font):
    for item in data:
        # Split the position into start and end parts
        start_pos, end_pos = item["position"]
        start_col, start_row = start_pos[:len(start_pos)-1], int(start_pos[len(start_pos)-1:])
        end_col, end_row = end_pos[:len(end_pos)-1], int(end_pos[len(end_pos)-1:])

        # Convert column letters and row numbers to coordinates
        x1 = column_letter_to_number(start_col) * grid_spacing + grid_spacing
        y1 = (start_row - 1) * grid_spacing + grid_spacing
        x2 = (column_letter_to_number(end_col) + 1) * grid_spacing
        y2 = end_row * grid_spacing

        # Draw the rectangle
        draw.rectangle([x1, y1, x2, y2], outline="red", width=2)

        # Draw the label and content
        label = item.get("label", "") + ": " + item.get("content", "")
        text_width, text_height = font.getsize(label)
        draw.text((x1 + (x2 - x1 - text_width) / 2, y1 + (y2 - y1 - text_height) / 2), label, fill="red", font=font)

        # Recursively process children
        if "children" in item:
            draw_box_and_label(item["children"], draw, grid_spacing, font)

# Draw bounding boxes and labels
draw_box_and_label(parsed_json["children"], draw, GRID_SPACING, get_font(GRID_SPACING))

# Display the image
img.show()
