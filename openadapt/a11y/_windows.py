from loguru import logger
import pywinauto
import re


def get_active_window() -> pywinauto.application.WindowSpecification:
    """Get the active window object.

    Returns:
        pywinauto.application.WindowSpecification: The active window object.
    """
    app = pywinauto.application.Application(backend="uia").connect(active_only=True)
    window = app.top_window()
    return window.wrapper_object()


def get_element_value(active_window, role="Text"):
    """Find the display element.

    Args:
        active_window: The parent window to search within.
        role (str): The role of the element to search for.

    Returns:
        The found display element value.

    Raises:
        ValueError: If the element is not found.
    """
    try:
        elements = active_window.descendants()  # Retrieve all descendants
        for elem in elements:
            if (
                elem.element_info.control_type == role
                and elem.element_info.name.startswith("Display is")
            ):
                # Extract the number from the element's name
                match = re.search(r"[-+]?\d*\.?\d+", elem.element_info.name)
                if match:
                    return str(match.group())
        raise ValueError("Display element not found")
    except Exception as exc:
        logger.warning(f"Error in get_element_value: {exc}")
        return None
