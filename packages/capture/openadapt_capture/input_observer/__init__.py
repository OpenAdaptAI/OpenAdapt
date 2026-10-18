"""Permissively licensed native global input observation."""

from .base import (
    InputCallback,
    InputObserver,
    InputObserverError,
    InputObserverPermissionError,
    InputObserverUnavailableError,
    ObservedInput,
    ObservedKey,
    ObservedMouseButton,
    ObservedMouseMove,
    ObservedMouseScroll,
    ThreadedInputObserver,
    add_exception_note,
)
from .factory import create_input_observer

__all__ = [
    "InputCallback",
    "InputObserver",
    "InputObserverError",
    "InputObserverPermissionError",
    "InputObserverUnavailableError",
    "ObservedInput",
    "ObservedKey",
    "ObservedMouseButton",
    "ObservedMouseMove",
    "ObservedMouseScroll",
    "ThreadedInputObserver",
    "add_exception_note",
    "create_input_observer",
]
