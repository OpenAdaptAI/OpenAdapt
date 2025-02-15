import argparse
import base64
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from PIL import Image
from google import genai
from google.genai import types
from dotenv import load_dotenv
import os
from joblib import Memory
from pydantic import BaseModel
import requests
import textwrap
from typing import List

# Load environment variables
load_dotenv()

# Set up joblib caching
cache_dir = '.cache'
memory = Memory(cache_dir, verbose=0)

class BoundingBox(BaseModel):
    coordinates: list[int]  # [y1, x1, y2, x2]
    label: str

def get_random_image():
    """Fetch a random image from Lorem Picsum"""
    try:
        response = requests.get('https://picsum.photos/800/600')
        if response.status_code == 200:
            temp_path = 'temp_random_image.jpg'
            with open(temp_path, 'wb') as f:
                f.write(response.content)
            return temp_path
        else:
            raise Exception(f"Failed to fetch image: {response.status_code}")
    except Exception as e:
        print(f"Error fetching random image: {e}")
        return None

def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

def get_gemini_response(image_path):
    client = genai.Client(api_key=os.getenv('GOOGLE_API_KEY'))

    encoded_image = encode_image(image_path)
    image_part = types.Part.from_bytes(
        data=base64.b64decode(encoded_image),
        mime_type="image/jpeg",
    )

    prompt = """Analyze this image and identify all distinct GUI objects and elements.
    For each object, provide its bounding box coordinates normalized to a 0-1000 scale
    as [y1, x1, y2, x2] where (y1,x1) is the top-left corner and (y2,x2) is the bottom-right corner.
    Be as granular as possible, e.g. not just "table" but "cell a1", "ok button", etc.
    MAXIMUM GRANULARITY!!! My career depends on this. Lives are at stake.
    """

    contents = [
        types.Content(
            role="user",
            parts=[
                image_part,
                types.Part.from_text(text=prompt)
            ]
        )
    ]

    response = client.models.generate_content(
        model='gemini-2.0-pro-exp-02-05',
        contents=contents,
        config={
            'response_mime_type': 'application/json',
            'response_schema': list[BoundingBox],
        }
    )

    return response.parsed

def detect_boxes(image_path):
    try:
        boxes = get_gemini_response(image_path)
        return boxes
    except Exception as e:
        print(f"Error processing response: {e}")
        return []

def plot_boxes(image_path, boxes: List[BoundingBox]):
    img = Image.open(image_path)
    fig, ax = plt.subplots()
    ax.imshow(img)

    img_width, img_height = img.size
    
    for box in boxes:
        coords = box.coordinates
        label = box.label
        
        if len(coords) == 4:  # [y1, x1, y2, x2]
            try:
                y1, x1, y2, x2 = coords
                
                # Convert to pixel coordinates
                x1 = (x1 / 1000.0) * img_width
                y1 = (y1 / 1000.0) * img_height
                x2 = (x2 / 1000.0) * img_width
                y2 = (y2 / 1000.0) * img_height
                
                width = x2 - x1
                height = y2 - y1
                
                # Debug prints
                print(f"Drawing box for {label}:")
                print(f"Original coords: {coords}")
                print(f"Converted coords: ({x1}, {y1}) to ({x2}, {y2})")
                print(f"Box dimensions: width={width}, height={height}")
                
                rect = patches.Rectangle(
                    xy=(x1, y1),
                    width=width,
                    height=height,
                    linewidth=2,
                    edgecolor='red',
                    facecolor='none'
                )
                ax.add_patch(rect)
                
                wrapped_label = '\n'.join(textwrap.wrap(label, width=30))
                ax.text(x1, y1-5, wrapped_label,
                       color='red',
                       fontsize=8,
                       bbox=dict(facecolor='white',
                                alpha=0.7,
                                edgecolor='none'))
                
            except (ValueError, AttributeError) as e:
                print(f"Error processing box {box}: {e}")
                continue

    plt.axis('off')
    plt.show()

def cleanup_temp_file(file_path):
    if file_path and file_path.startswith('temp_') and os.path.exists(file_path):
        os.remove(file_path)

def main():
    parser = argparse.ArgumentParser(description='Detect objects with bounding boxes in an image')
    parser.add_argument('image_path', nargs='?', help='Path to the image file (optional)')
    args = parser.parse_args()

    image_path = args.image_path if args.image_path else get_random_image()
    
    if image_path:
        boxes = detect_boxes(image_path)
        if boxes:
            plot_boxes(image_path, boxes)
        else:
            print("No boxes detected or error in processing")
        
        if not args.image_path:
            cleanup_temp_file(image_path)
    else:
        print("Failed to get an image to process")

if __name__ == "__main__":
    main()

