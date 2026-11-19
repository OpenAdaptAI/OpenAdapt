"""A desktop approval reads the app's text fields by value (#2189).

On the hosted Windows governed run, two reads of an unchanged Patient Notes
window gave different OCR words: the caret in the search box split "Neil"
into "i n", and the title bar's close button read as "x" once and not the
next time. Every resumed approval then halted on a form that had not
changed. Real OCR on rendered frames here: a caret or a title-bar glyph must
not change the digest, while a changed value still must, whether it is in a
text field (read through UI Automation) or anywhere else in the window (read
by OCR).
"""

from __future__ import annotations

import io
import sys
import types
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw, ImageFont

import openadapt_flow.vision as vision
from openadapt_flow.approval_mode.form_state import (
    approval_form_state,
    approval_window_fields,
    screen_text_state,
    window_fields_state,
)
from openadapt_flow.backends.windows_backend import WindowsBackend

SIZE = (1024, 768)
WINDOW = (0, 0, 1024, 720)
TITLE_BAR = (0, 0, 1024, 30)
SEARCH = (20, 44, 480, 26)
NOTE = (20, 340, 600, 120)


def _font(size: int):
    for name in ("DejaVuSans.ttf", "Arial.ttf", "/System/Library/Fonts/Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _frame(
    *,
    search: str = "Neil",
    caret: bool = False,
    close_hover: bool = False,
    banner: str = "Patient: Neil Sorenson (1984-03-12)",
    note: str = "BP 128/82; follow-up in 2 weeks",
    clock: str = "7:27 PM",
) -> bytes:
    image = Image.new("RGB", SIZE, "white")
    draw = ImageDraw.Draw(image)
    font = _font(20)
    draw.text((8, 4), "Patient Notes - Benchmark Harness", fill="black", font=font)
    if close_hover:
        draw.rectangle((976, 0, 1023, 29), fill=(232, 17, 35))
        draw.text((992, 3), "X", fill="white", font=font)
    else:
        draw.text((994, 3), "x", fill="black", font=font)
    draw.rectangle(
        (SEARCH[0], SEARCH[1], SEARCH[0] + SEARCH[2], SEARCH[1] + SEARCH[3]),
        outline="gray",
    )
    draw.text((SEARCH[0] + 4, SEARCH[1] + 2), search, fill="black", font=font)
    if caret:
        draw.line(
            (SEARCH[0] + 44, SEARCH[1] + 4, SEARCH[0] + 44, SEARCH[1] + 22),
            fill="black",
            width=2,
        )
    draw.text((20, 110), "Neil   Sorenson   1984-03-12", fill="black", font=font)
    draw.text((20, 300), banner, fill="black", font=font)
    draw.rectangle(
        (NOTE[0], NOTE[1], NOTE[0] + NOTE[2], NOTE[1] + NOTE[3]), outline="gray"
    )
    draw.text((NOTE[0] + 4, NOTE[1] + 4), note, fill="black", font=font)
    draw.text((20, 500), "Save Note", fill="black", font=font)
    draw.rectangle((0, 720, SIZE[0], SIZE[1]), fill=(32, 32, 32))
    draw.text((900, 732), clock, fill="white", font=font)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _fields(*, search: str = "Neil", note: str = "BP 128/82; follow-up in 2 weeks"):
    return {
        "bounds": WINDOW,
        "fields": [
            {
                "kind": "title_bar",
                "rect": TITLE_BAR,
                "text": "Patient Notes - Benchmark Harness",
            },
            {"kind": "edit", "rect": SEARCH, "text": search},
            {"kind": "edit", "rect": NOTE, "text": note},
        ],
    }


def _state(fields=None, **frame):
    return window_fields_state(vision, _frame(**frame), fields or _fields())


def test_the_issue_reproduces_with_ocr_alone():
    # The old read: OCR of the whole window. A caret is enough to change it.
    plain = screen_text_state(vision, _frame(), [WINDOW])
    assert plain is not None
    assert screen_text_state(vision, _frame(caret=True), [WINDOW]) != plain


def test_a_caret_or_a_title_bar_glyph_leaves_the_form_unchanged():
    before = _state()
    assert before is not None and before[0] == "window_fields"
    assert _state(caret=True) == before
    assert _state(close_hover=True) == before
    assert _state(caret=True, close_hover=True, clock="7:28 PM") == before


@pytest.mark.parametrize(
    "fields,frame",
    [
        (
            _fields(note="BP 138/82; follow-up in 2 weeks"),
            {"note": "BP 138/82; follow-up in 2 weeks"},
        ),
        (_fields(note="BP 138/82; follow-up in 2 weeks"), {}),
        (_fields(search="Nell"), {"search": "Nell"}),
        (None, {"banner": "Patient: Neil Sorensen (1983-01-19)"}),
    ],
    ids=["note-value", "note-value-not-yet-painted", "search-value", "banner-by-ocr"],
)
def test_a_changed_value_changes_the_form(fields, frame):
    before = _state()
    after = _state(fields, **frame)
    assert before is not None and after is not None
    assert before != after


def test_a_moved_field_or_window_changes_the_form():
    before = _state()
    moved = _fields()
    moved["fields"][2] = {**moved["fields"][2], "rect": (20, 341, 600, 120)}
    assert _state(moved) != before
    window = {**_fields(), "bounds": (0, 0, 1024, 719)}
    assert _state(window) != before


def test_a_field_covers_only_its_own_pixels():
    # A field rectangle that the agent reported over the banner would hide
    # the banner from OCR; its rectangle is then part of the digest, and
    # its value must be read. The banner change still shows through the
    # other channel: a different field set is a different digest.
    covering = _fields()
    covering["fields"].append({"kind": "edit", "rect": (20, 296, 500, 30), "text": ""})
    assert _state(covering) != _state()


def test_a_window_that_reads_nothing_is_unreadable():
    blank = io.BytesIO()
    Image.new("RGB", SIZE, "white").save(blank, format="PNG")
    empty = {"bounds": WINDOW, "fields": []}
    assert window_fields_state(vision, blank.getvalue(), empty) is None
    off = {"bounds": (2000, 0, 10, 10), "fields": []}
    assert window_fields_state(vision, _frame(), off) is None


_RESULTS = [
    SimpleNamespace(step_id="s1", resolution=SimpleNamespace(point=(40, 50))),
    SimpleNamespace(step_id="save", resolution=SimpleNamespace(point=(9, 9))),
]


def test_fields_are_read_for_the_window_without_the_save_step():
    asked = []

    def fields(exclude_step, points):
        asked.append((exclude_step, points))
        return _fields()

    backend = SimpleNamespace(
        approval_form_fields=fields,
        approval_form_region=lambda *_a: (0, 0, 1024, 720),
    )
    frame = _frame()
    state = approval_form_state(backend, vision, frame, "save", _RESULTS)
    assert asked == [("save", [(40, 50)])]
    assert state == window_fields_state(vision, frame, _fields())


@pytest.mark.parametrize(
    "answer",
    [None, "fields", {"bounds": WINDOW}, {"bounds": WINDOW, "fields": [], "x": 1}],
    ids=["none", "not-a-dict", "missing", "extra"],
)
def test_without_fields_the_window_is_read_by_ocr(answer):
    backend = SimpleNamespace(
        approval_form_fields=lambda *_a: answer,
        approval_form_region=lambda *_a: WINDOW,
    )
    frame = _frame()
    assert approval_window_fields(backend, "save", _RESULTS) is None
    assert approval_form_state(backend, vision, frame, "save", _RESULTS) == (
        screen_text_state(vision, frame, [WINDOW])
    )


def test_a_request_and_recheck_through_different_channels_never_match():
    frame = _frame()
    by_fields = approval_form_state(
        SimpleNamespace(approval_form_fields=lambda *_a: _fields()),
        vision,
        frame,
        "save",
    )
    by_ocr = approval_form_state(
        SimpleNamespace(approval_form_region=lambda *_a: WINDOW),
        vision,
        frame,
        "save",
    )
    assert by_fields is not None and by_ocr is not None
    assert by_fields[0] != by_ocr[0] and by_fields != by_ocr


# -- WindowsBackend -----------------------------------------------------------


def _windows_backend(captures, answer):
    backend = object.__new__(WindowsBackend)
    backend._verified_windows = dict(captures)
    backend._conflicted_windows = set()
    sent = []

    def post(path, payload):
        sent.append((path, payload))
        return SimpleNamespace(payload=answer)

    backend._post_typed_read = post
    return backend, sent


_GOOD = {
    "status": "ok",
    "window": {"hwnd": 7, "pid": 10, "bounds": [0, 0, 1024, 720]},
    "fields": [
        {"kind": "title_bar", "rect": [0, 0, 1024, 30], "text": "Patient Notes"},
        {"kind": "edit", "rect": [20, 44, 480, 26], "text": "Neil"},
    ],
}


def test_the_windows_backend_reads_the_fields_of_its_one_window():
    backend, sent = _windows_backend({"s1": (7, 10), "save": (99, 10)}, _GOOD)
    answer = backend.approval_form_fields("save")
    assert sent == [("/window/form-fields", {"hwnd": 7, "pid": 10})]
    assert answer == {
        "bounds": (0, 0, 1024, 720),
        "fields": [
            {"kind": "title_bar", "rect": (0, 0, 1024, 30), "text": "Patient Notes"},
            {"kind": "edit", "rect": (20, 44, 480, 26), "text": "Neil"},
        ],
    }


def _bad(**change):
    answer = {**_GOOD, "fields": [dict(f) for f in _GOOD["fields"]]}
    for key, value in change.items():
        if key.startswith("field_"):
            answer["fields"][1][key[len("field_") :]] = value
        else:
            answer[key] = value
    return answer


@pytest.mark.parametrize(
    "captures,answer",
    [
        ({}, _GOOD),
        ({"s1": (7, 10), "s2": (8, 10)}, _GOOD),
        ({"s1": (7, 10)}, None),
        ({"s1": (7, 10)}, {**_GOOD, "window": None}),
        (
            {"s1": (7, 10)},
            {**_GOOD, "window": {"hwnd": 8, "pid": 10, "bounds": [0, 0, 9, 9]}},
        ),
        ({"s1": (7, 10)}, {**_GOOD, "extra": 1}),
        ({"s1": (7, 10)}, _bad(fields="x")),
        ({"s1": (7, 10)}, _bad(field_kind="button")),
        ({"s1": (7, 10)}, _bad(field_text=None)),
        ({"s1": (7, 10)}, _bad(field_rect=[20, 44, True, 26])),
        ({"s1": (7, 10)}, _bad(field_rect=[20, 44, 0, 26])),
        ({"s1": (7, 10)}, _bad(field_extra=1)),
    ],
    ids=[
        "none-verified",
        "two-windows",
        "no-answer",
        "no-window",
        "other-window",
        "extra-key",
        "fields-not-a-list",
        "unknown-kind",
        "no-text",
        "bool-rect",
        "empty-rect",
        "extra-field-key",
    ],
)
def test_the_windows_backend_refuses_doubtful_fields(captures, answer):
    backend, _sent = _windows_backend(captures, answer)
    assert backend.approval_form_fields("save") is None


# -- win_agent ----------------------------------------------------------------


class _Rect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _Control:
    def __init__(
        self,
        control_type,
        *,
        bounds=(0, 0, 10, 10),
        value=None,
        offscreen=False,
        name="",
        children=(),
    ):
        self.ControlTypeName = control_type
        self.BoundingRectangle = _Rect(*bounds)
        self.IsOffscreen = offscreen
        self.Name = name
        self._value = value
        self._children = list(children)

    def GetChildren(self):
        return self._children

    def GetValuePattern(self):
        if self._value is None:
            raise RuntimeError("no value pattern")
        return SimpleNamespace(Value=self._value)


class _Desktop:
    def __init__(self, extra_windows=()):
        self.windows = {7: (10, (0, 0, 1024, 720), True, False)}
        for hwnd, entry in extra_windows:
            self.windows[hwnd] = entry

    def per_monitor_dpi(self):
        return True

    def virtual_origin(self):
        return (0, 0)

    def top_level_windows(self):
        return list(self.windows)

    def is_window(self, hwnd):
        return hwnd in self.windows

    def pid_of(self, hwnd):
        return self.windows[hwnd][0]

    def rect(self, hwnd):
        return self.windows[hwnd][1]

    def is_visible(self, hwnd):
        return self.windows[hwnd][2]

    def is_cloaked(self, hwnd):
        return self.windows[hwnd][3]


class _Init:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


def _window(*children):
    return _Control(
        "WindowControl",
        bounds=(0, 0, 1024, 720),
        name="Patient Notes",
        children=children,
    )


def _agent(monkeypatch, window, desktop=None):
    # Only the agent module sees Windows; pytest's own paths stay POSIX.
    import os

    import openadapt_flow.backends.win_agent.server as agent_server

    monkeypatch.setattr(
        agent_server, "os", types.SimpleNamespace(**{**vars(os), "name": "nt"})
    )
    monkeypatch.setattr(agent_server, "_user32_factory", lambda: desktop or _Desktop())
    auto = types.SimpleNamespace(
        ControlFromHandle=lambda hwnd: window if hwnd == 7 else None,
        UIAutomationInitializerInThread=_Init,
    )
    monkeypatch.setitem(sys.modules, "uiautomation", auto)
    client = types.SimpleNamespace(gen_dir="unset")
    monkeypatch.setitem(sys.modules, "comtypes", types.SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, "comtypes.client", client)
    return agent_server._perform_uia


def test_the_agent_reads_the_title_bar_and_every_readable_text_field(monkeypatch):
    window = _window(
        _Control(
            "TitleBarControl",
            bounds=(0, 0, 1024, 30),
            children=[_Control("ButtonControl", name="Close")],
        ),
        _Control(
            "PaneControl",
            bounds=(0, 30, 1024, 720),
            children=[
                _Control("EditControl", bounds=(20, 44, 500, 70), value="Neil"),
                _Control(
                    "EditControl",
                    bounds=(20, 80, 500, 100),
                    value="hidden",
                    offscreen=True,
                ),
                _Control(
                    "EditControl", bounds=(20, 110, 500, 130)
                ),  # password: no value
                _Control(
                    "DocumentControl", bounds=(20, 340, 620, 460), value="BP 128/82"
                ),
                _Control(
                    "TextControl", bounds=(20, 300, 500, 320), name="Patient: Neil"
                ),
            ],
        ),
    )
    perform = _agent(monkeypatch, window)
    answer = perform("window-form-fields", {"hwnd": 7, "pid": 10})
    assert answer["window"] == {"hwnd": 7, "pid": 10, "bounds": [0, 0, 1024, 720]}
    assert answer["fields"] == [
        {"kind": "title_bar", "rect": [0, 0, 1024, 30], "text": "Patient Notes"},
        {"kind": "edit", "rect": [20, 44, 480, 26], "text": "Neil"},
        {"kind": "edit", "rect": [20, 340, 600, 120], "text": "BP 128/82"},
    ]


@pytest.mark.parametrize(
    "extra",
    [
        [(8, (10, (100, 100, 300, 200), True, False))],
        [(8, (10, (100, 100, 300, 200), True, None))],
    ],
    ids=["another-app-window", "cloak-unknown"],
)
def test_the_agent_refuses_when_the_app_shows_another_window(monkeypatch, extra):
    window = _window(_Control("EditControl", bounds=(20, 44, 500, 70), value="Neil"))
    perform = _agent(monkeypatch, window, _Desktop(extra))
    assert perform("window-form-fields", {"hwnd": 7, "pid": 10})["window"] is None


def test_the_agent_ignores_hidden_or_cloaked_windows_and_other_apps(monkeypatch):
    window = _window(_Control("EditControl", bounds=(20, 44, 500, 70), value="Neil"))
    desktop = _Desktop(
        [
            (8, (10, (100, 100, 300, 200), False, False)),
            (9, (10, (100, 100, 300, 200), True, True)),
            (11, (99, (0, 720, 1024, 768), True, False)),
        ]
    )
    perform = _agent(monkeypatch, window, desktop)
    assert perform("window-form-fields", {"hwnd": 7, "pid": 10})["window"] is not None


def test_the_agent_refuses_a_walk_it_cannot_finish(monkeypatch):
    import openadapt_flow.backends.win_agent.server as agent_server

    monkeypatch.setattr(agent_server, "_MAX_UIA_NODES", 2)
    window = _window(*[_Control("PaneControl") for _ in range(5)])
    perform = _agent(monkeypatch, window)
    assert perform("window-form-fields", {"hwnd": 7, "pid": 10})["window"] is None


def test_the_agent_refuses_a_window_uia_cannot_open(monkeypatch):
    perform = _agent(monkeypatch, None)
    assert perform("window-form-fields", {"hwnd": 7, "pid": 10})["window"] is None
    assert perform("window-form-fields", {"hwnd": 7, "pid": 11})["window"] is None
