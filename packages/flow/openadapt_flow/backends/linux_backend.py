"""Native Linux desktop backend using AT-SPI, scoped to one exact window.

The backend is deliberately small and lives under the existing governed
runtime contracts:

* AT-SPI supplies structured target enumeration and native actions.
* the configured application and window title are exact, case-insensitive
  selectors; zero or multiple matches refuse before capture or input;
* structural resolution enumerates every matching candidate and refuses
  ambiguity instead of selecting the first match;
* native action receipts prove delivery only. Runtime postconditions and
  system-of-record effects remain authoritative for business outcomes;
* global pointer/keyboard synthesis is disabled by default and is available
  only as an explicit, window-bound fallback.

Imports are headless-safe. ``gi.repository.Atspi`` and Linux display access are
loaded only when the default :class:`AtspiLinuxClient` is constructed; unit
tests inject the typed :class:`LinuxClient` protocol without a Linux host.

X11 is the initial live transport. On Wayland, a client must prove that it owns
an operator-approved XDG Desktop Portal RemoteDesktop/ScreenCast session. The
default client does not pretend to own such a session and therefore refuses.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from PIL import Image, ImageGrab

from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.ir import (
    ActionDeliveryReceipt,
    StructuralHandle,
    StructuralLocator,
)


class LinuxBackendError(RuntimeError):
    """Linux capture, resolution, or input could not be performed safely."""


@dataclass(frozen=True)
class LinuxWindow:
    """One top-level AT-SPI window in global screen coordinates."""

    native_id: str
    app_name: str
    title: str
    pid: int
    bounds: tuple[int, int, int, int]
    native: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class LinuxElement:
    """One AT-SPI candidate, including its exact live-object identity."""

    native_id: str
    accessible_id: Optional[str]
    role: Optional[str]
    name: Optional[str]
    app_name: str
    window_title: str
    pid: int
    bounds: tuple[int, int, int, int]
    supported_operations: tuple[str, ...] = ()
    native: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class LinuxCandidateSet:
    """Bounded result of a full AT-SPI candidate enumeration."""

    candidates: tuple[LinuxElement, ...]
    truncated: bool = False


@runtime_checkable
class LinuxClient(Protocol):
    """Typed platform client; injected in CI, backed by AT-SPI on Linux."""

    @property
    def session_type(self) -> str: ...

    def portal_session_ready(self) -> bool:
        """Whether an approved Wayland RemoteDesktop/ScreenCast session exists."""
        ...

    def find_windows(self, app: str, title: str) -> list[LinuxWindow]: ...

    def window_is_active(self, window: LinuxWindow) -> bool: ...

    def focus_window(self, window: LinuxWindow) -> bool: ...

    def capture_window(self, window: LinuxWindow) -> tuple[bytes, int, int]: ...

    def element_at_point(
        self, window: LinuxWindow, x: int, y: int
    ) -> Optional[LinuxElement]: ...

    def focused_element(self, window: LinuxWindow) -> Optional[LinuxElement]:
        """Return the unique live focused element inside ``window``."""
        ...

    def find_candidates(
        self, window: LinuxWindow, locator: StructuralLocator
    ) -> LinuxCandidateSet: ...

    def structured_text(self, element: LinuxElement) -> Optional[str]: ...

    def perform_native(self, element: LinuxElement, operation: str) -> str:
        """Perform one AT-SPI action and return its concrete operation name."""
        ...

    def replace_text(self, element: LinuxElement, text: str) -> bool: ...

    def physical_click(self, x: int, y: int, *, double: bool = False) -> bool: ...

    def physical_type_text(self, text: str) -> bool: ...

    def physical_press(self, key: str) -> bool: ...

    def physical_scroll(self, dx: int, dy: int) -> bool: ...


_ROLE_MAP = {
    "push button": "button",
    "button": "button",
    "link": "link",
    "menu item": "menuitem",
    "page tab": "tab",
    "list item": "listitem",
    "check box": "checkbox",
    "radio button": "radio",
    "text": "textbox",
    "entry": "textbox",
    "password text": "textbox",
    "combo box": "combobox",
}

_ACTION_ROLES = {
    "button": "invoke",
    "link": "invoke",
    "menuitem": "invoke",
    "checkbox": "toggle",
    "radio": "select",
    "tab": "select",
    "listitem": "select",
    "textbox": "focus",
    "combobox": "focus",
}

_ACTION_ALIASES = {
    "invoke": frozenset({"activate", "click", "invoke", "open", "press"}),
    "toggle": frozenset({"check", "toggle", "uncheck"}),
    "select": frozenset({"select"}),
}

_CONTEXT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def _application_identifier(name: str) -> Optional[str]:
    """Return a bounded app id without including a sensitive window title."""

    ascii_name = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    )
    identifier = re.sub(r"[^A-Za-z0-9._:/-]+", "-", ascii_name).strip("-._:/")
    identifier = identifier.casefold()[:128]
    return identifier if _CONTEXT_ID_RE.fullmatch(identifier) else None


def _linux_session_facts() -> Optional[tuple[str, int, int]]:
    """Return kernel boot/audit-session facts, never environment assertions."""

    if not sys.platform.startswith("linux"):
        return None
    try:
        boot_id = (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        )
        session_text = Path("/proc/self/sessionid").read_text(encoding="ascii").strip()
        if not re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            boot_id,
        ):
            return None
        session_id = int(session_text)
        # UINT32_MAX is the kernel's "audit session not assigned" sentinel.
        if session_id < 0 or session_id == 0xFFFFFFFF:
            return None
        return boot_id.casefold(), session_id, os.getuid()
    except (OSError, UnicodeError, ValueError):
        return None


def _clean_text(value: object) -> Optional[str]:
    text = " ".join(str(value or "").split())
    return text or None


def _fingerprint(element: LinuxElement) -> str:
    """Bind resolution to the exact live AT-SPI object and its geometry."""
    identity = {
        "native_id": element.native_id,
        "accessible_id": element.accessible_id,
        "role": element.role,
        "name": element.name,
        "app_name": element.app_name,
        "window_title": element.window_title,
        "pid": element.pid,
        "bounds": element.bounds,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _receipt(
    operation: str,
    *,
    native: bool,
    target_fingerprint: Optional[str] = None,
) -> ActionDeliveryReceipt:
    """Create an input-delivery receipt that cannot assert an outcome."""
    return ActionDeliveryReceipt(
        receipt_id=secrets.token_hex(12),
        operation=operation,
        native=native,
        target_fingerprint=target_fingerprint,
        delivered_at=datetime.now(timezone.utc).isoformat(),
        outcome_verified=False,
    )


class LinuxBackend:
    """Drive one exact local Linux application window through AT-SPI."""

    def __init__(
        self,
        client: Optional[LinuxClient] = None,
        *,
        app: str,
        window_title: str,
        allow_physical_input: bool = False,
        require_active_window_for_capture: bool = True,
    ) -> None:
        if not app.strip():
            raise ValueError("native Linux backend requires a non-empty app name")
        if not window_title.strip():
            raise ValueError(
                "native Linux backend requires an exact non-empty window title"
            )
        self._client = client if client is not None else AtspiLinuxClient()
        self._app = app.strip()
        self._window_title = window_title.strip()
        self._allow_physical_input = bool(allow_physical_input)
        self._require_active_window_for_capture = bool(
            require_active_window_for_capture
        )
        self._captured_window: Optional[LinuxWindow] = None
        self._viewport: Optional[tuple[int, int]] = None
        self._focused_element: Optional[LinuxElement] = None
        self._focused_fingerprint: Optional[str] = None
        self._guarded_coordinate: Optional[
            tuple[
                tuple[int, int],
                LinuxWindow,
                Optional[str],
                Optional[str],
            ]
        ] = None
        self._guarded_keyboard: Optional[tuple[LinuxWindow, str, Optional[str]]] = None
        self._assert_session_supported()

    def _assert_session_supported(self) -> None:
        session = self._client.session_type.strip().lower()
        if session == "x11":
            return
        if session == "wayland":
            if self._client.portal_session_ready():
                return
            raise LinuxBackendError(
                "Wayland native desktop control requires an operator-approved "
                "XDG Desktop Portal RemoteDesktop/ScreenCast session. No live "
                "portal grant is bound; refusing capture and input."
            )
        raise LinuxBackendError(
            f"unsupported or headless Linux session {session!r}; an interactive "
            "X11 session is required (Wayland requires a bound portal session)"
        )

    def _resolve_window(self) -> LinuxWindow:
        matches = self._client.find_windows(self._app, self._window_title)
        if not matches:
            raise LinuxBackendError(
                "no Linux window with exact app/title "
                f"{self._app!r}/{self._window_title!r}"
            )
        if len(matches) != 1:
            summary = ", ".join(
                f"id={window.native_id} pid={window.pid} title={window.title!r}"
                for window in matches[:5]
            )
            raise LinuxBackendError(
                "ambiguous Linux target: exact selector matched "
                f"{len(matches)} windows ({summary}); refusing first-match input"
            )
        window = matches[0]
        _, _, width, height = window.bounds
        if width <= 0 or height <= 0:
            raise LinuxBackendError(
                f"Linux target window has invalid bounds {window.bounds!r}"
            )
        return window

    def _require_same_window(
        self, expected: LinuxWindow, *, require_active: bool = False
    ) -> LinuxWindow:
        current = self._resolve_window()
        if current != expected:
            raise LinuxBackendError(
                "Linux target window changed after binding; refusing stale "
                f"input (expected {expected.native_id!r} {expected.bounds!r}, "
                f"got {current.native_id!r} {current.bounds!r})"
            )
        if require_active and not self._client.window_is_active(current):
            raise LinuxBackendError(
                "the exact Linux target window is not active; refusing global input"
            )
        return current

    @property
    def viewport(self) -> tuple[int, int]:
        if self._viewport is None:
            self.screenshot()
        assert self._viewport is not None
        return self._viewport

    def screenshot(self) -> bytes:
        window = self._resolve_window()
        if (
            self._require_active_window_for_capture
            and not self._client.window_is_active(window)
        ):
            raise LinuxBackendError(
                "the exact Linux target window is not active, so a bounded "
                "screen capture could contain an occluding application; "
                "foreground the target window and retry"
            )
        png, width, height = self._client.capture_window(window)
        if width <= 0 or height <= 0:
            raise LinuxBackendError("Linux target-window capture returned no pixels")
        try:
            with Image.open(io.BytesIO(png)) as image:
                image.load()
                if image.size != (width, height):
                    raise LinuxBackendError(
                        "Linux target-window capture dimensions do not match "
                        f"the declared viewport: {image.size!r} != {(width, height)!r}"
                    )
        except LinuxBackendError:
            raise
        except Exception as exc:
            raise LinuxBackendError(
                "Linux target-window capture did not return a valid image"
            ) from exc
        current = self._require_same_window(window)
        if current.bounds[2:] != (width, height):
            raise LinuxBackendError(
                "Linux target window changed size during capture; refusing a "
                "stale coordinate mapping"
            )
        self._captured_window = current
        self._viewport = (width, height)
        return png

    # -- live execution-context identity -----------------------------------

    def application_identity(self) -> Optional[str]:
        """Return a live exact AT-SPI application id, never a window title."""

        try:
            window = self._resolve_window()
        except Exception:
            return None
        if window.app_name.casefold() != self._app.casefold():
            return None
        return _application_identifier(window.app_name)

    def session_identity(self) -> Optional[str]:
        """Hash only live kernel boot/audit-session continuity facts."""

        facts = _linux_session_facts()
        if facts is None:
            return None
        boot_id, session_id, uid = facts
        material = f"linux-session-v1\0{boot_id}\0{session_id}\0{uid}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def workflow_state_identity(self) -> Optional[str]:
        """Workflow state requires an explicitly qualified AT-SPI marker."""

        return None

    def _global_point(
        self, x: int, y: int, *, require_active: bool = True
    ) -> tuple[LinuxWindow, int, int]:
        if self._captured_window is None or self._viewport is None:
            self.screenshot()
        assert self._captured_window is not None
        assert self._viewport is not None
        width, height = self._viewport
        if not (0 <= x < width and 0 <= y < height):
            raise LinuxBackendError(
                f"point ({x}, {y}) is outside captured viewport {(width, height)}"
            )
        window = self._require_same_window(
            self._captured_window, require_active=require_active
        )
        return window, window.bounds[0] + int(x), window.bounds[1] + int(y)

    def structural_locator_at(self, x: int, y: int) -> Optional[StructuralLocator]:
        window, global_x, global_y = self._global_point(
            int(x), int(y), require_active=False
        )
        element = self._client.element_at_point(window, global_x, global_y)
        if element is None:
            return None
        if element.app_name.casefold() != self._app.casefold():
            return None
        if element.window_title.casefold() != self._window_title.casefold():
            return None
        left, top, width, height = element.bounds
        wx, wy, ww, wh = window.bounds
        if not (
            width > 0
            and height > 0
            and left <= global_x < left + width
            and top <= global_y < top + height
            and left >= wx
            and top >= wy
            and left + width <= wx + ww
            and top + height <= wy + wh
        ):
            return None
        if not element.accessible_id and not (element.role and element.name):
            return None
        return StructuralLocator(
            automation_id=element.accessible_id,
            role=element.role,
            name=element.name,
            window_name=window.title,
        )

    def _candidate_set(self, locator: StructuralLocator) -> LinuxCandidateSet:
        window = self._resolve_window()
        if (
            locator.window_name
            and locator.window_name.casefold() != window.title.casefold()
        ):
            return LinuxCandidateSet(())
        result = self._client.find_candidates(window, locator)
        if result.truncated:
            raise StructuralResolutionRefused(
                "AT-SPI candidate enumeration exceeded its bound; refusing "
                "partial/first-match resolution"
            )
        candidates: list[LinuxElement] = []
        for candidate in result.candidates:
            if not self._candidate_matches(candidate, locator, window):
                raise StructuralResolutionRefused(
                    "AT-SPI client returned a candidate outside the exact "
                    "locator/app/window contract"
                )
            candidates.append(candidate)
        return LinuxCandidateSet(tuple(candidates))

    @staticmethod
    def _candidate_matches(
        candidate: LinuxElement,
        locator: StructuralLocator,
        window: LinuxWindow,
    ) -> bool:
        if (
            candidate.app_name.casefold() != window.app_name.casefold()
            or candidate.window_title.casefold() != window.title.casefold()
            or candidate.pid != window.pid
        ):
            return False
        left, top, width, height = candidate.bounds
        wx, wy, ww, wh = window.bounds
        if (
            width <= 0
            or height <= 0
            or left < wx
            or top < wy
            or left + width > wx + ww
            or top + height > wy + wh
        ):
            return False
        if locator.automation_id:
            return candidate.accessible_id == locator.automation_id and (
                locator.role is None or candidate.role == locator.role
            )
        return (
            bool(locator.role)
            and bool(locator.name)
            and candidate.role == locator.role
            and candidate.name == locator.name
        )

    def _unique_candidate(self, locator: StructuralLocator) -> Optional[LinuxElement]:
        result = self._candidate_set(locator)
        if not result.candidates:
            return None
        if len(result.candidates) != 1:
            raise StructuralResolutionRefused(
                f"AT-SPI locator is ambiguous: candidate_count={len(result.candidates)}"
            )
        candidate = result.candidates[0]
        if (
            candidate.app_name.casefold() != self._app.casefold()
            or candidate.window_title.casefold() != self._window_title.casefold()
        ):
            raise StructuralResolutionRefused(
                "AT-SPI candidate escaped the configured app/window scope"
            )
        return candidate

    def locate_structural(
        self, locator: StructuralLocator
    ) -> Optional[StructuralHandle]:
        if not locator.automation_id and not (locator.role and locator.name):
            return None
        candidate = self._unique_candidate(locator)
        if candidate is None:
            return None
        window = self._resolve_window()
        x, y, width, height = candidate.bounds
        wx, wy, ww, wh = window.bounds
        if (
            width <= 0
            or height <= 0
            or x < wx
            or y < wy
            or x + width > wx + ww
            or y + height > wy + wh
        ):
            return None
        local_x = x - wx
        local_y = y - wy
        return StructuralHandle(
            point=(local_x + width // 2, local_y + height // 2),
            region=(local_x, local_y, width, height),
            target_fingerprint=_fingerprint(candidate),
            candidate_count=1,
            supported_operations=list(candidate.supported_operations),
        )

    def structured_text_at(self, x: int, y: int) -> Optional[str]:
        try:
            window, global_x, global_y = self._global_point(
                int(x), int(y), require_active=False
            )
            element = self._client.element_at_point(window, global_x, global_y)
            if element is None:
                return None
            return _clean_text(self._client.structured_text(element))
        except Exception:
            return None

    def act_structural(
        self,
        locator: StructuralLocator,
        handle: StructuralHandle,
        *,
        double: bool = False,
    ) -> ActionDeliveryReceipt:
        expected = handle.target_fingerprint
        if not expected:
            raise LinuxBackendError(
                "native AT-SPI actuation requires a target fingerprint"
            )
        candidate = self._unique_candidate(locator)
        if candidate is None:
            raise LinuxBackendError(
                "AT-SPI target disappeared between resolution and actuation"
            )
        current = _fingerprint(candidate)
        if not hmac.compare_digest(expected, current):
            raise LinuxBackendError(
                "AT-SPI target changed between resolution and actuation; "
                "refusing a stale native action"
            )

        operation: Optional[str] = None
        if not double and candidate.supported_operations:
            preferred = _ACTION_ROLES.get(candidate.role or "")
            if preferred in candidate.supported_operations:
                operation = preferred
            elif "invoke" in candidate.supported_operations:
                operation = "invoke"
            elif "focus" in candidate.supported_operations:
                operation = "focus"
        if operation is not None:
            concrete = self._client.perform_native(candidate, operation)
            if not concrete.startswith("atspi_"):
                raise LinuxBackendError(
                    "AT-SPI client returned an invalid native operation receipt"
                )
            if operation == "focus":
                self._focused_element = candidate
                self._focused_fingerprint = current
            else:
                self._clear_focused_element()
            return _receipt(concrete, native=True, target_fingerprint=expected)

        if not self._allow_physical_input:
            detail = "double-click" if double else "click"
            raise LinuxBackendError(
                f"AT-SPI target has no native {detail} operation and physical "
                "input fallback is disabled; set linux_allow_physical_input "
                "only for a workflow-qualified interactive session"
            )
        self._physical_element_click(candidate, expected, double=double)
        self._clear_focused_element()
        return _receipt(
            "physical_double_click" if double else "physical_click",
            native=False,
            target_fingerprint=expected,
        )

    def arm_guarded_coordinate(self, x: int, y: int) -> None:
        """Bind one visual point, exact window, and optional AT-SPI element."""

        self.cancel_guarded_coordinate()
        point = (int(x), int(y))
        window, global_x, global_y = self._global_point(*point)
        element = self._client.element_at_point(window, global_x, global_y)
        fingerprint = _fingerprint(element) if element is not None else None
        self._guarded_coordinate = (
            point,
            window,
            fingerprint,
            self.session_identity(),
        )

    def cancel_guarded_coordinate(self) -> None:
        self._guarded_coordinate = None

    def _assert_guarded_frame(
        self,
        window: LinuxWindow,
        expected_frame_sha256: str,
        expected_session: Optional[str],
    ) -> None:
        current = self._require_same_window(window, require_active=True)
        png, width, height = self._client.capture_window(current)
        if (width, height) != current.bounds[2:]:
            raise LinuxBackendError(
                "Linux target window dimensions changed before guarded input"
            )
        if not hmac.compare_digest(
            hashlib.sha256(png).hexdigest(), expected_frame_sha256
        ):
            raise LinuxBackendError(
                "Linux target frame changed after identity verification"
            )
        if expected_session is not None and not hmac.compare_digest(
            self.session_identity() or "", expected_session
        ):
            raise LinuxBackendError(
                "Linux desktop session changed after identity verification"
            )

    def act_guarded_coordinate(
        self,
        x: int,
        y: int,
        *,
        expected_frame_sha256: str,
        double: bool = False,
    ) -> ActionDeliveryReceipt:
        pending = self._guarded_coordinate
        self._guarded_coordinate = None
        if pending is None:
            raise LinuxBackendError(
                "Linux coordinate actuation has no pre-identity binding"
            )
        point, window, expected_element, expected_session = pending
        if point != (int(x), int(y)):
            raise LinuxBackendError(
                "Linux coordinate target changed after identity verification"
            )
        if not self._allow_physical_input:
            raise LinuxBackendError(
                "guarded Linux coordinate input requires the explicitly "
                "qualified physical-input fallback"
            )
        self._assert_guarded_frame(
            window,
            expected_frame_sha256,
            expected_session,
        )
        _, global_x, global_y = self._global_point(*point)
        if expected_element is not None:
            current_element = self._client.element_at_point(window, global_x, global_y)
            if current_element is None or not hmac.compare_digest(
                expected_element, _fingerprint(current_element)
            ):
                raise LinuxBackendError(
                    "Linux accessibility target changed before coordinate delivery"
                )
        if not self._client.physical_click(global_x, global_y, double=double):
            raise LinuxBackendError("Linux guarded coordinate click was rejected")
        self._clear_focused_element()
        return _receipt(
            "guarded_coordinate_double_click" if double else "guarded_coordinate_click",
            native=False,
            target_fingerprint=expected_element,
        )

    def guarded_keyboard_frame(self) -> bytes:
        return self.screenshot()

    def arm_guarded_keyboard(self, x: int, y: int) -> None:
        """Bind the unique live focused AT-SPI element at the resolved point."""

        self.cancel_guarded_keyboard()
        window, global_x, global_y = self._global_point(int(x), int(y))
        focused = self._client.focused_element(window)
        if focused is None:
            raise LinuxBackendError(
                "Linux keyboard action has no unique focused AT-SPI element"
            )
        left, top, width, height = focused.bounds
        if not (left <= global_x < left + width and top <= global_y < top + height):
            raise LinuxBackendError(
                "Linux focused element does not contain the resolved target point"
            )
        self._guarded_keyboard = (
            window,
            _fingerprint(focused),
            self.session_identity(),
        )

    def cancel_guarded_keyboard(self) -> None:
        self._guarded_keyboard = None

    def _consume_guarded_keyboard(
        self, expected_frame_sha256: str
    ) -> tuple[LinuxElement, str]:
        pending = self._guarded_keyboard
        self._guarded_keyboard = None
        if pending is None:
            raise LinuxBackendError(
                "Linux keyboard actuation has no pre-identity focused binding"
            )
        window, fingerprint, expected_session = pending
        self._assert_guarded_frame(
            window,
            expected_frame_sha256,
            expected_session,
        )
        focused = self._client.focused_element(window)
        if focused is None or not hmac.compare_digest(
            fingerprint, _fingerprint(focused)
        ):
            raise LinuxBackendError(
                "Linux focused element changed after identity verification"
            )
        return focused, fingerprint

    def type_text_guarded(
        self,
        text: str,
        *,
        expected_frame_sha256: str,
    ) -> ActionDeliveryReceipt:
        focused, fingerprint = self._consume_guarded_keyboard(expected_frame_sha256)
        if text and not self._client.replace_text(focused, text):
            raise LinuxBackendError("Linux guarded text delivery was rejected")
        return _receipt(
            "guarded_atspi_type",
            native=True,
            target_fingerprint=fingerprint,
        )

    def press_guarded(
        self,
        key: str,
        *,
        expected_frame_sha256: str,
    ) -> ActionDeliveryReceipt:
        _, fingerprint = self._consume_guarded_keyboard(expected_frame_sha256)
        if not self._allow_physical_input:
            raise LinuxBackendError(
                "guarded Linux KEY delivery requires the explicitly qualified "
                "physical-input fallback"
            )
        if not self._client.physical_press(key):
            raise LinuxBackendError(f"Linux guarded key delivery was rejected: {key!r}")
        self._clear_focused_element()
        return _receipt(
            "guarded_atspi_key",
            native=False,
            target_fingerprint=fingerprint,
        )

    def _physical_element_click(
        self, candidate: LinuxElement, expected: str, *, double: bool
    ) -> None:
        locator = StructuralLocator(
            automation_id=candidate.accessible_id,
            role=candidate.role,
            name=candidate.name,
            window_name=candidate.window_title,
        )
        fresh = self._unique_candidate(locator)
        if fresh is None or not hmac.compare_digest(expected, _fingerprint(fresh)):
            raise LinuxBackendError(
                "AT-SPI target changed before physical fallback; refusing input"
            )
        window = self._resolve_window()
        if not self._client.focus_window(window) or not self._client.window_is_active(
            window
        ):
            raise LinuxBackendError(
                "exact Linux target window could not be focused for physical input"
            )
        x, y, width, height = fresh.bounds
        if not self._client.physical_click(
            x + width // 2, y + height // 2, double=double
        ):
            raise LinuxBackendError("Linux physical click delivery was rejected")

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        if not self._allow_physical_input:
            raise LinuxBackendError(
                "coordinate input is disabled for native Linux; use an AT-SPI "
                "structural target or explicitly enable qualified physical fallback"
            )
        window, global_x, global_y = self._global_point(int(x), int(y))
        if not self._client.focus_window(window) or not self._client.window_is_active(
            window
        ):
            raise LinuxBackendError(
                "exact Linux target window could not be focused for coordinate input"
            )
        self._require_same_window(window, require_active=True)
        if not self._client.physical_click(global_x, global_y, double=double):
            raise LinuxBackendError("Linux coordinate click delivery was rejected")
        self._clear_focused_element()

    def type_text(self, text: str) -> None:
        if not text:
            return
        if self._focused_element is not None and self._focused_fingerprint is not None:
            locator = StructuralLocator(
                automation_id=self._focused_element.accessible_id,
                role=self._focused_element.role,
                name=self._focused_element.name,
                window_name=self._focused_element.window_title,
            )
            fresh = self._unique_candidate(locator)
            if fresh is None or not hmac.compare_digest(
                self._focused_fingerprint, _fingerprint(fresh)
            ):
                raise LinuxBackendError(
                    "focused AT-SPI text target changed before editable-text delivery"
                )
            if not self._client.replace_text(fresh, text):
                raise LinuxBackendError(
                    "AT-SPI editable-text replacement was unavailable or rejected"
                )
            return
        if not self._allow_physical_input:
            raise LinuxBackendError(
                "no verified AT-SPI editable-text target is focused and physical "
                "keyboard fallback is disabled"
            )
        window = self._resolve_window()
        if not self._client.focus_window(window) or not self._client.window_is_active(
            window
        ):
            raise LinuxBackendError(
                "exact Linux target window could not be focused for text input"
            )
        if not self._client.physical_type_text(text):
            raise LinuxBackendError("Linux physical text delivery was rejected")

    def press(self, key: str) -> None:
        if not self._allow_physical_input:
            raise LinuxBackendError(
                "global Linux keyboard synthesis is disabled; explicitly enable "
                "qualified physical fallback for KEY steps"
            )
        window = self._resolve_window()
        if not self._client.focus_window(window) or not self._client.window_is_active(
            window
        ):
            raise LinuxBackendError(
                "exact Linux target window could not be focused for key input"
            )
        if not self._client.physical_press(key):
            raise LinuxBackendError(f"Linux key delivery was rejected: {key!r}")
        self._clear_focused_element()

    def scroll(self, dx: int, dy: int) -> None:
        if not dx and not dy:
            return
        if not self._allow_physical_input:
            raise LinuxBackendError(
                "global Linux scroll synthesis is disabled; explicitly enable "
                "qualified physical fallback for SCROLL steps"
            )
        window = self._resolve_window()
        if not self._client.focus_window(window) or not self._client.window_is_active(
            window
        ):
            raise LinuxBackendError(
                "exact Linux target window could not be focused for scroll input"
            )
        if not self._client.physical_scroll(int(dx), int(dy)):
            raise LinuxBackendError("Linux scroll delivery was rejected")

    def _clear_focused_element(self) -> None:
        self._focused_element = None
        self._focused_fingerprint = None


class AtspiLinuxClient:
    """Lazy GI-backed AT-SPI client for an interactive Linux session."""

    _MAX_NODES = 20_000

    def __init__(self) -> None:
        try:
            import gi

            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi
        except Exception as exc:
            raise LinuxBackendError(
                "native Linux requires AT-SPI GI bindings. Install the "
                "'openadapt-flow[linux]' extra and the distribution packages "
                "providing libatspi/typelibs, then run inside an interactive session"
            ) from exc
        self._atspi = Atspi
        try:
            initialized = bool(Atspi.is_initialized())
            status = 0 if initialized else int(Atspi.init())
        except Exception as exc:
            raise LinuxBackendError(
                "failed to initialize the AT-SPI accessibility registry"
            ) from exc
        if status != 0:
            raise LinuxBackendError(
                f"AT-SPI accessibility registry initialization failed ({status})"
            )
        self._session_type = self._detect_session_type()
        if self._session_type == "x11" and not os.environ.get("DISPLAY"):
            raise LinuxBackendError(
                "X11 session selected but DISPLAY is unset; refusing headless control"
            )

    @staticmethod
    def _detect_session_type() -> str:
        declared = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
        if declared in {"x11", "wayland"}:
            return declared
        if os.environ.get("WAYLAND_DISPLAY"):
            return "wayland"
        if os.environ.get("DISPLAY"):
            return "x11"
        return "headless"

    @property
    def session_type(self) -> str:
        return self._session_type

    def portal_session_ready(self) -> bool:
        # A real XDG portal session is a live D-Bus/PipeWire/libei capability,
        # not a boolean environment toggle. This client has no such transport.
        return False

    @staticmethod
    def _call(obj: Any, *names: str, default: Any = None) -> Any:
        for name in names:
            member = getattr(obj, name, None)
            if callable(member):
                try:
                    return member()
                except Exception:
                    continue
            if member is not None:
                return member
        return default

    def _children(self, node: Any) -> list[Any]:
        count = self._call(node, "get_child_count", default=0)
        try:
            bounded = min(max(int(count), 0), self._MAX_NODES)
        except Exception:
            return []
        children: list[Any] = []
        for index in range(bounded):
            try:
                child = node.get_child_at_index(index)
            except Exception:
                continue
            if child is not None:
                children.append(child)
        return children

    def _desktop(self) -> Any:
        try:
            desktop = self._atspi.get_desktop(0)
        except Exception as exc:
            raise LinuxBackendError("AT-SPI desktop is unavailable") from exc
        if desktop is None:
            raise LinuxBackendError("AT-SPI returned no desktop")
        return desktop

    def _role(self, node: Any) -> Optional[str]:
        return _ROLE_MAP.get(self._raw_role(node))

    def _raw_role(self, node: Any) -> str:
        value = self._call(node, "get_role_name", default=None)
        return str(value or "").strip().casefold()

    def _bounds(self, node: Any) -> Optional[tuple[int, int, int, int]]:
        component = self._call(
            node, "get_component_iface", "queryComponent", default=None
        )
        if component is None:
            return None
        rect = None
        for coord_name in ("SCREEN", "WINDOW"):
            coord = getattr(self._atspi.CoordType, coord_name, None)
            if coord is None:
                continue
            try:
                rect = component.get_extents(coord)
                break
            except Exception:
                continue
        if rect is None:
            return None
        try:
            result = (int(rect.x), int(rect.y), int(rect.width), int(rect.height))
        except Exception:
            try:
                values = tuple(int(value) for value in rect)
            except Exception:
                return None
            if len(values) != 4:
                return None
            result = (values[0], values[1], values[2], values[3])
        return result if result[2] > 0 and result[3] > 0 else None

    def _pid(self, node: Any) -> int:
        value = self._call(node, "get_process_id", default=0)
        try:
            return max(0, int(value))
        except Exception:
            return 0

    def _state_contains(self, node: Any, name: str) -> bool:
        state_set = self._call(node, "get_state_set", default=None)
        state = getattr(self._atspi.StateType, name, None)
        if state_set is None or state is None:
            return False
        try:
            return bool(state_set.contains(state))
        except Exception:
            return False

    def _native_id(self, path: tuple[int, ...]) -> str:
        return ".".join(str(part) for part in path)

    def _accessible_id(self, node: Any) -> Optional[str]:
        return _clean_text(
            self._call(
                node,
                "get_accessible_id",
                "get_id",
                default=None,
            )
        )

    def _action_names(self, node: Any) -> tuple[str, ...]:
        if not self._state_contains(node, "ENABLED"):
            return ()
        action = self._call(node, "get_action_iface", "queryAction", default=None)
        names: list[str] = []
        if action is not None:
            count = self._call(action, "get_n_actions", default=0)
            try:
                bounded = min(max(int(count), 0), 64)
            except Exception:
                bounded = 0
            for index in range(bounded):
                value = None
                for method_name in ("get_action_name", "get_name"):
                    method = getattr(action, method_name, None)
                    if callable(method):
                        try:
                            value = method(index)
                            break
                        except Exception:
                            continue
                clean = str(value or "").strip().casefold()
                if clean:
                    names.append(clean)
        role = self._role(node)
        operations: list[str] = []
        if role in {"textbox", "combobox"}:
            operations.append("focus")
        mapped = _ACTION_ROLES.get(role or "", "invoke")
        aliases = _ACTION_ALIASES.get(mapped, frozenset())
        if any(name in aliases for name in names):
            operations.append(mapped)
        return tuple(dict.fromkeys(operations))

    def _window_from_node(
        self, node: Any, *, app_name: str, path: tuple[int, ...]
    ) -> Optional[LinuxWindow]:
        if not (
            self._state_contains(node, "VISIBLE")
            and self._state_contains(node, "SHOWING")
        ):
            return None
        bounds = self._bounds(node)
        title = _clean_text(self._call(node, "get_name", default=None))
        if bounds is None or title is None:
            return None
        return LinuxWindow(
            native_id=self._native_id(path),
            app_name=app_name,
            title=title,
            pid=self._pid(node),
            bounds=bounds,
            native=node,
        )

    def find_windows(self, app: str, title: str) -> list[LinuxWindow]:
        matches: list[LinuxWindow] = []
        desktop = self._desktop()
        for app_index, application in enumerate(self._children(desktop)):
            app_name = _clean_text(self._call(application, "get_name", default=None))
            if app_name is None or app_name.casefold() != app.casefold():
                continue
            for window_index, node in enumerate(self._children(application)):
                window = self._window_from_node(
                    node,
                    app_name=app_name,
                    path=(app_index, window_index),
                )
                if window is not None and window.title.casefold() == title.casefold():
                    matches.append(window)
        return matches

    def window_is_active(self, window: LinuxWindow) -> bool:
        return self._state_contains(window.native, "ACTIVE")

    def focus_window(self, window: LinuxWindow) -> bool:
        component = self._call(
            window.native, "get_component_iface", "queryComponent", default=None
        )
        if component is None:
            return False
        try:
            return bool(component.grab_focus())
        except Exception:
            return False

    def capture_window(self, window: LinuxWindow) -> tuple[bytes, int, int]:
        x, y, width, height = window.bounds
        try:
            image = ImageGrab.grab(bbox=(x, y, x + width, y + height))
        except Exception as exc:
            raise LinuxBackendError(
                "target-window X11 capture failed; verify DISPLAY access and "
                "install a supported Pillow capture helper"
            ) from exc
        if image.size != (width, height):
            raise LinuxBackendError(
                f"X11 capture returned {image.size!r}, expected {(width, height)!r}"
            )
        output = io.BytesIO()
        image.convert("RGB").save(output, format="PNG")
        return output.getvalue(), width, height

    def _walk(
        self, window: LinuxWindow
    ) -> tuple[list[tuple[Any, tuple[int, ...]]], bool]:
        result: list[tuple[Any, tuple[int, ...]]] = []
        stack: list[tuple[Any, tuple[int, ...]]] = [(window.native, ())]
        while stack:
            node, path = stack.pop()
            result.append((node, path))
            if len(result) >= self._MAX_NODES:
                return result, True
            children = self._children(node)
            stack.extend(
                (child, path + (index,))
                for index, child in reversed(list(enumerate(children)))
            )
        return result, False

    def _element(
        self,
        node: Any,
        path: tuple[int, ...],
        window: LinuxWindow,
    ) -> Optional[LinuxElement]:
        if not (
            self._state_contains(node, "VISIBLE")
            and self._state_contains(node, "SHOWING")
        ):
            return None
        bounds = self._bounds(node)
        if bounds is None:
            return None
        return LinuxElement(
            native_id=f"{window.native_id}:{self._native_id(path)}",
            accessible_id=self._accessible_id(node),
            role=self._role(node),
            name=_clean_text(self._call(node, "get_name", default=None)),
            app_name=window.app_name,
            window_title=window.title,
            pid=window.pid,
            bounds=bounds,
            supported_operations=self._action_names(node),
            native=node,
        )

    def element_at_point(
        self, window: LinuxWindow, x: int, y: int
    ) -> Optional[LinuxElement]:
        nodes, truncated = self._walk(window)
        if truncated:
            raise LinuxBackendError("AT-SPI tree exceeded the bounded traversal limit")
        matches: list[tuple[int, LinuxElement]] = []
        for node, path in nodes:
            candidate = self._element(node, path, window)
            if candidate is None:
                continue
            left, top, width, height = candidate.bounds
            if left <= x < left + width and top <= y < top + height:
                matches.append((len(path), candidate))
        if not matches:
            return None
        matches.sort(key=lambda item: item[0], reverse=True)
        actionable = next(
            (candidate for _, candidate in matches if candidate.supported_operations),
            matches[0][1],
        )
        return actionable

    def focused_element(self, window: LinuxWindow) -> Optional[LinuxElement]:
        nodes, truncated = self._walk(window)
        if truncated:
            raise LinuxBackendError("AT-SPI tree exceeded the bounded traversal limit")
        found: list[LinuxElement] = []
        for node, path in nodes:
            if not self._state_contains(node, "FOCUSED"):
                continue
            candidate = self._element(node, path, window)
            if candidate is not None:
                found.append(candidate)
        if len(found) != 1:
            return None
        return found[0]

    def find_candidates(
        self, window: LinuxWindow, locator: StructuralLocator
    ) -> LinuxCandidateSet:
        nodes, truncated = self._walk(window)
        found: list[LinuxElement] = []
        for node, path in nodes:
            candidate = self._element(node, path, window)
            if candidate is None:
                continue
            if locator.automation_id:
                match = candidate.accessible_id == locator.automation_id
                if match and locator.role:
                    match = candidate.role == locator.role
            else:
                match = (
                    bool(locator.role)
                    and bool(locator.name)
                    and candidate.role == locator.role
                    and candidate.name == locator.name
                )
            if match:
                found.append(candidate)
        return LinuxCandidateSet(tuple(found), truncated=truncated)

    def structured_text(self, element: LinuxElement) -> Optional[str]:
        # Identity must not be "verified" from the target's own mutable label.
        # Ascend to a row-like record container and return its sibling text,
        # mirroring the Windows UIA backend. A standalone control abstains.
        own = element.native
        node = own
        row = None
        row_roles = {
            "list item",
            "table cell",
            "table row",
            "tree item",
        }
        for _ in range(8):
            if self._raw_role(node) in row_roles:
                row = node
                break
            parent = self._call(node, "get_parent", default=None)
            if parent is None:
                break
            node = parent
        if row is None:
            return None

        branch = own
        while True:
            parent = self._call(branch, "get_parent", default=None)
            if parent is None:
                return None
            if parent is row:
                break
            branch = parent

        parts: list[str] = []
        for child in self._children(row):
            if child is branch:
                continue
            name = _clean_text(self._call(child, "get_name", default=None))
            if name:
                parts.append(name)
            text_iface = self._call(child, "get_text_iface", "queryText", default=None)
            if text_iface is None:
                continue
            count = self._call(text_iface, "get_character_count", default=0)
            try:
                text = _clean_text(text_iface.get_text(0, int(count)))
            except Exception:
                text = None
            if text and text != name:
                parts.append(text)
        return _clean_text(" ".join(parts))

    def perform_native(self, element: LinuxElement, operation: str) -> str:
        if operation == "focus":
            component = self._call(
                element.native,
                "get_component_iface",
                "queryComponent",
                default=None,
            )
            if component is None:
                raise LinuxBackendError("AT-SPI target has no Component interface")
            try:
                accepted = bool(component.grab_focus())
            except Exception as exc:
                raise LinuxBackendError("AT-SPI focus request failed") from exc
            if not accepted:
                raise LinuxBackendError("AT-SPI focus request was rejected")
            return "atspi_focus"

        action = self._call(
            element.native, "get_action_iface", "queryAction", default=None
        )
        if action is None:
            raise LinuxBackendError("AT-SPI target has no Action interface")
        count = self._call(action, "get_n_actions", default=0)
        try:
            bounded = min(max(int(count), 0), 64)
        except Exception:
            bounded = 0
        if bounded == 0:
            raise LinuxBackendError("AT-SPI target exposes no native action")
        aliases = _ACTION_ALIASES.get(operation)
        if not aliases:
            raise LinuxBackendError(
                f"AT-SPI operation is not allow-listed: {operation!r}"
            )
        selected: Optional[int] = None
        for index in range(bounded):
            name = None
            for method_name in ("get_action_name", "get_name"):
                method = getattr(action, method_name, None)
                if callable(method):
                    try:
                        name = str(method(index) or "").strip().casefold()
                        break
                    except Exception:
                        continue
            if name in aliases:
                selected = index
                break
        if selected is None:
            raise LinuxBackendError(
                f"AT-SPI target exposes no allow-listed {operation!r} action"
            )
        try:
            accepted = bool(action.do_action(selected))
        except Exception as exc:
            raise LinuxBackendError("AT-SPI native action failed") from exc
        if not accepted:
            raise LinuxBackendError("AT-SPI native action was rejected")
        return f"atspi_{operation}"

    def replace_text(self, element: LinuxElement, text: str) -> bool:
        editable = self._call(
            element.native,
            "get_editable_text_iface",
            "queryEditableText",
            default=None,
        )
        if editable is None:
            return False
        try:
            return bool(editable.set_text_contents(text))
        except Exception:
            return False

    def physical_click(self, x: int, y: int, *, double: bool = False) -> bool:
        try:
            count = 2 if double else 1
            for _ in range(count):
                if not self._atspi.generate_mouse_event(int(x), int(y), "b1c"):
                    return False
            return True
        except Exception:
            return False

    def physical_type_text(self, text: str) -> bool:
        kind = getattr(self._atspi.KeySynthType, "STRING", None)
        if kind is None:
            return False
        try:
            return bool(self._atspi.generate_keyboard_event(0, text, kind))
        except Exception:
            return False

    def physical_press(self, key: str) -> bool:
        # Chords need a layout-aware key-symbol mapper and ordered modifier
        # release. Refuse them until a qualified transport supplies that proof.
        if "+" in key:
            return False
        kind = getattr(self._atspi.KeySynthType, "STRING", None)
        if kind is None:
            return False
        names = {
            "enter": "\n",
            "return": "\n",
            "tab": "\t",
            "space": " ",
            "escape": "\x1b",
            "backspace": "\b",
        }
        value = names.get(key.casefold(), key if len(key) == 1 else "")
        if not value:
            return False
        try:
            return bool(self._atspi.generate_keyboard_event(0, value, kind))
        except Exception:
            return False

    def physical_scroll(self, dx: int, dy: int) -> bool:
        # AT-SPI mouse wheel synthesis is backend/toolkit-dependent and does
        # not provide a reliable pixel-delta contract. Refuse rather than
        # fabricate delivery evidence.
        return False
