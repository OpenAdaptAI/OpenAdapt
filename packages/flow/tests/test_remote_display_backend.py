"""Offline unit tests for the remote-display (Citrix-analog) pixel backend.

These run with NO live window and NO macOS permissions: a fake
:class:`WindowClient` records every call so the load-bearing logic is asserted
directly — the captured-pixel<->screen-point coordinate mapping (the DPI/scale
gap a real Citrix Workspace window also imposes), the fail-LOUD contract when
input cannot be delivered (a dropped click must never look like success), the
frontmost/occlusion requirement, keycode-based typing (a remote display forwards
scancodes, not synthetic Unicode), and — critically — that record targeting and
record identity remain pixel-only while explicit PHI-free markers can provide
live execution-context identity.
"""

from __future__ import annotations

import hashlib
import io

import pytest
from PIL import Image

from openadapt_flow.backend import (
    ActionDeliveryUncertain,
    Backend,
    ExecutionContextIdentityBackend,
    FreshActuationRequired,
    IdentityBackend,
    PreparedPointerActuationBackend,
    StructuralActionBackend,
    StructuralBackend,
    StructuralResolutionRefused,
)
from openadapt_flow.backends.remote_display import (
    RemoteDisplayBackend,
    RemoteDisplayError,
    RemoteInputRefused,
    WindowInfo,
    _canonical_rgb_digest,
    _split_chord,
    resolve_mac_key,
)
from openadapt_flow.remote_frame_contract import RemoteFrameContract
from openadapt_flow.runtime.resolver import visual_resolution_point_fingerprint


class FakeClient:
    """A scripted :class:`WindowClient` that records calls (no macOS bindings)."""

    def __init__(
        self,
        *,
        trusted: bool = True,
        frontmost: bool = True,
        window: WindowInfo | None = None,
        px: tuple[int, int] = (3024, 1888),
        key_window_id: int | None = None,
        hit_window_id: int | None = None,
    ) -> None:
        self.trusted = trusted
        self._frontmost = frontmost
        self.px = px
        self.window = (
            window
            if window is not None
            else WindowInfo(
                window_id=1,
                owner="Parallels Desktop",
                title="Windows 11",
                pid=99,
                bounds=(0.0, 38.0, 1512.0, 944.0),
                on_screen=True,
            )
        )
        self.windows = [self.window]
        self._key_window_id = key_window_id
        self._hit_window_id = hit_window_id
        self.frame_color = (11, 22, 33)
        self.frame_overrides: dict[tuple[int, int], tuple[int, int, int]] = {}
        self.png_kwargs = {}
        self.calls: list[tuple] = []

    def input_trusted(self) -> bool:
        return self.trusted

    def frontmost_pid(self):
        return self.window.pid if self._frontmost else 7

    def find_windows(self, owner, title):
        return [
            win
            for win in self.windows
            if win.owner.casefold() == owner.casefold()
            and (title is None or win.title.casefold() == title.casefold())
        ]

    def key_window_id(self, pid):
        if not self._frontmost:
            return None
        return self._key_window_id or self.window.window_id

    def window_at_point(self, x, y):
        return self._hit_window_id or self.window.window_id

    def capture(self, window_id):
        img = Image.new("RGB", self.px, self.frame_color)
        for point, color in self.frame_overrides.items():
            img.putpixel(point, color)
        buf = io.BytesIO()
        img.save(buf, format="PNG", **self.png_kwargs)
        return buf.getvalue(), self.px[0], self.px[1]

    def activate(self, pid):
        self.calls.append(("activate", pid))

    def mouse(self, x, y, *, button, down, click_count):
        self.calls.append(
            ("mouse", round(x, 1), round(y, 1), button, down, click_count)
        )

    def mouse_move(self, x, y):
        self.calls.append(("move", round(x, 1), round(y, 1)))

    def type_chars(self, text):
        self.calls.append(("type", text))

    def key(self, keycode, *, down, flags):
        self.calls.append(("key", keycode, down, tuple(flags)))

    def scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))

    def resolve_key(self, token):
        # The mac resolution rules: named keys unshifted, chars may add Shift.
        return resolve_mac_key(token)


def _backend(**kw) -> tuple[RemoteDisplayBackend, FakeClient]:
    client = FakeClient(**kw)
    return RemoteDisplayBackend(client=client, settle_s=0.0), client


def test_exposes_pixel_targeting_plus_context_identity_protocol() -> None:
    """No UIA/structured record identity; context markers are a separate seam."""
    backend, _ = _backend()
    assert isinstance(backend, Backend)
    assert isinstance(backend, ExecutionContextIdentityBackend)
    assert isinstance(backend, PreparedPointerActuationBackend)
    assert not isinstance(backend, StructuralActionBackend)
    assert not isinstance(backend, IdentityBackend)
    assert not isinstance(backend, StructuralBackend)
    assert not hasattr(backend, "structural_locator_at")
    assert not hasattr(backend, "structured_text_at")
    assert not hasattr(backend, "locate_structural")


def test_live_context_markers_preserve_an_unchanged_actuation_lease() -> None:
    sensitive_title = "Patient Jane Doe - Account 004271"
    client = FakeClient(
        window=WindowInfo(
            window_id=1,
            owner="Parallels Desktop",
            title=sensitive_title,
            pid=99,
            bounds=(0.0, 38.0, 1512.0, 944.0),
        )
    )

    def expected_frame(png: bytes) -> bool:
        return Image.open(io.BytesIO(png)).convert("RGB").getpixel((0, 0)) == (
            11,
            22,
            33,
        )

    backend = RemoteDisplayBackend(
        client=client,
        settle_s=0.0,
        application_marker="Accuro",
        application_marker_probe=expected_frame,
        workflow_state_marker="Appointment entry",
        workflow_state_marker_probe=expected_frame,
        session_marker="clinic-session-7",
        session_marker_probe=expected_frame,
    )
    backend.screenshot()
    backend.prepare_pointer_actuation(100, 100)
    backend.acquire_actuation_frame()

    application = backend.application_identity()
    workflow_state = backend.workflow_state_identity()
    session = backend.session_identity()

    assert application == "Accuro"
    assert workflow_state == "Appointment entry"
    assert (
        session
        == hashlib.sha256(
            b"openadapt.remote-session-marker.v1\x00clinic-session-7"
        ).hexdigest()
    )
    assert sensitive_title not in repr((application, workflow_state, session))

    backend.click(100, 100)
    assert any(call[0] == "mouse" for call in client.calls)


def test_absent_application_marker_returns_none_and_invalidates_lease() -> None:
    client = FakeClient()
    backend = RemoteDisplayBackend(
        client=client,
        settle_s=0.0,
        application_marker="Accuro",
        application_marker_probe=lambda _png: False,
    )
    backend.screenshot()
    backend.prepare_pointer_actuation(100, 100)
    backend.acquire_actuation_frame()

    assert backend.application_identity() is None
    with pytest.raises(RemoteDisplayError, match="invalidated"):
        backend.click(100, 100)
    assert not any(call[0] == "mouse" for call in client.calls)


def test_qualification_environment_rechecks_version_on_actuation_frame() -> None:
    client = FakeClient()
    version_ok = {"value": True}
    backend = RemoteDisplayBackend(
        client=client,
        settle_s=0.0,
        application_marker="Accuro",
        application_marker_probe=lambda _png: True,
        application_version_marker="8.0.0.3",
        application_version_marker_probe=lambda _png: version_ok["value"],
        environment_marker="clinic-remote-environment",
        environment_marker_probe=lambda _png: True,
        session_marker="clinic-session-qualification",
        session_marker_probe=lambda _png: True,
    )
    assert backend.qualification_environment_identity() is not None
    version_ok["value"] = False

    with pytest.raises(
        RemoteDisplayError, match="application, version, environment, or session"
    ):
        backend.acquire_actuation_frame()
    assert not any(call[0] == "mouse" for call in client.calls)


def test_context_observation_refuses_mutated_frame_before_input() -> None:
    client = FakeClient()
    backend = RemoteDisplayBackend(
        client=client,
        settle_s=0.0,
        workflow_state_marker="Ready to submit",
        workflow_state_marker_probe=lambda _png: True,
    )
    backend.screenshot()
    backend.prepare_pointer_actuation(100, 100)
    backend.acquire_actuation_frame()
    client.frame_color = (11, 22, 34)

    assert backend.workflow_state_identity() is None
    with pytest.raises(RemoteDisplayError, match="invalidated"):
        backend.click(100, 100)
    assert not any(call[0] == "mouse" for call in client.calls)


def test_hover_content_change_is_included_in_final_actuation_frame() -> None:
    class HoverChangingClient(FakeClient):
        def mouse_move(self, x, y):
            super().mouse_move(x, y)
            self.frame_color = (33, 22, 11)

    client = HoverChangingClient()
    backend = RemoteDisplayBackend(
        client=client,
        settle_s=0.0,
        application_marker="Accuro",
        application_marker_probe=lambda _png: True,
    )
    backend.screenshot()
    backend.prepare_pointer_actuation(100, 100)
    backend.acquire_actuation_frame()
    assert backend.application_identity() == "Accuro"

    backend.click(100, 100)

    assert [call[0] for call in client.calls].count("move") == 1
    assert [call[0] for call in client.calls].count("mouse") == 2


def test_delayed_remote_hover_paint_settles_before_final_frame() -> None:
    class DelayedHoverClient(FakeClient):
        captures_after_move = 0

        def mouse_move(self, x, y):
            super().mouse_move(x, y)
            self.captures_after_move = 0

        def capture(self, window_id):
            if any(call[0] == "move" for call in self.calls):
                self.captures_after_move += 1
                # A, B, B, C, C, C, C: two identical intermediate frames are
                # not enough to prove the asynchronously painted hover settled.
                if self.captures_after_move == 2:
                    self.frame_color = (22, 33, 11)
                elif self.captures_after_move == 4:
                    self.frame_color = (33, 22, 11)
            return super().capture(window_id)

    window = WindowInfo(
        window_id=1,
        owner="Parallels Desktop",
        title="Windows 11",
        pid=99,
        bounds=(0.0, 0.0, 200.0, 100.0),
        on_screen=True,
    )
    client = DelayedHoverClient(window=window, px=(200, 100))
    backend = RemoteDisplayBackend(
        client=client,
        settle_s=0.0,
        pointer_settle_stable_frames=4,
    )
    backend.screenshot()
    backend.prepare_pointer_actuation(100, 50)
    backend.acquire_actuation_frame()
    backend.click(100, 50)

    assert client.captures_after_move >= 7
    assert [call[0] for call in client.calls].count("move") == 1
    assert [call[0] for call in client.calls].count("mouse") == 2


def test_live_session_digest_change_invalidates_before_input() -> None:
    client = FakeClient()
    live = {"digest": "a" * 64}
    backend = RemoteDisplayBackend(
        client=client,
        settle_s=0.0,
        session_identity_observer=lambda: live["digest"],
    )
    backend.screenshot()
    backend.prepare_pointer_actuation(100, 100)
    backend.acquire_actuation_frame()
    assert backend.session_identity() == "a" * 64

    live["digest"] = "b" * 64
    assert backend.session_identity() is None
    with pytest.raises(RemoteDisplayError, match="invalidated"):
        backend.click(100, 100)
    assert not any(call[0] == "mouse" for call in client.calls)


def test_raw_or_malformed_session_values_are_never_exposed() -> None:
    sensitive = "Jane Doe encounter 004271"
    live = {"value": sensitive}
    backend = RemoteDisplayBackend(
        client=FakeClient(),
        settle_s=0.0,
        session_identity_observer=lambda: live["value"],
    )

    observed = backend.session_identity()

    assert observed is None
    assert sensitive not in repr(observed)
    live["value"] = "A" * 64
    assert backend.session_identity() is None


def test_context_marker_must_be_bounded_single_line_token() -> None:
    with pytest.raises(ValueError, match="printable PHI-free token"):
        RemoteDisplayBackend(
            client=FakeClient(),
            application_marker="Accuro\nPatient Jane Doe",
        )
    with pytest.raises(ValueError, match="at most 128 bytes"):
        RemoteDisplayBackend(
            client=FakeClient(),
            workflow_state_marker="x" * 129,
        )


def test_viewport_and_scale_from_capture() -> None:
    backend, _ = _backend(px=(3024, 1888))
    assert backend.viewport == (3024, 1888)
    backend.screenshot()
    assert backend._scale == pytest.approx(2.0)  # 3024 px / 1512 pt window


def test_new_capture_rebaselines_resize_and_cross_monitor_scale_between_actions() -> (
    None
):
    backend, client = _backend(px=(3024, 1888))
    backend.screenshot()
    old = client.window

    # Move the client to a left-side, non-Retina monitor and resize it. Negative
    # desktop coordinates are valid on a multi-monitor host. A new capture
    # establishes the new coordinate space before the next action.
    client.window = WindowInfo(
        window_id=old.window_id,
        owner=old.owner,
        title=old.title,
        pid=old.pid,
        bounds=(-1280.0, 100.0, 1280.0, 800.0),
        on_screen=True,
    )
    client.windows = [client.window]
    client.px = (1280, 800)
    backend.screenshot()
    backend.click(640, 400)

    assert backend.viewport == (1280, 800)
    downs = [call for call in client.calls if call[0] == "mouse" and call[4] is True]
    assert downs[-1][1:3] == (-640.0, 500.0)


def test_screenshot_returns_png() -> None:
    backend, _ = _backend()
    png = backend.screenshot()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_click_maps_captured_pixels_to_screen_points() -> None:
    """captured (px) -> origin + px/scale. Window at (0,38), scale 2.0."""
    backend, client = _backend()
    backend.screenshot()
    backend.click(1000, 500)
    downs = [c for c in client.calls if c[0] == "mouse" and c[4] is True]
    assert downs and downs[0][1] == pytest.approx(500.0)  # 0 + 1000/2
    assert downs[0][2] == pytest.approx(288.0)  # 38 + 500/2


def test_rich_pointer_actions_share_the_remote_frame_lease() -> None:
    backend, client = _backend()
    backend.screenshot()
    backend.right_click(100, 100)
    backend.drag(100, 100, 200, 150)
    edges = [call for call in client.calls if call[0] == "mouse"]
    assert [edge[3:5] for edge in edges] == [
        ("right", True),
        ("right", False),
        ("left", True),
        ("left", False),
    ]


@pytest.mark.parametrize(
    ("action", "operation", "destination"),
    [
        ("click", "remote_click", None),
        ("double_click", "remote_double_click", None),
        ("right_click", "remote_right_click", None),
        ("drag", "remote_drag", (200, 150)),
    ],
)
def test_guarded_pointer_actions_return_exact_delivery_receipts(
    action: str,
    operation: str,
    destination: tuple[int, int] | None,
) -> None:
    backend, _client = _backend()
    source = (100, 100)
    backend.screenshot()
    backend.prepare_pointer_actuation(*source)
    frame = backend.acquire_actuation_frame()
    frame_sha256 = hashlib.sha256(frame).hexdigest()

    if action == "click":
        receipt = backend.click_guarded(*source, expected_frame_sha256=frame_sha256)
    elif action == "double_click":
        receipt = backend.click_guarded(
            *source,
            expected_frame_sha256=frame_sha256,
            double=True,
        )
    elif action == "right_click":
        receipt = backend.right_click_guarded(
            *source,
            expected_frame_sha256=frame_sha256,
        )
    else:
        assert destination is not None
        receipt = backend.drag_guarded(
            *source,
            *destination,
            expected_frame_sha256=frame_sha256,
        )

    assert receipt.operation == operation
    assert receipt.native is False
    assert receipt.target_fingerprint == visual_resolution_point_fingerprint(
        frame_sha256,
        source,
    )
    assert receipt.destination_fingerprint == (
        visual_resolution_point_fingerprint(frame_sha256, destination)
        if destination is not None
        else None
    )


@pytest.mark.parametrize("action", ["click", "double_click", "right_click", "drag"])
@pytest.mark.parametrize("refusal", ["wrong_hash", "invalidated"])
def test_guarded_pointer_actions_refuse_without_the_exact_live_lease(
    action: str,
    refusal: str,
) -> None:
    backend, client = _backend()
    backend.screenshot()
    backend.prepare_pointer_actuation(100, 100)
    frame = backend.acquire_actuation_frame()
    frame_sha256 = hashlib.sha256(frame).hexdigest()
    if refusal == "wrong_hash":
        frame_sha256 = "0" * 64
    else:
        backend.screenshot()

    with pytest.raises(StructuralResolutionRefused, match="fresh-frame lease"):
        if action == "click":
            backend.click_guarded(100, 100, expected_frame_sha256=frame_sha256)
        elif action == "double_click":
            backend.click_guarded(
                100,
                100,
                expected_frame_sha256=frame_sha256,
                double=True,
            )
        elif action == "right_click":
            backend.right_click_guarded(
                100,
                100,
                expected_frame_sha256=frame_sha256,
            )
        else:
            backend.drag_guarded(
                100,
                100,
                200,
                150,
                expected_frame_sha256=frame_sha256,
            )

    assert not any(call[0] == "mouse" for call in client.calls)


@pytest.mark.parametrize(
    ("action", "operation"),
    [
        ("click", "remote_click"),
        ("double_click", "remote_double_click"),
        ("right_click", "remote_right_click"),
        ("drag", "remote_drag"),
    ],
)
def test_guarded_pointer_actions_never_receipt_uncertain_delivery(
    action: str,
    operation: str,
) -> None:
    class FailingPointerClient(FakeClient):
        fail_pointer = False

        def mouse(self, x, y, *, button, down, click_count):
            if self.fail_pointer:
                raise RuntimeError("pointer transport failed")
            super().mouse(
                x,
                y,
                button=button,
                down=down,
                click_count=click_count,
            )

    client = FailingPointerClient()
    backend = RemoteDisplayBackend(client=client, settle_s=0.0)
    backend.screenshot()
    backend.prepare_pointer_actuation(100, 100)
    frame = backend.acquire_actuation_frame()
    frame_sha256 = hashlib.sha256(frame).hexdigest()
    client.fail_pointer = True
    receipt = None

    with pytest.raises(ActionDeliveryUncertain) as raised:
        if action == "click":
            receipt = backend.click_guarded(
                100,
                100,
                expected_frame_sha256=frame_sha256,
            )
        elif action == "double_click":
            receipt = backend.click_guarded(
                100,
                100,
                expected_frame_sha256=frame_sha256,
                double=True,
            )
        elif action == "right_click":
            receipt = backend.right_click_guarded(
                100,
                100,
                expected_frame_sha256=frame_sha256,
            )
        else:
            receipt = backend.drag_guarded(
                100,
                100,
                200,
                150,
                expected_frame_sha256=frame_sha256,
            )

    assert raised.value.operation == operation
    assert receipt is None


def test_click_refuses_point_outside_captured_frame() -> None:
    backend, client = _backend()
    backend.screenshot()
    with pytest.raises(RemoteDisplayError, match="outside captured frame"):
        backend.click(client.px[0], 20)
    assert not any(c[0] == "mouse" for c in client.calls)


def test_click_refuses_stale_frame_lease(monkeypatch) -> None:
    now = {"value": 100.0}
    monkeypatch.setattr(
        "openadapt_flow.backends.remote_display.time.monotonic",
        lambda: now["value"],
    )
    client = FakeClient()
    backend = RemoteDisplayBackend(client=client, settle_s=0.0, max_frame_age_s=1.0)
    backend.screenshot()
    now["value"] = 101.01
    with pytest.raises(RemoteDisplayError, match="frame is stale"):
        backend.click(100, 100)
    assert not any(c[0] == "mouse" for c in client.calls)


def test_click_refuses_window_move_after_capture() -> None:
    backend, client = _backend()
    backend.screenshot()
    old = client.window
    client.window = WindowInfo(
        window_id=old.window_id,
        owner=old.owner,
        title=old.title,
        pid=old.pid,
        bounds=(old.bounds[0] + 80.0, *old.bounds[1:]),
        on_screen=True,
    )
    client.windows = [client.window]
    with pytest.raises(RemoteDisplayError, match="geometry changed"):
        backend.click(100, 100)
    assert not any(c[0] == "mouse" for c in client.calls)


def test_input_requires_frontmost_after_activation() -> None:
    backend, client = _backend(frontmost=False)
    backend.screenshot()
    with pytest.raises(RemoteDisplayError, match="not visible, app-frontmost"):
        backend.click(100, 100)
    assert not any(c[0] == "mouse" for c in client.calls)


def test_capture_refuses_uncalibrated_anisotropic_dpi() -> None:
    backend, _ = _backend(px=(2800, 1888))
    with pytest.raises(RemoteDisplayError, match="inconsistent DPI scale"):
        backend.screenshot()


def test_readiness_probe_refuses_locked_or_unexpected_session() -> None:
    client = FakeClient()
    backend = RemoteDisplayBackend(
        client=client,
        settle_s=0.0,
        readiness_probe=lambda _png: False,
    )
    backend.screenshot()
    with pytest.raises(RemoteDisplayError, match="readiness probe rejected"):
        backend.click(100, 100)
    assert not any(c[0] == "mouse" for c in client.calls)


def test_bound_actuation_refuses_same_window_content_change_before_input() -> None:
    client = FakeClient()
    backend = RemoteDisplayBackend(
        client=client,
        settle_s=0.0,
        readiness_probe=lambda _png: True,
    )
    backend.screenshot()
    backend.prepare_pointer_actuation(100, 100)
    backend.acquire_actuation_frame()
    # PID, window id, bounds, dimensions, focus, and readiness all remain
    # valid. Only the remote pixels changed after resolution.
    client.frame_color = (11, 22, 34)

    with pytest.raises(FreshActuationRequired) as raised:
        backend.click(100, 100)

    assert not any(call[0] == "mouse" for call in client.calls)
    assert raised.value.operation == "remote_click"
    assert raised.value.changed_pixel_count == client.px[0] * client.px[1]
    assert raised.value.changed_bbox == (0, 0, client.px[0], client.px[1])
    assert raised.value.frame_size == client.px


def _remote_frame_contract_backend() -> tuple[RemoteDisplayBackend, FakeClient]:
    size = (300, 200)
    client = FakeClient(
        window=WindowInfo(
            window_id=1,
            owner="Parallels Desktop",
            title="Windows 11",
            pid=99,
            bounds=(0.0, 0.0, float(size[0]), float(size[1])),
            on_screen=True,
        ),
        px=size,
    )
    contract = RemoteFrameContract(
        frame_width=size[0],
        frame_height=size[1],
        volatile_regions=((0, 0, 32, 32),),
        protected_regions=((90, 90, 20, 20),),
    )
    backend = RemoteDisplayBackend(
        client=client,
        settle_s=0.0,
        readiness_probe=lambda _png: True,
        remote_frame_contract=contract,
    )
    backend.screenshot()
    backend.prepare_pointer_actuation(100, 100)
    backend.acquire_actuation_frame()
    backend.arm_remote_frame_contract(protected_regions=((90, 90, 20, 20),))
    return backend, client


def test_bound_actuation_allows_only_qualified_remote_display_volatile_change() -> None:
    backend, client = _remote_frame_contract_backend()
    leased_raw_digest = backend._last_frame_digest
    client.frame_overrides[(1, 1)] = (0, 0, 0)
    current_png, _, _ = client.capture(client.window.window_id)

    assert leased_raw_digest is not None
    assert _canonical_rgb_digest(current_png) != leased_raw_digest

    backend.click(100, 100)

    assert len([call for call in client.calls if call[0] == "mouse"]) == 2
    assert backend._last_frame_digest == leased_raw_digest


def test_bound_actuation_refuses_remote_display_change_outside_volatile_region() -> (
    None
):
    backend, client = _remote_frame_contract_backend()
    client.frame_overrides[(50, 50)] = (0, 0, 0)

    with pytest.raises(FreshActuationRequired) as raised:
        backend.click(100, 100)

    assert not any(call[0] == "mouse" for call in client.calls)
    assert raised.value.operation == "remote_click"
    assert raised.value.changed_pixel_count == 1
    assert raised.value.changed_bbox == (50, 50, 1, 1)
    assert raised.value.frame_size == client.px


def test_replayer_reacquires_real_remote_display_lease_after_zero_edge_change(
    tmp_path,
) -> None:
    from tests.test_replayer import FakeVision, Match, click_step, make_png

    class OneMismatchBackend(RemoteDisplayBackend):
        def __init__(self, *, client):
            super().__init__(client=client, settle_s=0.0)
            self.click_attempts = 0

        def click(self, x, y, *, double=False):
            self.click_attempts += 1
            if self.click_attempts == 1:
                self._client.frame_color = (11, 22, 34)
            return super().click(x, y, double=double)

    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime.replayer import Replayer

    client = FakeClient(
        window=WindowInfo(
            window_id=1,
            owner="Parallels Desktop",
            title="Windows 11",
            pid=99,
            bounds=(0.0, 0.0, 300.0, 200.0),
            on_screen=True,
        ),
        px=(300, 200),
    )
    backend = OneMismatchBackend(client=client)
    vision = FakeVision()
    vision.template_results = [
        Match(point=(110, 105), region=(100, 100, 50, 20), confidence=0.95)
        for _ in range(3)
    ]
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "btn.png").write_bytes(make_png((50, 20)))

    report = Replayer(backend, vision=vision).run(
        Workflow(name="real-lease-retry", steps=[click_step(risk="irreversible")]),
        bundle_dir=bundle,
        run_dir=tmp_path / "run",
    )

    assert report.success is True
    assert backend.click_attempts == 2
    assert len([call for call in client.calls if call[0] == "mouse"]) == 2
    assert [event.retried for event in report.results[0].fresh_actuation_events] == [
        True
    ]


def test_bound_actuation_accepts_same_pixels_with_different_png_encoding() -> None:
    client = FakeClient()
    client.png_kwargs = {"compress_level": 0}
    backend = RemoteDisplayBackend(
        client=client,
        settle_s=0.0,
        readiness_probe=lambda _png: True,
    )
    backend.screenshot()
    backend.prepare_pointer_actuation(100, 100)
    backend.acquire_actuation_frame()
    # Container bytes differ, while decoded dimensions and RGB pixels do not.
    client.png_kwargs = {"compress_level": 9}

    backend.click(100, 100)

    assert any(call[0] == "mouse" for call in client.calls)


def test_observation_invalidates_bound_actuation_before_input() -> None:
    client = FakeClient()
    backend = RemoteDisplayBackend(client=client, settle_s=0.0)
    backend.screenshot()
    backend.prepare_pointer_actuation(100, 100)
    backend.acquire_actuation_frame()
    backend.screenshot()

    with pytest.raises(RemoteDisplayError, match="invalidated"):
        backend.click(100, 100)

    assert not any(call[0] == "mouse" for call in client.calls)


def test_armed_click_without_prepositioned_pointer_refuses_before_input() -> None:
    client = FakeClient()
    backend = RemoteDisplayBackend(client=client, settle_s=0.0)
    backend.acquire_actuation_frame()

    with pytest.raises(RemoteDisplayError, match="not pre-positioned"):
        backend.click(100, 100)

    assert not any(call[0] in {"move", "mouse"} for call in client.calls)


def test_coordinate_click_requires_prior_frame_lease() -> None:
    backend, client = _backend()
    with pytest.raises(RemoteDisplayError, match="no captured frame lease"):
        backend.click(100, 100)
    assert not any(c[0] == "mouse" for c in client.calls)


def test_duplicate_exact_windows_refused_instead_of_largest_selection() -> None:
    backend, client = _backend()
    old = client.window
    sensitive_title = "Patient Jane Doe - Claims"
    backend._title_substr = sensitive_title
    client.window = WindowInfo(
        window_id=old.window_id,
        owner=old.owner,
        title=sensitive_title,
        pid=old.pid,
        bounds=old.bounds,
        on_screen=old.on_screen,
    )
    old = client.window
    client.windows = [old]
    client.windows.append(
        WindowInfo(
            window_id=2,
            owner=old.owner,
            title=old.title,
            pid=old.pid,
            bounds=(20.0, 58.0, 900.0, 700.0),
            on_screen=True,
        )
    )
    with pytest.raises(
        RemoteDisplayError, match="ambiguous remote-display target"
    ) as exc_info:
        backend.screenshot()
    assert sensitive_title not in str(exc_info.value)


def test_partial_owner_match_is_not_accepted() -> None:
    client = FakeClient()
    sensitive_owner = "Patient Jane Doe - Citrix"
    backend = RemoteDisplayBackend(
        client=client, owner_substr=sensitive_owner, settle_s=0.0
    )
    with pytest.raises(
        RemoteDisplayError, match="no window exactly matching"
    ) as exc_info:
        backend.screenshot()
    assert sensitive_owner not in str(exc_info.value)


def test_same_pid_front_window_mismatch_refuses_keyboard_and_click() -> None:
    backend, client = _backend(key_window_id=2)
    backend.screenshot()
    with pytest.raises(RemoteDisplayError, match="keyboard-frontmost"):
        backend.click(100, 100)
    with pytest.raises(RemoteDisplayError, match="keyboard-frontmost"):
        backend.type_text("x")
    assert not any(c[0] in {"mouse", "type"} for c in client.calls)


def test_click_point_occluded_by_other_window_is_refused() -> None:
    backend, client = _backend(hit_window_id=2)
    backend.screenshot()
    with pytest.raises(RemoteDisplayError, match="covered by window 2"):
        backend.click(100, 100)
    assert not any(c[0] == "mouse" for c in client.calls)


def test_frame_age_rechecked_after_blocking_readiness(monkeypatch) -> None:
    now = {"value": 100.0}
    monkeypatch.setattr(
        "openadapt_flow.backends.remote_display.time.monotonic",
        lambda: now["value"],
    )

    def slow_probe(_png):
        now["value"] = 102.0
        return True

    client = FakeClient()
    backend = RemoteDisplayBackend(
        client=client,
        settle_s=0.0,
        max_frame_age_s=1.0,
        readiness_probe=slow_probe,
    )
    backend.screenshot()
    with pytest.raises(RemoteDisplayError, match="frame is stale"):
        backend.click(100, 100)
    assert not any(c[0] == "mouse" for c in client.calls)


def test_key_window_rechecked_after_blocking_readiness() -> None:
    client = FakeClient()

    def focus_changing_probe(_png):
        client._key_window_id = 2
        return True

    backend = RemoteDisplayBackend(
        client=client,
        settle_s=0.0,
        readiness_probe=focus_changing_probe,
    )
    backend.screenshot()
    with pytest.raises(RemoteDisplayError, match="changed during readiness"):
        backend.click(100, 100)
    assert not any(c[0] == "mouse" for c in client.calls)


def test_double_click_click_state() -> None:
    backend, client = _backend()
    backend.screenshot()
    backend.click(200, 200, double=True)
    click_states = [c[5] for c in client.calls if c[0] == "mouse" and c[4] is True]
    assert click_states == [1, 2]


def test_type_text_routes_to_keycodes() -> None:
    backend, client = _backend()
    backend.type_text("Neil-1")
    assert ("type", "Neil-1") in client.calls


def test_type_failure_is_delivery_uncertain() -> None:
    class FailingTypeClient(FakeClient):
        def type_chars(self, text):
            raise RuntimeError("host input failed after possible delivery")

    backend = RemoteDisplayBackend(client=FailingTypeClient(), settle_s=0.0)
    with pytest.raises(ActionDeliveryUncertain) as raised:
        backend.type_text("Neil-1")
    assert raised.value.operation == "remote_type_text"
    assert raised.value.cause_type == "RuntimeError"


def test_press_named_key_enter() -> None:
    backend, client = _backend()
    backend.press("Enter")
    keys = [c for c in client.calls if c[0] == "key"]
    assert keys[0][1] == 0x24  # Return keycode
    assert keys[0][3] == ()  # no modifiers


def test_press_chord_ctrl_a_uses_control_flag() -> None:
    backend, client = _backend()
    backend.press("ControlOrMeta+a")
    keys = [c for c in client.calls if c[0] == "key"]
    assert keys, "no key events emitted"
    # 'a' keycode 0, control flag applied on both down and up (fail-safe release)
    assert all(k[1] == 0x00 and "control" in k[3] for k in keys)
    assert keys[0][2] is True and keys[-1][2] is False


def test_press_failure_is_uncertain_and_still_attempts_release() -> None:
    class FailingKeyClient(FakeClient):
        def key(self, keycode, *, down, flags):
            super().key(keycode, down=down, flags=flags)
            if down:
                raise RuntimeError("key failed after possible delivery")

    client = FailingKeyClient()
    backend = RemoteDisplayBackend(client=client, settle_s=0.0)
    with pytest.raises(ActionDeliveryUncertain) as raised:
        backend.press("Enter")
    assert raised.value.operation == "remote_press"
    assert [call[2] for call in client.calls if call[0] == "key"] == [True, False]


def test_typed_pre_delivery_key_refusal_is_preserved() -> None:
    class RefusingKeyClient(FakeClient):
        def key(self, keycode, *, down, flags):
            if down:
                raise RemoteInputRefused("session policy refused before input")
            super().key(keycode, down=down, flags=flags)

    client = RefusingKeyClient()
    backend = RemoteDisplayBackend(client=client, settle_s=0.0)
    with pytest.raises(RemoteInputRefused):
        backend.press("Enter")
    assert [call[2] for call in client.calls if call[0] == "key"] == []


def test_press_bare_char_types_it() -> None:
    backend, client = _backend()
    backend.press("x")
    assert ("type", "x") in client.calls


def test_scroll_noop_when_zero() -> None:
    backend, client = _backend()
    backend.scroll(0, 0)
    assert not any(c[0] == "scroll" for c in client.calls)


def test_scroll_dispatches() -> None:
    backend, client = _backend()
    backend.scroll(0, 120)
    assert any(c[0] == "scroll" for c in client.calls)


def test_scroll_failure_is_delivery_uncertain() -> None:
    class FailingScrollClient(FakeClient):
        def scroll(self, dx, dy):
            raise RuntimeError("scroll failed after possible delivery")

    backend = RemoteDisplayBackend(client=FailingScrollClient(), settle_s=0.0)
    with pytest.raises(ActionDeliveryUncertain) as raised:
        backend.scroll(0, 120)
    assert raised.value.operation == "remote_scroll"


def test_fail_loud_when_not_accessibility_trusted() -> None:
    """A dropped synthetic click must never look like success -> refuse to act."""
    backend, _ = _backend(trusted=False)
    with pytest.raises(RemoteDisplayError, match="Accessibility"):
        backend.click(10, 10)
    with pytest.raises(RemoteDisplayError, match="Accessibility"):
        backend.type_text("x")
    with pytest.raises(RemoteDisplayError, match="Accessibility"):
        backend.press("Enter")


def test_require_input_trust_can_be_disabled_for_capture_only() -> None:
    """Capture-only use (no input) does not require Accessibility."""
    client = FakeClient(trusted=False)
    backend = RemoteDisplayBackend(
        client=client, require_input_trust=False, settle_s=0.0
    )
    assert backend.screenshot()[:8] == b"\x89PNG\r\n\x1a\n"


def test_window_not_found_raises() -> None:
    class NoWindow(FakeClient):
        def find_windows(self, owner, title):
            return []

    backend = RemoteDisplayBackend(client=NoWindow(), settle_s=0.0)
    with pytest.raises(RemoteDisplayError, match="no window exactly matching"):
        backend.screenshot()


def test_ensure_foreground_requires_frontmost_not_just_onscreen() -> None:
    """An occluded (not-frontmost) window fails ensure_foreground: capture works
    through occlusion but a coordinate click would hit the occluder."""
    backend, _ = _backend(frontmost=False)
    with pytest.raises(RemoteDisplayError, match="foreground"):
        backend.ensure_foreground(retries=2, settle_s=0.0)


def test_ensure_foreground_succeeds_when_frontmost() -> None:
    backend, client = _backend(frontmost=True)
    backend.ensure_foreground(retries=2, settle_s=0.0)
    assert any(c[0] == "activate" for c in client.calls)


def test_split_chord() -> None:
    assert _split_chord("ControlOrMeta+a") == (["control"], "a")
    assert _split_chord("Shift+Tab") == (["shift"], "Tab")
    assert _split_chord("Enter") == ([], "Enter")
    with pytest.raises(ValueError):
        _split_chord("")
