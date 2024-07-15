import AppKit
import ApplicationServices


def get_attribute(element, attribute):
    result, value = ApplicationServices.AXUIElementCopyAttributeValue(
        element, attribute, None
    )
    if result == 0:
        return value
    return None


def find_element_by_attribute(element, attribute, value):
    if get_attribute(element, attribute) == value:
        return element
    children = get_attribute(element, ApplicationServices.kAXChildrenAttribute) or []
    for child in children:
        found = find_element_by_attribute(child, attribute, value)
        if found:
            return found
    return None


def get_active_window():
    """Get the active window object.

    Returns:
        AXUIElement: The active window object.
    """
    workspace = AppKit.NSWorkspace.sharedWorkspace()
    active_app = workspace.frontmostApplication()
    app_element = ApplicationServices.AXUIElementCreateApplication(
        active_app.processIdentifier()
    )

    error_code, focused_window = ApplicationServices.AXUIElementCopyAttributeValue(
        app_element, ApplicationServices.kAXFocusedWindowAttribute, None
    )
    if error_code:
        raise Exception("Could not get the active window.")
    return focused_window


def get_element_value(element, role="AXStaticText"):
    """Get the value of a specific element .

    Args:
        element: The AXUIElement to search within.

    Returns:
        str: The value of the element, or an error message if not found.
    """
    target_element = find_element_by_attribute(
        element, ApplicationServices.kAXRoleAttribute, role
    )
    if not target_element:
        return f"AXStaticText element not found."

    value = get_attribute(target_element, ApplicationServices.kAXValueAttribute)
    return value if value else f"No value for AXStaticText element."
