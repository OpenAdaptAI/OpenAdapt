"""macOS AX structured-layer capability: IdentityBackend + StructuralActionBackend.

The native macOS backend now owns a structured layer (the Accessibility tree),
so it must behave like the other structural substrates (browser DOM, Windows
UIA, Linux AT-SPI): record a stable locator, re-find the UNIQUE element at
replay, refuse ambiguity/truncation/scope-escape instead of guessing, and hand
back the real structured text under a point. This is the macOS analog of
``test_linux_backend``'s structural section and ``test_structural_rung``'s
clean/ambiguous/stale trio.

Every test injects a fake AX client (``FakeMacAXClient``) plus the fake window
client so the suite runs headless in CI with no PyObjC/AX dependency; a live-AX
run against a real macOS app is the separate qualification evidence in
``scripts/qualify_macos_ax_identity.py`` / ``benchmark/macos_native``.
"""

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
from openadapt_flow.backends.macos_backend import (
    MacCandidateSet,
    MacElement,
    MacOSBackend,
)
from openadapt_flow.backends.remote_display import WindowInfo
from openadapt_flow.ir import StructuralHandle, StructuralLocator

# Window at screen origin (10, 20), 400x300 points; the fake capture is
# 800x600 pixels, so the backend derives a 2x DPI scale. A captured-pixel point
# (px, py) therefore maps to screen point (10 + px/2, 20 + py/2), and an
# element's screen rect maps back to window-relative pixels by (s - origin) * 2.
TARGET_WINDOW = WindowInfo(41, "TextEdit", "oa-trial.txt", 9001, (10, 20, 400, 300))

# A Save button whose screen rect is (210, 40, 40, 20): window-relative pixels
# (400, 40, 80, 40), center pixel (440, 60).
SAVE_ELEMENT = MacElement(
    ax_path="0/1/2",
    accessible_id="save-button",
    role="button",
    name="Save",
    app_pid=9001,
    window_title="oa-trial.txt",
    bounds=(210.0, 40.0, 40.0, 20.0),
    text="Save",
    supported_operations=("invoke",),
)
# A text body element covering most of the window.
BODY_ELEMENT = MacElement(
    ax_path="0/1/0",
    accessible_id="body",
    role="textbox",
    name="Document",
    app_pid=9001,
    window_title="oa-trial.txt",
    bounds=(30.0, 60.0, 300.0, 200.0),
    text="Account 100512",
    supported_operations=("focus",),
)


class FakeMacClient:
    """Minimal window/capture/input client (mirrors test_macos_backend)."""

    def __init__(self, *, windows: list[WindowInfo] | None = None) -> None:
        self.windows = windows or [TARGET_WINDOW]
        self._frontmost_window_id = self.windows[0].window_id
        self._active_pid = self.windows[0].pid
        self._ax_focused_pid: int | None = self.windows[0].pid
        self._point_window_id = self.windows[0].window_id
        self._exact_ax_focus = True
        self.calls: list[tuple] = []

    def capture_trusted(self) -> bool:
        return True

    def input_trusted(self) -> bool:
        return True

    def request_capture_access(self) -> bool:
        return True

    def request_input_access(self) -> bool:
        return True

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
        image = Image.new("RGB", (800, 600), (20, 30, 40))
        output = io.BytesIO()
        image.save(output, format="PNG")
        self.calls.append(("capture", window_id))
        return output.getvalue(), 800, 600

    def frontmost_pid(self):
        return self._active_pid

    def frontmost_window_id(self):
        return self._frontmost_window_id

    def focused_application_pid(self):
        return self._ax_focused_pid

    def window_id_at_point(self, _x, _y):
        return self._point_window_id

    def activate(self, pid):
        self._active_pid = pid
        self._ax_focused_pid = pid

    def raise_window(self, window):
        self._frontmost_window_id = window.window_id
        return True

    def exact_window_focused_main(self, window):
        return self._exact_ax_focus

    def mouse(self, x, y, *, button, down, click_count):
        self.calls.append(("mouse", x, y, button, down, click_count))

    def mouse_move(self, x, y):
        self.calls.append(("move", x, y))

    def replace_selected_text(self, window, text):
        return True

    def key(self, keycode, *, down, flags):
        self.calls.append(("key", keycode, down, tuple(flags)))

    def scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))


class FakeMacAXClient:
    """Scripted AX element client injected in place of QuartzMacAXClient."""

    def __init__(
        self,
        *,
        at_point: MacElement | None = SAVE_ELEMENT,
        candidates: list[MacElement] | None = None,
        truncated: bool = False,
        text: str | None = "Account 100512",
    ) -> None:
        self.at_point = at_point
        self.candidates = list(candidates) if candidates is not None else [SAVE_ELEMENT]
        self.truncated = truncated
        self.text = text
        self.calls: list[tuple] = []

    def element_at_point(self, pid, window_title, x, y):
        self.calls.append(("element-at", pid, window_title, x, y))
        return self.at_point

    def find_candidates(self, pid, window_title, locator, *, limit):
        self.calls.append(("find", pid, window_title, locator, limit))
        return MacCandidateSet(tuple(self.candidates), self.truncated)

    def structured_text(self, element):
        self.calls.append(("structured", element.ax_path))
        return self.text


def backend(
    client: FakeMacClient | None = None,
    ax_client: FakeMacAXClient | None = None,
    **kwargs,
) -> MacOSBackend:
    return MacOSBackend(
        client or FakeMacClient(),
        app="TextEdit",
        window_title="oa-trial",
        settle_s=0,
        foreground_settle_s=0,
        ax_client=ax_client or FakeMacAXClient(),
        **kwargs,
    )


def test_macos_backend_now_advertises_structured_identity_capability() -> None:
    target = backend()
    assert isinstance(target, Backend)
    assert isinstance(target, IdentityBackend)
    assert isinstance(target, StructuralActionBackend)
    # It does NOT claim native AXPress actuation: a resolved element is acted on
    # through the fully gated physical click, not a bypassing AX action.
    assert not isinstance(target, NativeStructuralActionBackend)


def test_injected_clients_are_headless_safe() -> None:
    # No PyObjC / AX import happens for construction or capture.
    assert backend().viewport == (800, 600)


def test_record_locator_reuses_accessibility_id_and_exact_window() -> None:
    ax = FakeMacAXClient()
    target = backend(ax_client=ax)
    locator = target.structural_locator_at(440, 60)
    assert locator == StructuralLocator(
        automation_id="save-button",
        role="button",
        name="Save",
        window_name="oa-trial.txt",
    )
    # Captured-pixel (440, 60) is converted to the window's screen point
    # (10 + 440/2, 20 + 60/2) = (230.0, 50.0) before the AX hit test.
    assert ("element-at", 9001, "oa-trial.txt", 230.0, 50.0) in ax.calls


def test_record_locator_is_none_without_stable_identity() -> None:
    bare = MacElement(
        ax_path="0/1/2",
        accessible_id=None,
        role=None,
        name=None,
        app_pid=9001,
        window_title="oa-trial.txt",
        bounds=(210.0, 40.0, 40.0, 20.0),
    )
    target = backend(ax_client=FakeMacAXClient(at_point=bare))
    assert target.structural_locator_at(440, 60) is None


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
    assert handle.point == (440, 60)
    assert handle.region == (400, 40, 80, 40)
    assert handle.candidate_count == 1
    assert handle.supported_operations == ["invoke"]
    assert handle.target_fingerprint is not None
    assert len(handle.target_fingerprint) == 64


def test_missing_window_name_or_absent_target_is_an_ordinary_miss() -> None:
    target = backend()
    assert (
        target.locate_structural(
            StructuralLocator(automation_id="save-button", window_name="different.txt")
        )
        is None
    )
    empty = backend(ax_client=FakeMacAXClient(candidates=[]))
    assert (
        empty.locate_structural(StructuralLocator(automation_id="save-button")) is None
    )


def test_locator_without_any_identity_field_never_hits_ax() -> None:
    ax = FakeMacAXClient()
    target = backend(ax_client=ax)
    # No automation_id and no role+name -> unresolvable; must not enumerate.
    assert target.locate_structural(StructuralLocator(selector="#x")) is None
    assert not any(call[0] == "find" for call in ax.calls)


def test_ambiguous_enumeration_refuses_visual_fallthrough() -> None:
    duplicate = MacElement(
        ax_path="0/3/2",
        accessible_id="save-button",
        role="button",
        name="Save",
        app_pid=9001,
        window_title="oa-trial.txt",
        bounds=(210.0, 200.0, 40.0, 20.0),
        supported_operations=("invoke",),
    )
    ax = FakeMacAXClient(candidates=[SAVE_ELEMENT, duplicate])
    with pytest.raises(StructuralResolutionRefused, match="candidate_count=2"):
        backend(ax_client=ax).locate_structural(
            StructuralLocator(automation_id="save-button")
        )


def test_truncated_enumeration_refuses() -> None:
    ax = FakeMacAXClient(truncated=True)
    with pytest.raises(StructuralResolutionRefused, match="exceeded its bound"):
        backend(ax_client=ax).locate_structural(
            StructuralLocator(automation_id="save-button")
        )


def test_candidate_outside_configured_scope_refuses() -> None:
    escaped = MacElement(
        ax_path="0/1/2",
        accessible_id="save-button",
        role="button",
        name="Save",
        app_pid=500,  # different application
        window_title="other.txt",
        bounds=(210.0, 40.0, 40.0, 20.0),
        supported_operations=("invoke",),
    )
    with pytest.raises(StructuralResolutionRefused, match="outside the exact"):
        backend(ax_client=FakeMacAXClient(candidates=[escaped])).locate_structural(
            StructuralLocator(automation_id="save-button")
        )


def test_client_candidate_that_does_not_match_locator_refuses() -> None:
    wrong = MacElement(
        ax_path=SAVE_ELEMENT.ax_path,
        accessible_id="different-id",
        role=SAVE_ELEMENT.role,
        name=SAVE_ELEMENT.name,
        app_pid=SAVE_ELEMENT.app_pid,
        window_title=SAVE_ELEMENT.window_title,
        bounds=SAVE_ELEMENT.bounds,
        supported_operations=SAVE_ELEMENT.supported_operations,
    )
    with pytest.raises(StructuralResolutionRefused, match="outside the exact"):
        backend(ax_client=FakeMacAXClient(candidates=[wrong])).locate_structural(
            StructuralLocator(automation_id="save-button")
        )


def test_element_outside_window_rect_is_a_miss_not_a_wrong_point() -> None:
    off_window = MacElement(
        ax_path="0/9",
        accessible_id="save-button",
        role="button",
        name="Save",
        app_pid=9001,
        window_title="oa-trial.txt",
        bounds=(900.0, 900.0, 40.0, 20.0),  # far outside the 400x300 window
        supported_operations=("invoke",),
    )
    target = backend(ax_client=FakeMacAXClient(candidates=[off_window]))
    assert (
        target.locate_structural(StructuralLocator(automation_id="save-button")) is None
    )


def test_stale_tree_that_became_ambiguous_refuses_rather_than_resolving() -> None:
    # A locator that resolved uniquely at record time becomes ambiguous when the
    # live tree drifts (a second identical control appears): the backend refuses,
    # it does NOT silently resolve one of the two.
    ax = FakeMacAXClient()
    target = backend(ax_client=ax)
    locator = target.structural_locator_at(440, 60)
    assert locator is not None
    ax.candidates = [
        SAVE_ELEMENT,
        MacElement(
            ax_path="0/7/2",
            accessible_id="save-button",
            role="button",
            name="Save",
            app_pid=9001,
            window_title="oa-trial.txt",
            bounds=(210.0, 250.0, 40.0, 20.0),
            supported_operations=("invoke",),
        ),
    ]
    with pytest.raises(StructuralResolutionRefused, match="candidate_count=2"):
        target.locate_structural(locator)


def test_structured_text_is_exact_ax_text_or_none() -> None:
    target = backend(ax_client=FakeMacAXClient(at_point=BODY_ELEMENT, text="MG4408"))
    assert target.structured_text_at(200, 200) == "MG4408"

    missing = backend(ax_client=FakeMacAXClient(at_point=None))
    assert missing.structured_text_at(200, 200) is None


def test_structured_text_out_of_scope_point_is_none() -> None:
    foreign = MacElement(
        ax_path="0/0",
        accessible_id="x",
        role="text",
        name="x",
        app_pid=777,  # not the target app
        window_title="other.txt",
        bounds=(30.0, 60.0, 100.0, 20.0),
        text="do not read",
    )
    target = backend(ax_client=FakeMacAXClient(at_point=foreign))
    assert target.structured_text_at(200, 200) is None


def test_resolved_structural_point_still_faces_the_identity_gate() -> None:
    """Structure makes identity STRONGER, never bypasses it: a structurally
    resolved point is still checked against the recorded structured identity,
    catching the one-glyph sibling the OCR band cannot separate."""
    from pathlib import Path

    from openadapt_flow.ir import ActionKind, Anchor, Step, Workflow
    from openadapt_flow.runtime.replayer import Replayer

    recorded = "MG4408 Okafor, Philip 1966-01-17"
    sibling = "MG44O8 Okafor, Philip 1966-01-17"  # one-glyph-different patient
    ax = FakeMacAXClient(at_point=BODY_ELEMENT, text=sibling)
    target = backend(ax_client=ax)
    anchor = Anchor(
        template="templates/x.png",
        region=(100, 100, 40, 20),
        click_point=(440, 60),
        ocr_text="Open",
        structural=StructuralLocator(
            automation_id="save-button", window_name="oa-trial.txt"
        ),
        structured_identity=recorded,
    )
    step = Step(id="s1", intent="open patient", action=ActionKind.CLICK, anchor=anchor)
    rp = Replayer(target, poll_interval_s=0.01)
    screen = target.screenshot()
    resolution, _region, err = rp._resolve_step(
        step, screen, Path("."), Workflow(name="wf", steps=[step])
    )
    assert err is None
    assert resolution is not None and resolution.rung == "structural"
    assert resolution.point == (440, 60)
    check = rp._verify_identity(step, resolution, screen, {}, Workflow(name="wf"), None)
    assert check.status == "mismatch"
    assert check.mode == "structured"


def test_returned_handle_is_a_structural_handle() -> None:
    handle = backend().locate_structural(StructuralLocator(automation_id="save-button"))
    assert isinstance(handle, StructuralHandle)


# ---------------------------------------------------------------------------
# record -> compile -> replay conformance (unmodified stack), model_calls == 0
# ---------------------------------------------------------------------------
#
# The UNMODIFIED Recorder -> compiler -> Replayer drive the real MacOSBackend
# end to end against a stateful fake TextEdit-like desktop. The click step
# resolves through the NEW AX structural rung, and a HEALTHY replay makes ZERO
# model calls (the maturity backlog's macOS zero-model assertion). This is the
# macOS analog of test_windows_backend / test_rdp_backend's conformance test.

CONF_VIEWPORT = (800, 600)
CONF_WINDOW = WindowInfo(70, "TextEdit", "oa-conf.txt", 9100, (0, 0, 800, 600))
# The document text area: the click focuses it and the typed note renders
# inside it, so the type verifier (scoped to the structurally resolved element
# region) sees the change exactly where it expects.
CONF_TEXTBOX = (60, 120, 680, 360)  # x, y, w, h (pixels == screen points at 1x)
CONF_TEXTBOX_CENTER = (
    CONF_TEXTBOX[0] + CONF_TEXTBOX[2] // 2,
    CONF_TEXTBOX[1] + CONF_TEXTBOX[3] // 2,
)
CONF_BANNER_SAVED = "Encounter Saved Successfully"
CONF_NOTE = "confidential follow up note"
CONF_TEXTBOX_ELEMENT = MacElement(
    ax_path="0/1/0",
    accessible_id="doc-body",
    role="textbox",
    name="Document",
    app_pid=CONF_WINDOW.pid,
    window_title=CONF_WINDOW.title,
    bounds=(
        float(CONF_TEXTBOX[0]),
        float(CONF_TEXTBOX[1]),
        float(CONF_TEXTBOX[2]),
        float(CONF_TEXTBOX[3]),
    ),
    text="Document",
    supported_operations=("focus",),
)


def _conf_frames():
    import cv2
    import numpy as np

    def blank():
        return np.full((CONF_VIEWPORT[1], CONF_VIEWPORT[0], 3), 245, dtype=np.uint8)

    def text(img, x, y, s):
        cv2.putText(
            img, s, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2, cv2.LINE_AA
        )

    def doc(img):
        x, y, w, h = CONF_TEXTBOX
        cv2.rectangle(img, (x, y), (x + w, y + h), (255, 255, 255), -1)
        cv2.rectangle(img, (x, y), (x + w, y + h), (120, 120, 120), 2)

    # state0/1: empty focused document (the click only focuses -> no visible
    # change, an ordinary text-field focus); state2: note typed inside the box;
    # state3: save banner inside the box.
    s0 = blank()
    text(s0, 300, 90, "MockMed TextEdit")
    doc(s0)
    s1 = s0.copy()
    s2 = s1.copy()
    text(s2, 90, 320, CONF_NOTE)
    s3 = s2.copy()
    text(s3, 90, 200, CONF_BANNER_SAVED)
    return [
        Image.fromarray(cv2.cvtColor(s, cv2.COLOR_BGR2RGB)) for s in (s0, s1, s2, s3)
    ]


class StatefulMacClient(FakeMacClient):
    """A stateful fake TextEdit: renders app frames and advances on input."""

    def __init__(self) -> None:
        super().__init__(windows=[CONF_WINDOW])
        self._frames = _conf_frames()
        self.state = 0

    def reset(self) -> None:
        self.state = 0

    def capture(self, window_id):
        image = self._frames[self.state]
        output = io.BytesIO()
        image.save(output, format="PNG")
        self.calls.append(("capture", window_id, self.state))
        return output.getvalue(), image.width, image.height

    def mouse(self, x, y, *, button, down, click_count):
        super().mouse(x, y, button=button, down=down, click_count=click_count)
        bx, by, bw, bh = CONF_TEXTBOX
        if down and self.state == 0 and bx <= x < bx + bw and by <= y < by + bh:
            self.state = 1

    def replace_selected_text(self, window, text):
        if self.state == 1:
            self.state = 2
        return True

    def key(self, keycode, *, down, flags):
        super().key(keycode, down=down, flags=flags)
        if down and self.state == 2:
            self.state = 3


@pytest.mark.timeout(300)
def test_record_compile_replay_over_macos_backend(tmp_path) -> None:
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import ActionKind
    from openadapt_flow.recorder import Recorder
    from openadapt_flow.runtime.replayer import Replayer

    client = StatefulMacClient()
    ax = FakeMacAXClient(
        at_point=CONF_TEXTBOX_ELEMENT,
        candidates=[CONF_TEXTBOX_ELEMENT],
        text="Document",
    )
    be = MacOSBackend(
        client,
        app="TextEdit",
        window_title="oa-conf",
        settle_s=0,
        foreground_settle_s=0,
        ax_client=ax,
    )

    recording_dir = tmp_path / "recording"
    bundle_dir = tmp_path / "bundle"
    run_dir = tmp_path / "run"

    recorder = Recorder(be, recording_dir, settle_interval_s=0.02, settle_timeout_s=2.0)
    recorder.click(*CONF_TEXTBOX_CENTER)
    recorder.type_text(CONF_NOTE, param="note")
    recorder.press("Enter")
    recorder.finish()
    assert client.state == 3  # the stateful app reached its final state

    workflow = compile_recording(recording_dir, bundle_dir, name="macos-smoke")
    assert [s.action for s in workflow.steps] == [
        ActionKind.CLICK,
        ActionKind.TYPE,
        ActionKind.KEY,
    ]

    client.reset()
    report = Replayer(be, poll_interval_s=0.02).run(
        workflow,
        params={"note": CONF_NOTE},
        bundle_dir=bundle_dir,
        run_dir=run_dir,
    )
    assert report.success, [r.model_dump() for r in report.results]
    assert client.state == 3
    # HEALTHY replay makes ZERO model calls -- deterministic compiled execution.
    assert report.model_calls == 0
    # And the click resolved through the NEW AX structural rung (not a fallback).
    click_result = next(r for r in report.results if r.resolution is not None)
    assert click_result.resolution.rung == "structural"
