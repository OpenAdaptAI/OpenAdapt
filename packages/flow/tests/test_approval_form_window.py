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
from openadapt_flow.approval_mode.form_state import screen_text_state
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


def _regions(backend, frame=None, save_step="save"):
    replayer = SimpleNamespace(backend=backend, _approval_form_step_id=save_step)
    return Replayer._approval_form_regions(replayer, frame or _frame())


def test_the_replayer_reads_the_window_the_backend_names_without_the_save_step():
    asked = []

    def region(exclude_step):
        asked.append(exclude_step)
        return (-8, 0, 2000, 728)

    assert _regions(SimpleNamespace(approval_form_region=region)) == [WINDOW]
    assert asked == ["save"]


@pytest.mark.parametrize(
    "backend",
    [
        SimpleNamespace(),
        SimpleNamespace(approval_form_region=lambda _step: None),
        SimpleNamespace(approval_form_region=lambda _step: 1 / 0),
        SimpleNamespace(approval_form_region=lambda _step: (2000, 0, 10, 10)),
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
