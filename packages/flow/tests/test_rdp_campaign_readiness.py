"""Software controls for the actual fixture predicate; no native session."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from tests.e2e import rdp_campaign_fixture as fixture
from tests.fixtures import rdp_write_app as app

NAMES = ("application", "version", "environment")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("native API called by software control")

    monkeypatch.setattr(fixture.subprocess, "Popen", forbidden)
    monkeypatch.setattr(fixture.XFreeRDPTransport, "connect", forbidden)
    monkeypatch.setattr(fixture.XFreeRDPTransport, "framebuffer", forbidden)
    session = fixture.LoopbackSession(tmp_path / "session", tmp_path / "unused.db")
    calls = []
    state = {"alive": True, "identity": True, "listener": True, "exception": None}

    def poll():
        calls.append("child")
        return None if state["alive"] else 1

    def identity(_display):
        calls.append("identity")
        if state["exception"] is not None:
            raise state["exception"]
        return {"private": "identity"} if state["identity"] else {"private": "changed"}

    def listener(_shadow, _port):
        calls.append("listener")
        return state["listener"]

    session.processes = [SimpleNamespace(poll=poll)]
    session.identity = {"private": "identity"}
    session.shadow = object()
    monkeypatch.setattr(fixture, "display_identity", identity)
    monkeypatch.setattr(fixture, "owned_listener", listener)
    image = Image.new("RGB", (1280, 800), "white")
    for position, value in zip(
        app.MARKERS, (app.APP, session.version, app.ENVIRONMENT)
    ):
        image.paste(Image.open(io.BytesIO(app.marker_bytes(value))), position)
    session.transport = SimpleNamespace(framebuffer=lambda: (image, *image.size))
    probes = tuple(
        session._marker_probe(i, value, ":777", 12345)
        for i, value in enumerate((app.APP, session.version, app.ENVIRONMENT))
    )
    return SimpleNamespace(
        session=session, image=image, probes=probes, state=state, calls=calls
    )


def test_actual_predicate_accepts_exact_pixels_and_records_only_fixed_fields(rig):
    assert rig.session._initial_frame_ready(rig.probes) is True
    assert rig.calls == ["child", "identity", "listener"] * 3
    record = rig.session.readiness_last
    assert record["frame_size"] == [1280, 800]
    assert record["exception_category"] is None and record["diagnostic_error"] is False
    assert set(record) == {
        "schema",
        "stage",
        "exception_category",
        "frame_size",
        "diagnostic_error",
        "markers",
        "checks",
    }
    for name in NAMES:
        assert record["checks"][name] == dict.fromkeys(
            ("children_alive", "display_matches", "listener_owned", "pixels_match"),
            True,
        )
        marker = record["markers"][name]
        assert set(marker) == {
            "region",
            "expected_rgb_sha256",
            "observed_rgb_sha256",
            "matches",
        }
        assert marker["matches"] is True
        assert marker["expected_rgb_sha256"] == marker["observed_rgb_sha256"]
    text = json.dumps(record)
    assert (
        "private" not in text
        and app.APP not in text
        and rig.session.version not in text
    )
    assert "PNG" not in text and 'identity"' not in text


@pytest.mark.parametrize("index", range(3))
def test_one_changed_pixel_in_each_marker_refuses_with_unchanged_short_circuit(
    rig, index
):
    x, y = app.MARKERS[index]
    rig.image.putpixel((x, y), (17, 29, 41))
    assert rig.session._initial_frame_ready(rig.probes) is False
    assert rig.calls == ["child", "identity", "listener"] * (index + 1)
    record = rig.session.readiness_last
    assert record["markers"][NAMES[index]]["matches"] is False
    assert record["checks"][NAMES[index]]["pixels_match"] is False
    for name in NAMES[index + 1 :]:
        assert all(value is None for value in record["checks"][name].values())
        assert record["markers"][name]["matches"] is True


@pytest.mark.parametrize(
    "field,expected",
    [
        ("alive", ["child"]),
        ("identity", ["child", "identity"]),
        ("listener", ["child", "identity", "listener"]),
    ],
)
def test_ownership_refusal_preserves_reader_order_and_does_not_admit_matching_pixels(
    rig, field, expected
):
    rig.state[field] = False
    assert rig.session._initial_frame_ready(rig.probes) is False
    assert rig.calls == expected
    record = rig.session.readiness_last
    assert (
        record["checks"]["application"][
            {
                "alive": "children_alive",
                "identity": "display_matches",
                "listener": "listener_owned",
            }[field]
        ]
        is False
    )
    assert all(marker["matches"] for marker in record["markers"].values())
    assert all(value is None for value in record["checks"]["version"].values())


def test_wrong_frame_geometry_refuses_even_when_all_marker_pixels_match(rig):
    image = rig.image.crop((0, 0, 1279, 800))
    rig.session.transport = SimpleNamespace(framebuffer=lambda: (image, *image.size))
    assert rig.session._initial_frame_ready(rig.probes) is False
    assert rig.session.readiness_last["frame_size"] == [1279, 800]
    assert all(m["matches"] for m in rig.session.readiness_last["markers"].values())
    assert rig.calls == ["child", "identity", "listener"]


@pytest.mark.parametrize(
    "kind,category",
    [
        (FileNotFoundError, "file_unavailable"),
        (ConnectionRefusedError, "connection_refused"),
        (ValueError, "other"),
    ],
)
def test_original_exception_identity_and_fixed_category_preserved(rig, kind, category):
    error = kind("must-never-export-this-reason")
    rig.state["exception"] = error
    with pytest.raises(kind) as caught:
        rig.session._initial_frame_ready(rig.probes)
    assert caught.value is error
    record = rig.session.readiness_last
    assert record["stage"] == "application.display_matches"
    assert record["exception_category"] == category
    assert record["checks"]["application"]["display_matches"] is None
    assert rig.calls == ["child", "identity"]
    assert "must-never-export" not in json.dumps(record)
    assert rig.session._readiness_current is None


@pytest.mark.parametrize("outcome", ["valid", "refuse", "exception"])
def test_projection_failure_neither_changes_admission_nor_masks_original_exception(
    rig, monkeypatch, outcome
):
    error = ConnectionRefusedError("original-secret-like-reason")
    if outcome == "refuse":
        rig.state["listener"] = False
    if outcome == "exception":
        rig.state["exception"] = error

    def broken(image, observation):
        observation["markers"]["application"] = {"text": "never-export-projection"}
        raise ValueError("never-export-projection")

    monkeypatch.setattr(rig.session, "_project_readiness_frame", broken)
    if outcome == "exception":
        with pytest.raises(ConnectionRefusedError) as caught:
            rig.session._initial_frame_ready(rig.probes)
        assert caught.value is error
    else:
        assert rig.session._initial_frame_ready(rig.probes) is (outcome == "valid")
    record = rig.session.readiness_last
    assert record["diagnostic_error"] is True
    assert record["frame_size"] is None and record["markers"] == dict.fromkeys(NAMES)
    assert "never-export" not in json.dumps(record)
    assert "original-secret" not in json.dumps(record)


def test_first_last_are_bounded_and_short_circuited_fields_reset(rig):
    assert rig.session._initial_frame_ready(rig.probes) is True
    first = json.dumps(rig.session.readiness_first, sort_keys=True)
    rig.state["alive"] = False
    for _ in range(3):
        assert rig.session._initial_frame_ready(rig.probes) is False
    assert json.dumps(rig.session.readiness_first, sort_keys=True) == first
    assert (
        rig.session.readiness_last["checks"]["application"]["children_alive"] is False
    )
    assert (
        rig.session.readiness_last["checks"]["application"]["display_matches"] is None
    )
    assert all(
        v is None for v in rig.session.readiness_last["checks"]["version"].values()
    )
    assert rig.session.readiness_first is not rig.session.readiness_last


def test_framebuffer_exception_remains_original_without_extra_capture(rig):
    error = RuntimeError("private framebuffer reason")
    calls = []

    def fail():
        calls.append(True)
        raise error

    rig.session.transport = SimpleNamespace(framebuffer=fail)
    with pytest.raises(RuntimeError) as caught:
        rig.session._initial_frame_ready(rig.probes)
    assert caught.value is error and calls == [True] and rig.calls == []
    record = rig.session.readiness_last
    assert record["stage"] == "framebuffer" and record["exception_category"] == "other"
    assert record["frame_size"] is None and record["markers"] == dict.fromkeys(NAMES)
    assert "private framebuffer" not in json.dumps(record)


def test_malformed_diagnostic_geometry_is_not_exported(rig):
    observation = {"frame_size": None, "markers": dict.fromkeys(NAMES)}
    with pytest.raises(ValueError, match="diagnostic geometry"):
        rig.session._project_readiness_frame(
            SimpleNamespace(size=(9000, 800)), observation
        )
    assert observation == {"frame_size": None, "markers": dict.fromkeys(NAMES)}
