"""Test openadapt.browser module."""

import pytest
from unittest.mock import MagicMock
from openadapt.models import ActionEvent, BrowserEvent
from openadapt.browser import assign_browser_events, fit_linear_transformation


def generate_coord_mappings(
    client_start: float,
    client_end: float,
    screen_start: float,
    screen_end: float,
    steps: int = 2,
) -> dict:
    """Generates coordinate mappings for client and screen coordinates."""
    client_coords = [
        client_start + i * (client_end - client_start) / (steps - 1)
        for i in range(steps)
    ]
    screen_coords = [
        screen_start + i * (screen_end - screen_start) / (steps - 1)
        for i in range(steps)
    ]
    return {"client": client_coords, "screen": screen_coords}


def generate_tlbr_from_coords(client_coords: dict, screen_coords: dict) -> (str, str):
    """Generates top/left/bottom/right for given client/screen mappings."""
    client_top, client_bottom = min(client_coords["client"]), max(
        client_coords["client"]
    )
    client_left, client_right = min(screen_coords["client"]), max(
        screen_coords["client"]
    )

    screen_top, screen_bottom = min(client_coords["screen"]), max(
        client_coords["screen"]
    )
    screen_left, screen_right = min(screen_coords["screen"]), max(
        screen_coords["screen"]
    )

    data_tlbr_client = f"{client_top},{client_left},{client_bottom},{client_right}"
    data_tlbr_screen = f"{screen_top},{screen_left},{screen_bottom},{screen_right}"
    return data_tlbr_client, data_tlbr_screen


def compute_screen_coords(
    client_coords: str, x_mappings: dict, y_mappings: dict
) -> str:
    """Computes the screen coordinates using the provided coordinates and mappings."""
    client_top, client_left, client_bottom, client_right = map(
        float, client_coords.split(",")
    )

    # Compute scales and offsets
    sx_scale, sx_offset = fit_linear_transformation(
        x_mappings["client"], x_mappings["screen"]
    )
    sy_scale, sy_offset = fit_linear_transformation(
        y_mappings["client"], y_mappings["screen"]
    )

    # Calculate screen coordinates using the computed scale and offset
    screen_top = sy_scale * client_top + sy_offset
    screen_left = sx_scale * client_left + sx_offset
    screen_bottom = sy_scale * client_bottom + sy_offset
    screen_right = sx_scale * client_right + sx_offset

    return f"{screen_top},{screen_left},{screen_bottom},{screen_right}"


@pytest.fixture
def mock_session() -> MagicMock:
    """Creates a mock SQLAlchemy session."""
    return MagicMock()


@pytest.fixture
def fake_action_events() -> list[ActionEvent]:
    """Creates a list of fake ActionEvent instances."""
    return [
        ActionEvent(
            id=1,
            name="click",
            timestamp=1.0,
            mouse_x=100.0,
            mouse_y=200.0,
            mouse_button_name="left",
        ),
        ActionEvent(id=2, name="press", timestamp=2.0, key_char="a"),
        ActionEvent(id=3, name="release", timestamp=3.0, key_char="a"),
    ]


@pytest.fixture
def fake_browser_events() -> list[BrowserEvent]:
    """Creates a list of fake BrowserEvent instances programmatically."""
    # Reusable coordinate values
    clientX_start = 100.0
    clientX_end = 150.0
    clientY_start = 200.0
    clientY_end = 250.0
    screenX_start = 300.0
    screenX_end = 450.0
    screenY_start = 600.0
    screenY_end = 750.0

    # Generate coordinate mappings
    coord_mappings_x = generate_coord_mappings(
        clientX_start, clientX_end, screenX_start, screenX_end
    )
    coord_mappings_y = generate_coord_mappings(
        clientY_start, clientY_end, screenY_start, screenY_end
    )
    coord_mappings = {"x": coord_mappings_x, "y": coord_mappings_y}

    # Generate the bounding box strings dynamically
    data_tlbr_client, _ = generate_tlbr_from_coords(coord_mappings_x, coord_mappings_y)

    # Compute the correct screen coordinates using the same logic as in browser.py
    data_tlbr_screen = compute_screen_coords(
        data_tlbr_client, coord_mappings_x, coord_mappings_y
    )

    # Generate the visible HTML string with dynamically calculated bounding boxes
    visible_html_string = (
        f'<html><body><div data-id="1" data-tlbr-client="{data_tlbr_client}"'
        f' data-tlbr-screen="{data_tlbr_screen}"></div></body></html>'
    )

    return [
        BrowserEvent(
            id=1,
            timestamp=1.0,
            message={
                "type": "USER_EVENT",
                "eventType": "click",
                "button": 0,
                "clientX": clientX_start,
                "clientY": clientY_start,
                "screenX": screenX_start,
                "screenY": screenY_start,
                "visibleHtmlString": visible_html_string,
                "targetId": "1",
                "coordMappings": coord_mappings,
                "timestamp": 1.0,
            },
        ),
        BrowserEvent(
            id=2,
            timestamp=2.0,
            message={
                "type": "USER_EVENT",
                "eventType": "keydown",
                "key": "a",
                "timestamp": 2.0,
            },
        ),
        BrowserEvent(
            id=3,
            timestamp=3.0,
            message={
                "type": "USER_EVENT",
                "eventType": "keyup",
                "key": "a",
                "timestamp": 3.0,
            },
        ),
    ]


def test_assign_browser_events(
    mock_session: MagicMock,
    fake_action_events: list[ActionEvent],
    fake_browser_events: list[BrowserEvent],
) -> None:
    """Tests the assign_browser_events function with simulated events."""
    # Call the function with the fake data
    assign_browser_events(mock_session, fake_action_events, fake_browser_events)

    # Inspect the assignments of ActionEvent instances
    for action_event in fake_action_events:
        if action_event.name == "click":
            assert action_event.browser_event_id == 1
            assert action_event.browser_event_timestamp == 1.0
        elif action_event.name == "press":
            assert action_event.browser_event_id == 2
            assert action_event.browser_event_timestamp == 2.0
        elif action_event.name == "release":
            assert action_event.browser_event_id == 3
            assert action_event.browser_event_timestamp == 3.0

    # Verify that the session's add method was called for each action event
    assert mock_session.add.call_count == len(fake_action_events)
