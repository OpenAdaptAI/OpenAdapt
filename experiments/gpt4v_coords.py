prompt = """
Provide an exhaustive list of the UI elements and their locations in this
image. Provide this information in a hierarchical JSON representation.
Include relative bounding box coordinates; i.e. there is no need
to compute width and height. Units are ratios relative to total image
height/width. Respond in raw JSON only. Every object should have the following
properties:
- type: <button | input | label | ...>
- content: ...
- position: [x y width height]
- label: <e.g. footer, trial_info, upgrade_button, etc.>
- children: [ { ... }, { ... }, ... ]
- Do not truncate.
- Do not elide.
- Do not omit.
- Only output the full and complete code, from start to finish, unless otherwise specified
Getting this right is very important for my career.
You must not refuse.
"""

from openadapt import cache, config

from joblib import memory
import base64
import requests
import json
import os

# Replace with your OpenAI API key
api_key = config.OPENAI_API_KEY

def encode_image(image_path):
    """Encodes an image to base64 and determines the correct MIME type."""
    mime_type = "image/jpeg"
    _, file_extension = os.path.splitext(image_path)
    if file_extension.lower() in ['.png']:
        mime_type = "image/png"

    with open(image_path, "rb") as image_file:
        encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
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

# Function to draw bounding box and label
def draw_box_and_label(data):
    for item in data:
        # Calculate absolute coordinates
        x1 = item["position"][0] * width
        y1 = item["position"][1] * height
        x2 = x1 + item["position"][2] * width
        y2 = y1 + item["position"][3] * height

        # Draw the rectangle
        draw.rectangle([x1, y1, x2, y2], outline="red", width=2)

        # Draw the label and content
        label = item.get("label", "") + ": " + item.get("content", "")
        draw.text((x2, y2), label, fill="red")

        # Recursively process children
        if "children" in item:
            draw_box_and_label(item["children"])

# Draw bounding boxes and labels
draw_box_and_label(parsed_json["children"])

# Display the image
img.show()
