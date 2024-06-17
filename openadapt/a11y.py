from typing import Any, Union
from loguru import logger
import AppKit
import ApplicationServices

def get_attribute(element, attribute):
    result, value = ApplicationServices.AXUIElementCopyAttributeValue(element, attribute, None)
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

def find_application(app_name: str):
    """Find an application by its name and return its accessibility element.

    Args:
        app_name (str): The name of the application to find.

    Returns:
        AXUIElement or None: The AXUIElement representing the application,
        or None if the application is not running.
    """
    workspace = AppKit.NSWorkspace.sharedWorkspace()
    running_apps = workspace.runningApplications()
    app = next((app for app in running_apps if app.localizedName() == app_name), None)
    if app is None:
        logger.error(f"{app_name} application is not running.")
        return None

    app_element = ApplicationServices.AXUIElementCreateApplication(app.processIdentifier())
    return app_element

def get_main_window(app_element):
    """Get the main window of an application.

    Args:
        app_element: The AXUIElement of the application.

    Returns:
        AXUIElement or None: The AXUIElement representing the main window,
        or None if no windows are found.
    """
    error_code, windows = ApplicationServices.AXUIElementCopyAttributeValue(app_element, ApplicationServices.kAXWindowsAttribute, None)
    if error_code or not windows:
        return None

    return windows[0] if windows else None

def get_element_value(element, role: str):
    """Get the value of a specific element by its role.

    Args:
        element: The AXUIElement to search within.
        role (str): The role of the element to find (e.g., "AXStaticText").

    Returns:
        str: The value of the element, or an error message if not found.
    """
    target_element = find_element_by_attribute(element, ApplicationServices.kAXRoleAttribute, role)
    if not target_element:
        return f"{role} element not found."

    value = get_attribute(target_element, ApplicationServices.kAXValueAttribute)
    return value if value else f"No value for {role} element."
