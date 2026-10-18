"""End-to-end tests for browser event capture pipeline.

Tests the full flow: store browser events in DB → load via CaptureSession →
iterate as typed Pydantic models.
"""

import time

from openadapt_capture.browser_events import (
    BrowserClickEvent,
    BrowserInputEvent,
    BrowserKeyEvent,
    BrowserMouseMoveEvent,
    BrowserNavigationEvent,
    BrowserScrollEvent,
)
from openadapt_capture.capture import CaptureSession
from openadapt_capture.db import create_db
from openadapt_capture.db.crud import insert_browser_event, insert_recording


def _make_element_payload(
    role="button",
    name="Submit",
    tag="button",
    xpath="/html/body/form/button",
):
    """Create a minimal semantic element ref payload."""
    return {
        "role": role,
        "name": name,
        "tagName": tag,
        "xpath": xpath,
        "cssSelector": f"{tag}",
        "bbox": {"x": 100, "y": 200, "width": 80, "height": 30},
        "state": {"enabled": True, "focused": False, "visible": True},
        "id": None,
        "classList": [],
    }


def _setup_capture_db(tmp_path):
    """Create a capture DB with a recording and return (session, recording, db_path)."""
    db_path = str(tmp_path / "recording.db")
    engine, Session = create_db(db_path)
    session = Session()

    recording = insert_recording(session, {
        "timestamp": time.time(),
        "monitor_width": 1920,
        "monitor_height": 1080,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5,
        "platform": "darwin",
        "task_description": "Test browser capture",
    })
    return session, recording, db_path


class TestBrowserEventsPayloadWrapped:
    """Test browser events stored with payload-wrapped message format."""

    def test_empty_browser_events(self, tmp_path):
        """Capture with no browser events returns empty list."""
        session, recording, db_path = _setup_capture_db(tmp_path)

        capture = CaptureSession.load(str(tmp_path))
        assert capture.browser_events() == []
        assert capture.browser_event_count == 0
        capture.close()
        session.close()

    def test_click_event_roundtrip(self, tmp_path):
        """Click event stored in DB is parsed back as BrowserClickEvent."""
        session, recording, db_path = _setup_capture_db(tmp_path)

        ts = time.time()
        msg = {
            "type": "DOM_EVENT",
            "timestamp": ts * 1000,
            "tabId": 1,
            "payload": {
                "eventType": "click",
                "url": "https://app.appfolio.com/tenants",
                "clientX": 150,
                "clientY": 220,
                "pageX": 150,
                "pageY": 220,
                "button": 0,
                "clickCount": 1,
                "element": _make_element_payload(),
            },
        }
        insert_browser_event(session, recording, ts, {"message": msg})
        session.close()

        capture = CaptureSession.load(str(tmp_path))
        events = capture.browser_events()
        assert len(events) == 1
        assert isinstance(events[0], BrowserClickEvent)
        assert events[0].client_x == 150
        assert events[0].client_y == 220
        assert events[0].element.role == "button"
        assert events[0].element.name == "Submit"
        assert events[0].element.xpath == "/html/body/form/button"
        assert events[0].url == "https://app.appfolio.com/tenants"
        capture.close()

    def test_key_event_roundtrip(self, tmp_path):
        """Key event with modifiers is parsed correctly."""
        session, recording, db_path = _setup_capture_db(tmp_path)

        ts = time.time()
        msg = {
            "type": "DOM_EVENT",
            "timestamp": ts * 1000,
            "tabId": 1,
            "payload": {
                "eventType": "keydown",
                "url": "https://app.appfolio.com/search",
                "key": "a",
                "code": "KeyA",
                "keyCode": 65,
                "shiftKey": False,
                "ctrlKey": True,
                "altKey": False,
                "metaKey": False,
                "element": _make_element_payload(
                    role="textbox",
                    name="Search",
                    tag="input",
                    xpath="/html/body/form/input",
                ),
            },
        }
        insert_browser_event(session, recording, ts, {"message": msg})
        session.close()

        capture = CaptureSession.load(str(tmp_path))
        events = capture.browser_events()
        assert len(events) == 1
        assert isinstance(events[0], BrowserKeyEvent)
        assert events[0].key == "a"
        assert events[0].ctrl_key is True
        assert events[0].element.role == "textbox"
        capture.close()

    def test_input_event_roundtrip(self, tmp_path):
        """Input event captures field value."""
        session, recording, db_path = _setup_capture_db(tmp_path)

        ts = time.time()
        msg = {
            "type": "DOM_EVENT",
            "timestamp": ts * 1000,
            "tabId": 1,
            "payload": {
                "eventType": "input",
                "url": "https://app.appfolio.com/form",
                "inputType": "insertText",
                "data": "John Doe",
                "value": "John Doe",
                "element": _make_element_payload(
                    role="textbox",
                    name="Tenant Name",
                    tag="input",
                    xpath="/html/body/form/input[name='tenant']",
                ),
            },
        }
        insert_browser_event(session, recording, ts, {"message": msg})
        session.close()

        capture = CaptureSession.load(str(tmp_path))
        events = capture.browser_events()
        assert len(events) == 1
        assert isinstance(events[0], BrowserInputEvent)
        assert events[0].value == "John Doe"
        assert events[0].element.name == "Tenant Name"
        capture.close()

    def test_scroll_event_roundtrip(self, tmp_path):
        """Scroll event captures position and delta."""
        session, recording, db_path = _setup_capture_db(tmp_path)

        ts = time.time()
        msg = {
            "type": "DOM_EVENT",
            "timestamp": ts * 1000,
            "tabId": 1,
            "payload": {
                "eventType": "scroll",
                "url": "https://app.appfolio.com/list",
                "scrollX": 0,
                "scrollY": 500,
                "deltaX": 0,
                "deltaY": 100,
            },
        }
        insert_browser_event(session, recording, ts, {"message": msg})
        session.close()

        capture = CaptureSession.load(str(tmp_path))
        events = capture.browser_events()
        assert len(events) == 1
        assert isinstance(events[0], BrowserScrollEvent)
        assert events[0].scroll_y == 500
        assert events[0].delta_y == 100
        capture.close()

    def test_navigation_event_roundtrip(self, tmp_path):
        """Navigation event captures URL transition."""
        session, recording, db_path = _setup_capture_db(tmp_path)

        ts = time.time()
        msg = {
            "type": "DOM_EVENT",
            "timestamp": ts * 1000,
            "tabId": 1,
            "payload": {
                "eventType": "navigate",
                "url": "https://app.appfolio.com/tenants/123",
                "previousUrl": "https://app.appfolio.com/tenants",
                "navigationType": "link",
            },
        }
        insert_browser_event(session, recording, ts, {"message": msg})
        session.close()

        capture = CaptureSession.load(str(tmp_path))
        events = capture.browser_events()
        assert len(events) == 1
        assert isinstance(events[0], BrowserNavigationEvent)
        assert events[0].previous_url == "https://app.appfolio.com/tenants"
        assert events[0].url == "https://app.appfolio.com/tenants/123"
        capture.close()

    def test_mixed_events_ordering(self, tmp_path):
        """Multiple event types maintain timestamp ordering."""
        session, recording, db_path = _setup_capture_db(tmp_path)

        base_ts = time.time()
        events_data = [
            (base_ts, "navigate", {
                "url": "https://example.com",
                "previousUrl": "",
                "navigationType": "typed",
            }),
            (base_ts + 1, "click", {
                "url": "https://example.com",
                "clientX": 100, "clientY": 200,
                "pageX": 100, "pageY": 200,
                "button": 0, "clickCount": 1,
                "element": _make_element_payload(),
            }),
            (base_ts + 2, "input", {
                "url": "https://example.com",
                "inputType": "insertText",
                "data": "test",
                "value": "test",
                "element": _make_element_payload(
                    role="textbox", name="Field", tag="input",
                    xpath="/html/body/input",
                ),
            }),
            (base_ts + 3, "scroll", {
                "url": "https://example.com",
                "scrollX": 0, "scrollY": 300,
                "deltaX": 0, "deltaY": 300,
            }),
        ]

        for ts, event_type, payload in events_data:
            payload["eventType"] = event_type
            msg = {
                "type": "DOM_EVENT",
                "timestamp": ts * 1000,
                "tabId": 1,
                "payload": payload,
            }
            insert_browser_event(session, recording, ts, {"message": msg})

        session.close()

        capture = CaptureSession.load(str(tmp_path))
        events = capture.browser_events()
        assert len(events) == 4
        assert isinstance(events[0], BrowserNavigationEvent)
        assert isinstance(events[1], BrowserClickEvent)
        assert isinstance(events[2], BrowserInputEvent)
        assert isinstance(events[3], BrowserScrollEvent)

        # Verify ordering
        for i in range(len(events) - 1):
            assert events[i].timestamp <= events[i + 1].timestamp

        assert capture.browser_event_count == 4
        capture.close()

    def test_malformed_event_skipped(self, tmp_path):
        """Events with unparseable messages are skipped gracefully."""
        session, recording, db_path = _setup_capture_db(tmp_path)

        ts = time.time()
        # Valid event
        insert_browser_event(session, recording, ts, {
            "message": {
                "type": "DOM_EVENT",
                "timestamp": ts * 1000,
                "tabId": 1,
                "payload": {
                    "eventType": "scroll",
                    "url": "https://example.com",
                    "scrollX": 0, "scrollY": 0,
                    "deltaX": 0, "deltaY": 50,
                },
            }
        })
        # Malformed event (no eventType)
        insert_browser_event(session, recording, ts + 1, {
            "message": {"garbage": True}
        })
        session.close()

        capture = CaptureSession.load(str(tmp_path))
        events = capture.browser_events()
        # Only the valid event should parse
        assert len(events) == 1
        assert isinstance(events[0], BrowserScrollEvent)
        # But browser_event_count counts raw DB rows
        assert capture.browser_event_count == 2
        capture.close()

    def test_element_state_preserved(self, tmp_path):
        """Element state (checked, value, etc.) survives roundtrip."""
        session, recording, db_path = _setup_capture_db(tmp_path)

        ts = time.time()
        msg = {
            "type": "DOM_EVENT",
            "timestamp": ts * 1000,
            "tabId": 1,
            "payload": {
                "eventType": "click",
                "url": "https://example.com/form",
                "clientX": 50, "clientY": 50,
                "pageX": 50, "pageY": 50,
                "button": 0, "clickCount": 1,
                "element": {
                    "role": "checkbox",
                    "name": "Government Assistance",
                    "tagName": "input",
                    "xpath": "/html/body/form/input[@type='checkbox']",
                    "cssSelector": "input[type='checkbox']",
                    "bbox": {"x": 40, "y": 40, "width": 20, "height": 20},
                    "state": {
                        "enabled": True,
                        "focused": True,
                        "visible": True,
                        "checked": True,
                        "value": "on",
                    },
                    "id": "gov-assist",
                    "classList": ["form-check"],
                },
            },
        }
        insert_browser_event(session, recording, ts, {"message": msg})
        session.close()

        capture = CaptureSession.load(str(tmp_path))
        events = capture.browser_events()
        assert len(events) == 1
        click = events[0]
        assert click.element.state.checked is True
        assert click.element.state.value == "on"
        assert click.element.id == "gov-assist"
        assert click.element.class_list == ["form-check"]
        capture.close()


class TestContentScriptFlatFormat:
    """Test parsing of flat content-script events (real Chrome extension format)."""

    def test_raw_click_event(self, tmp_path):
        """Click from content script with flat format is parsed correctly."""
        session, recording, db_path = _setup_capture_db(tmp_path)

        ts = time.time()
        raw_event = {
            "type": "USER_EVENT",
            "eventType": "click",
            "targetId": "elem-109",
            "timestamp": ts,
            "devicePixelRatio": 2.0,
            "element": {
                "role": "link",
                "name": "55\u00a0comments",
                "dataId": "elem-109",
                "bbox": {"x": 323, "y": 131, "width": 63, "height": 11},
                "tagName": "a",
                "id": None,
                "classList": None,
            },
            "clientX": 346,
            "clientY": 141,
            "screenX": 606,
            "screenY": 372,
            "button": 0,
            "url": "https://news.ycombinator.com/",
            "tabId": 1,
        }
        insert_browser_event(session, recording, ts, {"message": raw_event})
        session.close()

        capture = CaptureSession.load(str(tmp_path))
        events = capture.browser_events()
        assert len(events) == 1
        assert isinstance(events[0], BrowserClickEvent)
        assert events[0].client_x == 346
        assert events[0].client_y == 141
        assert events[0].element.role == "link"
        assert events[0].element.tag_name == "a"
        assert events[0].url == "https://news.ycombinator.com/"
        capture.close()

    def test_raw_keydown_event(self, tmp_path):
        """Keydown from content script is parsed correctly."""
        session, recording, db_path = _setup_capture_db(tmp_path)

        ts = time.time()
        raw_event = {
            "type": "USER_EVENT",
            "eventType": "keydown",
            "timestamp": ts,
            "element": {
                "role": "textbox",
                "name": "",
                "dataId": "elem-0",
                "bbox": {"x": 59, "y": 95, "width": 657, "height": 129},
                "tagName": "textarea",
                "id": None,
                "classList": None,
            },
            "key": "t",
            "code": "KeyT",
            "shiftKey": False,
            "ctrlKey": False,
            "altKey": False,
            "metaKey": False,
            "url": "https://example.com/",
            "tabId": 1,
        }
        insert_browser_event(session, recording, ts, {"message": raw_event})
        session.close()

        capture = CaptureSession.load(str(tmp_path))
        events = capture.browser_events()
        assert len(events) == 1
        assert isinstance(events[0], BrowserKeyEvent)
        assert events[0].key == "t"
        assert events[0].code == "KeyT"
        assert events[0].element.role == "textbox"
        assert events[0].element.tag_name == "textarea"
        capture.close()

    def test_raw_scroll_event(self, tmp_path):
        """Scroll from content script (scrollDeltaX/Y) is parsed correctly."""
        session, recording, db_path = _setup_capture_db(tmp_path)

        ts = time.time()
        raw_event = {
            "type": "USER_EVENT",
            "eventType": "scroll",
            "timestamp": ts,
            "scrollDeltaX": 0,
            "scrollDeltaY": -1.14,
            "clientX": 538,
            "clientY": 300,
            "url": "https://example.com/",
            "tabId": 1,
        }
        insert_browser_event(session, recording, ts, {"message": raw_event})
        session.close()

        capture = CaptureSession.load(str(tmp_path))
        events = capture.browser_events()
        assert len(events) == 1
        assert isinstance(events[0], BrowserScrollEvent)
        assert events[0].delta_y == -1.14
        assert events[0].delta_x == 0
        capture.close()

    def test_raw_mousemove_event(self, tmp_path):
        """Mousemove from content script is parsed as BrowserMouseMoveEvent."""
        session, recording, db_path = _setup_capture_db(tmp_path)

        ts = time.time()
        raw_event = {
            "type": "USER_EVENT",
            "eventType": "mousemove",
            "timestamp": ts,
            "element": {
                "role": None,
                "name": "Some text",
                "dataId": "elem-0",
                "bbox": {"x": 69, "y": 183, "width": 652, "height": 19},
                "tagName": "td",
                "id": None,
                "classList": ["title"],
            },
            "clientX": 577,
            "clientY": 185,
            "screenX": 1010,
            "screenY": 449,
            "url": "https://example.com/",
            "tabId": 1,
        }
        insert_browser_event(session, recording, ts, {"message": raw_event})
        session.close()

        capture = CaptureSession.load(str(tmp_path))
        events = capture.browser_events()
        assert len(events) == 1
        assert isinstance(events[0], BrowserMouseMoveEvent)
        assert events[0].client_x == 577
        assert events[0].client_y == 185
        assert events[0].screen_x == 1010
        assert events[0].element.tag_name == "td"
        assert events[0].element.class_list == ["title"]
        capture.close()

    def test_raw_mixed_events(self, tmp_path):
        """Mixed raw content-script events are all parsed."""
        session, recording, db_path = _setup_capture_db(tmp_path)

        base_ts = time.time()
        raw_events = [
            {"type": "USER_EVENT", "eventType": "mousemove", "timestamp": base_ts,
             "clientX": 100, "clientY": 200, "screenX": 200, "screenY": 300,
             "url": "https://example.com/", "tabId": 1,
             "element": {"role": None, "name": "", "dataId": "elem-0",
                         "bbox": {"x": 0, "y": 0, "width": 100, "height": 100},
                         "tagName": "div", "id": None, "classList": None}},
            {"type": "USER_EVENT", "eventType": "click", "timestamp": base_ts + 1,
             "clientX": 100, "clientY": 200, "screenX": 200, "screenY": 300,
             "button": 0, "url": "https://example.com/", "tabId": 1,
             "element": {"role": "button", "name": "Submit", "dataId": "elem-1",
                         "bbox": {"x": 90, "y": 190, "width": 20, "height": 20},
                         "tagName": "button", "id": "submit-btn", "classList": ["btn"]}},
            {"type": "USER_EVENT", "eventType": "keydown", "timestamp": base_ts + 2,
             "key": "Enter", "code": "Enter", "shiftKey": False, "ctrlKey": False,
             "altKey": False, "metaKey": False, "url": "https://example.com/", "tabId": 1,
             "element": {"role": "textbox", "name": "Search", "dataId": "elem-2",
                         "bbox": {"x": 50, "y": 50, "width": 200, "height": 30},
                         "tagName": "input", "id": None, "classList": None}},
            {"type": "USER_EVENT", "eventType": "scroll", "timestamp": base_ts + 3,
             "scrollDeltaX": 0, "scrollDeltaY": 100, "clientX": 400, "clientY": 300,
             "url": "https://example.com/", "tabId": 1},
        ]
        for i, evt in enumerate(raw_events):
            insert_browser_event(session, recording, base_ts + i, {"message": evt})
        session.close()

        capture = CaptureSession.load(str(tmp_path))
        events = capture.browser_events()
        assert len(events) == 4
        assert isinstance(events[0], BrowserMouseMoveEvent)
        assert isinstance(events[1], BrowserClickEvent)
        assert isinstance(events[2], BrowserKeyEvent)
        assert isinstance(events[3], BrowserScrollEvent)
        assert events[1].element.id == "submit-btn"
        assert events[1].element.class_list == ["btn"]
        capture.close()


class TestCLIBrowserFlag:
    """Test that the CLI record function accepts browser_events flag."""

    def test_record_accepts_browser_events_param(self):
        """Verify record() function signature includes browser_events."""
        import inspect

        from openadapt_capture.cli import record

        sig = inspect.signature(record)
        assert "browser_events" in sig.parameters
        # Default should be False
        assert sig.parameters["browser_events"].default is False
