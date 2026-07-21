"""Native macOS window backend with fail-closed targeting and permissions.

This backend reuses the proven CoreGraphics/AppKit primitives from
``remote_display`` while preserving native Mac semantics:

* ``ControlOrMeta`` / ``Meta`` / ``Command`` mean the Command key, not Control.
* text uses exact-window-bound AX selected-text delivery rather than silently
  droppable Unicode event payloads or keyboard-layout-dependent scancodes.
* every target selector must resolve to exactly one normal application window.
* the exact target window must be topmost before input; app-level focus alone is
  insufficient when an application owns multiple overlapping windows.
* Screen Recording and Accessibility denial fail before capture/input instead
  of producing a blank frame or a silently dropped event.

In addition to actuation the backend now exposes the OPTIONAL structured-layer
capabilities the runtime probes on every substrate that owns one -- the
:class:`~openadapt_flow.backend.IdentityBackend` (structured a11y text under a
point) and :class:`~openadapt_flow.backend.StructuralActionBackend`
(record-time locator + replay-time deterministic element re-find). Both read
the macOS Accessibility (AX) tree of the ALREADY exact, uniquely resolved target
window, scope every candidate to that one window, enumerate under a bounded node
budget, and REFUSE (never guess) when a locator is ambiguous, its enumeration is
truncated, or a candidate escapes the configured app/window -- mirroring the
Linux AT-SPI and Windows UIA contracts. This closes the macOS structured-identity
gap so the same-name / same-DOB sibling adversary the other native backends catch
is caught here too, instead of falling through to the OCR ladder.

Structural resolution stays ADDITIVE: a structurally-resolved point flows through
the IDENTICAL fail-closed point-bound :meth:`click` (the exact-window binding, the
topmost/focus proofs, the pre-click identity gate, and the irreversible risk gate
all still fire), and the visual ladder remains the honest fallback wherever AX
exposes no durable element. The backend does NOT claim native AXPress actuation
(:class:`~openadapt_flow.backend.NativeStructuralActionBackend`); a resolved
element is acted on by the gated physical click, not a bypassing AX action.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.backends.remote_display import (
    _CHAR_KEYCODES,
    _MAC_KEYCODES,
    _NAMED_KEY_ALIASES,
    MacWindowClient,
    RemoteDisplayBackend,
    RemoteDisplayError,
    WindowClient,
    WindowInfo,
)
from openadapt_flow.ir import (
    StructuralHandle,
    StructuralLocator,
)


class MacOSBackendError(RemoteDisplayError):
    """Native macOS capture/input could not be performed safely."""


@runtime_checkable
class MacOSClient(WindowClient, Protocol):
    """Typed native-Mac extension of the shared window/input client."""

    def capture_trusted(self) -> bool: ...

    def request_capture_access(self) -> bool: ...

    def request_input_access(self) -> bool: ...

    def find_windows(
        self, owner_substr: str, title_substr: Optional[str]
    ) -> list[WindowInfo]: ...

    def frontmost_window_id(self) -> Optional[int]: ...

    def focused_application_pid(self) -> Optional[int]: ...

    def window_id_at_point(self, x: float, y: float) -> Optional[int]: ...

    def raise_window(self, window: WindowInfo) -> bool: ...

    def exact_window_focused_main(self, window: WindowInfo) -> bool: ...

    def replace_selected_text(self, window: WindowInfo, text: str) -> bool: ...


# Maximum AX nodes enumerated for one structural resolution. A window whose
# subtree exceeds this bound is refused (truncated), never resolved from a
# partial/first-match walk -- the same halt-don't-guess rule the Linux AT-SPI
# candidate enumeration applies.
_AX_ENUMERATION_LIMIT = 4000

# AXRole -> backend-neutral role, mirroring the Linux AT-SPI ``_ROLE_MAP``. The
# structural locator stores the neutral role so a recorded locator is comparable
# across substrates; unmapped roles are kept verbatim so an exact AXRole still
# matches itself at replay.
_MAC_ROLE_MAP = {
    "AXButton": "button",
    "AXMenuButton": "button",
    "AXLink": "link",
    "AXMenuItem": "menuitem",
    "AXMenuBarItem": "menuitem",
    "AXTabButton": "tab",
    "AXRadioButton": "radio",
    "AXCheckBox": "checkbox",
    "AXTextField": "textbox",
    "AXTextArea": "textbox",
    "AXSecureTextField": "textbox",
    "AXComboBox": "combobox",
    "AXPopUpButton": "combobox",
    "AXStaticText": "text",
    "AXCell": "cell",
    "AXRow": "row",
}

# AX action name -> backend-neutral operation. Recorded ONLY as diagnostic
# ``supported_operations`` on a resolved handle; the macOS backend does not
# perform native AX actions (it re-uses the fully gated physical click), so these
# never bypass an input gate.
_MAC_ACTION_MAP = {
    "AXPress": "invoke",
    "AXOpen": "invoke",
    "AXConfirm": "invoke",
    "AXPick": "select",
    "AXShowMenu": "menu",
    "AXIncrement": "increment",
    "AXDecrement": "decrement",
}


@dataclass(frozen=True)
class MacElement:
    """One AX candidate, scoped to a single exact application window.

    ``ax_path`` is the child-index path from the window element to this node in
    the enumerated snapshot (e.g. ``"0/2/1"``). AX exposes no persistent element
    id, so the path plus role/name/geometry forms the resolve/act fingerprint:
    if the live tree changes between resolution and use, the path or bounds move
    and the fingerprint no longer matches, so a stale action is refused.

    ``bounds`` is ``(x, y, width, height)`` in **screen points** (top-left
    origin -- the same space as :class:`WindowInfo` bounds and ``CGEvent``
    coordinates), NOT captured pixels; the backend converts to the click
    coordinate space using the captured window origin and DPI scale.
    """

    ax_path: str
    accessible_id: Optional[str]
    role: Optional[str]
    name: Optional[str]
    app_pid: int
    window_title: str
    bounds: tuple[float, float, float, float]
    text: Optional[str] = None
    supported_operations: tuple[str, ...] = ()
    native: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class MacCandidateSet:
    """Bounded result of a full AX candidate enumeration for one locator."""

    candidates: tuple[MacElement, ...]
    truncated: bool = False


@runtime_checkable
class MacAXClient(Protocol):
    """Typed AX element-tree client; injected in CI, backed by AX on macOS.

    Every method is scoped to ONE exact window, identified by its owning
    ``pid`` and exact ``window_title`` (the same uniqueness the backend already
    proves for capture/input). Implementations MUST refuse to cross into any
    other window or application -- the backend re-checks the returned scope and
    treats an escape as a safety refusal, never a silent match.
    """

    def element_at_point(
        self, pid: int, window_title: str, x: float, y: float
    ) -> Optional[MacElement]:
        """The AX element at screen point (x, y), if it belongs to the window."""
        ...

    def find_candidates(
        self,
        pid: int,
        window_title: str,
        locator: StructuralLocator,
        *,
        limit: int,
    ) -> MacCandidateSet:
        """Every in-window element matching ``locator``, bounded by ``limit``."""
        ...

    def structured_text(self, element: MacElement) -> Optional[str]:
        """Exact AX text of ``element`` (value/title/description), or None."""
        ...


def _clean_text(value: object) -> Optional[str]:
    """Collapse whitespace to a single-spaced string, or None if empty."""
    text = " ".join(str(value or "").split())
    return text or None


def _fingerprint(element: MacElement) -> str:
    """Bind resolution to the exact live AX node and its geometry."""
    identity = {
        "ax_path": element.ax_path,
        "accessible_id": element.accessible_id,
        "role": element.role,
        "name": element.name,
        "app_pid": element.app_pid,
        "window_title": element.window_title,
        "bounds": [round(coord, 3) for coord in element.bounds],
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class QuartzMacAXClient:
    """Live :class:`MacAXClient` over the macOS Accessibility (AX) API.

    Every ``ApplicationServices`` binding is imported inside the method that
    uses it, so importing this module costs nothing and CI (Linux) can unit-test
    the backend against a fake client without any macOS dependency.

    Read-only: it copies AX attributes and walks children; it never performs an
    AX action or sets a value. Enumeration is scoped to the one window whose AX
    title equals ``window_title`` and refuses (``truncated``) past ``limit``
    nodes rather than returning a partial candidate set.
    """

    def _window_element(self, pid: int, window_title: str) -> Any:
        from ApplicationServices import (
            AXUIElementCopyAttributeValue,
            AXUIElementCreateApplication,
            kAXTitleAttribute,
            kAXWindowsAttribute,
        )

        app = AXUIElementCreateApplication(int(pid))
        windows_error, ax_windows = AXUIElementCopyAttributeValue(
            app, kAXWindowsAttribute, None
        )
        if windows_error != 0 or ax_windows is None:
            return None
        matches = []
        for candidate in ax_windows:
            title_error, title = AXUIElementCopyAttributeValue(
                candidate, kAXTitleAttribute, None
            )
            if title_error == 0 and str(title or "") == window_title:
                matches.append(candidate)
        # Exactly-one is the same uniqueness the backend proves for input; a
        # duplicate AX window title is not silently disambiguated here.
        if len(matches) != 1:
            return None
        return matches[0]

    @staticmethod
    def _attr(element: Any, attribute: str) -> Any:
        from ApplicationServices import AXUIElementCopyAttributeValue

        error, value = AXUIElementCopyAttributeValue(element, attribute, None)
        if error != 0:
            return None
        return value

    def _bounds(self, element: Any) -> Optional[tuple[float, float, float, float]]:
        from ApplicationServices import (
            AXUIElementCopyAttributeValue,
            AXValueGetValue,
            kAXPositionAttribute,
            kAXSizeAttribute,
            kAXValueCGPointType,
            kAXValueCGSizeType,
        )

        pos_error, pos_value = AXUIElementCopyAttributeValue(
            element, kAXPositionAttribute, None
        )
        size_error, size_value = AXUIElementCopyAttributeValue(
            element, kAXSizeAttribute, None
        )
        if pos_error != 0 or size_error != 0 or pos_value is None or size_value is None:
            return None
        # PyObjC: the out-pointer argument MUST be None; the extracted struct is
        # returned alongside the success flag.
        ok_pos, point = AXValueGetValue(pos_value, kAXValueCGPointType, None)
        ok_size, size = AXValueGetValue(size_value, kAXValueCGSizeType, None)
        if not ok_pos or not ok_size:
            return None
        return (float(point.x), float(point.y), float(size.width), float(size.height))

    def _role(self, element: Any) -> Optional[str]:
        from ApplicationServices import kAXRoleAttribute

        raw = self._attr(element, kAXRoleAttribute)
        if raw is None:
            return None
        role = str(raw)
        return _MAC_ROLE_MAP.get(role, role) or None

    def _name(self, element: Any) -> Optional[str]:
        from ApplicationServices import (
            kAXDescriptionAttribute,
            kAXTitleAttribute,
        )

        for attribute in (kAXTitleAttribute, kAXDescriptionAttribute):
            name = _clean_text(self._attr(element, attribute))
            if name:
                return name
        return None

    def _accessible_id(self, element: Any) -> Optional[str]:
        from ApplicationServices import kAXIdentifierAttribute

        return _clean_text(self._attr(element, kAXIdentifierAttribute))

    def _operations(self, element: Any) -> tuple[str, ...]:
        try:
            from ApplicationServices import AXUIElementCopyActionNames

            error, actions = AXUIElementCopyActionNames(element, None)
            if error != 0 or actions is None:
                return ()
            mapped = [
                _MAC_ACTION_MAP[str(name)]
                for name in actions
                if str(name) in _MAC_ACTION_MAP
            ]
            # De-duplicate while preserving order.
            return tuple(dict.fromkeys(mapped))
        except Exception:  # noqa: BLE001 - missing action support is not fatal
            return ()

    def _text(self, element: Any) -> Optional[str]:
        from ApplicationServices import (
            kAXTitleAttribute,
            kAXValueAttribute,
        )

        for attribute in (kAXValueAttribute, kAXTitleAttribute):
            value = self._attr(element, attribute)
            if isinstance(value, str):
                text = _clean_text(value)
                if text:
                    return text
        return self._name(element)

    def _to_element(
        self, native: Any, ax_path: str, pid: int, window_title: str
    ) -> Optional[MacElement]:
        bounds = self._bounds(native)
        if bounds is None:
            return None
        return MacElement(
            ax_path=ax_path,
            accessible_id=self._accessible_id(native),
            role=self._role(native),
            name=self._name(native),
            app_pid=int(pid),
            window_title=window_title,
            bounds=bounds,
            text=self._text(native),
            supported_operations=self._operations(native),
            native=native,
        )

    def element_at_point(
        self, pid: int, window_title: str, x: float, y: float
    ) -> Optional[MacElement]:
        try:
            from ApplicationServices import (
                AXUIElementCopyAttributeValue,
                AXUIElementCopyElementAtPosition,
                AXUIElementCreateApplication,
                kAXTitleAttribute,
                kAXTopLevelUIElementAttribute,
            )

            app = AXUIElementCreateApplication(int(pid))
            hit_error, element = AXUIElementCopyElementAtPosition(
                app, float(x), float(y), None
            )
            if hit_error != 0 or element is None:
                return None
            top_error, top_level = AXUIElementCopyAttributeValue(
                element, kAXTopLevelUIElementAttribute, None
            )
            if top_error != 0 or top_level is None:
                return None
            title_error, title = AXUIElementCopyAttributeValue(
                top_level, kAXTitleAttribute, None
            )
            if title_error != 0 or str(title or "") != window_title:
                return None
            return self._to_element(element, "@point", int(pid), window_title)
        except Exception:  # noqa: BLE001 - unobservable AX is an ordinary miss
            return None

    def find_candidates(
        self,
        pid: int,
        window_title: str,
        locator: StructuralLocator,
        *,
        limit: int,
    ) -> MacCandidateSet:
        from ApplicationServices import kAXChildrenAttribute

        window = self._window_element(int(pid), window_title)
        if window is None:
            return MacCandidateSet(())
        matches: list[MacElement] = []
        visited = 0
        # Iterative DFS with an explicit budget: a runaway subtree is refused
        # (truncated), never silently first-matched.
        stack: list[tuple[Any, str]] = [(window, "0")]
        while stack:
            native, path = stack.pop()
            visited += 1
            if visited > limit:
                return MacCandidateSet(tuple(matches), truncated=True)
            element = self._to_element(native, path, int(pid), window_title)
            if element is not None and self._locator_matches(element, locator):
                matches.append(element)
            children = self._attr(native, kAXChildrenAttribute) or []
            for index, child in enumerate(children):
                stack.append((child, f"{path}/{index}"))
        return MacCandidateSet(tuple(matches))

    @staticmethod
    def _locator_matches(element: MacElement, locator: StructuralLocator) -> bool:
        if locator.automation_id:
            return element.accessible_id == locator.automation_id and (
                locator.role is None or element.role == locator.role
            )
        return (
            bool(locator.role)
            and bool(locator.name)
            and element.role == locator.role
            and element.name == locator.name
        )

    def structured_text(self, element: MacElement) -> Optional[str]:
        try:
            if element.native is not None:
                return _clean_text(self._text(element.native))
            return element.text
        except Exception:  # noqa: BLE001 - a momentary AX failure is not fatal
            return None


_NATIVE_MODIFIER_FLAGS = {
    "ctrl": "control",
    "control": "control",
    "controlormeta": "command",
    "meta": "command",
    "cmd": "command",
    "command": "command",
    "win": "command",
    "alt": "alternate",
    "option": "alternate",
    "shift": "shift",
}


def _split_native_chord(key: str) -> tuple[list[str], str]:
    parts = [part for part in key.split("+") if part]
    if not parts:
        raise ValueError(f"empty key chord: {key!r}")
    modifiers: list[str] = []
    for part in parts[:-1]:
        flag = _NATIVE_MODIFIER_FLAGS.get(part.lower())
        if flag is None:
            raise ValueError(f"unknown modifier in chord {key!r}: {part!r}")
        modifiers.append(flag)
    final = _NAMED_KEY_ALIASES.get(parts[-1].lower(), parts[-1])
    return modifiers, final


class MacOSBackend(RemoteDisplayBackend):
    """Drive one uniquely selected native macOS application window."""

    def __init__(
        self,
        client: Optional[MacOSClient] = None,
        *,
        app: str,
        window_title: Optional[str] = None,
        require_capture_trust: bool = True,
        require_input_trust: bool = True,
        settle_s: float = 0.03,
        foreground_retries: int = 10,
        foreground_settle_s: float = 0.1,
        ax_client: Optional[MacAXClient] = None,
    ) -> None:
        if not app.strip():
            raise ValueError("native macOS backend requires a non-empty app name")
        native_client = client if client is not None else MacWindowClient()
        self._mac_client: MacOSClient = native_client
        self._ax_client: MacAXClient = (
            ax_client if ax_client is not None else QuartzMacAXClient()
        )
        self._require_capture_trust = require_capture_trust
        self._foreground_retries = max(1, int(foreground_retries))
        self._foreground_settle_s = max(0.0, float(foreground_settle_s))
        self._captured_window: Optional[WindowInfo] = None
        super().__init__(
            native_client,
            owner_substr=app.strip(),
            title_substr=window_title.strip() if window_title else None,
            require_input_trust=require_input_trust,
            activate_before_input=False,
            settle_s=settle_s,
        )

    def _resolve_window(self, *, refresh: bool = False) -> WindowInfo:
        if self._window is not None and not refresh:
            return self._window
        candidates = self._mac_client.find_windows(
            self._owner_substr, self._title_substr
        )
        if not candidates:
            raise MacOSBackendError(
                "no native macOS window matching app "
                f"{self._owner_substr!r} title {self._title_substr!r}"
            )
        if len(candidates) != 1:
            summary = ", ".join(
                f"id={window.window_id} pid={window.pid} title={window.title!r}"
                for window in candidates[:5]
            )
            raise MacOSBackendError(
                "ambiguous native macOS target: selector matched "
                f"{len(candidates)} windows ({summary}); provide a unique "
                "macos_window_title. Refusing first-match input."
            )
        self._window = candidates[0]
        return candidates[0]

    def screenshot(self) -> bytes:
        if self._require_capture_trust and not self._mac_client.capture_trusted():
            raise MacOSBackendError(
                "Screen Recording is not granted, so target-window pixels may "
                "be blank or unavailable. In System Settings > Privacy & "
                "Security > Screen & System Audio Recording, enable the app "
                "that launches openadapt-flow, then restart it."
            )
        frame = super().screenshot()
        self._captured_window = self._resolve_window()
        return frame

    def _assert_bound_physical_target(
        self,
        bound: WindowInfo,
        *,
        point: Optional[tuple[float, float]] = None,
    ) -> None:
        """Require every global-input proof to still match one bound window."""
        current = self._resolve_window(refresh=True)
        point_window_id = (
            self._mac_client.window_id_at_point(*point) if point is not None else None
        )
        if (
            current != bound
            or not current.on_screen
            or self._mac_client.focused_application_pid() != bound.pid
            or self._mac_client.frontmost_window_id() != bound.window_id
            or not self._mac_client.exact_window_focused_main(bound)
            or (point is not None and point_window_id != bound.window_id)
        ):
            raise MacOSBackendError(
                "native physical-input target changed after foreground proof; "
                f"bound id={bound.window_id} pid={bound.pid} "
                f"bounds={bound.bounds!r} title={bound.title!r}. Refusing "
                "coordinate/global input against a stale window mapping"
            )

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        """Click only through one foregrounded, frame-compatible window binding."""
        if self._captured_window is None or self._viewport is None:
            self._ensure_input_ready()
            self.screenshot()
        assert self._captured_window is not None
        assert self._viewport is not None
        frame_window = self._captured_window
        bound = self._bind_physical_target()
        if (
            bound.window_id != frame_window.window_id
            or bound.pid != frame_window.pid
            or bound.title != frame_window.title
            or bound.bounds[2:] != frame_window.bounds[2:]
        ):
            raise MacOSBackendError(
                "native window was resized, reopened, or retitled after its "
                "frame was captured; refusing to map stale pixel coordinates "
                f"from id={frame_window.window_id} bounds={frame_window.bounds!r} "
                f"to id={bound.window_id} bounds={bound.bounds!r}"
            )
        width, height = self._viewport
        if not (0 <= x < width and 0 <= y < height):
            raise MacOSBackendError(
                f"click ({x}, {y}) is outside captured viewport {self._viewport}"
            )
        scale = self._scale or 1.0
        sx = bound.bounds[0] + x / scale
        sy = bound.bounds[1] + y / scale

        point = (sx, sy)
        self._assert_bound_physical_target(bound, point=point)
        self._mac_client.mouse_move(sx, sy)
        time.sleep(self._settle_s)
        self._assert_bound_physical_target(bound, point=point)
        counts = 2 if double else 1
        for index in range(counts):
            self._assert_bound_physical_target(bound, point=point)
            self._mac_client.mouse(
                sx,
                sy,
                button="left",
                down=True,
                click_count=index + 1,
            )
            try:
                time.sleep(self._settle_s)
                self._assert_bound_physical_target(bound, point=point)
            finally:
                # Always release a button that this backend pressed. If focus
                # changed after mouse-down, the assertion still propagates and
                # no further click is attempted, but leaving a global button
                # latched would corrupt the operator's next physical action.
                self._mac_client.mouse(
                    sx,
                    sy,
                    button="left",
                    down=False,
                    click_count=index + 1,
                )
            time.sleep(self._settle_s)
            self._assert_bound_physical_target(bound, point=point)

    def ensure_foreground(
        self, *, retries: Optional[int] = None, settle_s: Optional[float] = None
    ) -> None:
        """Require the exact selected window, not merely its app, to be topmost."""
        attempts = self._foreground_retries if retries is None else max(1, retries)
        pause = self._foreground_settle_s if settle_s is None else max(0.0, settle_s)
        target = self._resolve_window(refresh=True)
        for _ in range(attempts):
            self._mac_client.activate(target.pid)
            # Activating an application is not enough when macOS restores a
            # sibling document window in the same process. Raise the uniquely
            # selected AX window, then still verify its exact CoreGraphics id
            # below before input. A failed AX raise never relaxes the check.
            exact_ax_focus = self._mac_client.raise_window(target)
            target = self._resolve_window(refresh=True)
            if (
                exact_ax_focus
                and target.on_screen
                and self._mac_client.focused_application_pid() == target.pid
                and self._mac_client.frontmost_window_id() == target.window_id
            ):
                return
            # Activation is asynchronous on some applications, so retry after
            # the configured settle interval. Check before sleeping: a remote
            # control or notification window can legitimately take focus, and
            # sleeping after AXRaise widens the race between proof and input.
            time.sleep(pause)
        raise MacOSBackendError(
            f"target window id={target.window_id} app={target.owner!r} "
            f"title={target.title!r} is not the topmost window; refusing input "
            "that could land in another application window"
        )

    def _ensure_input_trusted(self) -> None:
        if self._require_input_trust and not self._mac_client.input_trusted():
            raise MacOSBackendError(
                "Accessibility is not granted, so macOS would silently drop "
                "synthetic input. In System Settings > Privacy & Security > "
                "Accessibility, enable the app that launches openadapt-flow, "
                "then restart it. Refusing to emit input."
            )

    def _ensure_input_ready(self, *, point: Optional[tuple[int, int]] = None) -> None:
        """Gate physical/global input on both active app and exact window."""
        if point is not None:
            # The base remote-display hook receives captured-pixel coordinates,
            # while native macOS must bind and convert the exact screen point in
            # its click override. Never reinterpret those pixels as global
            # coordinates if a caller bypasses the native point-bound path.
            raise MacOSBackendError(
                "native macOS coordinate input must use the point-bound click "
                "path; refusing an unconverted base-backend point"
            )
        self._ensure_input_trusted()
        self.ensure_foreground()
        bound = self._resolve_window(refresh=True)
        self._assert_bound_physical_target(bound)

    def _bind_physical_target(self) -> WindowInfo:
        """Run the base-compatible input gate and return its exact binding."""
        self._ensure_input_ready()
        bound = self._resolve_window(refresh=True)
        self._assert_bound_physical_target(bound)
        return bound

    def type_text(self, text: str) -> None:
        if not text:
            return
        # AX text replacement is addressed to one exact focused element, not
        # routed through the global keyboard event stream. NSWorkspace can lag
        # behind the WindowServer/AX state under remote control, so bound AX
        # delivery does not require its frontmost PID. It still requires all
        # exact target proofs below. Physical input paths continue to call
        # _ensure_input_ready(), which retains the active-app PID requirement.
        self._ensure_input_trusted()
        target = self._resolve_window(refresh=True)
        for _ in range(self._foreground_retries):
            self._mac_client.activate(target.pid)
            exact_ax_focus = self._mac_client.raise_window(target)
            target = self._resolve_window(refresh=True)
            exact_cg_topmost = (
                target.on_screen
                and self._mac_client.frontmost_window_id() == target.window_id
            )
            if exact_ax_focus and exact_cg_topmost:
                if self._mac_client.replace_selected_text(target, text):
                    return
                # Never retry or fall back after an AX delivery attempt. An AX
                # error is not proof that the target remained unchanged, so a
                # retry could duplicate or redirect consequential text.
                raise MacOSBackendError(
                    "the exact focused native text element was not writable or "
                    f"did not confirm AX delivery for window id={target.window_id} "
                    f"pid={target.pid} title={target.title!r}; refusing physical "
                    "keyboard or clipboard fallback"
                )
            time.sleep(self._foreground_settle_s)
        raise MacOSBackendError(
            "native text target could not be bound to the unique exact topmost "
            f"CG and focused/main AX window id={target.window_id} pid={target.pid} "
            f"title={target.title!r}; refusing text delivery"
        )

    def press(self, key: str) -> None:
        """Press a native Mac key/chord with Command-correct semantics."""
        modifiers, final = _split_native_chord(key)
        if len(final) == 1 and not modifiers:
            self.type_text(final)
            return
        bound = self._bind_physical_target()
        code = _MAC_KEYCODES.get(final.lower())
        shift = False
        if code is None:
            character = _CHAR_KEYCODES.get(final)
            if character is not None:
                code, shift = character
        if code is None:
            raise MacOSBackendError(f"no native Mac key mapping for {final!r}")
        flags = list(modifiers) + (["shift"] if shift else [])
        self._assert_bound_physical_target(bound)
        try:
            self._mac_client.key(code, down=True, flags=flags)
            self._assert_bound_physical_target(bound)
        finally:
            self._mac_client.key(code, down=False, flags=flags)
        self._assert_bound_physical_target(bound)

    def scroll(self, dx: int, dy: int) -> None:
        if dx == 0 and dy == 0:
            return
        raise MacOSBackendError(
            "native global scroll is not point-bound and could affect the "
            "window under the operator's cursor; refusing until a verified "
            "target-point scroll operation is available"
        )

    # -- structured-layer (AX) capability -------------------------------------
    #
    # IdentityBackend + StructuralActionBackend. All three methods observe the
    # AX tree of the ALREADY exact, uniquely resolved target window (read-only);
    # they never widen the window selector, and every candidate is re-checked
    # against the configured app/window before it is used.

    def _ensure_captured(self) -> WindowInfo:
        """Guarantee a fresh capture so window bounds and DPI scale are known."""
        if self._captured_window is None or self._viewport is None:
            self.screenshot()
        assert self._captured_window is not None
        assert self._viewport is not None
        return self._captured_window

    def _pixel_to_screen(
        self, window: WindowInfo, x: int, y: int
    ) -> tuple[float, float]:
        """Map a captured-pixel point into the window's screen-point space."""
        width, height = self._viewport  # type: ignore[misc]
        if not (0 <= x < width and 0 <= y < height):
            raise MacOSBackendError(
                f"point ({x}, {y}) is outside captured viewport {self._viewport}"
            )
        return (
            window.bounds[0] + x / (self._scale_x or 1.0),
            window.bounds[1] + y / (self._scale_y or 1.0),
        )

    def _in_window_pixel_rect(
        self, window: WindowInfo, bounds: tuple[float, float, float, float]
    ) -> Optional[tuple[int, int, int, int]]:
        """Convert an element's screen rect to a window-relative pixel rect.

        Returns None (an ordinary miss) when the element has non-positive size
        or is not fully contained by the captured window -- the runtime then
        uses the visual ladder rather than an off-window structural point.
        """
        ex, ey, ew, eh = bounds
        wx, wy, ww, wh = window.bounds
        if ew <= 0 or eh <= 0:
            return None
        if not (
            ex >= wx
            and ey >= wy
            and ex + ew <= wx + ww + 1e-6
            and ey + eh <= wy + wh + 1e-6
        ):
            return None
        scale_x = self._scale_x or 1.0
        scale_y = self._scale_y or 1.0
        local_x = int(round((ex - wx) * scale_x))
        local_y = int(round((ey - wy) * scale_y))
        pixel_w = int(round(ew * scale_x))
        pixel_h = int(round(eh * scale_y))
        return (local_x, local_y, pixel_w, pixel_h)

    def _scoped_to_target(self, element: MacElement) -> bool:
        """Whether an element belongs to THIS backend's exact app/window."""
        window = self._resolve_window()
        return (
            element.app_pid == window.pid
            and element.window_title == window.title
            and (
                self._title_substr is None
                or self._title_substr.casefold() in element.window_title.casefold()
            )
            and self._owner_substr.casefold() in window.owner.casefold()
        )

    def structural_locator_at(self, x: int, y: int) -> Optional[StructuralLocator]:
        """RECORD-time: a stable AX locator for the element at pixel (x, y)."""
        try:
            window = self._ensure_captured()
            screen_x, screen_y = self._pixel_to_screen(window, int(x), int(y))
        except MacOSBackendError:
            return None
        element = self._ax_client.element_at_point(
            window.pid, window.title, screen_x, screen_y
        )
        if element is None or not self._scoped_to_target(element):
            return None
        if self._in_window_pixel_rect(window, element.bounds) is None:
            return None
        if not element.accessible_id and not (element.role and element.name):
            return None
        return StructuralLocator(
            automation_id=element.accessible_id,
            role=element.role,
            name=element.name,
            window_name=window.title,
        )

    def _unique_candidate(self, locator: StructuralLocator) -> Optional[MacElement]:
        window = self._resolve_window()
        if (
            locator.window_name
            and locator.window_name.casefold() != window.title.casefold()
        ):
            return None
        result = self._ax_client.find_candidates(
            window.pid, window.title, locator, limit=_AX_ENUMERATION_LIMIT
        )
        if result.truncated:
            raise StructuralResolutionRefused(
                "AX candidate enumeration exceeded its bound; refusing "
                "partial/first-match resolution"
            )
        for candidate in result.candidates:
            if not self._scoped_to_target(candidate) or not self._locator_matches(
                candidate, locator
            ):
                raise StructuralResolutionRefused(
                    "AX client returned a candidate outside the exact "
                    "locator/app/window contract"
                )
        if not result.candidates:
            return None
        if len(result.candidates) != 1:
            raise StructuralResolutionRefused(
                f"AX locator is ambiguous: candidate_count={len(result.candidates)}"
            )
        return result.candidates[0]

    @staticmethod
    def _locator_matches(element: MacElement, locator: StructuralLocator) -> bool:
        if locator.automation_id:
            return element.accessible_id == locator.automation_id and (
                locator.role is None or element.role == locator.role
            )
        return (
            bool(locator.role)
            and bool(locator.name)
            and element.role == locator.role
            and element.name == locator.name
        )

    def locate_structural(
        self, locator: StructuralLocator
    ) -> Optional[StructuralHandle]:
        """REPLAY-time: find ``locator``'s unique element; refuse ambiguity."""
        if not locator.automation_id and not (locator.role and locator.name):
            return None
        window = self._ensure_captured()
        candidate = self._unique_candidate(locator)
        if candidate is None:
            return None
        rect = self._in_window_pixel_rect(window, candidate.bounds)
        if rect is None:
            return None
        local_x, local_y, pixel_w, pixel_h = rect
        return StructuralHandle(
            point=(local_x + pixel_w // 2, local_y + pixel_h // 2),
            region=(local_x, local_y, pixel_w, pixel_h),
            target_fingerprint=_fingerprint(candidate),
            candidate_count=1,
            supported_operations=list(candidate.supported_operations),
        )

    def structured_text_at(self, x: int, y: int) -> Optional[str]:
        """Return the exact AX text at/around pixel (x, y), or None."""
        try:
            window = self._ensure_captured()
            screen_x, screen_y = self._pixel_to_screen(window, int(x), int(y))
            element = self._ax_client.element_at_point(
                window.pid, window.title, screen_x, screen_y
            )
            if element is None or not self._scoped_to_target(element):
                return None
            return _clean_text(self._ax_client.structured_text(element))
        except Exception:  # noqa: BLE001 - a momentary AX failure is not fatal
            return None
