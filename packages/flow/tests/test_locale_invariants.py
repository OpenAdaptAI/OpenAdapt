"""Locale, date order and keyboard layout invariants (#1897)."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.locale_invariants import (
    LocaleInvariantMismatch,
    LocaleInvariants,
    check_locale_invariants,
    date_order_from_format,
    enforce_workflow_locale_invariants,
    observe_locale_invariants,
    worklist_date_needs_explicit_format,
    worklist_number_needs_explicit_format,
)


def test_matching_invariants_pass() -> None:
    recorded = LocaleInvariants(locale="en_US", date_order="mdy", keyboard_layout="us")
    current = LocaleInvariants(
        locale="en_US.UTF-8", date_order="mdy", keyboard_layout="us"
    )
    assert check_locale_invariants(recorded, current).ok


def test_date_order_mismatch_refuses() -> None:
    recorded = LocaleInvariants(locale="en_US", date_order="mdy", keyboard_layout="us")
    current = LocaleInvariants(locale="en_US", date_order="dmy", keyboard_layout="us")
    result = check_locale_invariants(recorded, current)
    assert not result.ok
    assert result.mismatched == ("date_order",)
    with pytest.raises(LocaleInvariantMismatch):
        result.raise_if_refused()


def test_keyboard_mismatch_refuses_when_keystrokes_used() -> None:
    recorded = LocaleInvariants(locale="en_US", date_order="mdy", keyboard_layout="us")
    current = LocaleInvariants(locale="en_US", date_order="mdy", keyboard_layout="fr")
    assert not check_locale_invariants(recorded, current).ok
    # Semantic value-setting may ignore keyboard layout.
    assert check_locale_invariants(recorded, current, keystroke_steps=False).ok


def test_locale_mismatch_refuses() -> None:
    recorded = LocaleInvariants(locale="en_US", date_order="mdy", keyboard_layout="us")
    current = LocaleInvariants(locale="fr_CA", date_order="mdy", keyboard_layout="us")
    result = check_locale_invariants(recorded, current)
    assert result.mismatched == ("locale",)


def test_worklist_ambiguous_numeric_date_needs_explicit_format() -> None:
    assert worklist_date_needs_explicit_format("03/04/2026")
    assert not worklist_date_needs_explicit_format("2026-03-04")
    assert not worklist_date_needs_explicit_format("14/03/2026")  # day > 12
    assert not worklist_date_needs_explicit_format("03 March 2026")
    assert not worklist_date_needs_explicit_format("03/03/2026")  # equal parts


def test_workflow_field_omitted_when_unset() -> None:
    import json

    wf = Workflow(name="demo")
    payload = json.loads(wf.model_dump_json())
    assert "locale_invariants" not in payload
    loaded = Workflow.model_validate_json(wf.model_dump_json())
    assert loaded.locale_invariants is None


def test_workflow_round_trips_locale_invariants() -> None:
    inv = LocaleInvariants(locale="fr_CA", date_order="dmy", keyboard_layout="ca")
    wf = Workflow(name="demo", locale_invariants=inv)
    loaded = Workflow.model_validate_json(wf.model_dump_json())
    assert loaded.locale_invariants is not None
    assert loaded.locale_invariants.date_order == "dmy"
    assert loaded.locale_invariants.keyboard_layout == "ca"


def test_enforce_refuses_mismatched_workflow() -> None:
    wf = Workflow(
        name="demo",
        locale_invariants=LocaleInvariants(
            locale="de_DE", date_order="dmy", keyboard_layout="de"
        ),
        steps=[
            Step(id="s1", action=ActionKind.TYPE, intent="type a date"),
        ],
    )
    current = LocaleInvariants(locale="en_US", date_order="mdy", keyboard_layout="us")
    with pytest.raises(LocaleInvariantMismatch) as excinfo:
        enforce_workflow_locale_invariants(wf, current=current)
    assert "date_order" in excinfo.value.mismatched
    assert "locale" in excinfo.value.mismatched


def test_observe_accepts_explicit_overrides() -> None:
    observed = observe_locale_invariants(
        locale_name="fr_FR", date_order="dmy", keyboard_layout="fr"
    )
    assert observed.locale.startswith("fr_fr")
    assert observed.date_order == "dmy"
    assert observed.keyboard_layout == "fr"


# ---------------------------------------------- review fixes: real detection


def _observe_under(env_locale: str) -> dict:
    """Observe in a fresh interpreter whose environment names ``env_locale``.

    A real process, so Python's start-up ``C`` LC_TIME is in play: this is
    the case that used to report ``mdy`` for every locale.
    """

    env = {k: v for k, v in os.environ.items() if not k.startswith("LC_")}
    env.update(LANG=env_locale, LC_ALL=env_locale)
    code = (
        "import json, sys\n"
        "from openadapt_flow.locale_invariants import _posix_user_time_locale\n"
        "name, order = _posix_user_time_locale()\n"
        "print(json.dumps({'name': name, 'order': order}))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(out.stdout)


def _locale_available(name: str) -> bool:
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import locale; locale.setlocale(locale.LC_TIME, {name!r})",
        ],
        capture_output=True,
        timeout=60,
    )
    return out.returncode == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX locale reader")
@pytest.mark.parametrize(
    "name,order",
    [
        ("de_DE.UTF-8", "dmy"),
        ("fr_FR.UTF-8", "dmy"),
        ("en_GB.UTF-8", "dmy"),
        ("en_US.UTF-8", "mdy"),
    ],
)
def test_real_user_locale_sets_the_date_order(name: str, order: str) -> None:
    if not _locale_available(name):
        pytest.skip(f"{name} is not installed on this host")
    seen = _observe_under(name)
    assert seen["order"] == order
    assert seen["name"] and seen["name"].lower().startswith(name[:5].lower())


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX locale reader")
def test_c_locale_is_unknown_not_en_us() -> None:
    seen = _observe_under("C")
    assert seen == {"name": None, "order": None}


def test_unknown_current_facts_refuse_instead_of_defaulting() -> None:
    recorded = LocaleInvariants(locale="en_US", date_order="mdy", keyboard_layout="us")
    result = check_locale_invariants(recorded, LocaleInvariants())
    assert not result.ok
    assert set(result.unknown) == {"locale", "date_order", "keyboard_layout"}
    with pytest.raises(LocaleInvariantMismatch):
        result.raise_if_refused()


def test_an_unreadable_layout_is_unknown_not_the_locale_language() -> None:
    # The old code reported the locale's language ("en") as the layout.
    seen = observe_locale_invariants(platform="linux", run=lambda cmd: None, env={})
    assert seen.keyboard_layout is None


def test_layout_change_under_the_same_locale_is_caught() -> None:
    # en_US with a French AZERTY layout: the old code reported "en" for both.
    def run(cmd: list[str]):
        if cmd[:2] == ["setxkbmap", "-query"]:
            return "rules:      evdev\nmodel:      pc105\nlayout:     fr\n"
        return None

    current = observe_locale_invariants(
        platform="linux", run=run, env={"DISPLAY": ":0"}, xkb_group=lambda d: None
    )
    assert current.keyboard_layout == "fr"
    recorded = LocaleInvariants(
        locale=current.locale, date_order=current.date_order, keyboard_layout="us"
    )
    result = check_locale_invariants(recorded, current)
    assert "keyboard_layout" in result.mismatched


def test_configured_layouts_without_an_x_server_are_unknown() -> None:
    # localectl and XKB_DEFAULT_LAYOUT name configuration, not the layout
    # the input target is using, so they no longer stand in for it.
    def run(cmd: list[str]):
        if cmd[:2] == ["localectl", "status"]:
            return "   X11 Layout: ca\n  X11 Variant: multix\n"
        return None

    env = {"XKB_DEFAULT_LAYOUT": "ca", "XKB_DEFAULT_VARIANT": "multix"}
    seen = observe_locale_invariants(platform="linux", run=run, env=env)
    assert seen.keyboard_layout is None


def test_macos_reader_uses_os_region_and_input_source() -> None:
    answers = {
        "AppleLocale": "de_DE@rg=chzzzz\n",
        "AppleICUDateFormatStrings": '{\n    1 = "dd.MM.yy";\n}\n',
        "AppleCurrentKeyboardLayoutInputSourceID": "com.apple.keylayout.German\n",
    }

    def run(cmd: list[str]):
        return answers.get(cmd[-1])

    seen = observe_locale_invariants(platform="darwin", run=run)
    assert seen.locale == "de_de"
    assert seen.date_order == "dmy"
    assert seen.keyboard_layout == "german"


@pytest.mark.skipif(sys.platform != "darwin", reason="real macOS defaults")
def test_real_macos_reader_reads_every_fact() -> None:
    seen = observe_locale_invariants()
    assert seen.locale and seen.date_order and seen.keyboard_layout


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows locale APIs")
def test_real_windows_reader_reads_every_fact() -> None:
    seen = observe_locale_invariants()
    assert seen.locale and seen.date_order and seen.keyboard_layout


@pytest.mark.parametrize(
    "fmt,order",
    [
        ("%d.%m.%Y", "dmy"),
        ("%e/%m/%y", "dmy"),
        ("%-d/%-m/%Y", "dmy"),
        ("%D", "mdy"),
        ("%F", "ymd"),
        ("M/d/yyyy", "mdy"),
        ("dd.MM.yyyy", "dmy"),
        ("yyyy-MM-dd", "ymd"),
        ("dddd, d MMMM yyyy", "dmy"),
        ("", None),
        ("%H:%M", None),
    ],
)
def test_date_order_from_strftime_and_icu_patterns(fmt: str, order) -> None:
    assert date_order_from_format(fmt) == order


@pytest.mark.parametrize("raw", ["03/04/26", "03.04.2026", "3-4-26", "04/03", "0304"])
def test_unrecognized_numeric_dates_need_a_format(raw: str) -> None:
    assert worklist_date_needs_explicit_format(raw)


def test_decimal_separators_that_read_two_ways_need_a_format() -> None:
    assert worklist_number_needs_explicit_format("1.234")
    assert worklist_number_needs_explicit_format("1,234")
    assert not worklist_number_needs_explicit_format("1.234,56")
    assert not worklist_number_needs_explicit_format("1,234.56")
    assert not worklist_number_needs_explicit_format("12.5")


def test_compile_carries_the_recording_invariants(tmp_path) -> None:
    from openadapt_flow.compiler.compile import _locale_invariants_from_meta

    assert _locale_invariants_from_meta({}) is None
    inv = _locale_invariants_from_meta(
        {"locale_invariants": {"locale": "de_DE", "date_order": "dmy"}}
    )
    assert inv.locale == "de_de" and inv.keyboard_layout is None
    with pytest.raises(ValueError):
        _locale_invariants_from_meta({"locale_invariants": {"date_order": "dym"}})
    with pytest.raises(ValueError):
        _locale_invariants_from_meta({"locale_invariants": "de_DE"})


# ------------------------------- re-review fixes: unknown recording facts


def test_a_layout_the_recording_could_not_read_refuses_keystrokes() -> None:
    # Used to be skipped, so keystrokes replayed on any layout.
    recorded = LocaleInvariants(locale="de_DE", date_order="dmy")
    current = LocaleInvariants(locale="de_DE", date_order="dmy", keyboard_layout="us")
    result = check_locale_invariants(recorded, current)
    assert not result.ok
    assert result.unrecorded == ("keyboard_layout",)
    assert "when recording" in result.reason
    # No keystroke step: the layout isn't needed.
    assert check_locale_invariants(recorded, current, keystroke_steps=False).ok


def _typing_workflow(invariants) -> Workflow:
    return Workflow(
        name="demo",
        locale_invariants=invariants,
        steps=[Step(id="s1", action=ActionKind.TYPE, intent="type a date")],
    )


def test_a_bound_recording_with_every_fact_unknown_refuses() -> None:
    here = LocaleInvariants(locale="en_US", date_order="mdy", keyboard_layout="us")
    with pytest.raises(LocaleInvariantMismatch) as excinfo:
        enforce_workflow_locale_invariants(
            _typing_workflow(LocaleInvariants()), current=here
        )
    assert set(excinfo.value.mismatched) == {"locale", "date_order", "keyboard_layout"}
    # It survives the bundle round trip as a binding, not as "unbound".
    loaded = Workflow.model_validate_json(
        _typing_workflow(LocaleInvariants()).model_dump_json()
    )
    assert loaded.locale_invariants is not None
    with pytest.raises(LocaleInvariantMismatch):
        enforce_workflow_locale_invariants(loaded, current=here)


def test_a_recording_from_before_the_binding_is_not_checked() -> None:
    enforce_workflow_locale_invariants(
        _typing_workflow(None), current=LocaleInvariants()
    )


def test_compile_keeps_an_all_unknown_binding() -> None:
    from openadapt_flow.compiler.compile import _locale_invariants_from_meta

    unknown = {"locale": None, "date_order": None, "keyboard_layout": None}
    bound = _locale_invariants_from_meta({"locale_invariants": unknown})
    assert bound is not None and bound.unknown() == list(unknown)


# ----------------------------- re-review fixes: the active input target


def _xkb_query(layouts: str, variants: str = "") -> str:
    return f"rules:      evdev\nlayout:     {layouts}\nvariant:    {variants}\n"


def test_the_x_server_wins_over_xkb_default_layout() -> None:
    def run(cmd: list[str]):
        return _xkb_query("fr") if cmd[:2] == ["setxkbmap", "-query"] else None

    seen = observe_locale_invariants(
        platform="linux",
        run=run,
        env={"DISPLAY": ":0", "XKB_DEFAULT_LAYOUT": "us"},
        xkb_group=lambda d: 0,
    )
    assert seen.keyboard_layout == "fr"


def test_the_active_xkb_group_picks_the_layout() -> None:
    def run(cmd: list[str]):
        return _xkb_query("us,de", ",nodeadkeys")

    def at(group):
        return observe_locale_invariants(
            platform="linux", run=run, env={"DISPLAY": ":0"}, xkb_group=lambda d: group
        ).keyboard_layout

    # Switching groups changes the observed layout; it used to read "us,de"
    # both times and compare equal.
    assert at(0) == "us"
    assert at(1) == "de+nodeadkeys"
    # Several layouts and no readable group: unknown, not the list.
    assert at(None) is None
    assert at(5) is None


def test_wayland_layout_is_unknown() -> None:
    def run(cmd: list[str]):
        return _xkb_query("us")

    seen = observe_locale_invariants(
        platform="linux",
        run=run,
        env={"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"},
        xkb_group=lambda d: 0,
    )
    assert seen.keyboard_layout is None


def test_macos_reads_text_input_sources_first() -> None:
    def run(cmd: list[str]):
        if "-c" in cmd:
            return "com.apple.keylayout.German\ncom.apple.keylayout.German\n"
        if cmd[-1] == "AppleCurrentKeyboardLayoutInputSourceID":
            return "com.apple.keylayout.US\n"  # stale saved value
        return None

    seen = observe_locale_invariants(platform="darwin", run=run)
    assert seen.keyboard_layout == "german"


def test_macos_input_method_is_part_of_the_layout() -> None:
    def run(cmd: list[str]):
        if "-c" in cmd:
            return (
                "com.apple.keylayout.ABC\n"
                "com.apple.inputmethod.Kotoeri.RomajiTyping.Japanese\n"
            )
        return None

    seen = observe_locale_invariants(platform="darwin", run=run)
    assert seen.keyboard_layout == (
        "abc+com.apple.inputmethod.kotoeri.romajityping.japanese"
    )


@pytest.mark.parametrize(
    "hkl,klid",
    [
        (0x04090409, "00000409"),  # US
        (0x04070407, "00000407"),  # German
        (0x08090409, "00000809"),  # UK layout, English (US) language
        (0xF0020409, "00010409"),  # US-Dvorak, via its Layout Id
        (0xE0010411, "E0010411"),  # Japanese IME
        (0xFFFFFFFFF0020409, "00010409"),  # sign-extended 64-bit handle
        (0, None),
        (0xF0090409, None),  # Layout Id the registry doesn't know
    ],
)
def test_windows_klid_from_the_foreground_threads_hkl(hkl: int, klid) -> None:
    from openadapt_flow.locale_invariants import klid_from_hkl

    def layout_id(value: int):
        return {0x002: "00010409"}.get(value)

    assert klid_from_hkl(hkl, layout_id) == klid


def test_windows_reader_reads_the_foreground_thread() -> None:
    import inspect

    from openadapt_flow import locale_invariants

    source = inspect.getsource(locale_invariants._windows)
    assert "GetForegroundWindow" in source
    assert "GetWindowThreadProcessId" in source
    assert "GetKeyboardLayoutNameW" not in source


@pytest.mark.parametrize(
    "platform,run,env",
    [
        ("linux", lambda cmd: _xkb_query("us,de", ",nodeadkeys"), {"DISPLAY": ":0"}),
        ("linux", lambda cmd: _xkb_query("fr"), {"DISPLAY": ":1"}),
        ("linux", lambda cmd: None, {}),
        (
            "darwin",
            lambda cmd: {
                "AppleLocale": "de_DE@rg=chzzzz\n",
                "AppleICUDateFormatStrings": '{\n    1 = "dd.MM.yy";\n}\n',
            }.get(cmd[-1], "com.apple.keylayout.German\ncom.apple.keylayout.German\n"),
            None,
        ),
    ],
)
def test_capture_producer_reads_the_same_facts(platform, run, env) -> None:
    # Capture records the facts and Flow checks them; the two readers must
    # agree or every replay would refuse (or worse, match by accident).
    facts = pytest.importorskip("openadapt_capture.locale_facts")
    kwargs = dict(platform=platform, run=run, env=env, xkb_group=lambda d: 1)
    produced = facts.read_os_locale_facts(**kwargs)
    observed = observe_locale_invariants(**kwargs)
    assert LocaleInvariants.model_validate(produced) == observed
    assert produced == observed.model_dump()


def test_capture_producer_readers_are_the_same_code() -> None:
    import inspect

    from openadapt_flow import locale_invariants

    facts = pytest.importorskip("openadapt_capture.locale_facts")
    shared = [
        "_norm",
        "_locale_family",
        "date_order_from_format",
        "_run",
        "_posix_user_time_locale",
        "_posix_date_order_for",
        "_macos_layout",
        "_macos",
        "klid_from_hkl",
        "_windows_klid_for_layout_id",
        "_windows",
        "_xkb_active_group",
        "_xkb_layout",
        "_posix",
        "read_os_locale_facts",
    ]
    for name in shared:
        assert inspect.getsource(getattr(facts, name)) == inspect.getsource(
            getattr(locale_invariants, name)
        ), name
    assert facts._TIS_SCRIPT == locale_invariants._TIS_SCRIPT


# -- replayer: recheck after focus, before keyboard delivery ----------------------


def _locale_replayer_harness(tmp_path, monkeypatch, *, layouts):
    """Deterministic Replayer control with a scripted current layout.

    ``layouts`` is consumed by each ``observe_locale_invariants`` call. A
    focusing click can advance the script (simulate an app that switches the
    input layout on focus) by appending to ``layouts`` from the backend.
    """

    import io

    from PIL import Image

    from openadapt_flow.ir import ActionKind, Anchor, Step, Workflow
    from openadapt_flow.runtime.replayer import Replayer

    observations: list[str | None] = []
    script = list(layouts)

    def observe(**_kwargs):
        from openadapt_flow.locale_invariants import LocaleInvariants

        layout = script.pop(0) if script else "us"
        observations.append(layout)
        return LocaleInvariants(
            locale="en_US", date_order="mdy", keyboard_layout=layout
        )

    monkeypatch.setattr(
        "openadapt_flow.locale_invariants.observe_locale_invariants", observe
    )

    def make_png(size=(300, 200), color=(240, 240, 240)) -> bytes:
        buf = io.BytesIO()
        Image.new("RGB", size, color).save(buf, format="PNG")
        return buf.getvalue()

    class Match:
        def __init__(self, point, region, confidence=0.95):
            self.point = point
            self.region = region
            self.confidence = confidence

    class Vision:
        def __init__(self):
            self.template_results = [
                Match(point=(110, 105), region=(100, 100, 50, 20)),
                Match(point=(110, 105), region=(100, 100, 50, 20)),
            ]
            self.pixels_changed_results: list = []

        def find_template(self, *a, **k):
            return self.template_results.pop(0) if self.template_results else None

        def find_structural_template(self, *a, **k):
            return None

        def find_text(self, *a, **k):
            return None

        def text_present(self, *a, **k):
            return False

        def ocr(self, *a, **k):
            return []

        def pixels_changed(self, *a, **k):
            if self.pixels_changed_results:
                return self.pixels_changed_results.pop(0)
            return True

        def phash_png(self, *a, **k):
            return "aa"

        def phash_distance(self, *a, **k):
            return 0

        def wait_settled(self, backend, **k):
            return backend.screenshot()

    class Backend:
        def __init__(self, on_click=None):
            self._frame = make_png()
            self.actions: list = []
            self._on_click = on_click
            self._viewport = (300, 200)
            self._text_value = ""

        @property
        def viewport(self):
            return self._viewport

        def screenshot(self):
            return self._frame

        def click(self, x, y, *, double=False):
            self.actions.append(("click", x, y, double))
            if self._on_click is not None:
                self._on_click()

        def type_text(self, text):
            self.actions.append(("type", text))
            self._text_value = self._text_value + text

        def press(self, key):
            self.actions.append(("press", key))

        def text_value_at(self, x, y):
            return self._text_value

        def focused_text_value(self):
            return self._text_value

    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "btn.png").write_bytes(make_png((50, 20)))
    run_dir = tmp_path / "run"

    from openadapt_flow.locale_invariants import LocaleInvariants

    workflow = Workflow(
        name="locale-focus",
        locale_invariants=LocaleInvariants(
            locale="en_US", date_order="mdy", keyboard_layout="us"
        ),
        steps=[
            Step(
                id="s1",
                intent="focus field",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/btn.png",
                    region=(100, 100, 50, 20),
                    click_point=(110, 105),
                    ocr_text="Field",
                ),
            ),
            Step(
                id="s2",
                intent="type note",
                action=ActionKind.TYPE,
                text="hello",
            ),
        ],
    )
    return observations, script, Backend, Vision, workflow, bundle, run_dir, Replayer


def test_layout_change_after_focus_refuses_type_before_delivery(
    tmp_path, monkeypatch
) -> None:
    """P2 at 760632e99: a focusing click can switch the layout before TYPE.

    The run-start gate only saw US. After the click the controlled layout is
    German; TYPE must refuse before keystroke delivery.
    """

    observations, script, Backend, Vision, workflow, bundle, run_dir, Replayer = (
        _locale_replayer_harness(tmp_path, monkeypatch, layouts=["us"])
    )

    def on_click():
        # App switches the active input layout when the field takes focus.
        script.append("de")

    backend = Backend(on_click=on_click)
    vision = Vision()
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert ("type", "hello") not in backend.actions
    assert observations == ["us", "de"]
    assert any(r.error and "keyboard_layout" in r.error for r in report.results), [
        r.error for r in report.results
    ]


def test_stable_layout_still_passes_after_focus_recheck(tmp_path, monkeypatch) -> None:
    """Positive: the same US layout before and after focus still succeeds."""

    observations, script, Backend, Vision, workflow, bundle, run_dir, Replayer = (
        _locale_replayer_harness(tmp_path, monkeypatch, layouts=["us"])
    )

    def on_click():
        script.append("us")

    backend = Backend(on_click=on_click)
    vision = Vision()
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is True
    assert ("type", "hello") in backend.actions
    assert observations == ["us", "us"]


def test_unavailable_layout_after_focus_refuses_type(tmp_path, monkeypatch) -> None:
    """Refuse when the post-focus target observation cannot read the layout."""

    observations, script, Backend, Vision, workflow, bundle, run_dir, Replayer = (
        _locale_replayer_harness(tmp_path, monkeypatch, layouts=["us"])
    )

    def on_click():
        script.append(None)

    backend = Backend(on_click=on_click)
    vision = Vision()
    report = Replayer(backend, vision=vision, poll_interval_s=0.01).run(
        workflow, bundle_dir=bundle, run_dir=run_dir
    )
    assert report.success is False
    assert ("type", "hello") not in backend.actions
    assert observations == ["us", None]
    assert any(
        r.error and "could not be read on this computer" in r.error
        for r in report.results
    ), [r.error for r in report.results]
