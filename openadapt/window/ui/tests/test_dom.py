import pytest

from openadapt.window.ui.dom import (
    ActionableElements,
    DOMElement,
    DOMElementFactory,
    Frame,
    NonActionableElements,
    OSType,
)


@pytest.fixture
def sample_dom_dict_with_children():
    return {
        "AXTitle": "Root Element",
        "AXRole": "AXGroup",
        "AXRoleDescription": "A group element",
        "AXValue": "",
        "AXFrame": {"x": 0, "y": 0, "width": 100, "height": 100},
        "AXChildrenInNavigationOrder": [
            {
                "AXTitle": "Child Element 1",
                "AXRole": "AXButton",
                "AXRoleDescription": "A button element",
                "AXValue": "Click me",
                "AXFrame": {"x": 10, "y": 10, "width": 80, "height": 30},
                "AXChildren": [],
            },
            {
                "AXTitle": "Child Element 2",
                "AXRole": "AXTextField",
                "AXRoleDescription": "A text field element",
                "AXValue": "Input text",
                "AXFrame": {"x": 10, "y": 50, "width": 80, "height": 30},
                "AXChildren": [],
            },
        ],
    }


@pytest.fixture
def sample_dom_dict_no_children():
    return {
        "AXTitle": "Single Element",
        "AXRole": "AXButton",
        "AXRoleDescription": "A button element",
        "AXValue": "Click me",
        "AXFrame": {"x": 10, "y": 10, "width": 80, "height": 30},
    }


@pytest.fixture
def sample_dom_dict_with_multiple_layers():
    return {
        "AXTitle": "Root Element",
        "AXRole": "AXGroup",
        "AXRoleDescription": "A group element",
        "AXValue": "",
        "AXFrame": {"x": 0, "y": 0, "width": 100, "height": 100},
        "AXChildrenInNavigationOrder": [
            {
                "AXTitle": "Child Element 1",
                "AXRole": "AXButton",
                "AXRoleDescription": "A button element",
                "AXValue": "Click me",
                "AXFrame": {"x": 10, "y": 10, "width": 80, "height": 30},
                "AXChildren": [
                    {
                        "AXTitle": "Grandchild 1",
                        "AXRole": "AXTextField",
                        "AXRoleDescription": "A text field element",
                        "AXValue": "Input text",
                        "AXFrame": {"x": 10, "y": 50, "width": 80, "height": 30},
                        "AXChildren": [],  # Add children if needed
                    },
                ],
            },
            {
                "AXTitle": "Child Element 2",
                "AXRole": "AXTextField",
                "AXRoleDescription": "A text field element",
                "AXValue": "Input text",
                "AXFrame": {"x": 10, "y": 50, "width": 80, "height": 30},
                "AXChildren": [],  # Add children if needed
            },
        ],
    }


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
        ActionableElements.BUTTON,
        "Button",
        "Click Me",
        Frame(x=0, y=0, width=100, height=30),
    )

    child_element1 = DOMElement(
        "Child1",
        ActionableElements.LINK,
        "Link",
        "Visit",
        Frame(x=10, y=10, width=50, height=20),
    )

    child_element2 = DOMElement(
        "Child2",
        ActionableElements.TEXT_AREA,
        "Text",
        "Submit",
        Frame(x=20, y=20, width=50, height=20),
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

    assert len(actionable_elements2) == 2
    assert actionable_elements2[1] == child_element2

    assert len(actionable_elements_outside) == 0


def test_find_actionable_elements_ignore_nonactionable_items():
    parent_element = DOMElement(
        "Parent",
        ActionableElements.BUTTON,
        "Button",
        "Click Me",
        Frame(x=0, y=0, width=100, height=30),
    )

    child_element1 = DOMElement(
        "Child1",
        NonActionableElements.IMAGE,
        "Image",
        "Image",
        Frame(x=20, y=20, width=50, height=20),
    )

    child_element2 = DOMElement(
        "Child2",
        ActionableElements.TEXT_AREA,
        "Text",
        "Submit",
        Frame(x=20, y=20, width=50, height=20),
    )

    parent_element.add_child(child_element1)
    parent_element.add_child(child_element2)

    point_inside_child1 = (15, 15)
    point_inside_child2 = (25, 25)

    actionable_elements1 = DOMElement.find_actionable_elements(
        parent_element, point_inside_child1
    )
    actionable_elements2 = DOMElement.find_actionable_elements(
        parent_element, point_inside_child2
    )

    assert len(actionable_elements1) == 0

    assert len(actionable_elements2) == 1
    assert actionable_elements2[0] == child_element2


def test_find_actionable_elements_in_complex_dom():
    """create multiple levels of DOM elements and test if the correct"""
    parent_element = DOMElement(
        "Parent",
        ActionableElements.BUTTON,
        "Button",
        "Click Me",
        Frame(x=0, y=0, width=100, height=30),
    )

    child_element = DOMElement(
        "Child",
        ActionableElements.LINK,
        "Link",
        "Visit",
        Frame(x=10, y=10, width=50, height=20),
    )

    parent_element.add_child(child_element)

    grandchild_element = DOMElement(
        "Grandchild",
        ActionableElements.TEXT_AREA,
        "Text",
        "Submit",
        Frame(x=20, y=20, width=50, height=20),
    )

    child_element.add_child(grandchild_element)

    point_inside_grandchild = (25, 25)
    point_inside_child = (15, 15)
    point_outside_elements = (200, 200)

    actionable_elements1 = DOMElement.find_actionable_elements(
        parent_element, point_inside_grandchild
    )
    actionable_elements2 = DOMElement.find_actionable_elements(
        parent_element, point_inside_child
    )
    actionable_elements_outside = DOMElement.find_actionable_elements(
        parent_element, point_outside_elements
    )

    assert len(actionable_elements1) == 2
    assert actionable_elements1[1] == grandchild_element

    assert len(actionable_elements2) == 1
    assert actionable_elements2[0] == child_element

    assert len(actionable_elements_outside) == 0


@pytest.mark.parametrize(
    (
        "os_type, title, role, role_description, text, frame_data, expected_role,"
        " expected_exception"
    ),
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


def test_generate_element_from_dict_with_children(sample_dom_dict_with_children):
    os_type = OSType.MACOS

    root_element = DOMElementFactory.generate_element_from_dict(
        sample_dom_dict_with_children, os_type
    )

    assert isinstance(root_element, DOMElement)
    assert root_element.title == "Root Element"
    assert root_element.role == NonActionableElements.GROUP
    assert root_element.text == ""
    assert root_element.frame == {"x": 0, "y": 0, "width": 100, "height": 100}
    assert len(root_element.children) == 2

    child1 = root_element.children[0]
    child2 = root_element.children[1]

    assert isinstance(child1, DOMElement)
    assert child1.title == "Child Element 1"
    assert child1.role == ActionableElements.BUTTON
    assert child1.text == "Click me"
    assert child1.frame == {"x": 10, "y": 10, "width": 80, "height": 30}
    assert len(child1.children) == 0

    assert isinstance(child2, DOMElement)
    assert child2.title == "Child Element 2"
    assert child2.role == ActionableElements.TEXT_FIELD
    assert child2.text == "Input text"
    assert child2.frame == {"x": 10, "y": 50, "width": 80, "height": 30}
    assert len(child2.children) == 0


def test_generate_element_from_dict_no_children(sample_dom_dict_no_children):
    os_type = OSType.MACOS

    element = DOMElementFactory.generate_element_from_dict(
        sample_dom_dict_no_children, os_type
    )

    assert isinstance(element, DOMElement)
    assert element.title == "Single Element"
    assert element.role == ActionableElements.BUTTON
    assert element.text == "Click me"
    assert element.frame == Frame(x=10, y=10, width=80, height=30)
    assert len(element.children) == 0


def test_generate_element_from_dict_with_multiple_layers(
    sample_dom_dict_with_multiple_layers,
):
    os_type = OSType.MACOS

    root_element = DOMElementFactory.generate_element_from_dict(
        sample_dom_dict_with_multiple_layers, os_type
    )

    assert isinstance(root_element, DOMElement)
    assert root_element.title == "Root Element"
    assert root_element.role == NonActionableElements.GROUP
    assert root_element.text == ""
    assert root_element.frame == {"x": 0, "y": 0, "width": 100, "height": 100}
    assert len(root_element.children) == 2

    child1 = root_element.children[0]
    child2 = root_element.children[1]

    assert isinstance(child1, DOMElement)
    assert child1.title == "Child Element 1"
    assert child1.role == ActionableElements.BUTTON
    assert child1.text == "Click me"
    assert child1.frame == {"x": 10, "y": 10, "width": 80, "height": 30}
    assert len(child1.children) == 1

    grandchild1 = child1.children[0]

    assert isinstance(grandchild1, DOMElement)
    assert grandchild1.title == "Grandchild 1"
    assert grandchild1.role == ActionableElements.TEXT_FIELD
    assert grandchild1.text == "Input text"
    assert grandchild1.frame == {"x": 10, "y": 50, "width": 80, "height": 30}
    assert len(grandchild1.children) == 0

    assert isinstance(child2, DOMElement)
    assert child2.title == "Child Element 2"
    assert child2.role == ActionableElements.TEXT_FIELD
    assert child2.text == "Input text"
    assert child2.frame == {"x": 10, "y": 50, "width": 80, "height": 30}
    assert len(child2.children) == 0
