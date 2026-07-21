from __future__ import annotations

import io

import pytest
from PIL import Image

from openadapt_flow.backend import (
    Backend,
    IdentityBackend,
    NativeStructuralActionBackend,
    StructuralActionBackend,
    StructuralResolutionRefused,
)
from openadapt_flow.backends.factory import build_backend
from openadapt_flow.backends.linux_backend import (
    LinuxBackend,
    LinuxBackendError,
    LinuxCandidateSet,
    LinuxElement,
    LinuxWindow,
)
from openadapt_flow.deployment import BackendConfig
from openadapt_flow.ir import StructuralLocator

TARGET_WINDOW = LinuxWindow(
    "0.1",
    "gedit",
    "oa-trial.txt",
    9001,
    (100, 200, 640, 480),
)
TARGET_ELEMENT = LinuxElement(
    "0.1:2.4",
    "save-button",
    "button",
    "Save",
    "gedit",
    "oa-trial.txt",
    9001,
    (600, 220, 80, 40),
    ("invoke",),
)
TEXT_ELEMENT = LinuxElement(
    "0.1:2.2",
    "body",
    "textbox",
    "Document",
    "gedit",
    "oa-trial.txt",
    9001,
    (120, 260, 580, 380),
    ("focus",),
)


class FakeLinuxClient:
    def __init__(
        self,
        *,
        session_type: str = "x11",
        portal_ready: bool = False,
        windows: list[LinuxWindow] | None = None,
        candidates: list[LinuxElement] | None = None,
        active: bool = True,
        truncated: bool = False,
    ) -> None:
        self._session_type = session_type
        self._portal_ready = portal_ready
        self.windows = list(windows if windows is not None else [TARGET_WINDOW])
        self.candidates = list(
            candidates if candidates is not None else [TARGET_ELEMENT]
        )
        self.active = active
        self.truncated = truncated
        self.calls: list[tuple] = []
        self.text_at_point: LinuxElement | None = TARGET_ELEMENT
        self.text_value = "Account 100512"
        self.native_succeeds = True
        self.replace_succeeds = True
        self.physical_succeeds = True

    @property
    def session_type(self) -> str:
        return self._session_type

    def portal_session_ready(self) -> bool:
        return self._portal_ready

    def find_windows(self, app, title):
        return [
            window
            for window in self.windows
            if window.app_name.casefold() == app.casefold()
            and window.title.casefold() == title.casefold()
        ]

    def window_is_active(self, window):
        self.calls.append(("active", window.native_id))
        return self.active

    def focus_window(self, window):
        self.calls.append(("focus-window", window.native_id))
        self.active = True
        return True

    def capture_window(self, window):
        self.calls.append(("capture", window.native_id))
        image = Image.new("RGB", window.bounds[2:], (20, 30, 40))
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue(), image.width, image.height

    def element_at_point(self, window, x, y):
        self.calls.append(("element-at", window.native_id, x, y))
        return self.text_at_point

    def find_candidates(self, window, locator):
        self.calls.append(("find", window.native_id, locator))
        return LinuxCandidateSet(tuple(self.candidates), self.truncated)

    def structured_text(self, element):
        self.calls.append(("structured", element.native_id))
        return self.text_value

    def perform_native(self, element, operation):
        self.calls.append(("native", element.native_id, operation))
        if not self.native_succeeds:
            raise LinuxBackendError("native rejected")
        return f"atspi_{operation}"

    def replace_text(self, element, text):
        self.calls.append(("replace", element.native_id, text))
        return self.replace_succeeds

    def physical_click(self, x, y, *, double=False):
        self.calls.append(("physical-click", x, y, double))
        return self.physical_succeeds

    def physical_type_text(self, text):
        self.calls.append(("physical-type", text))
        return self.physical_succeeds

    def physical_press(self, key):
        self.calls.append(("physical-press", key))
        return self.physical_succeeds

    def physical_scroll(self, dx, dy):
        self.calls.append(("physical-scroll", dx, dy))
        return self.physical_succeeds


def backend(client: FakeLinuxClient | None = None, **kwargs) -> LinuxBackend:
    return LinuxBackend(
        client or FakeLinuxClient(),
        app="gedit",
        window_title="oa-trial.txt",
        **kwargs,
    )


def test_linux_backend_implements_existing_runtime_capabilities() -> None:
    target = backend()
    assert isinstance(target, Backend)
    assert isinstance(target, IdentityBackend)
    assert isinstance(target, StructuralActionBackend)
    assert isinstance(target, NativeStructuralActionBackend)


def test_import_and_injected_client_are_headless_safe() -> None:
    # Constructing with an injected client does not import GI or inspect DISPLAY.
    assert backend().viewport == (640, 480)


def test_exact_window_scope_refuses_zero_or_multiple_before_capture() -> None:
    missing = FakeLinuxClient(windows=[])
    with pytest.raises(LinuxBackendError, match="no Linux window"):
        backend(missing).screenshot()
    assert not any(call[0] == "capture" for call in missing.calls)

    duplicate = LinuxWindow("0.2", "gedit", "oa-trial.txt", 9002, (100, 200, 640, 480))
    ambiguous = FakeLinuxClient(windows=[TARGET_WINDOW, duplicate])
    with pytest.raises(LinuxBackendError, match="ambiguous.*2 windows"):
        backend(ambiguous).screenshot()
    assert not any(call[0] == "capture" for call in ambiguous.calls)


def test_wayland_requires_a_live_portal_session() -> None:
    with pytest.raises(LinuxBackendError, match="operator-approved.*Portal"):
        backend(FakeLinuxClient(session_type="wayland"))

    target = backend(FakeLinuxClient(session_type="wayland", portal_ready=True))
    assert target.viewport == (640, 480)


def test_headless_or_unknown_session_refuses() -> None:
    with pytest.raises(LinuxBackendError, match="headless"):
        backend(FakeLinuxClient(session_type="headless"))


def test_capture_is_window_scoped_and_requires_active_target() -> None:
    inactive = FakeLinuxClient(active=False)
    with pytest.raises(LinuxBackendError, match="occluding"):
        backend(inactive).screenshot()
    assert not any(call[0] == "capture" for call in inactive.calls)

    client = FakeLinuxClient()
    target = backend(client)
    assert Image.open(io.BytesIO(target.screenshot())).size == (640, 480)
    assert ("capture", TARGET_WINDOW.native_id) in client.calls


def test_record_locator_reuses_accessibility_id_and_exact_window() -> None:
    client = FakeLinuxClient()
    target = backend(client)
    locator = target.structural_locator_at(540, 40)
    assert locator == StructuralLocator(
        automation_id="save-button",
        role="button",
        name="Save",
        window_name="oa-trial.txt",
    )
    assert ("element-at", "0.1", 640, 240) in client.calls


def test_unique_locate_returns_window_relative_geometry_and_fingerprint() -> None:
    target = backend()
    handle = target.locate_structural(
        StructuralLocator(
            automation_id="save-button",
            role="button",
            name="Save",
            window_name="oa-trial.txt",
        )
    )
    assert handle is not None
    assert handle.point == (540, 40)
    assert handle.region == (500, 20, 80, 40)
    assert handle.candidate_count == 1
    assert handle.supported_operations == ["invoke"]
    assert handle.target_fingerprint is not None
    assert len(handle.target_fingerprint) == 64


def test_missing_window_name_or_target_is_an_ordinary_miss() -> None:
    target = backend()
    assert (
        target.locate_structural(
            StructuralLocator(
                automation_id="save-button",
                window_name="different.txt",
            )
        )
        is None
    )
    target._client.candidates = []
    assert (
        target.locate_structural(StructuralLocator(automation_id="save-button")) is None
    )


def test_ambiguous_or_truncated_enumeration_refuses_visual_fallthrough() -> None:
    duplicate = LinuxElement(
        "0.1:3.4",
        "save-button",
        "button",
        "Save",
        "gedit",
        "oa-trial.txt",
        9001,
        (500, 420, 80, 40),
        ("invoke",),
    )
    client = FakeLinuxClient(candidates=[TARGET_ELEMENT, duplicate])
    with pytest.raises(StructuralResolutionRefused, match="candidate_count=2"):
        backend(client).locate_structural(
            StructuralLocator(automation_id="save-button")
        )
    assert not any(call[0].startswith("physical") for call in client.calls)

    truncated = FakeLinuxClient(truncated=True)
    with pytest.raises(StructuralResolutionRefused, match="exceeded its bound"):
        backend(truncated).locate_structural(
            StructuralLocator(automation_id="save-button")
        )


def test_candidate_outside_configured_scope_refuses() -> None:
    escaped = LinuxElement(
        "0.2:2.4",
        "save-button",
        "button",
        "Save",
        "other-app",
        "other.txt",
        500,
        (600, 220, 80, 40),
        ("invoke",),
    )
    with pytest.raises(StructuralResolutionRefused, match="outside the exact"):
        backend(FakeLinuxClient(candidates=[escaped])).locate_structural(
            StructuralLocator(automation_id="save-button")
        )


def test_backend_rejects_client_candidate_that_does_not_match_locator() -> None:
    wrong = LinuxElement(
        TARGET_ELEMENT.native_id,
        "different-id",
        TARGET_ELEMENT.role,
        TARGET_ELEMENT.name,
        TARGET_ELEMENT.app_name,
        TARGET_ELEMENT.window_title,
        TARGET_ELEMENT.pid,
        TARGET_ELEMENT.bounds,
        TARGET_ELEMENT.supported_operations,
    )
    with pytest.raises(StructuralResolutionRefused, match="outside the exact"):
        backend(FakeLinuxClient(candidates=[wrong])).locate_structural(
            StructuralLocator(automation_id="save-button")
        )


def test_native_action_returns_delivery_only_receipt() -> None:
    client = FakeLinuxClient()
    target = backend(client)
    locator = StructuralLocator(
        automation_id="save-button",
        role="button",
        name="Save",
        window_name="oa-trial.txt",
    )
    handle = target.locate_structural(locator)
    assert handle is not None
    receipt = target.act_structural(locator, handle)
    assert receipt.status == "delivered"
    assert receipt.operation == "atspi_invoke"
    assert receipt.native is True
    assert receipt.outcome_verified is False
    assert receipt.target_fingerprint == handle.target_fingerprint
    assert ("native", TARGET_ELEMENT.native_id, "invoke") in client.calls


def test_stale_candidate_refuses_before_native_or_physical_input() -> None:
    client = FakeLinuxClient()
    target = backend(client)
    locator = StructuralLocator(automation_id="save-button")
    handle = target.locate_structural(locator)
    assert handle is not None
    client.candidates = [
        LinuxElement(
            TARGET_ELEMENT.native_id,
            TARGET_ELEMENT.accessible_id,
            TARGET_ELEMENT.role,
            TARGET_ELEMENT.name,
            TARGET_ELEMENT.app_name,
            TARGET_ELEMENT.window_title,
            TARGET_ELEMENT.pid,
            (610, 220, 80, 40),
            TARGET_ELEMENT.supported_operations,
        )
    ]
    with pytest.raises(LinuxBackendError, match="changed between"):
        target.act_structural(locator, handle)
    assert not any(call[0] in {"native", "physical-click"} for call in client.calls)


def test_native_focus_then_editable_text_rechecks_exact_target() -> None:
    client = FakeLinuxClient(candidates=[TEXT_ELEMENT])
    target = backend(client)
    locator = StructuralLocator(
        automation_id="body",
        role="textbox",
        name="Document",
        window_name="oa-trial.txt",
    )
    handle = target.locate_structural(locator)
    assert handle is not None
    receipt = target.act_structural(locator, handle)
    target.type_text("Résumé — 東京")
    assert receipt.operation == "atspi_focus"
    assert ("replace", TEXT_ELEMENT.native_id, "Résumé — 東京") in client.calls
    assert not any(call[0] == "physical-type" for call in client.calls)


def test_non_focus_native_action_clears_cached_editable_target() -> None:
    client = FakeLinuxClient(candidates=[TEXT_ELEMENT])
    target = backend(client)
    text_locator = StructuralLocator(automation_id="body")
    text_handle = target.locate_structural(text_locator)
    assert text_handle is not None
    target.act_structural(text_locator, text_handle)

    client.candidates = [TARGET_ELEMENT]
    button_locator = StructuralLocator(automation_id="save-button")
    button_handle = target.locate_structural(button_locator)
    assert button_handle is not None
    target.act_structural(button_locator, button_handle)

    with pytest.raises(LinuxBackendError, match="no verified AT-SPI"):
        target.type_text("must not reach the old field")
    assert not any(
        call[:2] == ("replace", TEXT_ELEMENT.native_id)
        and call[2] == "must not reach the old field"
        for call in client.calls
    )


def test_editable_text_failure_is_not_silently_reported() -> None:
    client = FakeLinuxClient(candidates=[TEXT_ELEMENT])
    client.replace_succeeds = False
    target = backend(client)
    locator = StructuralLocator(automation_id="body")
    handle = target.locate_structural(locator)
    assert handle is not None
    target.act_structural(locator, handle)
    with pytest.raises(LinuxBackendError, match="unavailable or rejected"):
        target.type_text("not delivered")


def test_physical_input_is_disabled_by_default() -> None:
    client = FakeLinuxClient()
    target = backend(client)
    with pytest.raises(LinuxBackendError, match="coordinate input is disabled"):
        target.click(10, 10)
    with pytest.raises(LinuxBackendError, match="keyboard synthesis is disabled"):
        target.press("Enter")
    with pytest.raises(LinuxBackendError, match="scroll synthesis is disabled"):
        target.scroll(0, 120)
    assert not any(call[0].startswith("physical") for call in client.calls)


def test_explicit_physical_fallback_is_window_bound() -> None:
    no_native = LinuxElement(
        "0.1:5",
        "canvas-target",
        "canvas",
        "Canvas",
        "gedit",
        "oa-trial.txt",
        9001,
        (300, 300, 40, 20),
        (),
    )
    client = FakeLinuxClient(candidates=[no_native])
    target = backend(client, allow_physical_input=True)
    locator = StructuralLocator(automation_id="canvas-target")
    handle = target.locate_structural(locator)
    assert handle is not None
    receipt = target.act_structural(locator, handle)
    assert receipt.operation == "physical_click"
    assert receipt.native is False
    assert receipt.outcome_verified is False
    assert ("focus-window", TARGET_WINDOW.native_id) in client.calls
    assert ("physical-click", 320, 310, False) in client.calls


def test_structured_text_is_exact_atspi_text_or_none() -> None:
    client = FakeLinuxClient()
    target = backend(client)
    assert target.structured_text_at(540, 40) == "Account 100512"
    client.text_at_point = None
    assert target.structured_text_at(540, 40) is None


def test_factory_requires_exact_target_and_threads_physical_opt_in() -> None:
    with pytest.raises(ValueError, match="requires backend.linux_app"):
        build_backend(BackendConfig(kind="linux"))
    with pytest.raises(ValueError, match="requires backend.linux_window_title"):
        build_backend(BackendConfig(kind="linux", linux_app="gedit"))

    client = FakeLinuxClient()
    target = build_backend(
        BackendConfig(
            kind="linux",
            linux_app="gedit",
            linux_window_title="oa-trial.txt",
            linux_allow_physical_input=True,
        ),
        linux_client=client,
    )
    assert isinstance(target, LinuxBackend)
    assert target._allow_physical_input is True
