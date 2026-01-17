"""WebSocket server for Chrome extension communication.

This module provides the BrowserBridge WebSocket server that connects to
the Chrome extension for capturing browser DOM events. It handles:
- Client connections/disconnections
- Mode management (idle, record, replay)
- Event processing and storage
- Broadcasting mode changes to all connected clients
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable

try:
    import websockets
    from websockets.server import WebSocketServerProtocol
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    WebSocketServerProtocol = Any  # type: ignore

if TYPE_CHECKING:
    from openadapt_capture.storage import CaptureStorage

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

logger = logging.getLogger(__name__)


# =============================================================================
# Browser Mode
# =============================================================================


class BrowserMode(str, Enum):
    """Operating mode for browser event capture."""

    IDLE = "idle"
    RECORD = "record"
    REPLAY = "replay"


# =============================================================================
# Browser Event (dataclass wrapper for storage)
# =============================================================================


@dataclass
class BrowserEventRecord:
    """Browser event record for storage.

    This is a simple dataclass that wraps browser events for storage,
    following the pattern used in openadapt-capture's events.py.
    """

    timestamp: float
    type: str  # e.g., "browser.click", "browser.keydown"
    url: str
    tab_id: int
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.timestamp,
            "type": self.type,
            "url": self.url,
            "tab_id": self.tab_id,
            "payload": self.payload,
        }


# =============================================================================
# WebSocket Message Types
# =============================================================================


class MessageType(str, Enum):
    """WebSocket message types."""

    # Server -> Extension
    SET_MODE = "SET_MODE"
    PING = "PING"
    EXECUTE_ACTION = "EXECUTE_ACTION"

    # Extension -> Server
    DOM_EVENT = "DOM_EVENT"
    DOM_SNAPSHOT = "DOM_SNAPSHOT"
    PONG = "PONG"
    ERROR = "ERROR"


# =============================================================================
# Browser Bridge
# =============================================================================


class BrowserBridge:
    """WebSocket server for Chrome extension communication.

    Provides bidirectional communication with the Chrome extension for:
    - Mode control (idle, record, replay)
    - DOM event capture
    - DOM snapshot capture
    - Action execution during replay

    Usage:
        bridge = BrowserBridge(port=8765)
        await bridge.start()

        # Set recording mode
        await bridge.set_mode(BrowserMode.RECORD)

        # Events are automatically stored when in RECORD mode
        # ...

        await bridge.stop()
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8765,
        storage: "CaptureStorage | None" = None,
        on_event: Callable[[BrowserEventRecord], None] | None = None,
        on_snapshot: Callable[[DOMSnapshot], None] | None = None,
    ):
        """Initialize the browser bridge.

        Args:
            host: WebSocket server host.
            port: WebSocket server port.
            storage: Optional CaptureStorage for persisting events.
            on_event: Optional callback for browser events.
            on_snapshot: Optional callback for DOM snapshots.
        """
        if not WEBSOCKETS_AVAILABLE:
            raise ImportError(
                "websockets package is required for BrowserBridge. "
                "Install it with: pip install websockets"
            )

        self.host = host
        self.port = port
        self.storage = storage
        self.on_event = on_event
        self.on_snapshot = on_snapshot

        self._mode = BrowserMode.IDLE
        self._clients: set[WebSocketServerProtocol] = set()
        self._server: Any = None
        self._running = False
        self._event_count = 0
        self._snapshot_count = 0
        self._events: list[BrowserEventRecord] = []
        self._snapshots: list[DOMSnapshot] = []

    @property
    def mode(self) -> BrowserMode:
        """Get the current browser mode."""
        return self._mode

    @property
    def client_count(self) -> int:
        """Get the number of connected clients."""
        return len(self._clients)

    @property
    def event_count(self) -> int:
        """Get the number of events captured."""
        return self._event_count

    @property
    def snapshot_count(self) -> int:
        """Get the number of DOM snapshots captured."""
        return self._snapshot_count

    @property
    def is_running(self) -> bool:
        """Check if the server is running."""
        return self._running

    def get_events(self) -> list[BrowserEventRecord]:
        """Get all captured events."""
        return self._events.copy()

    def get_snapshots(self) -> list[DOMSnapshot]:
        """Get all captured DOM snapshots."""
        return self._snapshots.copy()

    def clear_events(self) -> None:
        """Clear captured events and snapshots."""
        self._events.clear()
        self._snapshots.clear()
        self._event_count = 0
        self._snapshot_count = 0

    async def set_mode(self, mode: BrowserMode) -> None:
        """Set mode and broadcast to all connected clients.

        Args:
            mode: The new browser mode.
        """
        self._mode = mode
        message = json.dumps({
            "type": MessageType.SET_MODE.value,
            "timestamp": time.time() * 1000,
            "payload": {"mode": mode.value}
        })
        await self._broadcast(message)
        logger.info(f"Browser mode set to: {mode.value}")

    async def send_ping(self) -> None:
        """Send a ping to all connected clients."""
        message = json.dumps({
            "type": MessageType.PING.value,
            "timestamp": time.time() * 1000,
            "payload": {}
        })
        await self._broadcast(message)

    async def execute_action(
        self,
        action_type: str,
        xpath: str | None = None,
        css_selector: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Execute an action in the browser (replay mode).

        Args:
            action_type: Type of action (e.g., "click", "type").
            xpath: XPath to target element.
            css_selector: CSS selector for target element.
            **kwargs: Additional action parameters.
        """
        action = {
            "type": action_type,
            "xpath": xpath,
            "css_selector": css_selector,
            **kwargs,
        }
        message = json.dumps({
            "type": MessageType.EXECUTE_ACTION.value,
            "timestamp": time.time() * 1000,
            "payload": {"action": action}
        })
        await self._broadcast(message)
        logger.debug(f"Executed action: {action_type}")

    async def _broadcast(self, message: str) -> None:
        """Send message to all connected clients.

        Args:
            message: JSON message string to broadcast.
        """
        if not self._clients:
            return

        # Send to all clients, collecting any exceptions
        results = await asyncio.gather(
            *[client.send(message) for client in self._clients],
            return_exceptions=True
        )

        # Log any failures
        for client, result in zip(self._clients, results):
            if isinstance(result, Exception):
                logger.warning(f"Failed to send to client: {result}")

    async def _handle_client(self, websocket: WebSocketServerProtocol) -> None:
        """Handle a connected Chrome extension client.

        Args:
            websocket: The WebSocket connection.
        """
        self._clients.add(websocket)
        client_addr = websocket.remote_address if hasattr(websocket, 'remote_address') else "unknown"
        logger.info(f"Client connected: {client_addr} (total: {len(self._clients)})")

        try:
            # Send current mode on connect
            await websocket.send(json.dumps({
                "type": MessageType.SET_MODE.value,
                "timestamp": time.time() * 1000,
                "payload": {"mode": self._mode.value}
            }))

            # Process incoming messages
            async for message in websocket:
                await self._handle_message(websocket, message)

        except websockets.ConnectionClosed as e:
            logger.debug(f"Client disconnected: {e.code} {e.reason}")
        except Exception as e:
            logger.error(f"Error handling client: {e}")
        finally:
            self._clients.discard(websocket)
            logger.info(f"Client removed (remaining: {len(self._clients)})")

    async def _handle_message(
        self,
        websocket: WebSocketServerProtocol,
        message: str
    ) -> None:
        """Process a message from the Chrome extension.

        Args:
            websocket: The WebSocket connection.
            message: The JSON message string.
        """
        try:
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == MessageType.DOM_EVENT.value:
                await self._handle_dom_event(data)
            elif msg_type == MessageType.DOM_SNAPSHOT.value:
                await self._handle_dom_snapshot(data)
            elif msg_type == MessageType.PONG.value:
                logger.debug("Received PONG")
            elif msg_type == MessageType.ERROR.value:
                error_payload = data.get("payload", {})
                logger.error(
                    f"Browser error [{error_payload.get('code', 'UNKNOWN')}]: "
                    f"{error_payload.get('message', 'No message')}"
                )
            else:
                logger.warning(f"Unknown message type: {msg_type}")

        except json.JSONDecodeError:
            logger.error(f"Invalid JSON from extension: {message[:100]}")
        except Exception as e:
            logger.error(f"Error processing message: {e}")

    async def _handle_dom_event(self, data: dict) -> None:
        """Process a DOM event from the Chrome extension.

        Args:
            data: The parsed message data.
        """
        # Only process events in RECORD mode
        if self._mode != BrowserMode.RECORD:
            return

        payload = data.get("payload", {})
        event_type = payload.get("eventType", BrowserEventType.UNKNOWN.value)

        # Create event record
        event = BrowserEventRecord(
            timestamp=data.get("timestamp", 0) / 1000,  # Convert to seconds
            type=event_type,
            url=payload.get("url", ""),
            tab_id=data.get("tabId", 0),
            payload=payload,
        )

        # Store event
        self._events.append(event)
        self._event_count += 1

        # Parse into typed event if possible
        typed_event = self._parse_typed_event(event_type, payload, data)

        # Store in CaptureStorage if available
        if self.storage is not None:
            # Store as JSON in the events table
            # Note: We store the raw event, not Pydantic model to match storage patterns
            try:
                from openadapt_capture.events import BaseEvent
                # Create a minimal event for storage compatibility
                # Browser events don't fit the standard EventType enum
                # so we store them as raw JSON in a custom way
                pass  # Storage integration would go here
            except ImportError:
                pass

        # Notify callback
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception as e:
                logger.error(f"Error in event callback: {e}")

        logger.debug(f"Captured {event_type} event from {payload.get('url', 'unknown')}")

    def _parse_typed_event(
        self,
        event_type: str,
        payload: dict,
        data: dict,
    ) -> BrowserClickEvent | BrowserKeyEvent | BrowserScrollEvent | BrowserInputEvent | BrowserNavigationEvent | BrowserFocusEvent | None:
        """Parse payload into a typed Pydantic event.

        Args:
            event_type: The event type string.
            payload: The event payload.
            data: The full message data.

        Returns:
            A typed browser event or None if parsing fails.
        """
        try:
            timestamp = data.get("timestamp", 0) / 1000
            url = payload.get("url", "")
            tab_id = data.get("tabId", 0)

            if event_type == BrowserEventType.CLICK.value:
                element = self._parse_element_ref(payload.get("element", {}))
                return BrowserClickEvent(
                    timestamp=timestamp,
                    url=url,
                    tab_id=tab_id,
                    client_x=payload.get("clientX", 0),
                    client_y=payload.get("clientY", 0),
                    page_x=payload.get("pageX", 0),
                    page_y=payload.get("pageY", 0),
                    button=payload.get("button", 0),
                    click_count=payload.get("clickCount", 1),
                    element=element,
                )

            elif event_type in (BrowserEventType.KEYDOWN.value, BrowserEventType.KEYUP.value):
                element = None
                if payload.get("element"):
                    element = self._parse_element_ref(payload.get("element"))

                return BrowserKeyEvent(
                    timestamp=timestamp,
                    type=BrowserEventType(event_type),
                    url=url,
                    tab_id=tab_id,
                    key=payload.get("key", ""),
                    code=payload.get("code", ""),
                    key_code=payload.get("keyCode", 0),
                    shift_key=payload.get("shiftKey", False),
                    ctrl_key=payload.get("ctrlKey", False),
                    alt_key=payload.get("altKey", False),
                    meta_key=payload.get("metaKey", False),
                    element=element,
                )

            elif event_type == BrowserEventType.SCROLL.value:
                target = payload.get("target", "window")
                if isinstance(target, dict):
                    target = self._parse_element_ref(target)

                return BrowserScrollEvent(
                    timestamp=timestamp,
                    url=url,
                    tab_id=tab_id,
                    scroll_x=payload.get("scrollX", 0),
                    scroll_y=payload.get("scrollY", 0),
                    delta_x=payload.get("deltaX", 0),
                    delta_y=payload.get("deltaY", 0),
                    target=target,
                )

            elif event_type == BrowserEventType.INPUT.value:
                element = self._parse_element_ref(payload.get("element", {}))
                return BrowserInputEvent(
                    timestamp=timestamp,
                    url=url,
                    tab_id=tab_id,
                    input_type=payload.get("inputType", ""),
                    data=payload.get("data"),
                    value=payload.get("value", ""),
                    element=element,
                )

            elif event_type == BrowserEventType.NAVIGATE.value:
                nav_type = payload.get("navigationType", "link")
                return BrowserNavigationEvent(
                    timestamp=timestamp,
                    url=url,
                    tab_id=tab_id,
                    previous_url=payload.get("previousUrl", ""),
                    navigation_type=NavigationType(nav_type) if nav_type in [e.value for e in NavigationType] else NavigationType.LINK,
                )

            elif event_type in (BrowserEventType.FOCUS.value, BrowserEventType.BLUR.value):
                element = self._parse_element_ref(payload.get("element", {}))
                return BrowserFocusEvent(
                    timestamp=timestamp,
                    type=BrowserEventType(event_type),
                    url=url,
                    tab_id=tab_id,
                    element=element,
                )

        except Exception as e:
            logger.debug(f"Failed to parse typed event: {e}")

        return None

    def _parse_element_ref(self, data: dict) -> SemanticElementRef:
        """Parse element reference from payload.

        Args:
            data: Element reference dictionary.

        Returns:
            SemanticElementRef object.
        """
        bbox_data = data.get("bbox", {})
        bbox = BoundingBox(
            x=bbox_data.get("x", 0),
            y=bbox_data.get("y", 0),
            width=bbox_data.get("width", 0),
            height=bbox_data.get("height", 0),
        )

        state_data = data.get("state", {})
        state = ElementState(
            enabled=state_data.get("enabled", True),
            focused=state_data.get("focused", False),
            visible=state_data.get("visible", True),
            checked=state_data.get("checked"),
            selected=state_data.get("selected"),
            expanded=state_data.get("expanded"),
            value=state_data.get("value"),
        )

        return SemanticElementRef(
            role=data.get("role", ""),
            name=data.get("name", ""),
            bbox=bbox,
            xpath=data.get("xpath", ""),
            css_selector=data.get("css_selector", data.get("cssSelector", "")),
            state=state,
            tag_name=data.get("tag_name", data.get("tagName", "")),
            id=data.get("id"),
            class_list=data.get("class_list", data.get("classList", [])),
        )

    async def _handle_dom_snapshot(self, data: dict) -> None:
        """Process a DOM snapshot from the Chrome extension.

        Args:
            data: The parsed message data.
        """
        # Only process snapshots in RECORD mode
        if self._mode != BrowserMode.RECORD:
            return

        payload = data.get("payload", {})

        # Parse visible elements
        visible_elements = []
        for elem_data in payload.get("visibleElements", []):
            try:
                element_ref = self._parse_element_ref(elem_data.get("element", elem_data))
                center = elem_data.get("center", {})
                visible_elements.append(VisibleElement(
                    element=element_ref,
                    center_x=center.get("x", element_ref.bbox.x + element_ref.bbox.width / 2),
                    center_y=center.get("y", element_ref.bbox.y + element_ref.bbox.height / 2),
                    som_id=elem_data.get("id"),
                ))
            except Exception as e:
                logger.debug(f"Failed to parse visible element: {e}")

        snapshot = DOMSnapshot(
            timestamp=data.get("timestamp", 0) / 1000,
            url=payload.get("url", ""),
            title=payload.get("title", ""),
            tab_id=data.get("tabId", 0),
            html=payload.get("html"),
            visible_elements=visible_elements,
        )

        self._snapshots.append(snapshot)
        self._snapshot_count += 1

        # Notify callback
        if self.on_snapshot is not None:
            try:
                self.on_snapshot(snapshot)
            except Exception as e:
                logger.error(f"Error in snapshot callback: {e}")

        logger.debug(f"Captured DOM snapshot from {payload.get('url', 'unknown')}")

    async def start(self) -> None:
        """Start the WebSocket server."""
        if self._running:
            return

        self._running = True
        self._server = await websockets.serve(
            self._handle_client,
            self.host,
            self.port,
        )
        logger.info(f"Browser bridge listening on ws://{self.host}:{self.port}")

    async def stop(self) -> None:
        """Stop the WebSocket server."""
        if not self._running:
            return

        self._running = False

        # Close all client connections
        if self._clients:
            await asyncio.gather(
                *[client.close() for client in self._clients],
                return_exceptions=True
            )
            self._clients.clear()

        # Stop the server
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        logger.info("Browser bridge stopped")

    async def run_forever(self) -> None:
        """Run the server until cancelled.

        This is a convenience method for running the server as a standalone
        service. It blocks until the server is stopped or cancelled.
        """
        await self.start()
        try:
            # Run forever until cancelled
            await asyncio.Future()
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    def __repr__(self) -> str:
        return (
            f"BrowserBridge("
            f"host={self.host!r}, "
            f"port={self.port}, "
            f"mode={self._mode.value!r}, "
            f"clients={len(self._clients)}, "
            f"running={self._running})"
        )


# =============================================================================
# Convenience functions
# =============================================================================


async def run_browser_bridge(
    host: str = "localhost",
    port: int = 8765,
    mode: BrowserMode = BrowserMode.IDLE,
) -> None:
    """Run a standalone browser bridge server.

    This is a convenience function for running the browser bridge
    as a standalone service.

    Args:
        host: Server host.
        port: Server port.
        mode: Initial mode.
    """
    bridge = BrowserBridge(host=host, port=port)
    await bridge.start()
    await bridge.set_mode(mode)

    try:
        await asyncio.Future()  # Run forever
    except asyncio.CancelledError:
        pass
    finally:
        await bridge.stop()


def main() -> None:
    """Entry point for running the browser bridge as a script."""
    import argparse

    parser = argparse.ArgumentParser(description="Browser bridge WebSocket server")
    parser.add_argument("--host", default="localhost", help="Server host")
    parser.add_argument("--port", type=int, default=8765, help="Server port")
    parser.add_argument(
        "--mode",
        choices=["idle", "record", "replay"],
        default="idle",
        help="Initial mode"
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

    # Run the server
    try:
        asyncio.run(run_browser_bridge(
            host=args.host,
            port=args.port,
            mode=BrowserMode(args.mode),
        ))
    except KeyboardInterrupt:
        print("\nShutting down...")


if __name__ == "__main__":
    main()
