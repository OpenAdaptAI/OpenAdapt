import argparse
import base64
import json
import matplotlib.pyplot as plt
from PIL import Image
from google import genai
from google.genai import types
from dotenv import load_dotenv
import os
from joblib import Memory
from pydantic import BaseModel
import requests
from io import BytesIO

# Load environment variables
load_dotenv()

# Set up joblib caching
cache_dir = '.cache'
memory = Memory(cache_dir, verbose=0)

class Point(BaseModel):
    point: list[int]  # [y, x]
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

def get_gemini_response(image_path, is_gui_image):
    client = genai.Client(api_key=os.getenv('GOOGLE_API_KEY'))

    encoded_image = encode_image(image_path)
    image_part = types.Part.from_bytes(
        data=base64.b64decode(encoded_image),
        mime_type="image/jpeg",
    )

    if is_gui_image:
        prompt = """Analyze this image and identify all distinct GUI objects and elements.
        For each point, provide its coordinates as [x, y] normalized to 0-1000 scale and a descriptive label.
        Return the results as a JSON array where each element has a "point" property and a "label" property.
        Be as granular as possible, e.g. not just "table" but "cell a1", "ok button", etc.
        MAXIMUM GRANULARITY!!! My career depends on this. Lives are at stake.
        
        Example response:
        [
            {"point": [500, 300], "label": "cell A1"},
            {"point": [750, 200], "label": "ok button"}
        ]"""
    else:
        prompt = """Analyze this image and identify key points of interest.
        For each point, provide its coordinates as [x, y] normalized to 0-1000 scale and a descriptive label.
        Return the results as a JSON array where each element has a "point" property and a "label" property.

        Example response:
        [
            {"point": [500, 300], "label": "person's face"},
            {"point": [750, 200], "label": "clock on wall"}
        ]"""



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
            'response_schema': list[Point],
        }
    )
    
    cleaned_text = response.text.replace('```json', '').replace('```', '').strip()
    print("Raw response:", cleaned_text)
    
    try:
        return json.loads(cleaned_text)
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON: {e}")
        return []

def detect_points(image_path, is_gui_image):
    try:
        points = get_gemini_response(image_path, is_gui_image)
        return points
    except Exception as e:
        print(f"Error processing response: {e}")
        return []

def plot_points(image_path, points):
    img = Image.open(image_path)
    fig, ax = plt.subplots()
    ax.imshow(img)

    # Get image dimensions
    img_width, img_height = img.size
    
    for point_data in points:
        point = point_data.get('point', [])
        label = point_data.get('label', 'unknown')
        if point and len(point) == 2:
            try:
                # Denormalize coordinates from 0-1000 scale to image dimensions
                x = (point[1] / 1000.0) * img_width
                y = (point[0] / 1000.0) * img_height
                
                # Plot point
                ax.plot(x, y, 'ro', markersize=10)
                
                # Add label with offset
                ax.annotate(label, (x, y), xytext=(5, 5), 
                           textcoords='offset points',
                           color='red', fontsize=8)
            except (ValueError, AttributeError) as e:
                print(f"Error processing point {point}: {e}")
                continue

    plt.axis('off')
    plt.show()

def cleanup_temp_file(file_path):
    if file_path and file_path.startswith('temp_') and os.path.exists(file_path):
        os.remove(file_path)

def main():
    parser = argparse.ArgumentParser(description='Detect points of interest in an image')
    parser.add_argument('image_path', nargs='?', help='Path to the image file (optional)')
    args = parser.parse_args()

    image_path = args.image_path if args.image_path else get_random_image()
    is_gui_image = bool(args.image_path)
    
    if image_path:
        points = detect_points(image_path, is_gui_image)
        if points:
            plot_points(image_path, points)
        else:
            print("No points detected or error in processing")
        
        if not args.image_path:
            cleanup_temp_file(image_path)
    else:
        print("Failed to get an image to process")

if __name__ == "__main__":
    main()

