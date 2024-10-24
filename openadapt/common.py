"""This module defines common constants used in OpenAdapt."""

RAW_PRECISE_MOUSE_EVENTS = (
  "move",
  "click",
)
# location of cursor doesn't matter as much when scrolling compared to moving/clicking
RAW_IMPRECISE_MOUSE_EVENTS = (
  "scroll",
)
RAW_MOUSE_EVENTS = (
    tuple(list(RAW_PRECISE_MOUSE_EVENTS) + list(RAW_IMPRECISE_MOUSE_EVENTS))
)
FUSED_MOUSE_EVENTS = (
    "singleclick",
    "doubleclick",
)
MOUSE_EVENTS = tuple(list(RAW_MOUSE_EVENTS) + list(FUSED_MOUSE_EVENTS))
MOUSE_CLICK_EVENTS = (event for event in MOUSE_EVENTS if event.endswith("click"))
PRECISE_MOUSE_EVENTS = (
    tuple(list(RAW_PRECISE_MOUSE_EVENTS) + list(FUSED_MOUSE_EVENTS))
)

RAW_KEY_EVENTS = (
    "press",
    "release",
)
FUSED_KEY_EVENTS = ("type",)
KEY_EVENTS = tuple(list(RAW_KEY_EVENTS) + list(FUSED_KEY_EVENTS))

ALL_EVENTS = tuple(list(MOUSE_EVENTS) + list(KEY_EVENTS))
