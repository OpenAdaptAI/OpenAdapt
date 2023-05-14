from pprint import pformat
from threading import Timer
import html
import os
import string

from bokeh.io import output_file, show
from bokeh.layouts import layout, row
from bokeh.models.widgets import Div
from loguru import logger

from puterbot.crud import (
    get_latest_recording
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

#Global variables & Formatting:
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

#computes the total number of elements in a nested list of dictionaries based on a given key.
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

# converts a Python dictionary to an HTML
'''If obj is a list it converts each element of the list to an HTML string using recursion, 
 and concatenates the resulting strings with line breaks
    If obj is a dict the function iterates over each key-value pair in the dictionary and 
creates a table row for each non-empty value
    If neither function converts it to a string and escapes any HTML special characters.
 '''
def dict2html(obj, max_children=5):
    if isinstance(obj, list):
        children = [dict2html(value) for value in obj]
        if max_children is not None and len(children) > max_children:
            all_children = children
            children = evenly_spaced(children, max_children)
            children = indicate_missing(children, all_children, "...")
        html_str = "\n".join(children)
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


def main():
    configure_logging(logger, LOG_LEVEL)

    recording = get_latest_recording()
    logger.debug(f"{recording=}")

    meta = {} #a dictionary object that contains metadata about the recording (start and end times, the task description...)
    input_events = get_events(recording, process=PROCESS_EVENTS, meta=meta)
    event_dicts = rows2dicts(input_events)
    logger.info(f"event_dicts=\n{pformat(event_dicts)}")

    #TOP OF THE PAGE
    rows = [
        row(
            Div(
                text=f"<style>{CSS}</style>",
            ),
        ),
        row(
            Div(
                text=f"{dict2html(row2dict(recording))}",
            ),
        ),
        row(
            Div(
                text=f"{dict2html(meta)}",
                width_policy="max",
            ),
        )
    ]

    logger.info(f"{len(input_events)=}")
    #every parent information depicted
    for idx, input_event in enumerate(input_events):
        if idx == MAX_EVENTS:
            break
        image = display_event(input_event)
        diff = display_event(input_event, diff=True)
        mask = input_event.screenshot.diff_mask
        image_utf8 = image2utf8(image)
        diff_utf8 = image2utf8(diff)
        mask_utf8 = image2utf8(mask)
        width, height = image.size
        rows.append([
            row(
                Div(
                    text=f"""
                        <div class="screenshot">
                            <img
                                src="{image_utf8}"
                                style="
                                    aspect-ratio: {width}/{height};
                                "
                            >
                            <img
                                src="{diff_utf8}"
                                style="
                                    aspect-ratio: {width}/{height};
                                "
                            >
                            <img
                                src="{mask_utf8}"
                                style="
                                    aspect-ratio: {width}/{height};
                                "
                            >
                        </div>
                    """,
                ),
                Div(
                    text=f"""
                        <table>
                            {dict2html(row2dict(input_event))}
                        </table>
                    """
                ),
            ),
        ])

    title = f"recording-{recording.timestamp}"
    fname_out = f"recording-{recording.timestamp}.html"
    logger.info(f"{fname_out=}")
    output_file(fname_out, title=title)

    result = show(
        layout(
            rows,
        )
    )


    def cleanup():
        os.remove(fname_out)
        removed = not os.path.exists(fname_out)
        logger.info(f"{removed=}")


    Timer(1, cleanup).start()


if __name__ == "__main__":
    main()
