from __future__ import annotations

import io

import pytest
from PIL import Image

from openadapt_flow.backend import (
    Backend,
    ExecutionContextIdentityBackend,
    FreshActuationRequired,
    IdentityBackend,
    StructuralActionBackend,
)
from openadapt_flow.backends import macos_backend as macos_backend_module
from openadapt_flow.backends.factory import build_backend
from openadapt_flow.backends.macos_backend import MacOSBackend, MacOSBackendError
from openadapt_flow.backends.remote_display import WindowInfo
from openadapt_flow.deployment import BackendConfig


class FakeMacClient:
    def __init__(
        self,
        *,
        capture_trusted: bool = True,
        input_trusted: bool = True,
        windows: list[WindowInfo] | None = None,
        frontmost_window_id: int | None = 41,
        raise_succeeds: bool = True,
        activation_succeeds: bool = True,
        replace_succeeds: bool = True,
    ) -> None:
        self._capture_trusted = capture_trusted
        self._input_trusted = input_trusted
        self.windows = windows or [
            WindowInfo(41, "TextEdit", "oa-trial.txt", 9001, (10, 20, 400, 300))
        ]
        self._frontmost_window_id = frontmost_window_id
        self._raise_succeeds = raise_succeeds
        self._activation_succeeds = activation_succeeds
        self._replace_succeeds = replace_succeeds
        self._active_pid = 9001
        self._ax_focused_pid: int | None = 9001
        self._exact_ax_focus = True
        self._point_window_id = frontmost_window_id
        self._focused_element_token: object = object()
        self._focus_matches_point = True
        self.capture_color = (20, 30, 40)
        self.calls: list[tuple] = []

    def capture_trusted(self) -> bool:
        return self._capture_trusted

    def input_trusted(self) -> bool:
        return self._input_trusted

    def request_capture_access(self) -> bool:
        return self._capture_trusted

    def request_input_access(self) -> bool:
        return self._input_trusted

    def find_windows(self, owner_substr, title_substr):
        return [
            window
            for window in self.windows
            if owner_substr.lower() in window.owner.lower()
            and (title_substr is None or title_substr.lower() in window.title.lower())
        ]

    def find_window(self, owner_substr, title_substr):
        matches = self.find_windows(owner_substr, title_substr)
        return matches[0] if matches else None

    def capture(self, window_id):
        image = Image.new("RGB", (800, 600), self.capture_color)
        output = io.BytesIO()
        image.save(output, format="PNG")
        self.calls.append(("capture", window_id))
        return output.getvalue(), 800, 600

    def frontmost_pid(self):
        return self._active_pid

    def frontmost_window_id(self):
        return self._frontmost_window_id

    def key_window_id(self, pid):
        if pid != self._active_pid:
            return None
        return self._frontmost_window_id

    def focused_application_pid(self):
        return self._ax_focused_pid

    def window_id_at_point(self, _x, _y):
        return self._point_window_id

    def window_at_point(self, _x, _y):
        return self._point_window_id

    def activate(self, pid):
        self.calls.append(("activate", pid))
        if self._activation_succeeds:
            self._active_pid = pid
            self._ax_focused_pid = pid

    def raise_window(self, window):
        self.calls.append(("raise", window.window_id))
        if self._raise_succeeds:
            self._frontmost_window_id = window.window_id
            if self._point_window_id is None:
                self._point_window_id = window.window_id
        return self._raise_succeeds

    def exact_window_focused_main(self, window):
        self.calls.append(("focus-proof", window.window_id))
        return self._exact_ax_focus

    def focused_element_token(self, window):
        self.calls.append(("focused-element", window.window_id))
        return self._focused_element_token

    def focused_element_token_at_point(self, window, x, y):
        self.calls.append(("focused-element-at-point", window.window_id, x, y))
        return self._focused_element_token if self._focus_matches_point else None

    def focused_element_matches(self, window, expected):
        self.calls.append(("focused-element-matches", window.window_id))
        return expected is self._focused_element_token

    def mouse(self, x, y, *, button, down, click_count):
        self.calls.append(("mouse", x, y, button, down, click_count))

    def mouse_move(self, x, y):
        self.calls.append(("move", x, y))

    def type_chars(self, text):
        raise AssertionError("native backend must not use guest-scancode text")

    def replace_selected_text(self, window, text):
        self.calls.append(("replace", window.window_id, text))
        return self._replace_succeeds

    def replace_selected_text_guarded(
        self,
        window,
        text,
        *,
        expected_focused_element,
    ):
        self.calls.append(("guarded-replace-attempt", window.window_id))
        if expected_focused_element is not self._focused_element_token:
            return False
        self.calls.append(("replace", window.window_id, text))
        return self._replace_succeeds

    def key(self, keycode, *, down, flags):
        self.calls.append(("key", keycode, down, tuple(flags)))

    def scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))


def backend(client: FakeMacClient | None = None, **kwargs) -> MacOSBackend:
    return MacOSBackend(
        client or FakeMacClient(),
        app="TextEdit",
        window_title="oa-trial",
        settle_s=0,
        foreground_settle_s=0,
        **kwargs,
    )


def test_native_backend_exposes_ax_structured_identity_capability() -> None:
    target = backend()
    assert isinstance(target, Backend)
    # The AX tree gives macOS a structured layer, so — like the browser DOM,
    # Windows UIA, and Linux AT-SPI backends — it advertises structured-text
    # identity and the deterministic structural action rung. Detailed behavior
    # (record/locate/refuse) is covered in test_macos_structural.py.
    assert isinstance(target, IdentityBackend)
    assert isinstance(target, StructuralActionBackend)


def test_execution_context_identity_is_live_and_title_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_session = {"value": 7171}
    monkeypatch.setattr(
        macos_backend_module,
        "_mac_audit_session_id",
        lambda: audit_session["value"],
    )
    title = "Patient Alice Example MRN 100512"
    window = WindowInfo(41, "Accuro EMR", title, 9001, (10, 20, 400, 300))
    client = FakeMacClient(windows=[window])
    target = MacOSBackend(client, app="Accuro EMR", window_title=title)

    assert isinstance(target, ExecutionContextIdentityBackend)
    assert target.application_identity() == "accuro-emr"
    session = target.session_identity()
    assert session is not None and len(session) == 64 and session == session.casefold()
    assert title not in target.application_identity()
    assert target.workflow_state_identity() is None

    client.windows = [WindowInfo(42, "Unrelated App", title, 9002, (10, 20, 400, 300))]
    assert target.application_identity() is None
    assert target.session_identity() == session

    audit_session["value"] = 7272
    assert target.session_identity() != session


def test_target_window_capture_and_exact_focused_text_delivery() -> None:
    client = FakeMacClient()
    target = backend(client)
    assert target.viewport == (800, 600)
    target.type_text("Résumé — 東京")
    assert ("capture", 41) in client.calls
    assert ("replace", 41, "Résumé — 東京") in client.calls


def test_consequential_text_refuses_changed_frame_before_ax_delivery() -> None:
    client = FakeMacClient()
    target = backend(client)
    target.acquire_actuation_frame()
    target.arm_focused_element_lease(100, 100)
    client.capture_color = (50, 60, 70)

    with pytest.raises(FreshActuationRequired, match="frame content changed") as raised:
        target.type_text("must not land")

    assert isinstance(raised.value, MacOSBackendError)
    assert not any(call[0] == "replace" for call in client.calls)


def test_replayer_reacquires_macos_lease_after_zero_edge_change(tmp_path) -> None:
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime.replayer import Replayer
    from tests.test_replayer import FakeVision, Match, click_step, make_png

    class OneMismatchMacBackend(MacOSBackend):
        def __init__(self, client):
            super().__init__(
                client,
                app="TextEdit",
                window_title="oa-trial",
                settle_s=0,
                foreground_settle_s=0,
            )
            self.click_attempts = 0

        def click(self, x, y, *, double=False):
            self.click_attempts += 1
            if self.click_attempts == 1:
                self._mac_client.capture_color = (50, 60, 70)
            return super().click(x, y, double=double)

    client = FakeMacClient()
    target = OneMismatchMacBackend(client)
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
        for _ in range(3)
    ]
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "btn.png").write_bytes(make_png((50, 20)))

    report = Replayer(target, vision=vision).run(
        Workflow(name="mac-retry", steps=[click_step(risk="irreversible")]),
        bundle_dir=bundle,
        run_dir=tmp_path / "run",
    )

    assert report.success is True, report
    assert target.click_attempts == 2
    assert len([call for call in client.calls if call[0] == "mouse"]) == 2
    assert [event.retried for event in report.results[0].fresh_actuation_events] == [
        True
    ]


def test_consequential_text_uses_same_focused_ax_element_as_actuation_frame() -> None:
    client = FakeMacClient()
    target = backend(client)
    target.acquire_actuation_frame()
    target.arm_focused_element_lease(100, 100)

    target.type_text("bound delivery")

    assert ("guarded-replace-attempt", 41) in client.calls
    assert ("replace", 41, "bound delivery") in client.calls


def test_consequential_text_refuses_focus_outside_resolved_target() -> None:
    client = FakeMacClient()
    client._focus_matches_point = False
    target = backend(client)
    target.acquire_actuation_frame()

    with pytest.raises(MacOSBackendError, match="does not match"):
        target.arm_focused_element_lease(100, 100)

    assert not any(call[0] == "replace" for call in client.calls)


def test_consequential_text_refuses_unchanged_frame_focus_switch() -> None:
    client = FakeMacClient()
    target = backend(client)
    target.acquire_actuation_frame()
    target.arm_focused_element_lease(100, 100)
    client._focused_element_token = object()

    with pytest.raises(MacOSBackendError, match="not writable"):
        target.type_text("must not land")

    assert ("guarded-replace-attempt", 41) in client.calls
    assert not any(call[0] == "replace" for call in client.calls)


def test_consequential_key_refuses_unchanged_frame_focus_switch() -> None:
    client = FakeMacClient()
    target = backend(client)
    target.acquire_actuation_frame()
    target.arm_focused_element_lease(100, 100)
    client._focused_element_token = object()

    with pytest.raises(MacOSBackendError, match="focus changed"):
        target.press("Enter")

    assert not any(call[0] == "key" for call in client.calls)


def test_control_or_meta_and_meta_use_native_command() -> None:
    client = FakeMacClient()
    target = backend(client)
    target.press("ControlOrMeta+a")
    target.press("Meta+s")
    key_calls = [call for call in client.calls if call[0] == "key"]
    assert key_calls[0][1:] == (0x00, True, ("command",))
    assert key_calls[1][1:] == (0x00, False, ("command",))
    assert key_calls[2][1:] == (0x01, True, ("command",))
    assert key_calls[3][1:] == (0x01, False, ("command",))


def test_explicit_control_stays_control() -> None:
    client = FakeMacClient()
    backend(client).press("Control+a")
    key_calls = [call for call in client.calls if call[0] == "key"]
    assert all(call[3] == ("control",) for call in key_calls)


def test_ambiguous_selector_refuses_first_match_before_capture_or_input() -> None:
    windows = [
        WindowInfo(41, "TextEdit", "oa-trial-a.txt", 1, (0, 0, 400, 300)),
        WindowInfo(42, "TextEdit", "oa-trial-b.txt", 2, (0, 0, 400, 300)),
    ]
    client = FakeMacClient(windows=windows)
    target = backend(client)
    with pytest.raises(MacOSBackendError, match="ambiguous.*2 windows"):
        target.screenshot()
    assert not client.calls


def test_ambiguous_selector_refuses_bound_text_without_any_fallback() -> None:
    windows = [
        WindowInfo(41, "TextEdit", "oa-trial-a.txt", 9001, (0, 0, 400, 300)),
        WindowInfo(42, "TextEdit", "oa-trial-b.txt", 9002, (0, 0, 400, 300)),
    ]
    client = FakeMacClient(windows=windows)
    with pytest.raises(MacOSBackendError, match="ambiguous.*2 windows"):
        backend(client).type_text("must not land")
    assert not client.calls


def test_capture_and_input_permissions_fail_loud() -> None:
    capture_client = FakeMacClient(capture_trusted=False)
    with pytest.raises(MacOSBackendError, match="Screen Recording"):
        backend(capture_client).screenshot()
    assert not capture_client.calls

    input_client = FakeMacClient(input_trusted=False)
    with pytest.raises(MacOSBackendError, match="Accessibility"):
        backend(input_client).type_text("must not land")
    assert not input_client.calls


def test_base_pixel_point_gate_accepts_captured_coordinates_without_input() -> None:
    client = FakeMacClient()
    target = backend(client)
    target.screenshot()
    target._ensure_input_ready(point=(10, 10))
    assert not any(call[0] in {"mouse", "key", "replace"} for call in client.calls)


def test_input_refuses_when_exact_window_is_not_topmost() -> None:
    client = FakeMacClient(frontmost_window_id=999, raise_succeeds=False)
    with pytest.raises(MacOSBackendError, match="not the topmost window"):
        backend(client, foreground_retries=2).press("Enter")
    assert not any(call[0] in {"key", "unicode", "mouse"} for call in client.calls)


def test_bound_ax_text_refuses_when_exact_cg_window_is_not_topmost() -> None:
    client = FakeMacClient(frontmost_window_id=999, raise_succeeds=False)
    with pytest.raises(MacOSBackendError, match="could not be bound"):
        backend(client, foreground_retries=2).type_text("must not land")
    assert not any(call[0] in {"replace", "key", "mouse"} for call in client.calls)


def test_global_key_refuses_when_ax_focused_app_pid_is_wrong() -> None:
    client = FakeMacClient(
        frontmost_window_id=41,
        activation_succeeds=False,
    )
    client._ax_focused_pid = 777
    with pytest.raises(MacOSBackendError, match="not the topmost window"):
        backend(client, foreground_retries=2).press("ControlOrMeta+w")
    assert not any(call[0] in {"replace", "key", "mouse"} for call in client.calls)


def test_coordinate_input_refuses_when_ax_focused_app_pid_is_unavailable() -> None:
    client = FakeMacClient(
        frontmost_window_id=41,
        activation_succeeds=False,
    )
    client._ax_focused_pid = None
    with pytest.raises(MacOSBackendError, match="not the topmost window"):
        backend(client, foreground_retries=2).click(10, 10)
    assert not any(call[0] in {"replace", "key", "mouse"} for call in client.calls)


def test_coordinate_input_refuses_non_target_overlay_at_click_point() -> None:
    client = FakeMacClient()
    client._point_window_id = 999
    with pytest.raises(MacOSBackendError, match="stale window mapping"):
        backend(client).click(10, 10)
    assert not any(call[0] in {"move", "mouse"} for call in client.calls)


def test_coordinate_input_refuses_when_exact_ax_focus_proof_disagrees() -> None:
    client = FakeMacClient()
    client._exact_ax_focus = False
    with pytest.raises(MacOSBackendError, match="stale window mapping"):
        backend(client).click(10, 10)
    assert not any(call[0] in {"move", "mouse"} for call in client.calls)


def test_click_refreshes_moved_window_before_mapping() -> None:
    client = FakeMacClient()
    target = backend(client)
    target.screenshot()
    client.windows = [
        WindowInfo(41, "TextEdit", "oa-trial.txt", 9001, (110, 220, 400, 300))
    ]

    target.click(100, 200)

    assert ("move", 160.0, 320.0) in client.calls


@pytest.mark.parametrize(
    "replacement",
    [
        WindowInfo(41, "TextEdit", "oa-trial.txt", 9001, (10, 20, 500, 300)),
        WindowInfo(99, "TextEdit", "oa-trial.txt", 9001, (10, 20, 400, 300)),
    ],
)
def test_click_refuses_resized_or_reopened_window_after_capture(replacement) -> None:
    client = FakeMacClient()
    target = backend(client)
    target.screenshot()
    client.windows = [replacement]

    with pytest.raises(MacOSBackendError, match="resized, reopened, or retitled"):
        target.click(100, 200)
    assert not any(call[0] in {"move", "mouse"} for call in client.calls)


def test_click_refuses_window_move_between_pointer_move_and_mouse_down() -> None:
    class MovingClient(FakeMacClient):
        def mouse_move(self, x, y):
            super().mouse_move(x, y)
            current = self.windows[0]
            self.windows = [
                WindowInfo(
                    current.window_id,
                    current.owner,
                    current.title,
                    current.pid,
                    (current.bounds[0] + 20, *current.bounds[1:]),
                )
            ]

    client = MovingClient()
    with pytest.raises(MacOSBackendError, match="stale window mapping"):
        backend(client).click(100, 200)
    assert any(call[0] == "move" for call in client.calls)
    assert not any(call[0] == "mouse" and call[4] is True for call in client.calls)


def test_click_releases_button_then_refuses_race_after_mouse_down() -> None:
    class MovingClient(FakeMacClient):
        def mouse(self, x, y, *, button, down, click_count):
            super().mouse(
                x,
                y,
                button=button,
                down=down,
                click_count=click_count,
            )
            if down:
                current = self.windows[0]
                self.windows = [
                    WindowInfo(
                        current.window_id,
                        current.owner,
                        current.title,
                        current.pid,
                        (current.bounds[0] + 20, *current.bounds[1:]),
                    )
                ]

    client = MovingClient()
    with pytest.raises(MacOSBackendError, match="stale window mapping"):
        backend(client).click(100, 200, double=True)
    transitions = [call for call in client.calls if call[0] == "mouse"]
    assert [call[4] for call in transitions] == [True, False]


def test_native_scroll_refuses_until_point_bound() -> None:
    client = FakeMacClient()
    with pytest.raises(MacOSBackendError, match="not point-bound"):
        backend(client).scroll(0, 120)
    assert not any(call[0] == "scroll" for call in client.calls)


def test_bound_ax_text_allows_lagging_nsworkspace_pid_only() -> None:
    client = FakeMacClient(
        frontmost_window_id=41,
        activation_succeeds=False,
    )
    client._active_pid = 777
    backend(client, foreground_retries=2).type_text("exact bound text")
    assert ("replace", 41, "exact bound text") in client.calls
    assert not any(call[0] in {"key", "mouse"} for call in client.calls)


def test_global_key_uses_ax_focused_pid_when_nsworkspace_is_stale() -> None:
    client = FakeMacClient(
        frontmost_window_id=41,
        activation_succeeds=False,
    )
    client._active_pid = 777
    client._ax_focused_pid = 9001
    backend(client, foreground_retries=2).press("ControlOrMeta+w")
    assert any(call[0] == "key" for call in client.calls)


def test_input_refuses_when_cg_target_is_topmost_but_ax_focus_is_unproven() -> None:
    client = FakeMacClient(frontmost_window_id=41, raise_succeeds=False)
    with pytest.raises(MacOSBackendError, match="could not be bound"):
        backend(client, foreground_retries=2).type_text("must not land")
    assert not any(call[0] in {"replace", "key", "mouse"} for call in client.calls)


def test_text_delivery_refuses_when_focused_ax_element_is_unverified() -> None:
    client = FakeMacClient(replace_succeeds=False)
    with pytest.raises(MacOSBackendError, match="not writable"):
        backend(client).type_text("must not be reported as delivered")
    assert ("replace", 41, "must not be reported as delivered") in client.calls
    assert not any(call[0] in {"key", "mouse"} for call in client.calls)


def test_input_raises_exact_target_above_same_process_sibling() -> None:
    windows = [
        WindowInfo(41, "TextEdit", "oa-trial.txt", 9001, (10, 20, 400, 300)),
        WindowInfo(42, "TextEdit", "restored.txt", 9001, (20, 30, 400, 300)),
    ]
    client = FakeMacClient(windows=windows, frontmost_window_id=42)
    backend(client).press("Enter")
    assert ("raise", 41) in client.calls
    assert any(call[0] == "key" for call in client.calls)


def test_title_disappearing_during_raise_refuses_input() -> None:
    class TitleRaceClient(FakeMacClient):
        def raise_window(self, window):
            self.calls.append(("raise", window.window_id))
            self.windows = [
                WindowInfo(
                    window.window_id,
                    window.owner,
                    "renamed.txt",
                    window.pid,
                    window.bounds,
                )
            ]
            return True

    client = TitleRaceClient()
    with pytest.raises(MacOSBackendError, match="no native macOS window"):
        backend(client).press("Enter")
    assert not any(call[0] in {"key", "unicode", "mouse"} for call in client.calls)


def test_factory_requires_app_and_threads_unique_title() -> None:
    with pytest.raises(ValueError, match="requires backend.macos_app"):
        build_backend(BackendConfig(kind="macos"))

    client = FakeMacClient()
    target = build_backend(
        BackendConfig(
            kind="macos",
            macos_app="TextEdit",
            macos_window_title="oa-trial",
        ),
        macos_client=client,
    )
    assert isinstance(target, MacOSBackend)
    assert target._owner_substr == "TextEdit"
    assert target._title_substr == "oa-trial"
