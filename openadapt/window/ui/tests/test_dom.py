import pytest

from openadapt.window.ui.dom import (
    ActionableElements,
    DOMElement,
    DOMElementFactory,
    Frame,
    NonActionableElements,
    OSType,
)


def test_create_dom_element():
    title = "Button"
    role = ActionableElements.BUTTON
    role_description = "Button"
    text = "Click Me"
    frame_data = {"x": 0, "y": 0, "width": 100, "height": 30}
    frame = Frame(**frame_data)
    element = DOMElement(title, role, role_description, text, frame)

    assert element is not None
    assert element.title == title
    assert element.role == ActionableElements.BUTTON
    assert element.role_description == role_description
    assert element.text == text
    assert element.frame == frame
    assert element.children == []


def test_add_child_element():
    parent_element = DOMElement(
        "Parent",
        ActionableElements.BUTTON,
        "Button",
        "Click Me",
        Frame(x=0, y=0, width=100, height=30),
    )
    child_element = DOMElement(
        "Child", "AXLink", "Link", "Visit", Frame(x=10, y=10, width=50, height=20)
    )

    parent_element.add_child(child_element)
    assert len(parent_element.children) == 1
    assert parent_element.children[0] == child_element


def test_remove_child_element():
    parent_element = DOMElement(
        "Parent",
        "AXButton",
        "Button",
        "Click Me",
        Frame(x=0, y=0, width=100, height=30),
    )
    child_element = DOMElement(
        "Child", "AXLink", "Link", "Visit", Frame(x=10, y=10, width=50, height=20)
    )

    parent_element.add_child(child_element)
    parent_element.remove_child(child_element)
    assert len(parent_element.children) == 0


def test_find_actionable_elements():
    parent_element = DOMElement(
        "Parent",
        "AXButton",
        "Button",
        "Click Me",
        Frame(x=0, y=0, width=100, height=30),
    )
    child_element1 = DOMElement(
        "Child1", "AXLink", "Link", "Visit", Frame(x=10, y=10, width=50, height=20)
    )
    child_element2 = DOMElement(
        "Child2", "AXButton", "Button", "Submit", Frame(x=20, y=20, width=50, height=20)
    )

    parent_element.add_child(child_element1)
    parent_element.add_child(child_element2)

    point_inside_child1 = (15, 15)
    point_inside_child2 = (25, 25)
    point_outside_elements = (200, 200)

    actionable_elements1 = DOMElement.find_actionable_elements(
        parent_element, point_inside_child1
    )
    actionable_elements2 = DOMElement.find_actionable_elements(
        parent_element, point_inside_child2
    )
    actionable_elements_outside = DOMElement.find_actionable_elements(
        parent_element, point_outside_elements
    )

    assert len(actionable_elements1) == 1
    assert actionable_elements1[0] == child_element1

    assert len(actionable_elements2) == 1
    assert actionable_elements2[0] == child_element2

    assert len(actionable_elements_outside) == 0


@pytest.mark.parametrize(
    "os_type, title, role, role_description, text, frame_data, expected_role,"
    " expected_exception",
    [
        (
            OSType.MACOS,
            "Button title",
            "AXButton",
            "Button",
            "Click Me",
            {"x": 0, "y": 0, "width": 100, "height": 30},
            ActionableElements.BUTTON,
            None,
        ),
        (
            OSType.MACOS,
            "Image title",
            "AXImage",
            "Image",
            "Image Text",
            {"x": 0, "y": 0, "width": 80, "height": 60},
            NonActionableElements.IMAGE,
            None,
        ),
        (
            OSType.WINDOWS,
            "Button title",
            "AXButton",
            "Button",
            "Click Me",
            {"x": 0, "y": 0, "width": 100, "height": 30},
            ActionableElements.BUTTON,
            NotImplementedError,
        ),
    ],
)
def test_generate_element(
    os_type,
    title,
    role,
    role_description,
    text,
    frame_data,
    expected_role,
    expected_exception,
):
    if expected_exception:
        with pytest.raises(expected_exception):
            element = DOMElementFactory.generate_element(
                title, role, role_description, text, frame_data, os_type
            )
    else:
        element = DOMElementFactory.generate_element(
            title, role, role_description, text, frame_data, os_type
        )
        assert element is not None
        assert element.title == title
        assert element.role == expected_role
        assert element.role_description == role_description
        assert element.text == text
        assert element.frame == Frame(**frame_data)
        assert element.children == []


@pytest.mark.parametrize(
    "os_type, title, role, role_description, text, frame_data",
    [
        (
            OSType.MACOS,
            "Unknown title",
            "AXUnknown",
            "Unknown",
            "Unknown Text",
            {"x": 0, "y": 0, "width": 80, "height": 60},
        ),
    ],
)
def test_generate_unknown_element(
    os_type, title, role, role_description, text, frame_data
):
    element = DOMElementFactory.generate_element(
        title, role, role_description, text, frame_data, os_type
    )

    assert element is None
