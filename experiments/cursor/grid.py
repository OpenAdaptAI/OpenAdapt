from typing import Tuple
import os

from loguru import logger
from PIL import Image, ImageDraw, ImageFont, ImageEnhance

from openadapt.config import config
from openadapt.drivers import anthropic, openai, google
from openadapt.utils import parse_code_snippet

# Constants
DOWNSAMPLE_FACTOR = 2
GRID_SIZE = 25  # Adjust based on desired grid size
DRIVER = openai

def load_and_downsample_image(image_file_path, downsample_factor: int):
    # Open the image and convert to RGB
    image = Image.open(image_file_path).convert("RGB")
    
    # Get the original dimensions
    original_width, original_height = image.size
    
    # Calculate new dimensions (half of the original)
    new_width = original_width // downsample_factor
    new_height = original_height // downsample_factor
    
    # Resize the image to half its original size
    downsampled_image = image.resize((new_width, new_height), Image.LANCZOS)
    
    return downsampled_image

def dim_image(image: Image.Image) -> Image.Image:
    enhancer = ImageEnhance.Brightness(image)
    return enhancer.enhance(0.5)  # Dim the image by reducing brightness to 50%

from PIL import Image, ImageDraw, ImageFont

def add_grid_labels(image: Image.Image, grid_size: int) -> Image.Image:
    width, height = image.size
    grid_image = Image.new('RGB', (width + 50, height + 50), 'red')
    grid_image.paste(image, (50, 50))
    draw = ImageDraw.Draw(grid_image)

    # Calculate the maximum font size that fits within the grid cell
    max_font_size = min((height // grid_size) // 2, (width // grid_size) // 2)
    font_size = max_font_size
    font = ImageFont.truetype("Arial.ttf", font_size)

    # Adjust font size dynamically to fit within grid cells
    while True:
        fits = True
        for i in range(grid_size):
            row_text_size = draw.textbbox((0, 0), str(i + 1), font=font)[2:]
            col_text_size = draw.textbbox((0, 0), str(i + 1), font=font)[2:]
            if row_text_size[1] > height // grid_size or col_text_size[0] > width // grid_size:
                fits = False
                break
        if fits:
            break
        font_size -= 1
        font = ImageFont.truetype("Arial.ttf", font_size)
        if font_size <= 0:
            raise ValueError("Cannot find a suitable font size that fits within the grid cells")

    cell_width = width / grid_size
    cell_height = height / grid_size

    # Add labels for rows and columns with the determined font size
    for i in range(grid_size):
        draw.text((25, 50 + int(i * cell_height + cell_height / 2) - font_size // 2), str(i + 1), fill='white', font=font)
        draw.text((50 + int(i * cell_width + cell_width / 2) - font_size // 2, 25), str(i + 1), fill='white', font=font)

    # Draw semi-transparent grid lines
    overlay = Image.new('RGBA', grid_image.size, (255, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    for i in range(grid_size + 1):
        # Horizontal lines
        overlay_draw.line([(50, 50 + int(i * cell_height)), (50 + width, 50 + int(i * cell_height))], fill=(255, 255, 255, 128), width=1)
        # Vertical lines
        overlay_draw.line([(50 + int(i * cell_width), 50), (50 + int(i * cell_width), 50 + height)], fill=(255, 255, 255, 128), width=1)

    # Composite the overlay with the labeled image
    grid_image = Image.alpha_composite(grid_image.convert('RGBA'), overlay)

    return grid_image.convert('RGB')

def get_cell_coordinates(grid_size: int, target_cell: Tuple[int, int], image_size: Tuple[int, int]) -> Tuple[int, int]:
    row, col = target_cell
    cell_width = image_size[0] / grid_size
    cell_height = image_size[1] / grid_size
    x = int((col - 0.5) * cell_width)
    y = int((row - 0.5) * cell_height)
    return x, y

def draw_target_coordinates(image: Image.Image, coordinates: Tuple[int, int]) -> Image.Image:
    image = image.copy()
    label_offset = 50  # The offset added to the top and left for labels
    draw = ImageDraw.Draw(image)
    adjusted_coordinates = (coordinates[0] + label_offset, coordinates[1] + label_offset)
    draw.ellipse((adjusted_coordinates[0] - 5, adjusted_coordinates[1] - 5, adjusted_coordinates[0] + 5, adjusted_coordinates[1] + 5), fill='red', outline='red')
    return image

def main(target: str):
    # Load and dim the image
    image_file_path = os.path.join(config.ROOT_DIR_PATH, "../tests/assets/excel.png")
    image = load_and_downsample_image(image_file_path, DOWNSAMPLE_FACTOR)
    image = dim_image(image)

    # Add grid labels
    grid_image = add_grid_labels(image, GRID_SIZE)
    grid_image.show()

    row, column = None, None
    grid_image_with_target = None
    while True:
        prompt = f"""
Attached is an image containing a screenshot over which a grid has been overlaid.
The grid labels are white (255, 255, 255) on a red (255, 0, 0) background. The grid lines
are semi-transparent.
Your task is to identify the grid row and column containing the target.
The target is: "{target}".
Respond in JSON with the following keys:
    {{
        "target": "<the target you are looking for>",
        "analysis": "<natural language description of the location of the target, adjacent elements, and anything else that is relevant to identifying the correct row and column>",
        "row": "<grid row number>",
        "column": "<column row number>",
    }}
Make sure not to confuse the grid labels with the content of the screenshot.
For example, if the screenshot contains a spreadsheet, don't get confused between
the spreadsheet cell labels and the grid labels.
Wrap your code in triple backticks: ```
"""
        if row and column:
            prompt += f"""
Previously, you specified row {row}, column {column}. This cell has been marked
with a red dot. If you agree with your previous assessment, confirm by specifying
the same row and column again. Otherwise, please correct your previous assessment.
"""
        # Prompt the model to identify the cell
        response = DRIVER.prompt(
            prompt=prompt,
            system_prompt="You are an expert GUI interpreter. You are precise and accurate.",
            images=[grid_image_with_target or grid_image],
        )
        result = parse_code_snippet(response)

        prev_row, prev_column = row, column
        row = int(result["row"])
        column = int(result["column"])
        logger.info(f"{row=} {column=}")
        coordinates = get_cell_coordinates(GRID_SIZE, (row, column), image.size)

        # Draw the coordinates on the image
        grid_image_with_target = draw_target_coordinates(grid_image, coordinates)

        # Show the final image
        grid_image_with_target.show()

        if row == prev_row and column == prev_column:
            break

if __name__ == "__main__":
    #main("save button")
    #main("cell A1")
    #main("cell containing 'Marketing'")
    #main("font selector")
    #main("zoom slider")
    #main("paste button")
    main("font size dropdown")
