"""This module defines common constants used in OpenAdapt."""

RAW_MOUSE_EVENTS = (
    "move",
    "click",
    "scroll",
)
FUSED_MOUSE_EVENTS = (
    "singleclick",
    "doubleclick",
)
MOUSE_EVENTS = tuple(list(RAW_MOUSE_EVENTS) + list(FUSED_MOUSE_EVENTS))
MOUSE_CLICK_EVENTS = (event for event in MOUSE_EVENTS if "click" in event)

RAW_KEY_EVENTS = (
    "press",
    "release",
)
FUSED_KEY_EVENTS = ("type",)
KEY_EVENTS = tuple(list(RAW_KEY_EVENTS) + list(FUSED_KEY_EVENTS))

ALL_EVENTS = tuple(list(MOUSE_EVENTS) + list(KEY_EVENTS))
