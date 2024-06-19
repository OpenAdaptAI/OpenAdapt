from enum import Enum
from typing import Any, Iterable, List, Optional, Union

from pydantic import BaseModel


class OSType(Enum):
    MACOS = "macos"
    WINDOWS = "windows"


class ActionableElements(Enum):
    BUTTON = "Button"
    LINK = "Link"
    CHECKBOX = "Checkbox"
    RADIO_BUTTON = "RadioButton"
    TEXT_FIELD = "TextField"
    DROPDOWN_MENU = "DropdownMenu"
    SLIDER = "Slider"
    TAB = "Tab"
    DATE_PICKER = "DatePicker"
    TIME_PICKER = "TimePicker"
    STEPPER = "Stepper"
    MENU = "Menu"
    CONTEXTUAL_MENU = "ContextualMenu"
    DRAG_AND_DROP = "DragAndDrop"
    PROGRESS_BAR = "ProgressBar"
    CUSTOM_CONTROL = "CustomControl"
    ACTION_SHEET = "ActionSheet"
    SEGMENTED_CONTROL = "SegmentedControl"
    TEXT_AREA = "TextArea"
    ICON = "Icon"
    NAVIGATION_BAR = "NavigationBar"
    DIALOG_BUTTON = "DialogButton"


class NonActionableElements(Enum):
    IMAGE = "Image"
    LABEL = "Label"
    STATIC_TEXT = "StaticText"
    TABLE = "Table"
    TEXT_FIELD_WITH_PLACEHOLDER = "TextFieldWithPlaceholder"
    GROUP = "Group"
    CELL = "Cell"
    CELL_GROUP = "CellGroup"
    SWITCH = "Switch"
    SCROLL_AREA = "ScrollArea"
    MENU_BAR = "MenuBar"
    TOOL_BAR = "ToolBar"
    MENU_ITEM = "MenuItem"
    POP_UP_BUTTON = "PopUpButton"
    ROW = "Row"
    COLUMN = "Column"
    PROGRESS_INDICATOR = "ProgressIndicator"
    VALUE_INDICATOR = "ValueIndicator"


class Frame(BaseModel):
    x: int
    y: int
    width: int
    height: int


class DOMElement:
    """DOMElement class for storing information about an element on the screen"""

    def __init__(
        self,
        title: str,
        role: Union[ActionableElements, NonActionableElements],
        role_description: str,
        text: str,
        frame: Frame,
        children: List["DOMElement"] = None,
    ):
        self.title = title
        self.tag_name = role
        self.role = role
        self.role_description = role_description
        self.text = text
        self.frame = frame
        self.children = children or []

    def __str__(self):
        return f"<{self.role_description}>"

    def __repr__(self):
        return self.__str__()

    def add_child(self, child: "DOMElement"):
        self.children.append(child)

    def remove_child(self, child: "DOMElement"):
        if child in self.children:
            self.children.remove(child)

    @classmethod
    def find_actionable_elements(
        cls, element: "DOMElement", point: (int, int)
    ) -> Iterable["DOMElement"]:
        """Find actionable elements at a given point in the DOM tree

        Args:
            element (DOMElement): Root element to search from
            point (int, int): Point to search from

        Returns:
            Iterable[DOMElement]: List of actionable elements at the given point. The lowest element in the DOM tree
            will be returned last.
        """
        actionable_elements = []
        queue = element.children.copy()
        while queue:
            current_element = queue.pop(0)
            if (
                current_element.frame.x
                <= point[0]
                <= current_element.frame.x + current_element.frame.width
                and current_element.frame.y
                <= point[1]
                <= current_element.frame.y + current_element.frame.height
            ):
                if isinstance(current_element.tag_name, ActionableElements):
                    actionable_elements.append(current_element)
            queue.extend(current_element.children)
        return actionable_elements


class DOMElementFactory:
    """Factory class for generating DOMElements from dicts
    It should support both macOS and Windows dicts
    """

    macos_mapping = {
        "AXButton": ActionableElements.BUTTON,
        "AXLink": ActionableElements.LINK,
        "AXCheckbox": ActionableElements.CHECKBOX,
        "AXRadioButton": ActionableElements.RADIO_BUTTON,
        "AXTextField": ActionableElements.TEXT_FIELD,
        "AXDropdownMenu": ActionableElements.DROPDOWN_MENU,
        "AXSlider": ActionableElements.SLIDER,
        "AXTab": ActionableElements.TAB,
        "AXDatePicker": ActionableElements.DATE_PICKER,
        "AXTimePicker": ActionableElements.TIME_PICKER,
        "AXStepper": ActionableElements.STEPPER,
        "AXMenu": ActionableElements.MENU,
        "AXContextualMenu": ActionableElements.CONTEXTUAL_MENU,
        "AXDragAndDrop": ActionableElements.DRAG_AND_DROP,
        "AXProgressBar": ActionableElements.PROGRESS_BAR,
        "AXCustomControl": ActionableElements.CUSTOM_CONTROL,
        "AXActionSheet": ActionableElements.ACTION_SHEET,
        "AXSegmentedControl": ActionableElements.SEGMENTED_CONTROL,
        "AXTextArea": ActionableElements.TEXT_AREA,
        "AXIcon": ActionableElements.ICON,
        "AXNavigationBar": ActionableElements.NAVIGATION_BAR,
        "AXDialogButton": ActionableElements.DIALOG_BUTTON,
        "AXImage": NonActionableElements.IMAGE,
        "AXLabel": NonActionableElements.LABEL,
        "AXStaticText": NonActionableElements.STATIC_TEXT,
        "AXTable": NonActionableElements.TABLE,
        "AXTextFieldWithPlaceholder": NonActionableElements.TEXT_FIELD_WITH_PLACEHOLDER,
        "AXGroup": NonActionableElements.GROUP,
        "AXCell": NonActionableElements.CELL,
        "AXCellGroup": NonActionableElements.CELL_GROUP,
        "AXSwitch": NonActionableElements.SWITCH,
        "AXScrollArea": NonActionableElements.SCROLL_AREA,
        "AXMenuBar": NonActionableElements.MENU_BAR,
        "AXToolBar": NonActionableElements.TOOL_BAR,
        "AXMenuItem": NonActionableElements.MENU_ITEM,
        "AXPopUpButton": NonActionableElements.POP_UP_BUTTON,
        "AXRow": NonActionableElements.ROW,
        "AXColumn": NonActionableElements.COLUMN,
        "AXProgressIndicator": NonActionableElements.PROGRESS_INDICATOR,
        "AXValueIndicator": NonActionableElements.VALUE_INDICATOR,
    }

    @classmethod
    def generate_element(
        cls,
        title: str,
        role: str,
        role_description: str,
        text: str,
        frame: dict,
        os_type: OSType,
    ) -> Optional[DOMElement]:
        """Generate a single DOMElement"""
        frame = Frame(**frame)
        # generate element based on os type
        if os_type == OSType.MACOS:
            element = cls.generate_macos_element(
                title, role, role_description, text, frame
            )
        elif os_type == OSType.WINDOWS:
            element = cls.generate_windows_element(
                title, role, role_description, text, frame
            )
        return element

    @staticmethod
    def generate_macos_element(
        title: str, role: str, role_description: str, text: str, frame: Frame
    ) -> Optional[DOMElement]:
        role = DOMElementFactory.macos_mapping.get(role)
        if role is None:
            return None
        return DOMElement(title, role, role_description, text, frame)

    @staticmethod
    def generate_windows_element(
        title: str, role: str, role_description: str, text: str, frame: Frame
    ) -> Optional[DOMElement]:
        raise NotImplementedError()

    @classmethod
    def generate_element_from_dict(
        cls, element_dict: dict, os_type: OSType
    ) -> Optional[DOMElement]:
        """Generate a DOMElement from a dict

        Args:
            element_dict (dict): Dict containing information about the element, usually
            a window state dict
            os_type (OSType): The operating system type

        Returns:
            Optional[DOMElement]: The root DOMElement which contains all the children and subchildren recursively.
            I.e: a DOM Tree
        """
        if os_type == OSType.MACOS:
            title = element_dict.get("AXTitle", "")
            role = element_dict.get("AXRole", "")
            role_description = element_dict.get("AXRoleDescription", "")
            text = element_dict.get("AXValue", "")
            frame = element_dict.get("AXFrame", {})
            children = element_dict.get(
                "AXChildrenInNavigationOrder", []
            ) or element_dict.get("AXChildren", [])
        elif os_type == OSType.WINDOWS:
            raise NotImplementedError()

        element = cls.generate_element(
            title, role, role_description, text, frame, os_type
        )

        if children and element:
            for child_dict in children:
                child_element = cls.generate_element_from_dict(child_dict, os_type)
                if child_element:
                    element.add_child(child_element)
        return element
