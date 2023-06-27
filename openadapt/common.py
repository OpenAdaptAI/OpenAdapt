"""
openadapt.common module.

This module defines common constants and variables used in OpenAdapt.

Example usage:
    from openadapt.common import ALL_EVENTS

    for event in ALL_EVENTS:
        print(event)
"""

MOUSE_EVENTS = (
    # raw
    "move",
    "click",
    "scroll",
    # processed
    "doubleclick",
    "singleclick",
)
KEY_EVENTS = (
    # raw
    "press",
    "release",
    # processed
    "type",
)
ALL_EVENTS = tuple(list(MOUSE_EVENTS) + list(KEY_EVENTS))
