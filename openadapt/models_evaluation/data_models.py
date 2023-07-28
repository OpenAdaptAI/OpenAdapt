from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel


class Window(BaseModel):
    title: str
    left: int
    top: int
    width: int
    height: int


class MouseActionType(Enum):
    click = "click"
    move = "move"


class MouseButton(Enum):
    left = "left"
    right = "right"


class KeyActionType(Enum):
    press = "press"
    release = "release"


class MouseAction(BaseModel):
    name: MouseActionType
    mouse_x: float
    mouse_y: float
    mouse_button_name: Optional[MouseButton]
    mouse_pressed: Optional[bool]
    element_state: Optional[dict[Any, Any]]


class KeyAction(BaseModel):
    name: KeyActionType
    key_name: Optional[str]
    element_state: Optional[dict[Any, Any]]


class ActionEvents(BaseModel):
    actions: list[Any]
