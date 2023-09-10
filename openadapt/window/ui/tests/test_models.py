import pytest

from openadapt.window.ui.models import (
    ActionableElements,
    ElementGenerator,
    Frame,
    NonActionableElements,
    OSType,
)


def test_generate_macos_button_element():
    role = "AXButton"
    title = "Click Me"
    frame = Frame(x=0, y=0, width=100, height=30)
    os_type = OSType.MACOS

    element = ElementGenerator.generate_element(role, title, frame, os_type)

    assert element is not None
    assert element.role == ActionableElements.BUTTON
    assert element.title == title
    assert element.actionable is True
    assert element.frame == frame
    assert element.children == []


def test_generate_macos_non_actionable_element():
    role = "AXImage"
    title = "Image Element"
    frame = Frame(x=10, y=20, width=50, height=50)
    os_type = OSType.MACOS

    element = ElementGenerator.generate_element(role, title, frame, os_type)

    assert element is not None
    assert element.title == title
    assert element.actionable is False
    assert element.frame == frame
    assert element.role == NonActionableElements.IMAGE
    assert element.children == []


def test_generate_macos_unknown_element():
    role = "AXUnknown"
    title = "Unknown Element"
    frame = Frame(x=10, y=20, width=50, height=50)
    os_type = OSType.MACOS

    element = ElementGenerator.generate_element(role, title, frame, os_type)

    assert element is None


def test_generate_element_unknown_os_type():
    role = "AXButton"
    title = "Click Me"
    frame = Frame(x=0, y=0, width=100, height=30)
    os_type = OSType.WINDOWS  # An unknown OS type
    # assert and expect raise NotImplementedError use pytest
    with pytest.raises(NotImplementedError):
        ElementGenerator.generate_element(role, title, frame, os_type)
