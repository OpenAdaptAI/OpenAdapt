"""Unit + conformance tests for the FreeRDPBackend (RDP over a transport).

No live RDP server: a scripted :class:`FakeRDPTransport` stands in for a real
RDP client — it serves a framebuffer image and records every pointer / key /
wheel event the backend sends. This is the RDP analog of ``test_windows_backend``'s
mock WAA server; it proves the adapter shape (framebuffer -> PNG, clicks/keys
-> transport events) without any RDP dependency, exactly the way CI must run.

The conformance test drives the UNMODIFIED Recorder -> compiler -> Replayer
stack over the FreeRDPBackend against a stateful fake desktop, proving the
4-method Backend protocol needs zero compiler/replayer changes for RDP.
"""

from __future__ import annotations

import io
import json
import os

import cv2
import numpy as np
import pytest
from PIL import Image

from openadapt_flow.backend import Backend, IdentityBackend, StructuralBackend
from openadapt_flow.backends.rdp_backend import (
    FreeRDPBackend,
    RDPTransport,
    normalize_chord,
)

VIEWPORT = (1280, 800)

# Synthetic desktop app (drawn with cv2, mirroring test_windows_backend).
BUTTON = (560, 400, 160, 48)  # x, y, w, h
BUTTON_CENTER = (BUTTON[0] + BUTTON[2] // 2, BUTTON[1] + BUTTON[3] // 2)
BANNER_LOADED = "Chart Loaded Ok"
BANNER_SAVED = "Encounter Saved Successfully"
NOTE_VALUE = "confidential follow up note"


def blank() -> np.ndarray:
    return np.full((VIEWPORT[1], VIEWPORT[0], 3), 245, dtype=np.uint8)


def draw_button(img: np.ndarray, x: int, y: int, w: int, h: int, label: str) -> None:
    cv2.rectangle(img, (x, y), (x + w, y + h), (205, 205, 205), -1)
    cv2.rectangle(img, (x, y), (x + w, y + h), (70, 70, 70), 2)
    cv2.putText(
        img,
        label,
        (x + 12, y + h // 2 + 8),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 0, 0),
        2,
        cv2.LINE_AA,
    )


def draw_text(img: np.ndarray, x: int, y: int, text: str) -> None:
    cv2.putText(
        img,
        text,
        (x, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 0, 0),
        2,
        cv2.LINE_AA,
    )


def app_screens() -> list[Image.Image]:
    """The 4 states of the synthetic desktop app, as RGB PIL images."""
    s0 = blank()
    draw_text(s0, 520, 84, "MockMed Desktop")
    draw_button(s0, *BUTTON, "Open Chart")

    s1 = s0.copy()
    draw_text(s1, 420, 244, BANNER_LOADED)

    s2 = s1.copy()
    draw_text(s2, 560, 470, NOTE_VALUE)

    s3 = s2.copy()
    draw_text(s3, 420, 320, BANNER_SAVED)
    # cv2 arrays are BGR; convert to RGB PIL images (transport-native form).
    return [
        Image.fromarray(cv2.cvtColor(s, cv2.COLOR_BGR2RGB)) for s in [s0, s1, s2, s3]
    ]


class FakeRDPTransport:
    """Scripted, event-recording stand-in for a real RDP transport.

    Serves ``screens[state]`` as the framebuffer and records every input the
    backend sends. When ``stateful`` is True it advances ``state`` like the
    real MockMed app would (click inside the button 0->1, first typed char
    1->2, Enter 2->3), so the unmodified record/replay pipeline can drive it.

    Args:
        screens: State ladder of PIL framebuffer images.
        stateful: Advance state on input (for the conformance test).
        as_raw_bytes: Return raw RGB bytes from ``framebuffer`` instead of a
            PIL image (exercises the backend's raw-bytes decode path).
        supports_hwheel: Whether horizontal wheel events are honored. Defaults
            to False to MATCH the real :class:`AardwolfTransport`, whose
            aardwolf ``send_mouse`` has no horizontal wheel — so a test cannot
            pass on a capability the live transport silently drops.
    """

    supports_hwheel = False

    def __init__(
        self,
        screens: list[Image.Image],
        *,
        stateful: bool = False,
        as_raw_bytes: bool = False,
        supports_hwheel: bool = False,
    ) -> None:
        self.screens = screens
        self.stateful = stateful
        self.as_raw_bytes = as_raw_bytes
        self.supports_hwheel = supports_hwheel
        self.state = 0
        self.connected = False
        self.disconnects = 0
        self.pointer_events: list[tuple[int, int, str, bool]] = []
        self.key_events: list[tuple[str, bool]] = []
        self.wheel_events: list[tuple[int, int]] = []

    # -- RDPTransport --------------------------------------------------------

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False
        self.disconnects += 1

    def framebuffer(self):
        img = self.screens[self.state]
        if self.as_raw_bytes:
            return img.tobytes(), img.width, img.height
        return img, img.width, img.height

    def pointer(self, x: int, y: int, button: str, down: bool) -> None:
        self.pointer_events.append((x, y, button, down))
        if self.stateful and not down and self.state == 0:
            bx, by, bw, bh = BUTTON
            if bx <= x <= bx + bw and by <= y <= by + bh:
                self.state = 1

    def key(self, keysym_or_char: str, down: bool) -> None:
        self.key_events.append((keysym_or_char, down))
        if not self.stateful or not down:
            return
        if self.state == 1 and keysym_or_char not in ("enter", "tab"):
            self.state = 2
        elif self.state == 2 and keysym_or_char == "enter":
            self.state = 3

    def wheel(self, dx: int, dy: int) -> None:
        # Mirror AardwolfTransport: horizontal wheel is unsupported unless
        # supports_hwheel is set, so a horizontal-only gesture records nothing
        # (exactly what the live transport does). Only the honored components
        # land in wheel_events.
        if dx and not self.supports_hwheel:
            dx = 0
        if dx == 0 and dy == 0:
            return
        self.wheel_events.append((dx, dy))

    def reset(self) -> None:
        self.state = 0
        self.pointer_events.clear()
        self.key_events.clear()
        self.wheel_events.clear()


class TransportError(RuntimeError):
    """The kind of error a real RDP transport raises mid-operation (a timeout
    on the wire). A distinct type so tests assert on exactly it."""


class RaisingRDPTransport(FakeRDPTransport):
    """A fake that can fail like a real RDP transport does — the mock the
    adversarial review found missing.

    A real ``key()`` / ``pointer()`` / ``framebuffer()`` / ``connect()`` can
    raise (a socket timeout) partway through a gesture; the plain
    ``FakeRDPTransport`` never raises, so the backend's failure paths (stuck
    modifiers, half-open teardown) had zero coverage. This transport raises
    :class:`TransportError` on demand, but only AFTER recording nothing for the
    failed call — a key whose DOWN raised is not recorded, so a test can prove
    the backend still released it.

    Args:
        raise_on_key_down: Key tokens whose *down* edge raises.
        raise_on_pointer: If True, every ``pointer`` call raises.
        raise_on_framebuffer: If True, every ``framebuffer`` call raises.
        raise_on_connect: If True, ``connect`` raises.
    """

    def __init__(
        self,
        screens: list[Image.Image],
        *,
        raise_on_key_down: frozenset[str] = frozenset(),
        raise_on_pointer: bool = False,
        raise_on_framebuffer: bool = False,
        raise_on_connect: bool = False,
        **kwargs: object,
    ) -> None:
        super().__init__(screens, **kwargs)  # type: ignore[arg-type]
        self.raise_on_key_down = raise_on_key_down
        self.raise_on_pointer = raise_on_pointer
        self.raise_on_framebuffer = raise_on_framebuffer
        self.raise_on_connect = raise_on_connect

    def connect(self) -> None:
        if self.raise_on_connect:
            raise TransportError("connect failed")
        super().connect()

    def framebuffer(self):
        if self.raise_on_framebuffer:
            raise TransportError("framebuffer read failed")
        return super().framebuffer()

    def pointer(self, x: int, y: int, button: str, down: bool) -> None:
        if self.raise_on_pointer:
            raise TransportError("pointer failed")
        super().pointer(x, y, button, down)

    def key(self, keysym_or_char: str, down: bool) -> None:
        if down and keysym_or_char in self.raise_on_key_down:
            raise TransportError(f"key {keysym_or_char!r} down failed")
        super().key(keysym_or_char, down)


def held_keys(key_events: list[tuple[str, bool]]) -> list[str]:
    """Keys still logically DOWN after replaying a down/up event log.

    A non-empty result means a stuck key — the exact silent-wrong-action the
    stuck-modifier fix prevents. A release of a key that was never down is a
    harmless no-op (it just doesn't appear as held)."""
    held: list[str] = []
    for token, down in key_events:
        if down:
            held.append(token)
        elif token in held:
            held.remove(token)
    return held


@pytest.fixture()
def transport() -> FakeRDPTransport:
    return FakeRDPTransport(app_screens())


@pytest.fixture()
def backend(transport: FakeRDPTransport) -> FreeRDPBackend:
    return FreeRDPBackend(transport)


# -- protocol conformance ------------------------------------------------------


def test_implements_backend_protocol(backend: FreeRDPBackend) -> None:
    assert isinstance(backend, Backend)


def test_connect_on_construction(transport: FakeRDPTransport) -> None:
    assert transport.connected is False
    FreeRDPBackend(transport)
    assert transport.connected is True


def test_no_connect_when_disabled(transport: FakeRDPTransport) -> None:
    FreeRDPBackend(transport, connect=False)
    assert transport.connected is False


def test_pixel_only_has_no_structured_or_structural_capabilities(
    backend: FreeRDPBackend,
) -> None:
    # RDP is a pure-pixel substrate: the backend must not claim a structured
    # (a11y/DOM) identity layer or cheap URL/title/page-count observations it
    # cannot honor. Identity honestly falls back to the OCR tier.
    assert not isinstance(backend, IdentityBackend)
    assert not isinstance(backend, StructuralBackend)
    for attr in ("structured_text_at", "url", "page_title", "page_count"):
        assert not hasattr(backend, attr)


# -- screenshot / viewport -----------------------------------------------------


def test_screenshot_returns_valid_png(backend: FreeRDPBackend) -> None:
    png = backend.screenshot()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    decoded = Image.open(io.BytesIO(png))
    assert decoded.size == VIEWPORT


def test_screenshot_matches_fake_framebuffer(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    png = backend.screenshot()
    decoded = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
    expected = np.asarray(transport.screens[0].convert("RGB"))
    assert np.array_equal(decoded, expected)


def test_screenshot_from_raw_bytes_framebuffer() -> None:
    # A transport that hands back raw RGB bytes (not a PIL image) must still
    # PNG-encode to the right pixels and dimensions.
    t = FakeRDPTransport(app_screens(), as_raw_bytes=True)
    b = FreeRDPBackend(t)
    png = b.screenshot()
    decoded = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
    expected = np.asarray(t.screens[0].convert("RGB"))
    assert decoded.shape == (VIEWPORT[1], VIEWPORT[0], 3)
    assert np.array_equal(decoded, expected)


def test_viewport_matches_framebuffer(backend: FreeRDPBackend) -> None:
    assert backend.viewport == VIEWPORT


def test_viewport_override(transport: FakeRDPTransport) -> None:
    b = FreeRDPBackend(transport, viewport=(640, 480))
    assert b.viewport == (640, 480)


# -- click ---------------------------------------------------------------------


def test_click_sends_pointer_down_then_up(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.screenshot()
    backend.click(10, 20)
    assert transport.pointer_events == [
        (10, 20, "left", True),
        (10, 20, "left", False),
    ]


def test_double_click_sends_sequence_twice(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.screenshot()
    backend.click(30, 40, double=True)
    assert transport.pointer_events == [
        (30, 40, "left", True),
        (30, 40, "left", False),
        (30, 40, "left", True),
        (30, 40, "left", False),
    ]


def test_click_coordinates_pass_through(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.screenshot()
    backend.click(*BUTTON_CENTER)
    xs = {(x, y) for (x, y, _b, _d) in transport.pointer_events}
    assert xs == {BUTTON_CENTER}


def test_click_refuses_point_outside_current_framebuffer(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.screenshot()
    with pytest.raises(RuntimeError, match="outside framebuffer"):
        backend.click(VIEWPORT[0], 10)
    assert transport.pointer_events == []


def test_click_refuses_stale_frame_lease(monkeypatch) -> None:
    now = {"value": 100.0}
    monkeypatch.setattr(
        "openadapt_flow.backends.rdp_backend.time.monotonic",
        lambda: now["value"],
    )
    transport = FakeRDPTransport(app_screens())
    backend = FreeRDPBackend(transport, max_frame_age_s=1.0)
    backend.screenshot()
    now["value"] = 101.01
    with pytest.raises(RuntimeError, match="frame is stale"):
        backend.click(*BUTTON_CENTER)
    assert transport.pointer_events == []


def test_click_refuses_framebuffer_resize_after_capture() -> None:
    transport = FakeRDPTransport(app_screens())
    backend = FreeRDPBackend(transport)
    backend.screenshot()
    transport.screens[0] = transport.screens[0].resize((640, 480))
    with pytest.raises(RuntimeError, match="framebuffer changed"):
        backend.click(320, 240)
    assert transport.pointer_events == []


def test_readiness_probe_refuses_locked_or_unexpected_session() -> None:
    transport = FakeRDPTransport(app_screens())
    backend = FreeRDPBackend(transport, readiness_probe=lambda _png: False)
    backend.screenshot()
    with pytest.raises(RuntimeError, match="readiness probe rejected"):
        backend.click(*BUTTON_CENTER)
    assert transport.pointer_events == []


def test_coordinate_click_requires_prior_frame_lease(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    with pytest.raises(RuntimeError, match="no captured RDP frame lease"):
        backend.click(*BUTTON_CENTER)
    assert transport.pointer_events == []


def test_frame_age_rechecked_after_blocking_readiness(monkeypatch) -> None:
    now = {"value": 100.0}
    monkeypatch.setattr(
        "openadapt_flow.backends.rdp_backend.time.monotonic",
        lambda: now["value"],
    )

    def slow_probe(_png):
        now["value"] = 102.0
        return True

    transport = FakeRDPTransport(app_screens())
    backend = FreeRDPBackend(transport, max_frame_age_s=1.0, readiness_probe=slow_probe)
    backend.screenshot()
    with pytest.raises(RuntimeError, match="frame is stale"):
        backend.click(*BUTTON_CENTER)
    assert transport.pointer_events == []


# -- type_text -----------------------------------------------------------------


def test_type_text_per_char_key_events(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.type_text("Ab1")
    assert transport.key_events == [
        ("A", True),
        ("A", False),
        ("b", True),
        ("b", False),
        ("1", True),
        ("1", False),
    ]


def test_type_text_empty_sends_nothing(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.type_text("")
    assert transport.key_events == []


def test_type_text_preserves_case_and_symbols(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.type_text("O'B")
    assert transport.key_events == [
        ("O", True),
        ("O", False),
        ("'", True),
        ("'", False),
        ("B", True),
        ("B", False),
    ]


# -- press ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("chord", "expected"),
    [
        ("Enter", ["enter"]),
        ("Escape", ["escape"]),
        ("ArrowDown", ["down"]),
        ("ControlOrMeta+a", ["ctrl", "a"]),
        ("Meta+d", ["meta", "d"]),
        ("Ctrl+Shift+Escape", ["ctrl", "shift", "escape"]),
    ],
)
def test_normalize_chord(chord: str, expected: list[str]) -> None:
    assert normalize_chord(chord) == expected


def test_press_single_key(transport: FakeRDPTransport, backend: FreeRDPBackend) -> None:
    backend.press("enter")
    assert transport.key_events == [("enter", True), ("enter", False)]


def test_press_named_key_maps(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.press("ArrowDown")
    assert transport.key_events == [("down", True), ("down", False)]


def test_press_chord_nests_down_then_reverse_up(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.press("ControlOrMeta+a")
    # Down in order, up in reverse: Ctrl down, a down, a up, Ctrl up.
    assert transport.key_events == [
        ("ctrl", True),
        ("a", True),
        ("a", False),
        ("ctrl", False),
    ]


def test_press_empty_raises(backend: FreeRDPBackend) -> None:
    with pytest.raises(ValueError):
        backend.press("")


# -- stuck-modifier robustness (transport raises mid-gesture) -------------------


def test_press_releases_all_keys_when_chord_key_raises() -> None:
    # Ctrl down succeeds, then 'a' down raises (a real RDP key() timeout). The
    # release phase MUST still run so Ctrl is not left latched — otherwise the
    # next click becomes Ctrl+click and the next text a shortcut: the silent
    # wrong-action the fix targets (Ctrl+a wipes a field).
    t = RaisingRDPTransport(app_screens(), raise_on_key_down=frozenset({"a"}))
    b = FreeRDPBackend(t)
    with pytest.raises(TransportError):
        b.press("ControlOrMeta+a")
    assert held_keys(t.key_events) == []  # nothing latched down
    # Specifically: Ctrl went down and came back up.
    assert ("ctrl", True) in t.key_events
    assert ("ctrl", False) in t.key_events


def test_press_releases_modifier_when_its_own_down_raises() -> None:
    # Even the failing key itself is released: its DOWN may already have
    # registered on the wire before the transport raised, so the backend
    # releases every part it attempted to press.
    t = RaisingRDPTransport(app_screens(), raise_on_key_down=frozenset({"ctrl"}))
    b = FreeRDPBackend(t)
    with pytest.raises(TransportError):
        b.press("ControlOrMeta+a")
    assert held_keys(t.key_events) == []
    assert ("ctrl", False) in t.key_events  # released despite its down failing


def test_type_text_releases_char_when_key_raises() -> None:
    # First char types cleanly; the second char's down raises. The first char
    # must already be released and no key may be left held.
    t = RaisingRDPTransport(app_screens(), raise_on_key_down=frozenset({"b"}))
    b = FreeRDPBackend(t)
    with pytest.raises(TransportError):
        b.type_text("ab")
    assert held_keys(t.key_events) == []
    assert ("a", True) in t.key_events and ("a", False) in t.key_events


def test_press_normal_chord_still_balanced_after_fix(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    # Regression: the try/finally must not change the happy-path event order.
    backend.press("ControlOrMeta+a")
    assert transport.key_events == [
        ("ctrl", True),
        ("a", True),
        ("a", False),
        ("ctrl", False),
    ]
    assert held_keys(transport.key_events) == []


def test_press_prefers_optional_physical_key_path_for_chords() -> None:
    class _PhysicalTransport(FakeRDPTransport):
        def __init__(self) -> None:
            super().__init__(app_screens())
            self.physical_events: list[tuple[str, bool]] = []

        @staticmethod
        def supports_physical_key(key: str) -> bool:
            return key in {"meta", "ctrl", "shift", "enter"} or (
                len(key) == 1 and key.isascii() and key.isalnum()
            )

        def physical_key(self, key: str, down: bool) -> None:
            self.physical_events.append((key, down))

    transport = _PhysicalTransport()
    backend = FreeRDPBackend(transport)
    backend.press("Meta+r")
    assert transport.physical_events == [
        ("meta", True),
        ("r", True),
        ("r", False),
        ("meta", False),
    ]
    assert transport.key_events == []

    backend.type_text("r")
    assert transport.key_events == [("r", True), ("r", False)]
    assert len(transport.physical_events) == 4


@pytest.mark.parametrize("chord", ["Meta+?", "Meta+R"])
def test_press_physical_path_refuses_unsupported_chord_before_input(chord) -> None:
    class _PhysicalTransport(FakeRDPTransport):
        physical_events: list[tuple[str, bool]] = []

        @staticmethod
        def supports_physical_key(key: str) -> bool:
            return key == "meta" or (
                len(key) == 1 and key.isascii() and key.isalnum() and key.islower()
            )

        def physical_key(self, key: str, down: bool) -> None:
            self.physical_events.append((key, down))

    transport = _PhysicalTransport(app_screens())
    backend = FreeRDPBackend(transport)
    with pytest.raises(ValueError, match="cannot safely emit physical chord"):
        backend.press(chord)
    assert transport.physical_events == []
    assert transport.key_events == []


def test_press_physical_path_releases_every_attempted_key_on_error() -> None:
    class _RaisingPhysicalTransport(FakeRDPTransport):
        def __init__(self) -> None:
            super().__init__(app_screens())
            self.physical_events: list[tuple[str, bool]] = []

        @staticmethod
        def supports_physical_key(key: str) -> bool:
            return key == "meta" or (len(key) == 1 and key.isalnum())

        def physical_key(self, key: str, down: bool) -> None:
            self.physical_events.append((key, down))
            if key == "r" and down:
                raise TransportError("physical r down failed")

    transport = _RaisingPhysicalTransport()
    backend = FreeRDPBackend(transport)
    with pytest.raises(TransportError, match="physical r down failed"):
        backend.press("Meta+r")
    assert transport.physical_events == [
        ("meta", True),
        ("r", True),
        ("r", False),
        ("meta", False),
    ]


# -- scroll --------------------------------------------------------------------


def test_scroll_sends_wheel(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.scroll(0, 400)
    assert transport.wheel_events == [(0, 400)]


def test_scroll_horizontal_dropped_matching_real_transport(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    # A horizontal-only scroll must record NOTHING: the real AardwolfTransport
    # cannot emit horizontal wheel events (documented limitation), and the fake
    # mirrors that so a test can't pass on a capability the live transport lacks.
    backend.scroll(120, 0)
    assert transport.wheel_events == []


def test_scroll_horizontal_honored_only_when_transport_supports_it() -> None:
    # The seam is explicit: a transport that DOES support horizontal wheel
    # records it. This documents that dropping dx is a transport-capability
    # decision, not the backend silently swallowing input.
    t = FakeRDPTransport(app_screens(), supports_hwheel=True)
    b = FreeRDPBackend(t)
    b.scroll(120, 0)
    assert t.wheel_events == [(120, 0)]


def test_scroll_mixed_keeps_vertical_when_horizontal_unsupported(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    # A diagonal scroll on the pixel-only transport keeps the vertical part and
    # drops the horizontal part (rather than dropping the whole gesture).
    backend.scroll(120, 400)
    assert transport.wheel_events == [(0, 400)]


def test_scroll_zero_sends_nothing(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.scroll(0, 0)
    assert transport.wheel_events == []


# -- lifecycle -----------------------------------------------------------------


def test_close_disconnects_transport(
    transport: FakeRDPTransport, backend: FreeRDPBackend
) -> None:
    backend.close()
    assert transport.connected is False
    assert transport.disconnects == 1


def test_fake_transport_satisfies_rdp_transport_protocol(
    transport: FakeRDPTransport,
) -> None:
    assert isinstance(transport, RDPTransport)


# -- record -> compile -> replay conformance (no compiler/replayer changes) -----


@pytest.mark.timeout(300)
def test_record_compile_replay_over_rdp_backend(tmp_path) -> None:
    """The unmodified Recorder, compiler and Replayer drive the FreeRDPBackend
    end to end against the stateful fake RDP desktop."""
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import ActionKind
    from openadapt_flow.recorder import Recorder
    from openadapt_flow.runtime.replayer import Replayer

    transport = FakeRDPTransport(app_screens(), stateful=True)
    backend = FreeRDPBackend(transport)

    recording_dir = tmp_path / "recording"
    bundle_dir = tmp_path / "bundle"
    run_dir = tmp_path / "run"

    recorder = Recorder(
        backend,
        recording_dir,
        settle_interval_s=0.02,
        settle_timeout_s=2.0,
    )
    recorder.click(*BUTTON_CENTER)
    recorder.type_text(NOTE_VALUE, param="note")
    recorder.press("Enter")
    recorder.finish()
    assert transport.state == 3  # the fake app reached its final state

    meta = json.loads((recording_dir / "meta.json").read_text())
    assert meta["viewport"] == list(VIEWPORT)
    assert meta["params"] == {"note": NOTE_VALUE}

    workflow = compile_recording(recording_dir, bundle_dir, name="rdp-smoke")
    assert [s.action for s in workflow.steps] == [
        ActionKind.CLICK,
        ActionKind.TYPE,
        ActionKind.KEY,
    ]

    transport.reset()
    report = Replayer(backend, poll_interval_s=0.02).run(
        workflow,
        params={"note": NOTE_VALUE},
        bundle_dir=bundle_dir,
        run_dir=run_dir,
    )
    assert report.success, [r.model_dump() for r in report.results]
    # Reference-bar invariant: a HEALTHY replay resolves from retained
    # evidence with ZERO model calls (docs/LIMITS.md "Healthy replay with
    # zero model calls"). The pixel-only RDP substrate must meet the same bar
    # the browser reference path and the Linux qualification already assert;
    # a nonzero count would mean the deterministic ladder silently fell
    # through to the optional grounder on a clean run.
    assert report.model_calls == 0, report.model_calls
    assert transport.state == 3


# -- AardwolfTransport internals (real transport; runs only with the rdp extra) -
#
# These exercise the REAL transport's failure/edge paths that the FakeRDPTransport
# cannot model (event-loop teardown, wheel dispatch position, horizontal-wheel
# capability). They need the optional `rdp` extra installed but NO live server —
# a fake aardwolf connection object stands in for the wire. Skipped in CI (extra
# absent); run locally / in the spike where aardwolf is present.


class _FakeConn:
    """Stand-in for an aardwolf RDPConnection: records send_mouse, no wire."""

    def __init__(self) -> None:
        self.mouse_calls: list = []
        self.key_calls: list = []
        self.scancode_calls: list = []
        self.terminated = 0

    async def send_mouse(self, button, x, y, pressed, steps=0):  # noqa: ANN001
        self.mouse_calls.append((button, x, y, pressed, steps))

    async def send_key_virtualkey(self, key, pressed, extended):  # noqa: ANN001
        self.key_calls.append(("virtual", key, pressed, extended))

    async def send_key_char(self, key, pressed):  # noqa: ANN001
        self.key_calls.append(("char", key, pressed))

    async def send_key_scancode(self, scancode, pressed, extended):  # noqa: ANN001
        self.scancode_calls.append((scancode, pressed, extended))

    async def terminate(self):
        self.terminated += 1


def _make_connected_aardwolf(conn):
    """An AardwolfTransport with a live event loop but a fake connection."""
    pytest.importorskip("aardwolf", reason="install the 'rdp' extra")
    from openadapt_flow.backends.rdp_backend import AardwolfTransport

    t = AardwolfTransport("rdp+ntlm-password://u:p@h:3389", width=1280, height=800)
    t._ensure_loop()
    t._conn = conn
    return t


def test_aardwolf_wheel_dispatched_at_last_pointer() -> None:
    pytest.importorskip("aardwolf", reason="install the 'rdp' extra")
    from aardwolf.commons.queuedata.constants import MOUSEBUTTON

    conn = _FakeConn()
    t = _make_connected_aardwolf(conn)
    try:
        t.pointer(640, 500, "left", True)
        t.pointer(640, 500, "left", False)
        conn.mouse_calls.clear()
        t.wheel(0, 300)  # scroll down
        assert len(conn.mouse_calls) == 1
        button, x, y, _pressed, steps = conn.mouse_calls[0]
        # Under the cursor the user last acted at — NOT the (0,0) origin, which
        # on Windows would scroll the top-left pane instead of the content.
        assert (x, y) == (640, 500)
        assert button == MOUSEBUTTON.MOUSEBUTTON_WHEEL_DOWN
        assert steps > 0
    finally:
        t.disconnect()


def test_aardwolf_wheel_falls_back_to_frame_centre_before_any_pointer() -> None:
    pytest.importorskip("aardwolf", reason="install the 'rdp' extra")
    conn = _FakeConn()
    t = _make_connected_aardwolf(conn)
    try:
        t.wheel(0, -200)  # scroll up, no pointer event sent yet
        assert len(conn.mouse_calls) == 1
        _button, x, y, _pressed, _steps = conn.mouse_calls[0]
        assert (x, y) == (1280 // 2, 800 // 2)  # centre, never the origin
    finally:
        t.disconnect()


def test_aardwolf_horizontal_wheel_dropped_and_warns() -> None:
    pytest.importorskip("aardwolf", reason="install the 'rdp' extra")
    conn = _FakeConn()
    t = _make_connected_aardwolf(conn)
    try:
        with pytest.warns(UserWarning, match="horizontal wheel"):
            t.wheel(120, 0)  # horizontal only: unsupported by aardwolf
        assert conn.mouse_calls == []  # nothing reached the wire (documented)
    finally:
        t.disconnect()


def test_aardwolf_physical_meta_r_is_all_scancodes_and_balanced() -> None:
    pytest.importorskip("aardwolf", reason="install the 'rdp' extra")
    conn = _FakeConn()
    t = _make_connected_aardwolf(conn)
    try:
        t.physical_key("meta", True)
        t.physical_key("r", True)
        t.physical_key("r", False)
        t.physical_key("meta", False)
        assert conn.scancode_calls == [
            (57435, True, True),
            (19, True, False),
            (19, False, False),
            (57435, False, True),
        ]
        assert conn.key_calls == []

        # Text entry deliberately stays on the Unicode path.
        t.key("r", True)
        t.key("r", False)
        assert conn.key_calls == [("char", "r", True), ("char", "r", False)]
        assert len(conn.scancode_calls) == 4
    finally:
        t.disconnect()


def test_aardwolf_physical_key_refuses_unsupported_character() -> None:
    pytest.importorskip("aardwolf", reason="install the 'rdp' extra")
    conn = _FakeConn()
    t = _make_connected_aardwolf(conn)
    try:
        assert t.supports_physical_key("meta") is True
        assert t.supports_physical_key("r") is True
        assert t.supports_physical_key("R") is False
        assert t.supports_physical_key("!") is False
        with pytest.raises(ValueError, match="implicit modifiers"):
            t.physical_key("R", True)
        with pytest.raises(ValueError, match="unsupported physical RDP chord"):
            t.physical_key("!", True)
        assert conn.scancode_calls == []
        assert conn.key_calls == []
    finally:
        t.disconnect()


def test_aardwolf_framebuffer_snapshot_runs_on_transport_event_loop() -> None:
    pytest.importorskip("aardwolf", reason="install the 'rdp' extra")
    import threading

    from aardwolf.commons.queuedata.constants import VIDEO_FORMAT

    class _FrameConn(_FakeConn):
        snapshot_thread_id: int | None = None

        def __init__(self) -> None:
            super().__init__()
            self.source = Image.new("RGB", (32, 24), "navy")

        def get_desktop_buffer(self, encoding):  # noqa: ANN001
            assert encoding == VIDEO_FORMAT.PIL
            self.snapshot_thread_id = threading.get_ident()
            return self.source

    conn = _FrameConn()
    caller_thread_id = threading.get_ident()
    t = _make_connected_aardwolf(conn)
    try:
        image, width, height = t.framebuffer()
        assert image.size == (32, 24)
        assert (width, height) == (32, 24)
        assert conn.snapshot_thread_id is not None
        assert conn.snapshot_thread_id != caller_thread_id
        assert conn.snapshot_thread_id == t._thread.ident
        conn.source.paste("white", (0, 0, 32, 24))
        assert image.getpixel((0, 0)) == (0, 0, 128)
    finally:
        t.disconnect()


@pytest.mark.parametrize(
    ("operation", "invoke"),
    [
        ("pointer input", lambda transport: transport.pointer(10, 20, "left", True)),
        ("virtual-key input", lambda transport: transport.key("Enter", True)),
        ("character input", lambda transport: transport.key("x", True)),
        (
            "physical scancode input",
            lambda transport: transport.physical_key("r", True),
        ),
        ("wheel input", lambda transport: transport.wheel(0, 120)),
    ],
)
def test_aardwolf_input_receipt_errors_are_never_silent(operation, invoke) -> None:
    pytest.importorskip("aardwolf", reason="install the 'rdp' extra")

    class _ErrorConn(_FakeConn):
        async def send_mouse(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return None, OSError(f"{operation} failed")

        async def send_key_virtualkey(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return None, OSError(f"{operation} failed")

        async def send_key_char(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return None, OSError(f"{operation} failed")

        async def send_key_scancode(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return None, OSError(f"{operation} failed")

    t = _make_connected_aardwolf(_ErrorConn())
    try:
        with pytest.raises(OSError, match=f"{operation} failed"):
            invoke(t)
    finally:
        t.disconnect()


def test_aardwolf_framebuffer_receipt_error_is_never_silent() -> None:
    pytest.importorskip("aardwolf", reason="install the 'rdp' extra")

    class _ErrorFrameConn(_FakeConn):
        def get_desktop_buffer(self, _encoding):
            return None, RuntimeError("frame copy failed")

    t = _make_connected_aardwolf(_ErrorFrameConn())
    try:
        with pytest.raises(RuntimeError, match="frame copy failed"):
            t.framebuffer()
    finally:
        t.disconnect()


def test_aardwolf_accepts_none_and_success_tuple_receipts() -> None:
    pytest.importorskip("aardwolf", reason="install the 'rdp' extra")

    class _SuccessConn(_FakeConn):
        async def send_mouse(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return None

        async def send_key_virtualkey(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return True, None

        async def send_key_char(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return True, None

    t = _make_connected_aardwolf(_SuccessConn())
    try:
        t.pointer(10, 20, "left", True)
        t.wheel(0, 120)
        t.key("Enter", True)
        t.key("x", True)
    finally:
        t.disconnect()


def test_aardwolf_connect_failure_terminates_and_stops_thread(monkeypatch) -> None:
    pytest.importorskip("aardwolf", reason="install the 'rdp' extra")
    import threading

    from openadapt_flow.backends.rdp_backend import AardwolfTransport

    class _InstantQueue:
        async def get(self):
            return None  # always ready, so the connect loop spins to deadline

    terminated = {"count": 0}

    class _NoFrameConn:
        desktop_buffer_has_data = False  # first frame NEVER arrives

        def __init__(self) -> None:
            self.ext_out_queue = _InstantQueue()

        async def connect(self):
            return None, None  # session opens OK...

        async def terminate(self):
            terminated["count"] += 1

    conn = _NoFrameConn()

    class _Factory:
        @staticmethod
        def from_url(url, iosettings):
            return _Factory()

        def get_connection(self, iosettings):
            return conn

    monkeypatch.setattr("aardwolf.commons.factory.RDPConnectionFactory", _Factory)

    t = AardwolfTransport("rdp+ntlm-password://u:p@h:3389", connect_timeout_s=0.05)
    with pytest.raises(TimeoutError):
        t.connect()

    # The half-open session was terminated (not leaked), and the event-loop
    # thread was stopped + joined so retries can't accumulate daemon threads.
    assert terminated["count"] == 1
    assert t._conn is None
    assert t._loop is None
    assert t._thread is None
    assert not any(
        th.name == "aardwolf-rdp" and th.is_alive() for th in threading.enumerate()
    )


# -- live smoke test (gated; skipped in CI) ------------------------------------
#
# Runs ONLY when a real RDP target is configured via env vars. It uses the
# real AardwolfTransport (which needs the optional `rdp` extra:
# `pip install 'openadapt-flow[rdp]'`), grabs ONE framebuffer, asserts it is a
# non-trivial image, and disconnects. Never runs in CI.
#
#   export OPENADAPT_FLOW_RDP_TARGET=host_or_ip[:port]
#   export OPENADAPT_FLOW_RDP_USER=username
#   export OPENADAPT_FLOW_RDP_PASS=password
#   # optional: OPENADAPT_FLOW_RDP_DOMAIN, OPENADAPT_FLOW_RDP_WIDTH/_HEIGHT
#   pytest tests/test_rdp_backend.py -k live_smoke -s


@pytest.mark.skipif(
    not (
        os.environ.get("OPENADAPT_FLOW_RDP_TARGET")
        and os.environ.get("OPENADAPT_FLOW_RDP_USER")
        and os.environ.get("OPENADAPT_FLOW_RDP_PASS")
    ),
    reason="live RDP target not configured (set OPENADAPT_FLOW_RDP_TARGET/_USER/_PASS)",
)
def test_live_smoke_real_rdp_framebuffer() -> None:
    pytest.importorskip("aardwolf", reason="install the 'rdp' extra")
    from openadapt_flow.backends.rdp_backend import AardwolfTransport

    target = os.environ["OPENADAPT_FLOW_RDP_TARGET"]
    host, _, port = target.partition(":")
    width = int(os.environ.get("OPENADAPT_FLOW_RDP_WIDTH", "1280"))
    height = int(os.environ.get("OPENADAPT_FLOW_RDP_HEIGHT", "800"))

    transport = AardwolfTransport.from_credentials(
        host,
        os.environ["OPENADAPT_FLOW_RDP_USER"],
        os.environ["OPENADAPT_FLOW_RDP_PASS"],
        domain=os.environ.get("OPENADAPT_FLOW_RDP_DOMAIN") or None,
        port=int(port) if port else 3389,
        width=width,
        height=height,
    )
    backend = FreeRDPBackend(transport)
    try:
        # POLL for a painted frame: the first frame(s) after connect are a
        # blank/black canvas before the desktop renders, so a single grab is
        # racy. wait_first_frame retries until a non-blank frame (or the budget
        # is spent) — the same guard a real EMR grab needs.
        png = backend.wait_first_frame(retries=40, settle_s=0.25)
        w, h = backend.viewport
        print(f"\n[live-smoke] connected; framebuffer {w}x{h}, {len(png)} PNG bytes")
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        img = Image.open(io.BytesIO(png))
        assert img.size == (w, h)
        # Non-trivial: more than one distinct colour (not a blank canvas).
        colors = img.convert("RGB").getcolors(maxcolors=1 << 24)
        assert colors is None or len(colors) > 1
    finally:
        backend.close()
