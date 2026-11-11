"""A desktop approval rereads the app's window, not the whole screen.

Real OCR on rendered frames: a clock outside the app's window ticking over
must not void a person's approval, while any change the person could see
inside the window still must: a field, the patient banner, a wrapped line,
or two values trading places.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

import openadapt_flow.vision as vision
from openadapt_flow.approval_mode.form_state import (
    approval_window_regions,
    screen_text_state,
)
from openadapt_flow.backends.windows_backend import WindowsBackend
from openadapt_flow.runtime.replayer import Replayer

SIZE = (1024, 768)
WINDOW = (0, 0, 1024, 728)  # the app; the taskbar and its clock lie below


def _font(size: int):
    for name in ("DejaVuSans.ttf", "Arial.ttf", "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _frame(
    *,
    patient: str = "Neil Sorenson",
    note: str = "BP 128/82",
    line3: str = "no known allergies",
    dose: str = "5 mg every 10 hours",
    clock: str = "8:21 PM",
) -> bytes:
    image = Image.new("RGB", SIZE, "white")
    draw = ImageDraw.Draw(image)
    font = _font(22)
    draw.text((40, 40), f"Patient: {patient}", fill="black", font=font)
    draw.text((40, 288), f"Clinical note: {note}", fill="black", font=font)
    draw.text((40, 320), "follow up in two weeks", fill="black", font=font)
    draw.text((40, 352), line3, fill="black", font=font)
    draw.text((40, 640), f"Dose: {dose}", fill="black", font=font)
    draw.rectangle((0, 728, SIZE[0], SIZE[1]), fill=(32, 32, 32))
    draw.text((900, 736), clock, fill="white", font=font)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _state(**changes):
    return screen_text_state(vision, _frame(**changes), [WINDOW])


def test_a_clock_outside_the_window_does_not_change_the_form():
    before = _state()
    assert before is not None
    assert _state(clock="8:22 PM") == before
    # Read as a whole screen, the clock voids the approval.
    assert screen_text_state(vision, _frame()) != screen_text_state(
        vision, _frame(clock="8:22 PM")
    )


@pytest.mark.parametrize(
    "change",
    [
        {"patient": "Neil Sorensen"},
        {"note": "BP 138/82"},
        {"line3": "allergic to penicillin"},
        {"dose": "50 mg every 10 hours"},
        {"dose": "10 mg every 5 hours"},
    ],
    ids=["banner", "field", "wrapped-line", "far-field", "swapped-values"],
)
def test_any_change_inside_the_window_changes_the_form(change):
    before = _state()
    after = _state(**change)
    assert before is not None and after is not None
    assert before != after


def test_a_moved_window_changes_the_form():
    frame = _frame()
    assert screen_text_state(vision, frame, [WINDOW]) != screen_text_state(
        vision, frame, [(0, 0, 1024, 727)]
    )


def test_a_window_that_reads_nothing_is_unreadable_not_unchanged():
    blank = io.BytesIO()
    Image.new("RGB", SIZE, "white").save(blank, format="PNG")
    assert screen_text_state(vision, blank.getvalue(), [WINDOW]) is None


_RESULTS = [
    SimpleNamespace(step_id="s1", resolution=SimpleNamespace(point=(40, 50))),
    SimpleNamespace(step_id="s2", resolution=None),
    SimpleNamespace(step_id="save", resolution=SimpleNamespace(point=(9, 9))),
]


def _regions(backend, frame=None, save_step="save"):
    return approval_window_regions(backend, frame or _frame(), save_step, _RESULTS)


def test_the_replayer_reads_the_window_the_backend_names_without_the_save_step():
    asked = []

    def region(exclude_step, points):
        asked.append((exclude_step, points))
        return (-8, 0, 2000, 728)

    assert _regions(SimpleNamespace(approval_form_region=region)) == [WINDOW]
    assert asked == [("save", [(40, 50)])]


def test_the_replayer_reads_its_approval_form_in_the_window_without_the_save_step():
    asked = []

    def region(exclude_step, points):
        asked.append((exclude_step, points))
        return (-8, 0, 2000, 728)

    frame = _frame()
    replayer = SimpleNamespace(
        backend=SimpleNamespace(approval_form_region=region),
        vision=vision,
        _web_form_form_state_set=False,
        _approval_form_step_id="save",
        _approval_report=SimpleNamespace(results=_RESULTS),
    )
    state = Replayer._approval_form_state(replayer, frame)
    assert asked == [("save", [(40, 50)])]
    assert state is not None
    assert state == screen_text_state(vision, frame, [WINDOW])


@pytest.mark.parametrize(
    "backend",
    [
        SimpleNamespace(),
        SimpleNamespace(approval_form_region=lambda _step, _points: None),
        SimpleNamespace(approval_form_region=lambda _step, _points: 1 / 0),
        SimpleNamespace(approval_form_region=lambda _step, _points: (2000, 0, 10, 10)),
    ],
    ids=["no-hook", "unknown", "raises", "off-frame"],
)
def test_without_a_window_the_whole_screen_is_read(backend):
    assert _regions(backend) is None


def _windows_backend(captures, conflicted=(), answer=None):
    backend = object.__new__(WindowsBackend)
    backend._verified_windows = dict(captures)
    backend._conflicted_windows = set(conflicted)
    sent = []

    def post(path, payload):
        sent.append((path, payload))
        if isinstance(answer, dict) and path in answer:
            return SimpleNamespace(payload=answer[path](payload))
        return SimpleNamespace(payload=answer)

    backend._post_typed_read = post
    return backend, sent


def test_the_windows_backend_names_the_one_window_its_steps_were_in():
    backend, sent = _windows_backend(
        {"s1": (7, 10), "s2": (7, 10), "save": (99, 10)},
        answer={
            "status": "ok",
            "window": {"hwnd": 7, "pid": 10, "bounds": [0, 0, 800, 600]},
        },
    )
    assert backend.approval_form_region("save") == (0, 0, 800, 600)
    assert sent == [("/window/bounds", {"hwnd": 7, "pid": 10})]


@pytest.mark.parametrize(
    "captures,conflicted,answer",
    [
        ({}, (), None),
        ({"s1": (7, 10), "s2": (8, 10)}, (), None),
        ({"s1": (7, 10)}, ("s2",), None),
        ({"s1": (7, 10)}, (), {"status": "ok", "window": None}),
        (
            {"s1": (7, 10)},
            (),
            {"status": "ok", "window": {"hwnd": 8, "pid": 10, "bounds": [0, 0, 9, 9]}},
        ),
        (
            {"s1": (7, 10)},
            (),
            {"status": "ok", "window": {"hwnd": 7, "pid": 10, "bounds": [0, 0, 0, 9]}},
        ),
        (
            {"s1": (7, 10)},
            (),
            {
                "status": "ok",
                "window": {"hwnd": 7, "pid": 10, "bounds": [0, 0, True, 9]},
            },
        ),
    ],
    ids=[
        "none-verified",
        "two-windows",
        "conflicted",
        "gone",
        "other-window",
        "empty",
        "malformed",
    ],
)
def test_the_windows_backend_refuses_a_doubtful_window(captures, conflicted, answer):
    backend, _sent = _windows_backend(captures, conflicted, answer)
    assert backend.approval_form_region("save") is None


class _Reached(Exception):
    """Raised once a run is past its refusal checks."""


def _replayer_on(backend):
    replayer = Replayer(backend, vision=SimpleNamespace())
    replayer.checkpoint_key = b"k" * 32
    return replayer


def test_a_refused_run_does_not_touch_the_captured_windows(monkeypatch, tmp_path):
    """The captures are cleared only after the refusal checks pass, so a
    refused run never touches the backend."""
    backend, sent = _windows_backend({"s1": (7, 10)}, ("s2",))
    monkeypatch.setattr(
        Replayer, "_managed_dispatch_refusal", lambda self: "refused here"
    )
    with pytest.raises(ValueError, match="refused here"):
        _replayer_on(backend).run(
            SimpleNamespace(), bundle_dir=tmp_path / "b", run_dir=tmp_path / "r"
        )
    assert backend._verified_windows == {"s1": (7, 10)}
    assert backend._conflicted_windows == {"s2"}
    assert sent == []


def test_a_run_forgets_the_windows_an_earlier_run_captured(monkeypatch, tmp_path):
    backend, _sent = _windows_backend({"s1": (7, 10)}, ("s2",))
    import openadapt_flow.jit.lifecycle as lifecycle
    import openadapt_flow.locale_invariants as locale_invariants

    for name in (
        "_managed_dispatch_refusal",
        "_production_qualification_refusal",
        "_qualification_campaign_refusal",
    ):
        monkeypatch.setattr(Replayer, name, lambda self, *a, **k: None)
    monkeypatch.setattr(Replayer, "_reset_render_scale", lambda self: None)
    monkeypatch.setattr(Replayer, "_install_execution_snapshots", lambda s, w: None)
    monkeypatch.setattr(
        locale_invariants, "enforce_workflow_locale_invariants", lambda w: None
    )
    monkeypatch.setattr(lifecycle, "ensure_runnable", lambda *a, **k: None)

    def reached(self, *a, **k):
        raise _Reached

    monkeypatch.setattr(Replayer, "_initial_external_network_calls", reached)
    with pytest.raises(_Reached):
        _replayer_on(backend).run(
            SimpleNamespace(interstitials=()),
            bundle_dir=tmp_path / "b",
            run_dir=tmp_path / "r",
        )
    assert backend._verified_windows == {} and backend._conflicted_windows == set()
    assert backend.approval_form_region("save") is None


def test_a_resumed_run_names_the_window_under_the_earlier_steps():
    """With no capture in this process, the window under every earlier
    step's point must be the same one; it is not kept as a capture."""

    def root_at(payload):
        hwnd = 7 if payload["x"] < 500 else 8
        return {"status": "ok", "window": {"hwnd": hwnd, "pid": 10}}

    def bounds(payload):
        return {"status": "ok", "window": {**payload, "bounds": [0, 0, 800, 600]}}

    answer = {"/window/root-at": root_at, "/window/bounds": bounds}
    backend, sent = _windows_backend({}, answer=answer)
    assert backend.approval_form_region("save", [(40, 50), (60, 70)]) == (
        0,
        0,
        800,
        600,
    )
    assert sent[-1] == ("/window/bounds", {"hwnd": 7, "pid": 10})
    assert backend._verified_windows == {}
    # Points in two windows, or a point the agent cannot place: no window.
    assert backend.approval_form_region("save", [(40, 50), (600, 70)]) is None
    backend, _sent = _windows_backend(
        {}, answer={"/window/root-at": lambda _p: {"status": "ok", "window": None}}
    )
    assert backend.approval_form_region("save", [(40, 50)]) is None
