import html
import requests
import os
import string
import PIL.Image
import PIL.ImageSequence
import imageio
import tempfile
import moviepy.editor as mp
import numpy as np
import google.auth
import time
import io
import base64
import tempfile
import google.auth
import google_auth_oauthlib.flow
import json
import shutil



from googleapiclient.errors import HttpError
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from nicegui import ui
from moviepy.video.io.VideoFileClip import VideoFileClip

from typing import List
from io import BytesIO
from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from IPython.display import HTML
from pprint import pformat
from threading import Timer
from PIL import Image, ImageTk
from bs4 import BeautifulSoup
from nicegui import ui
from bokeh.io import output_file, show
from bokeh.layouts import layout, row
from bokeh.models.widgets import Div
from loguru import logger

from puterbot.crud import (
    get_latest_recording,
    get_screenshots
)
from puterbot.events import (
    get_events,
)
from puterbot.utils import (
    configure_logging,
    display_event,
    evenly_spaced,
    image2utf8,
    EMPTY,
    row2dict,
    rows2dicts,
)
total_count = 0
LOG_LEVEL = "INFO"
MAX_EVENTS = None
PROCESS_EVENTS = True
IMG_WIDTH_PCT = 60
CSS = string.Template("""
    table {
        outline: 1px solid black;
    }
    table th {
        vertical-align: top;
    }
    .screenshot img {
        display: none;
        width: ${IMG_WIDTH_PCT}vw;
    }
    .screenshot img:nth-child(1) {
        display: block;
    }

    .screenshot:hover img:nth-child(1) {
        display: none;
    }
    .screenshot:hover img:nth-child(2) {
        display: block;
    }
    .screenshot:hover img:nth-child(3) {
        display: none;
    }

    .screenshot:active img:nth-child(1) {
        display: none;
    }
    .screenshot:active img:nth-child(2) {
        display: none;
    }
    .screenshot:active img:nth-child(3) {
        display: block;
    }
""").substitute(
    IMG_WIDTH_PCT=IMG_WIDTH_PCT,
)

def recursive_len(lst, key):
    _len = len(lst)
    for obj in lst:
        _len += recursive_len(obj[key], key)
    return _len

# If the value is a list, it adds information about the length of the list and the total number of elements in the nested lists. 
# If the value is not a list, it simply returns the key.
def format_key(key, value):
    if isinstance(value, list):
        return f"{key} ({len(value)}; {recursive_len(value, key)})"
    else:
        return key

#The function returns a new list that contains all the elements of some in the same order they appear in 'some', 
# but with 'indicator' inserted wherever an element is missing from 'every'.
def indicate_missing(some, every, indicator):
    rval = []
    some_idx = 0
    every_idx = 0
    while some_idx < len(some):
        skipped = False
        while some[some_idx] != every[every_idx]:
            every_idx += 1
            skipped = True
        if skipped:
            rval.append(indicator)
        rval.append(some[some_idx])
        some_idx += 1
        every_idx += 1
    return rval

def dict2html(obj, max_children=5):
   if isinstance(obj, list):
       children = [dict2html(value) for value in obj]
       if max_children is not None and len(children) > max_children:
           all_children = children
           children = evenly_spaced(children, max_children)
           children = indicate_missing(children, all_children, "...")
       html_str = "\n".join([f'<div style="border: 1px solid black; padding: 5px;">{child_html}</div>' if isinstance(child_obj, dict) else child_html for child_obj, child_html in zip(obj, children)])
   elif isinstance(obj, dict):
       rows_html = "\n".join([
           f"""
               <tr>
                   <th>{format_key(key, value)}</th>
                   <td>{dict2html(value)}</td>
               </tr>
           """
           for key, value in obj.items()
           if value not in EMPTY
       ])
       html_str = f"<table>{rows_html}</table>"
   else:
       html_str = html.escape(str(obj))
   return html_str

def html2table(html):
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.table

    headers = []
    rows = []
    for row in table.find_all('tr'):
        header = row.find('th')
        if header:
            headers.append(header.text)
    if headers:
        for row in table.find_all('tr')[1:]:
            row_values = {}
            for header, value in zip(headers, row.find_all('td')):
                row_values[header] = value.text
            rows.append(row_values)

    return headers, rows

def display_screenshots(screenshots):
    for screenshot in screenshots:
        image_utf8 = image2utf8(screenshot.image)
        ui.html(f'<img src="{image_utf8}"/>')

def screenshot2array(screenshot):
    # Convert the screenshot to a numpy array
    array = np.array(screenshot)

    # If the array has only one channel (i.e. grayscale), convert it to three channels (i.e. RGB)
    if len(array.shape) == 2:
        array = np.stack((array,)*3, axis=-1)

    return array

def create_video_from_images(images, output_dir=None):
    global video_file
    if output_dir is None:
        # Set a default output directory
        output_dir = os.path.join(os.getcwd(), "videos")
        os.makedirs(output_dir, exist_ok=True)
    else:
        # Create the output directory if it doesn't exist
        os.makedirs(output_dir, exist_ok=True)

    # Save each image in the output directory
    image_files = []
    for i, image in enumerate(images):
        image_file = os.path.join(output_dir, f"image{i}.png")
        image.save(image_file, format="PNG")
        image_files.append(image_file)

    # Create the video file from the image files using moviepy
    clip = mp.ImageSequenceClip(image_files, fps=30)
    video_file = os.path.join(output_dir, "output.mp4")
    clip.write_videofile(video_file, codec="libx264")

    # Check if the video file exists
    if os.path.exists(video_file):
        return video_file
    else:
        raise ValueError("Video file does not exist!")

def video_to_html(video_file_path):
    video_data = open(video_file_path, "rb").read()
    video_base64 = base64.b64encode(video_data).decode("utf-8")
    html = f'<video controls loop><source src="data:video/mp4;base64,{video_base64}" type="video/mp4"></video>'
    return html

def dict2html_table(d):
    html = '<table>'
    for key, value in d.items():
        html += f'<tr><td>{key}</td><td>{value}</td></tr>'
    html += '</table>'
    return html

def remove_files(file_list):
    for file in file_list:
        os.remove(file)

#getting the photos
recording = get_latest_recording()
screenshots = get_screenshots(recording)
image_list = [screenshot.image for screenshot in screenshots]

#setting the rest
logger.debug(f"{recording=}")

meta = {} #a dictionary object that contains metadata about the recording (start and end times, the task description...)
input_events = get_events(recording, process=PROCESS_EVENTS, meta=meta)
event_dicts = rows2dicts(input_events)
logger.info(f"event_dicts=\n{pformat(event_dicts)}")

topDict = dict2html(row2dict(recording))
#titles, values = html2table(topDict)

title_html = '<h1 style="text-align:right; font-weight:bold; font-size:50px;width: 150%;">Visualization By MLDSAI</h1>'
#ui.html(title_html)

# get the image from Google Drive
url = 'https://drive.google.com/uc?id=1DU0i2VeEyDcLHyvQ6xboUSSRvroKg042'
response = requests.get(url)
img = Image.open(BytesIO(response.content))

# display the image in a NiceGUI window
#ui.image(img)
with ui.row():
    ui.html('<img src="' + url + '" width="200" height="150" style="margin-right: 50px;">')
    ui.html(title_html)




#Top Info
box_style = "display: inline-block; border: 1px solid gray; padding: 10px; margin: 10px;"
topDict_html = f'<div style="{box_style}">{topDict}</div>'
meta_html = f'<div style="{box_style}">{dict2html(meta)}</div>'
#video of everyhing
path2video = create_video_from_images(image_list)
videohtml = video_to_html(path2video)


ui.html(f'<div style="display: flex; justify-content: center; align-items: center; width: 100%;">'
        #f'<div style="display: flex; align-items: center;">{title_html}</div>'
        f'<div style="display: flex; flex-direction: column; margin-right: 30px;">'
        f'<div><b>General Overview:</b></div>'
        f'    <div style="margin-bottom: 30px;">{topDict_html}</div>'
        f'    <div>{meta_html}</div>'
        f'</div>'
        f'<div style="margin-left: 30px;">{videohtml}</div>'
        f'</div>')

unique_timestamps = []
for idx, input_event in enumerate(input_events):
        #for the information
        if idx == MAX_EVENTS:
            break
        html_str = dict2html(row2dict(input_event))
        info_html = f'<div style="{box_style}"><table>{dict2html(row2dict(input_event))}</table></div>'
        #ui.html(info_html)

        #for the video
        my_dict = row2dict(input_event)
        children_value = my_dict["children"]
        screenshot_timestamps = [child_dict.get("screenshot_timestamp") for child_dict in children_value if "screenshot_timestamp" in child_dict]
        unique_screenshot_timestamps = list(set(screenshot_timestamps))
        unique_timestamps.extend(unique_screenshot_timestamps)
        unique_timestamps = sorted(list(set(unique_timestamps)))
        min_timestamp = min(unique_screenshot_timestamps)
        max_timestamp = max(unique_screenshot_timestamps)
        min_position = unique_timestamps.index(min_timestamp)
        max_position = unique_timestamps.index(max_timestamp)
        new_image_list = image_list[min_position:max_position+1]
        path2subvideo = create_video_from_images(new_image_list)
        subvideohtml = video_to_html(path2subvideo)
        #ui.html(subvideohtml)
        ui.html(f'<div style="display: flex; justify-content: center; align-items: center; width: 100%;">'
        f'<div style="display: flex; flex-direction: column; margin-right: 15px; flex: 1;">{info_html}</div>'
        f'<div style="margin-left: 30px; flex: 1;">{subvideohtml}</div>'
        f'</div>')

ui.run(title='Visualize')






