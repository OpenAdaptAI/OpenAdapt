"""Tests for browser bridge WebSocket server and events."""

import asyncio
import json

import pytest

from openadapt_capture.browser_bridge import (
    BrowserBridge,
    BrowserEventRecord,
    BrowserMode,
    MessageType,
)
from openadapt_capture.browser_events import (
    BoundingBox,
    BrowserClickEvent,
    BrowserEventType,
    BrowserFocusEvent,
    BrowserInputEvent,
    BrowserKeyEvent,
    BrowserNavigationEvent,
    BrowserScrollEvent,
    DOMSnapshot,
    ElementState,
    NavigationType,
    SemanticElementRef,
    VisibleElement,
)

# Check if websockets is available
try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False


# =============================================================================
# Tests for Browser Events (Pydantic schemas)
# =============================================================================


class TestSemanticElementRef:
    """Tests for SemanticElementRef schema."""

    def test_create_element_ref(self):
        """Test creating a basic element reference."""
        bbox = BoundingBox(x=100, y=200, width=50, height=30)
        state = ElementState(enabled=True, focused=False, visible=True)

        ref = SemanticElementRef(
            role="button",
            name="Submit",
            bbox=bbox,
            xpath="/html/body/button[1]",
            css_selector="#submit-btn",
            state=state,
            tag_name="button",
            id="submit-btn",
        )

        assert ref.role == "button"
        assert ref.name == "Submit"
        assert ref.bbox.x == 100
        assert ref.bbox.width == 50
        assert ref.state.enabled is True
        assert ref.xpath == "/html/body/button[1]"

    def test_element_ref_defaults(self):
        """Test element reference with default values."""
        bbox = BoundingBox(x=0, y=0, width=100, height=100)
        ref = SemanticElementRef(
            role="generic",
            bbox=bbox,
            xpath="/html/body/div",
        )

        assert ref.name == ""
        assert ref.css_selector == ""
        assert ref.tag_name == ""
        assert ref.id is None
        assert ref.class_list == []
        assert ref.state.enabled is True

    def test_element_state_checkbox(self):
        """Test element state for checkbox."""
        state = ElementState(
            enabled=True,
            focused=True,
            visible=True,
            checked=True,
        )

        assert state.checked is True
        assert state.selected is None


class TestBrowserClickEvent:
    """Tests for BrowserClickEvent schema."""

    def test_create_click_event(self):
        """Test creating a click event."""
        bbox = BoundingBox(x=100, y=200, width=50, height=30)
        element = SemanticElementRef(
            role="button",
            name="Click me",
            bbox=bbox,
            xpath="/html/body/button",
        )

        event = BrowserClickEvent(
            timestamp=1704067200.0,
            url="https://example.com",
            tab_id=123,
            client_x=125,
            client_y=215,
            page_x=125,
            page_y=515,
            button=0,
            click_count=1,
            element=element,
        )

        assert event.type == BrowserEventType.CLICK
        assert event.timestamp == 1704067200.0
        assert event.url == "https://example.com"
        assert event.client_x == 125
        assert event.button == 0
        assert event.element.role == "button"

    def test_click_event_double_click(self):
        """Test double click event."""
        bbox = BoundingBox(x=0, y=0, width=100, height=100)
        element = SemanticElementRef(role="link", bbox=bbox, xpath="/a")

        event = BrowserClickEvent(
            timestamp=1.0,
            url="https://example.com",
            client_x=50,
            client_y=50,
            page_x=50,
            page_y=50,
            click_count=2,
            element=element,
        )

        assert event.click_count == 2


class TestBrowserKeyEvent:
    """Tests for BrowserKeyEvent schema."""

    def test_create_keydown_event(self):
        """Test creating a keydown event."""
        event = BrowserKeyEvent(
            timestamp=1.0,
            type=BrowserEventType.KEYDOWN,
            url="https://example.com",
            key="Enter",
            code="Enter",
            key_code=13,
            shift_key=False,
            ctrl_key=False,
            alt_key=False,
            meta_key=False,
        )

        assert event.type == BrowserEventType.KEYDOWN
        assert event.key == "Enter"
        assert event.code == "Enter"
        assert event.element is None

    def test_create_keyup_with_modifiers(self):
        """Test creating a keyup event with modifiers."""
        bbox = BoundingBox(x=0, y=0, width=200, height=30)
        element = SemanticElementRef(role="textbox", bbox=bbox, xpath="/input")

        event = BrowserKeyEvent(
            timestamp=2.0,
            type=BrowserEventType.KEYUP,
            url="https://example.com",
            key="s",
            code="KeyS",
            shift_key=False,
            ctrl_key=True,
            alt_key=False,
            meta_key=True,
            element=element,
        )

        assert event.type == BrowserEventType.KEYUP
        assert event.ctrl_key is True
        assert event.meta_key is True
        assert event.element is not None


class TestBrowserScrollEvent:
    """Tests for BrowserScrollEvent schema."""

    def test_create_window_scroll(self):
        """Test creating a window scroll event."""
        event = BrowserScrollEvent(
            timestamp=1.0,
            url="https://example.com",
            scroll_x=0,
            scroll_y=500,
            delta_x=0,
            delta_y=100,
            target="window",
        )

        assert event.type == BrowserEventType.SCROLL
        assert event.scroll_y == 500
        assert event.delta_y == 100
        assert event.target == "window"

    def test_create_element_scroll(self):
        """Test creating an element scroll event."""
        bbox = BoundingBox(x=0, y=0, width=300, height=400)
        element = SemanticElementRef(role="region", bbox=bbox, xpath="/div[@id='scroll']")

        event = BrowserScrollEvent(
            timestamp=1.0,
            url="https://example.com",
            scroll_x=100,
            scroll_y=200,
            delta_x=50,
            delta_y=0,
            target=element,
        )

        assert isinstance(event.target, SemanticElementRef)


class TestBrowserInputEvent:
    """Tests for BrowserInputEvent schema."""

    def test_create_input_event(self):
        """Test creating an input event."""
        bbox = BoundingBox(x=0, y=0, width=200, height=30)
        element = SemanticElementRef(
            role="textbox",
            name="Username",
            bbox=bbox,
            xpath="/input[@id='username']",
        )

        event = BrowserInputEvent(
            timestamp=1.0,
            url="https://example.com",
            input_type="insertText",
            data="hello",
            value="hello",
            element=element,
        )

        assert event.type == BrowserEventType.INPUT
        assert event.input_type == "insertText"
        assert event.data == "hello"
        assert event.value == "hello"


class TestBrowserNavigationEvent:
    """Tests for BrowserNavigationEvent schema."""

    def test_create_navigation_event(self):
        """Test creating a navigation event."""
        event = BrowserNavigationEvent(
            timestamp=1.0,
            url="https://example.com/new-page",
            previous_url="https://example.com/",
            navigation_type=NavigationType.LINK,
        )

        assert event.type == BrowserEventType.NAVIGATE
        assert event.url == "https://example.com/new-page"
        assert event.previous_url == "https://example.com/"
        assert event.navigation_type == NavigationType.LINK


class TestBrowserFocusEvent:
    """Tests for BrowserFocusEvent schema."""

    def test_create_focus_event(self):
        """Test creating a focus event."""
        bbox = BoundingBox(x=0, y=0, width=200, height=30)
        element = SemanticElementRef(
            role="textbox",
            bbox=bbox,
            xpath="/input",
        )

        event = BrowserFocusEvent(
            timestamp=1.0,
            type=BrowserEventType.FOCUS,
            url="https://example.com",
            element=element,
        )

        assert event.type == BrowserEventType.FOCUS

    def test_create_blur_event(self):
        """Test creating a blur event."""
        bbox = BoundingBox(x=0, y=0, width=200, height=30)
        element = SemanticElementRef(role="textbox", bbox=bbox, xpath="/input")

        event = BrowserFocusEvent(
            timestamp=1.0,
            type=BrowserEventType.BLUR,
            url="https://example.com",
            element=element,
        )

        assert event.type == BrowserEventType.BLUR


class TestDOMSnapshot:
    """Tests for DOMSnapshot schema."""

    def test_create_empty_snapshot(self):
        """Test creating an empty DOM snapshot."""
        snapshot = DOMSnapshot(
            timestamp=1.0,
            url="https://example.com",
            title="Example Page",
        )

        assert snapshot.url == "https://example.com"
        assert snapshot.title == "Example Page"
        assert snapshot.html is None
        assert snapshot.visible_elements == []

    def test_create_snapshot_with_elements(self):
        """Test creating a DOM snapshot with visible elements."""
        bbox = BoundingBox(x=100, y=200, width=50, height=30)
        element = SemanticElementRef(role="button", bbox=bbox, xpath="/button")

        visible = VisibleElement(
            element=element,
            center_x=125,
            center_y=215,
            som_id=1,
        )

        snapshot = DOMSnapshot(
            timestamp=1.0,
            url="https://example.com",
            title="Test",
            visible_elements=[visible],
        )

        assert len(snapshot.visible_elements) == 1
        assert snapshot.visible_elements[0].som_id == 1


class TestBrowserEventRecord:
    """Tests for BrowserEventRecord dataclass."""

    def test_create_event_record(self):
        """Test creating a browser event record."""
        event = BrowserEventRecord(
            timestamp=1704067200.0,
            type="browser.click",
            url="https://example.com",
            tab_id=123,
            payload={"clientX": 100, "clientY": 200},
        )

        assert event.timestamp == 1704067200.0
        assert event.type == "browser.click"
        assert event.url == "https://example.com"
        assert event.tab_id == 123
        assert event.payload["clientX"] == 100

    def test_event_record_to_dict(self):
        """Test converting event record to dictionary."""
        event = BrowserEventRecord(
            timestamp=1.0,
            type="browser.keydown",
            url="https://example.com",
            tab_id=1,
            payload={"key": "Enter"},
        )

        data = event.to_dict()
        assert data["timestamp"] == 1.0
        assert data["type"] == "browser.keydown"
        assert data["payload"]["key"] == "Enter"


# =============================================================================
# Tests for Browser Bridge (WebSocket server)
# =============================================================================


@pytest.mark.skipif(not WEBSOCKETS_AVAILABLE, reason="websockets not installed")
class TestBrowserBridge:
    """Tests for BrowserBridge WebSocket server."""

    @pytest.fixture
    async def bridge(self):
        """Create a browser bridge instance for testing."""
        bridge = BrowserBridge(port=8766)
        await bridge.start()
        yield bridge
        await bridge.stop()

    @pytest.mark.asyncio
    async def test_bridge_initialization(self):
        """Test bridge initialization."""
        bridge = BrowserBridge(host="localhost", port=8767)

        assert bridge.host == "localhost"
        assert bridge.port == 8767
        assert bridge.mode == BrowserMode.IDLE
        assert bridge.client_count == 0
        assert bridge.is_running is False

    @pytest.mark.asyncio
    async def test_bridge_start_stop(self):
        """Test starting and stopping the bridge."""
        bridge = BrowserBridge(port=8768)

        assert bridge.is_running is False

        await bridge.start()
        assert bridge.is_running is True

        await bridge.stop()
        assert bridge.is_running is False

    @pytest.mark.asyncio
    async def test_client_connection(self, bridge):
        """Test extension can connect and receive mode."""
        async with websockets.connect("ws://localhost:8766") as ws:
            message = await asyncio.wait_for(ws.recv(), timeout=1.0)
            data = json.loads(message)

            assert data["type"] == MessageType.SET_MODE.value
            assert data["payload"]["mode"] == BrowserMode.IDLE.value

            # Check client count
            assert bridge.client_count == 1

    @pytest.mark.asyncio
    async def test_mode_broadcast(self, bridge):
        """Test mode changes broadcast to all clients."""
        async with websockets.connect("ws://localhost:8766") as ws1:
            async with websockets.connect("ws://localhost:8766") as ws2:
                # Drain initial mode messages
                await ws1.recv()
                await ws2.recv()

                # Change mode
                await bridge.set_mode(BrowserMode.RECORD)

                # Both clients should receive
                for ws in [ws1, ws2]:
                    message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    data = json.loads(message)
                    assert data["type"] == MessageType.SET_MODE.value
                    assert data["payload"]["mode"] == BrowserMode.RECORD.value

    @pytest.mark.asyncio
    async def test_dom_event_capture(self, bridge):
        """Test DOM events are captured in record mode."""
        await bridge.set_mode(BrowserMode.RECORD)

        async with websockets.connect("ws://localhost:8766") as ws:
            # Drain initial mode message
            await ws.recv()
            # Drain the mode change message from set_mode
            await ws.recv()

            # Send DOM event
            event = {
                "type": MessageType.DOM_EVENT.value,
                "timestamp": 1704067200000,
                "tabId": 123,
                "payload": {
                    "eventType": "browser.click",
                    "url": "https://example.com",
                    "clientX": 100,
                    "clientY": 200,
                    "pageX": 100,
                    "pageY": 500,
                    "button": 0,
                    "clickCount": 1,
                    "element": {
                        "role": "button",
                        "name": "Submit",
                        "bbox": {"x": 80, "y": 180, "width": 40, "height": 40},
                        "xpath": "/html/body/button",
                        "cssSelector": "#submit-btn",
                        "state": {"enabled": True, "focused": False, "visible": True},
                        "tagName": "button",
                        "id": "submit-btn",
                    }
                }
            }
            await ws.send(json.dumps(event))

            # Allow processing
            await asyncio.sleep(0.1)

            # Verify event was captured
            assert bridge.event_count == 1
            events = bridge.get_events()
            assert len(events) == 1
            assert events[0].type == "browser.click"
            assert events[0].url == "https://example.com"
            assert events[0].payload["element"]["role"] == "button"

    @pytest.mark.asyncio
    async def test_dom_event_ignored_in_idle_mode(self, bridge):
        """Test DOM events are ignored when not in record mode."""
        # Bridge starts in IDLE mode

        async with websockets.connect("ws://localhost:8766") as ws:
            await ws.recv()  # Drain initial mode message

            # Send DOM event
            event = {
                "type": MessageType.DOM_EVENT.value,
                "timestamp": 1704067200000,
                "tabId": 123,
                "payload": {
                    "eventType": "browser.click",
                    "url": "https://example.com",
                }
            }
            await ws.send(json.dumps(event))

            await asyncio.sleep(0.1)

            # Event should NOT be captured
            assert bridge.event_count == 0

    @pytest.mark.asyncio
    async def test_dom_snapshot_capture(self, bridge):
        """Test DOM snapshots are captured."""
        await bridge.set_mode(BrowserMode.RECORD)

        async with websockets.connect("ws://localhost:8766") as ws:
            # Drain messages
            await ws.recv()
            await ws.recv()

            # Send DOM snapshot
            snapshot = {
                "type": MessageType.DOM_SNAPSHOT.value,
                "timestamp": 1704067200000,
                "tabId": 123,
                "payload": {
                    "url": "https://example.com",
                    "title": "Example Page",
                    "html": "<html><body><button>Click</button></body></html>",
                    "visibleElements": [
                        {
                            "element": {
                                "role": "button",
                                "name": "Click",
                                "bbox": {"x": 0, "y": 0, "width": 100, "height": 40},
                                "xpath": "/button",
                            },
                            "center": {"x": 50, "y": 20},
                            "id": 1,
                        }
                    ]
                }
            }
            await ws.send(json.dumps(snapshot))

            await asyncio.sleep(0.1)

            # Verify snapshot was captured
            assert bridge.snapshot_count == 1
            snapshots = bridge.get_snapshots()
            assert len(snapshots) == 1
            assert snapshots[0].url == "https://example.com"
            assert snapshots[0].title == "Example Page"
            assert len(snapshots[0].visible_elements) == 1

    @pytest.mark.asyncio
    async def test_pong_handling(self, bridge):
        """Test PONG messages are handled."""
        async with websockets.connect("ws://localhost:8766") as ws:
            await ws.recv()  # Drain initial mode message

            # Send PONG
            pong = {
                "type": MessageType.PONG.value,
                "timestamp": 1704067200000,
                "payload": {}
            }
            await ws.send(json.dumps(pong))

            # Should not cause any errors
            await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_error_handling(self, bridge):
        """Test ERROR messages are handled."""
        async with websockets.connect("ws://localhost:8766") as ws:
            await ws.recv()  # Drain initial mode message

            # Send ERROR
            error = {
                "type": MessageType.ERROR.value,
                "timestamp": 1704067200000,
                "tabId": 123,
                "payload": {
                    "code": "ELEMENT_NOT_FOUND",
                    "message": "Could not locate element"
                }
            }
            await ws.send(json.dumps(error))

            # Should not cause any errors
            await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_invalid_json_handling(self, bridge):
        """Test invalid JSON is handled gracefully."""
        async with websockets.connect("ws://localhost:8766") as ws:
            await ws.recv()  # Drain initial mode message

            # Send invalid JSON
            await ws.send("not valid json {{{")

            # Should not cause any errors
            await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_ping_broadcast(self, bridge):
        """Test ping is broadcast to clients."""
        async with websockets.connect("ws://localhost:8766") as ws:
            await ws.recv()  # Drain initial mode message

            # Send ping from server
            await bridge.send_ping()

            # Client should receive ping
            message = await asyncio.wait_for(ws.recv(), timeout=1.0)
            data = json.loads(message)
            assert data["type"] == MessageType.PING.value

    @pytest.mark.asyncio
    async def test_execute_action(self, bridge):
        """Test execute action is broadcast to clients."""
        async with websockets.connect("ws://localhost:8766") as ws:
            await ws.recv()  # Drain initial mode message

            # Execute action
            await bridge.execute_action(
                action_type="click",
                xpath="/html/body/button",
                css_selector="#submit-btn",
            )

            # Client should receive action
            message = await asyncio.wait_for(ws.recv(), timeout=1.0)
            data = json.loads(message)
            assert data["type"] == MessageType.EXECUTE_ACTION.value
            assert data["payload"]["action"]["type"] == "click"
            assert data["payload"]["action"]["xpath"] == "/html/body/button"

    @pytest.mark.asyncio
    async def test_event_callback(self, bridge):
        """Test event callback is called."""
        received_events = []

        def on_event(event):
            received_events.append(event)

        bridge.on_event = on_event
        await bridge.set_mode(BrowserMode.RECORD)

        async with websockets.connect("ws://localhost:8766") as ws:
            await ws.recv()
            await ws.recv()

            # Send DOM event
            event = {
                "type": MessageType.DOM_EVENT.value,
                "timestamp": 1000,
                "tabId": 1,
                "payload": {
                    "eventType": "browser.click",
                    "url": "https://example.com",
                }
            }
            await ws.send(json.dumps(event))

            await asyncio.sleep(0.1)

            assert len(received_events) == 1
            assert received_events[0].type == "browser.click"

    @pytest.mark.asyncio
    async def test_snapshot_callback(self, bridge):
        """Test snapshot callback is called."""
        received_snapshots = []

        def on_snapshot(snapshot):
            received_snapshots.append(snapshot)

        bridge.on_snapshot = on_snapshot
        await bridge.set_mode(BrowserMode.RECORD)

        async with websockets.connect("ws://localhost:8766") as ws:
            await ws.recv()
            await ws.recv()

            # Send DOM snapshot
            snapshot = {
                "type": MessageType.DOM_SNAPSHOT.value,
                "timestamp": 1000,
                "tabId": 1,
                "payload": {
                    "url": "https://example.com",
                    "title": "Test",
                    "visibleElements": []
                }
            }
            await ws.send(json.dumps(snapshot))

            await asyncio.sleep(0.1)

            assert len(received_snapshots) == 1
            assert received_snapshots[0].url == "https://example.com"

    @pytest.mark.asyncio
    async def test_clear_events(self, bridge):
        """Test clearing events and snapshots."""
        await bridge.set_mode(BrowserMode.RECORD)

        async with websockets.connect("ws://localhost:8766") as ws:
            await ws.recv()
            await ws.recv()

            # Send event and snapshot
            await ws.send(json.dumps({
                "type": MessageType.DOM_EVENT.value,
                "timestamp": 1000,
                "tabId": 1,
                "payload": {"eventType": "browser.click", "url": "https://example.com"}
            }))

            await ws.send(json.dumps({
                "type": MessageType.DOM_SNAPSHOT.value,
                "timestamp": 1000,
                "tabId": 1,
                "payload": {"url": "https://example.com", "title": "Test", "visibleElements": []}
            }))

            await asyncio.sleep(0.1)

            assert bridge.event_count == 1
            assert bridge.snapshot_count == 1

            # Clear
            bridge.clear_events()

            assert bridge.event_count == 0
            assert bridge.snapshot_count == 0
            assert len(bridge.get_events()) == 0
            assert len(bridge.get_snapshots()) == 0

    @pytest.mark.asyncio
    async def test_client_disconnect_handling(self, bridge):
        """Test client disconnection is handled gracefully."""
        async with websockets.connect("ws://localhost:8766") as ws:
            await ws.recv()
            assert bridge.client_count == 1

        # After disconnect
        await asyncio.sleep(0.1)
        assert bridge.client_count == 0

    @pytest.mark.asyncio
    async def test_multiple_clients(self, bridge):
        """Test multiple clients can connect."""
        clients = []
        for _ in range(3):
            ws = await websockets.connect("ws://localhost:8766")
            await ws.recv()  # Drain mode message
            clients.append(ws)

        assert bridge.client_count == 3

        # Close all clients
        for ws in clients:
            await ws.close()

        await asyncio.sleep(0.1)
        assert bridge.client_count == 0

    @pytest.mark.asyncio
    async def test_repr(self, bridge):
        """Test bridge string representation."""
        repr_str = repr(bridge)
        assert "BrowserBridge" in repr_str
        assert "localhost" in repr_str
        assert "8766" in repr_str
        assert "idle" in repr_str


class TestBrowserMode:
    """Tests for BrowserMode enum."""

    def test_mode_values(self):
        """Test mode enum values."""
        assert BrowserMode.IDLE.value == "idle"
        assert BrowserMode.RECORD.value == "record"
        assert BrowserMode.REPLAY.value == "replay"

    def test_mode_from_string(self):
        """Test creating mode from string."""
        assert BrowserMode("idle") == BrowserMode.IDLE
        assert BrowserMode("record") == BrowserMode.RECORD
        assert BrowserMode("replay") == BrowserMode.REPLAY
