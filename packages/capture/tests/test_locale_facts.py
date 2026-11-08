"""Recording-time locale, date order and keyboard layout (#1897).

Capture reads these when a recording starts and persists them, so a later
conversion carries the recording computer's facts instead of its own.
"""

from __future__ import annotations

from types import SimpleNamespace

from openadapt_capture import locale_facts, recorder
from openadapt_capture.capture import CaptureSession


def _stub_display(monkeypatch) -> None:
    monkeypatch.setattr(recorder.utils, "get_monitor_dims", lambda: (1280, 720))
    monkeypatch.setattr(recorder.utils, "get_double_click_distance_pixels", lambda: 5.0)
    monkeypatch.setattr(
        recorder.utils, "get_double_click_interval_seconds", lambda: 0.5
    )
    monkeypatch.setattr(recorder.platform, "get_display_pixel_ratio", lambda: 2.0)


def test_recording_persists_the_facts_read_when_it_starts(
    monkeypatch, tmp_path
) -> None:
    _stub_display(monkeypatch)
    facts = {"locale": "de_de", "date_order": "dmy", "keyboard_layout": "de"}
    monkeypatch.setattr(recorder, "observe_locale_facts", lambda: dict(facts))

    recording, _ = recorder.create_recording("t", capture_dir=str(tmp_path / "c"))

    assert recording.config["capture_locale"] == facts
    with CaptureSession.load(str(tmp_path / "c")) as session:
        assert session.locale_facts == facts


def test_an_unreadable_observation_is_persisted_not_dropped(
    monkeypatch, tmp_path
) -> None:
    # Every fact unknown is still a new, bound recording. Dropping the key
    # would make it look like a recording from before #1897 and skip the check.
    _stub_display(monkeypatch)
    unknown = {"locale": None, "date_order": None, "keyboard_layout": None}
    monkeypatch.setattr(recorder, "observe_locale_facts", lambda: dict(unknown))

    recording, _ = recorder.create_recording("t", capture_dir=str(tmp_path / "c"))

    assert recording.config["capture_locale"] == unknown
    with CaptureSession.load(str(tmp_path / "c")) as session:
        assert session.locale_facts == unknown


def test_a_legacy_recording_has_no_locale_facts() -> None:
    session = CaptureSession("/tmp/unused", None, SimpleNamespace(config=None))
    assert session.locale_facts is None
    session = CaptureSession("/tmp/unused", None, SimpleNamespace(config={}))
    assert session.locale_facts is None


def test_the_reader_never_raises_and_never_guesses() -> None:
    def broken(cmd: list[str]):
        raise RuntimeError("no OS")

    seen = locale_facts.read_os_locale_facts(platform="darwin", run=broken)
    assert seen == {"locale": None, "date_order": None, "keyboard_layout": None}


def test_the_x_layout_is_the_active_group_not_the_configured_list() -> None:
    def run(cmd: list[str]):
        return "rules: evdev\nlayout:     us,de\nvariant:    ,nodeadkeys\n"

    env = {"DISPLAY": ":0"}
    assert locale_facts._xkb_layout(run, env, lambda d: 0) == "us"
    assert locale_facts._xkb_layout(run, env, lambda d: 1) == "de+nodeadkeys"
    assert locale_facts._xkb_layout(run, env, lambda d: None) is None
