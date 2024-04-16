"""This module defines common constants and variables used in OpenAdapt."""

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
