from enum import Enum
from typing import Any, List, Optional, Union

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


class BaseElement(BaseModel):
    title: Optional[str]
    role: Union[ActionableElements, NonActionableElements]
    children: List[Any] = []
    frame: Frame
    actionable: bool = False


class ElementGenerator:
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

    @staticmethod
    def generate_element(role: str, title: str, frame: Frame, os_type: OSType):
        if os_type == OSType.MACOS:
            return ElementGenerator.generate_macos_element(role, title, frame)
        elif os_type == OSType.WINDOWS:
            return ElementGenerator.generate_windows_element(role, title, frame)
        else:
            raise ValueError(f"Unknown OS type: {os_type}")

    @staticmethod
    def generate_macos_element(role: str, title: str, frame: Frame):
        element = ElementGenerator.macos_mapping.get(role)
        is_actionable = isinstance(element, ActionableElements)
        if not element:
            return
        return BaseElement(
            title=title, role=element, frame=frame, actionable=is_actionable
        )

    @staticmethod
    def generate_windows_element(role: str, title: str, frame: Frame):
        raise NotImplementedError()


# use main class to test

if __name__ == "__main__":
    frame = Frame(x=0, y=0, width=100, height=100)
    element = ElementGenerator.generate_element("AXColumn", "test", frame, OSType.MACOS)
    print(element)
