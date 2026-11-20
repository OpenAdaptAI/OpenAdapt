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

import hashlib
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
from openadapt_flow.backends.win_agent.native_value import (
    NativeOwner,
    NativeRead,
    valid_value_proof,
)
from openadapt_flow.backends.windows_backend import WindowsBackend


def _proof(text, hwnd=100, pid=10):
    raw = text.encode("utf-16-le", errors="surrogatepass")
    return {
        "schema": "openadapt.native-value-completeness/v1",
        "method": "uia-value+wm-gettext",
        "hwnd": hwnd,
        "pid": pid,
        "thread": 5,
        "utf16_units": len(raw) // 2,
        "sha256_utf16le": hashlib.sha256(raw).hexdigest(),
        "native_reads": 2,
        "uia_reads": 2,
    }


def _proved_fields(answer):
    for field in answer["fields"]:
        field["value_identity"] = (
            "a" * 64 if field["kind"] in {"edit", "value"} else None
        )
        field["value_proof"] = (
            _proof(field["text"]) if field["kind"] in {"edit", "value"} else None
        )
    return answer


def _without_proofs(fields):
    return [
        {
            key: value
            for key, value in field.items()
            if key not in {"value_proof", "value_identity"}
        }
        for field in fields
    ]


SIZE = (1024, 768)
WINDOW = (0, 0, 1024, 720)
TITLE_BAR = (0, 0, 1024, 30)
SEARCH = (20, 44, 480, 26)
NOTE = (20, 340, 600, 120)
ROW = (20, 100, 600, 40)
CELLS = [(20, 100, 200, 40), (220, 100, 200, 40), (420, 100, 200, 40)]


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
    row_fill: tuple[int, int, int] = (0, 120, 215),
    dose: str = "Dose: 12 | 5",
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
    # The selected grid row: its highlight dims when the window loses focus.
    draw.rectangle((ROW[0], ROW[1], ROW[0] + ROW[2], ROW[1] + ROW[3]), fill=row_fill)
    for (cx, _cy, _cw, _ch), cell in zip(CELLS, ("Neil", "Sorenson", "1984-03-12")):
        draw.text((cx + 4, ROW[1] + 8), cell, fill="white", font=font)
    draw.text((20, 600), dose, fill="black", font=font)
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


def _fields(
    *,
    search: str = "Neil",
    note: str = "BP 128/82; follow-up in 2 weeks",
    selected: str = "selected",
):
    cells = [
        {"kind": "text", "rect": rect, "text": text, "state": None}
        for rect, text in zip(CELLS, ("Neil", "Sorenson", "1984-03-12"))
    ]
    return _proved_fields(
        {
            "bounds": WINDOW,
            "fields": [
                {
                    "kind": "title_bar",
                    "rect": TITLE_BAR,
                    "text": "Patient Notes - Benchmark Harness",
                    "state": None,
                },
                {"kind": "edit", "rect": SEARCH, "text": search, "state": None},
                {"kind": "state", "rect": ROW, "text": "", "state": selected},
                *cells,
                {"kind": "edit", "rect": NOTE, "text": note, "state": None},
            ],
        }
    )


def _state(fields=None, **frame):
    return window_fields_state(vision, _frame(**frame), fields or _fields())


def test_a_caret_or_a_title_bar_glyph_leaves_the_form_unchanged():
    before = _state()
    assert before is not None and before[0] == "window_fields"
    assert _state(caret=True) == before
    assert _state(close_hover=True) == before
    assert _state(caret=True, close_hover=True, clock="7:28 PM") == before


def test_a_dimmed_selection_highlight_leaves_the_form_unchanged():
    # The window lost focus while the item waited: the selected row turns
    # grey, its cells read the same through UIA.
    assert _state(row_fill=(204, 204, 204)) == _state()


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
        (None, {"dose": "Dose: 1 | 25"}),
        (_fields(selected="unselected"), {}),
    ],
    ids=[
        "note-value",
        "note-value-not-yet-painted",
        "search-value",
        "banner-by-ocr",
        "adjacent-numbers-by-ocr",
        "selection",
    ],
)
def test_a_changed_value_changes_the_form(fields, frame):
    before = _state()
    after = _state(fields, **frame)
    assert before is not None and after is not None
    assert before != after


def test_a_moved_field_or_window_changes_the_form():
    before = _state()
    moved = _fields()
    moved["fields"][-1] = {**moved["fields"][-1], "rect": (20, 341, 600, 120)}
    assert _state(moved) != before
    window = {**_fields(), "bounds": (0, 0, 1024, 719)}
    assert _state(window) != before


def test_a_field_covers_only_its_own_pixels():
    # A field rectangle that the agent reported over the banner would hide
    # the banner from OCR; its rectangle is then part of the digest, and
    # its value must be read. The banner change still shows through the
    # other channel: a different field set is a different digest.
    covering = _fields()
    covering["fields"].append(
        {
            "kind": "edit",
            "rect": (20, 296, 500, 30),
            "text": "",
            "state": None,
            "value_proof": _proof(""),
            "value_identity": "a" * 64,
        }
    )
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
def test_an_advertised_but_unavailable_field_channel_never_downgrades_to_ocr(answer):
    backend = SimpleNamespace(
        approval_form_fields=lambda *_a: answer,
        approval_form_region=lambda *_a: WINDOW,
    )
    frame = _frame()
    assert approval_window_fields(backend, "save", _RESULTS) is None
    assert screen_text_state(vision, frame, [WINDOW]) is not None
    assert approval_form_state(backend, vision, frame, "save", _RESULTS) is None


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
        {
            "kind": "title_bar",
            "rect": [0, 0, 1024, 30],
            "text": "Patient Notes",
            "state": None,
        },
        {"kind": "edit", "rect": [20, 44, 480, 26], "text": "Neil", "state": None},
        {"kind": "state", "rect": [20, 100, 600, 40], "text": "", "state": "selected"},
    ],
}
_proved_fields(_GOOD)


def test_the_windows_backend_reads_the_fields_of_its_one_window():
    backend, sent = _windows_backend({"s1": (7, 10), "save": (99, 10)}, _GOOD)
    answer = backend.approval_form_fields("save")
    assert sent == [("/window/form-fields", {"hwnd": 7, "pid": 10})]
    assert answer is not None
    assert answer["bounds"] == (0, 0, 1024, 720)
    assert valid_value_proof(
        answer["fields"][1]["text"], answer["fields"][1]["value_proof"]
    )
    assert {**answer, "fields": _without_proofs(answer["fields"])} == {
        "bounds": (0, 0, 1024, 720),
        "fields": [
            {
                "kind": "title_bar",
                "rect": (0, 0, 1024, 30),
                "text": "Patient Notes",
                "state": None,
            },
            {"kind": "edit", "rect": (20, 44, 480, 26), "text": "Neil", "state": None},
            {
                "kind": "state",
                "rect": (20, 100, 600, 40),
                "text": "",
                "state": "selected",
            },
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
        ({"s1": (7, 10)}, _bad(field_state="maybe")),
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
        "unknown-state",
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
    next_hwnd = 100

    def __init__(
        self,
        control_type,
        *,
        bounds=(0, 0, 10, 10),
        value=None,
        offscreen=False,
        name="",
        children=(),
        selected=None,
        toggle=None,
        password=False,
    ):
        self.NativeWindowHandle = _Control.next_hwnd
        _Control.next_hwnd += 1
        self.ProcessId = 10
        self.parent = None
        self.native_override = None
        self.IsPassword = password
        self._selected = selected
        self._toggle = toggle
        self.ControlTypeName = control_type
        self.BoundingRectangle = _Rect(*bounds)
        self.IsOffscreen = offscreen
        self.Name = name
        self._value = value
        self._children = list(children)

    def GetRuntimeId(self):
        return [42, self.NativeWindowHandle]

    def GetParentControl(self):
        return self.parent

    def GetChildren(self):
        return self._children

    def GetValuePattern(self):
        if self._value is None:
            raise RuntimeError("no value pattern")
        return SimpleNamespace(Value=self._value, IsReadOnly=False)

    def GetSelectionItemPattern(self):
        if self._selected is None:
            raise RuntimeError("no selection item pattern")
        return SimpleNamespace(IsSelected=self._selected)

    def GetTogglePattern(self):
        if self._toggle is None:
            raise RuntimeError("no toggle pattern")
        return SimpleNamespace(ToggleState=self._toggle)


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
    import openadapt_flow.backends.win_agent.native_value as native

    controls = {}

    def attach(control, parent=None):
        control.parent = parent
        controls[control.NativeWindowHandle] = control
        for child in control._children:
            attach(child, control)

    if window is not None:
        window.NativeWindowHandle = 7
        attach(window)

    class Reader:
        def owner(self, hwnd):
            return NativeOwner(hwnd, controls[hwnd].ProcessId, 5)

        def read(self, owner):
            control = controls[owner.hwnd]
            value = (
                control._value
                if control.native_override is None
                else control.native_override
            )
            units = len(value.encode("utf-16-le", errors="surrogatepass")) // 2
            return NativeRead(owner, units, units, value)

    monkeypatch.setattr(native, "Win32TextReader", Reader)
    auto = types.SimpleNamespace(
        ControlFromHandle=lambda hwnd: controls.get(hwnd),
        UIAutomationInitializerInThread=_Init,
    )
    monkeypatch.setitem(sys.modules, "uiautomation", auto)
    client = types.SimpleNamespace(gen_dir="unset")
    monkeypatch.setitem(sys.modules, "comtypes", types.SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, "comtypes.client", client)
    return agent_server._perform_uia


def test_the_agent_reads_the_title_bar_and_every_readable_text_field(monkeypatch):
    row = _Control(
        "CustomControl",
        bounds=(20, 140, 620, 180),
        selected=True,
        children=[
            _Control("DataItemControl", bounds=(20, 140, 220, 180), value="Neil"),
            _Control("DataItemControl", bounds=(220, 140, 420, 180), value=""),
            # A cell whose only text is its position name: not read, so its
            # pixels stay in the OCR.
            _Control(
                "DataItemControl",
                bounds=(420, 140, 620, 180),
                value="",
                name="dob Row 0",
            ),
        ],
    )
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
                # A password box: no value, so its pixels stay in the OCR.
                _Control("EditControl", bounds=(20, 110, 500, 130), value=""),
                row,
                _Control(
                    "DocumentControl", bounds=(20, 340, 620, 460), value="BP 128/82"
                ),
                _Control(
                    "TextControl", bounds=(20, 300, 500, 320), name="Patient: Neil"
                ),
                _Control(
                    "CheckBoxControl",
                    bounds=(20, 470, 200, 490),
                    name="Urgent",
                    toggle=1,
                ),
                # A pane with no text of its own is not a field.
                _Control("PaneControl", bounds=(700, 40, 900, 90)),
            ],
        ),
    )
    perform = _agent(monkeypatch, window)
    answer = perform("window-form-fields", {"hwnd": 7, "pid": 10})
    assert answer["window"] == {"hwnd": 7, "pid": 10, "bounds": [0, 0, 1024, 720]}
    assert all(
        valid_value_proof(f["text"], f["value_proof"])
        for f in answer["fields"]
        if f["value_proof"] is not None
    )
    assert _without_proofs(answer["fields"]) == [
        {
            "kind": "title_bar",
            "rect": [0, 0, 1024, 30],
            "text": "Patient Notes",
            "state": None,
        },
        {"kind": "edit", "rect": [20, 44, 480, 26], "text": "Neil", "state": None},
        {"kind": "edit", "rect": [20, 110, 480, 20], "text": "", "state": None},
        {"kind": "state", "rect": [20, 140, 600, 40], "text": "", "state": "selected"},
        {"kind": "text", "rect": [20, 140, 200, 40], "text": "Neil", "state": None},
        {"kind": "text", "rect": [220, 140, 200, 40], "text": "", "state": None},
        {"kind": "text", "rect": [420, 140, 200, 40], "text": "", "state": None},
        {
            "kind": "edit",
            "rect": [20, 340, 600, 120],
            "text": "BP 128/82",
            "state": None,
        },
        {
            "kind": "text",
            "rect": [20, 300, 480, 20],
            "text": "Patient: Neil",
            "state": None,
        },
        {"kind": "text", "rect": [20, 470, 180, 20], "text": "Urgent", "state": "on"},
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


@pytest.mark.parametrize(
    "control_type,source",
    [
        ("EditControl", "value"),
        ("DocumentControl", "value"),
        ("DataItemControl", "value"),
        ("TextControl", "name"),
        ("ButtonControl", "name"),
        ("TitleBarControl", "title"),
    ],
)
def test_every_masked_text_source_retains_an_edit_beyond_preview_length(
    monkeypatch, control_type, source
):
    # Same visible prefix, different stored suffix: the projection must not
    # turn these distinct forms into the same approval digest.
    import openadapt_flow.backends.win_agent.server as agent_server

    prefix = "x" * agent_server._MAX_READ_TEXT
    control = _Control(control_type, bounds=(20, 44, 500, 70))
    window = _window(control)
    perform = _agent(monkeypatch, window)
    frame = io.BytesIO()
    Image.new("RGB", SIZE, "white").save(frame, format="PNG")
    no_words = SimpleNamespace(ocr=lambda _png: [])

    def read(suffix):
        value = prefix + suffix
        if source == "title":
            window.Name = value
        elif source == "name":
            control.Name = value
        else:
            control._value = value
        answer = perform("window-form-fields", {"hwnd": 7, "pid": 10})
        assert answer["fields"][0]["text"] == value
        return window_fields_state(
            no_words,
            frame.getvalue(),
            {"bounds": WINDOW, "fields": answer["fields"]},
        )

    before, after = read("a"), read("b")
    assert before is not None and after is not None and before != after


@pytest.mark.parametrize(
    "control_type", ["CheckBoxControl", "RadioButtonControl", "TabItemControl"]
)
def test_a_stateful_control_with_unreadable_state_keeps_its_pixels(
    monkeypatch, control_type
):
    control = _Control(control_type, name="Choice", bounds=(20, 44, 500, 70))
    perform = _agent(monkeypatch, _window(control))
    answer = perform("window-form-fields", {"hwnd": 7, "pid": 10})
    assert answer["window"] is not None
    assert answer["fields"] == []


@pytest.mark.parametrize("failed_at", ["window", "control"])
def test_a_failed_child_read_never_turns_an_unknown_subtree_into_a_mask(
    monkeypatch, failed_at
):
    control = _Control("TextControl", name="Summary", bounds=(20, 44, 500, 70))
    window = _window(control)

    def failed():
        raise RuntimeError("provider cannot enumerate this subtree")

    (window if failed_at == "window" else control).GetChildren = failed
    perform = _agent(monkeypatch, window)
    assert perform("window-form-fields", {"hwnd": 7, "pid": 10})["window"] is None


def test_a_value_container_does_not_hide_its_descendant_fields(monkeypatch):
    child = _Control("EditControl", value="actual field", bounds=(20, 44, 500, 70))
    document = _Control(
        "DocumentControl", value="summary", bounds=(0, 30, 1024, 720), children=[child]
    )
    perform = _agent(monkeypatch, _window(document))
    answer = perform("window-form-fields", {"hwnd": 7, "pid": 10})
    assert [field["text"] for field in answer["fields"]] == ["summary", "actual field"]
    assert [field["kind"] for field in answer["fields"]] == ["value", "edit"]


def test_a_depth_limited_form_walk_is_incomplete(monkeypatch):
    import openadapt_flow.backends.win_agent.server as agent_server

    monkeypatch.setattr(agent_server, "_MAX_UIA_DEPTH", 1)
    child = _Control("EditControl", value="unseen field")
    pane = _Control("PaneControl", children=[child])
    perform = _agent(monkeypatch, _window(pane))
    assert perform("window-form-fields", {"hwnd": 7, "pid": 10})["window"] is None


@pytest.mark.parametrize("control_type", ["EditControl", "DocumentControl"])
@pytest.mark.parametrize(
    "value,password",
    [("", False), ("", True), ("redacted", True), ("value", None)],
    ids=["empty", "empty-password", "redacted-password", "password-status-unknown"],
)
def test_an_empty_or_redacted_value_never_hides_the_visible_field(
    monkeypatch, control_type, value, password
):
    control = _Control(
        control_type, value=value, password=password, bounds=(20, 44, 500, 70)
    )
    perform = _agent(monkeypatch, _window(control))
    answer = perform("window-form-fields", {"hwnd": 7, "pid": 10})
    if password is False:
        assert answer["window"] is not None
        assert answer["fields"][0]["text"] == ""
        assert valid_value_proof("", answer["fields"][0]["value_proof"])
    else:
        assert answer["window"] is None
        assert answer["fields"] == []


def test_an_unreadable_title_never_hides_the_title_bar(monkeypatch):
    window = _window(_Control("TitleBarControl", bounds=(0, 0, 1024, 30)))
    window.Name = ""
    perform = _agent(monkeypatch, window)
    assert perform("window-form-fields", {"hwnd": 7, "pid": 10})["fields"] == []
