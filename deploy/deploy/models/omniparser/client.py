import argparse
import base64
import io
import requests

from loguru import logger
from PIL import Image, ImageDraw, ImageFont

def parse_arguments():
    parser = argparse.ArgumentParser(description='Omniparser Client')
    parser.add_argument('--image_path', type=str, required=True, help='Path to the image')
    parser.add_argument('--server_url', type=str, required=True, 
                        help='URL of the Omniparser server (e.g., http://localhost:8000)')
    args = parser.parse_args()
    return args

def image_to_base64(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

def base64_to_image(base64_string):
    img_data = base64.b64decode(base64_string)
    return Image.open(io.BytesIO(img_data))

def plot_results(original_image_path, som_image_base64, parsed_content_list):
    # Open original image
    image = Image.open(original_image_path)
    width, height = image.size
    
    # Create drawable image
    draw = ImageDraw.Draw(image)
    
    # Draw bounding boxes and labels
    for item in parsed_content_list:
        # Get normalized coordinates and convert to pixel coordinates
        x1, y1, x2, y2 = item['bbox']
        x1 = int(x1 * width)
        y1 = int(y1 * height)
        x2 = int(x2 * width)
        y2 = int(y2 * height)
        
        label = item['content']
        
        # Draw rectangle
        draw.rectangle([(x1, y1), (x2, y2)], outline='red', width=2)
        
        # Draw label background
        text_bbox = draw.textbbox((x1, y1), label)
        draw.rectangle([text_bbox[0]-2, text_bbox[1]-2, text_bbox[2]+2, text_bbox[3]+2], 
                      fill='white')
        
        # Draw label text
        draw.text((x1, y1), label, fill='red')
    
    # Show image
    image.show()

def main():
    args = parse_arguments()
    
    # Remove trailing slash from server_url if present
    server_url = args.server_url.rstrip('/')
    
    # Convert image to base64
    base64_image = image_to_base64(args.image_path)
    
    # Prepare request
    url = f"{server_url}/parse/"
    payload = {"base64_image": base64_image}
    
    try:
        # First, check if the server is available
        probe_url = f"{server_url}/probe/"
        probe_response = requests.get(probe_url)
        probe_response.raise_for_status()
        logger.info("Server is available")
        
        # Make request to API
        response = requests.post(url, json=payload)
        response.raise_for_status()
        
        # Parse response
        result = response.json()
        som_image_base64 = result['som_image_base64']
        parsed_content_list = result['parsed_content_list']
        
        # Plot results
        plot_results(args.image_path, som_image_base64, parsed_content_list)
        
        # Print latency
        logger.info(f"API Latency: {result['latency']:.2f} seconds")
        
    except requests.exceptions.ConnectionError:
        logger.error(f"Error: Could not connect to server at {server_url}")
        logger.error("Please check if the server is running and the URL is correct")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error making request to API: {e}")
    except Exception as e:
        logger.error(f"Error: {e}")

if __name__ == "__main__":
    main()

