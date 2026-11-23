"""Missing credential observability refuses before capture or actor input."""

from types import SimpleNamespace

import pytest

from openadapt_flow.authoring import AuthoringSession
from openadapt_flow.jit import SurfaceApp, TaskSignature, WebApp, compile_agent
from openadapt_flow.recorder import CredentialFieldError, Recorder
from openadapt_flow.secret_inputs import (
    CredentialObservationUnsupported,
    require_credential_observation,
)


class _NoFieldObservation:
    viewport = (100, 100)

    def __init__(self, events):
        self.events = events

    def close(self):
        self.events.append("close")

    def screenshot(self):
        raise AssertionError("unsupported withholding took a screenshot")

    def type_text(self, text):
        raise AssertionError("unsupported withholding delivered input")


@pytest.mark.parametrize("seam", ["absent", "noncallable", "raises"])
def test_capability_absence_is_typed_and_never_reflects_backend_detail(seam):
    class Broken:
        @property
        def focused_field_credential(self):
            raise RuntimeError("synthetic backend detail must stay private")

    backend = (
        SimpleNamespace()
        if seam == "absent"
        else SimpleNamespace(focused_field_credential=None)
        if seam == "noncallable"
        else Broken()
    )
    with pytest.raises(CredentialObservationUnsupported) as caught:
        require_credential_observation(backend)
    assert caught.value.code == "CAPTURE_CREDENTIAL_OBSERVATION_UNSUPPORTED"
    assert "synthetic backend detail" not in str(caught.value)


@pytest.mark.parametrize("entry", ["recorder", "authoring"])
def test_direct_withholding_refuses_before_any_input_or_frame(tmp_path, entry):
    events = []
    backend = _NoFieldObservation(events)
    out = tmp_path / entry
    target = (
        Recorder(backend, out)
        if entry == "recorder"
        else AuthoringSession(backend, out, backend_kind="windows")
    )
    with pytest.raises(CredentialObservationUnsupported):
        target.withhold_credentials({"password": "synthetic-only"})
    assert events == []
    assert not list(out.rglob("*.png"))
    # Recorder construction opens an empty log before this opt-in request.
    assert all(not path.read_bytes() for path in out.rglob("events.jsonl"))


@pytest.mark.parametrize("surface", ["web", "windows"])
@pytest.mark.parametrize("entry", ["run", "recording"])
def test_automatic_withholding_refuses_before_preparation_capture_or_actor(
    tmp_path, monkeypatch, capsys, surface, entry
):
    events = []
    backend = _NoFieldObservation(events)

    def opened(*args, **kwargs):
        events.append("open")
        return backend, backend.close

    if surface == "web":
        app = WebApp("http://synthetic.invalid/")
        monkeypatch.setattr(WebApp, "open", opened)
    else:
        app = SurfaceApp(
            {"kind": "windows", "agent_url": "http://127.0.0.1:1"},
            open_backend=lambda _: events.append("open") or backend,
        )
    actor = SimpleNamespace(
        dialect="generic",
        requires_model_key=False,
        run=lambda *_: events.append("actor"),
    )
    jit = compile_agent(
        actor,
        task=TaskSignature(
            "credential.unsupported", "type {password}", {"password": str}
        ),
        app=app,
        verifier=None,
        policy="clinical-write",
        cache_dir=tmp_path,
        before_capture=lambda _: events.append("prepare"),
        withhold_credentials=True,
    )
    value = "synthetic-value-must-not-be-recorded"
    with pytest.raises(CredentialObservationUnsupported):
        if entry == "run":
            jit.run(password=value)
        else:
            recording = jit.recording(password=value)
            # The request is snapshotted before an owned callback can mutate it.
            jit.withhold_credentials = False
            with recording:
                events.append("actor")
    assert events == ["open", "close"]
    assert jit.candidate() is None
    assert not list(tmp_path.rglob("*.png"))
    assert not list(tmp_path.rglob("events.jsonl"))
    out, err = capsys.readouterr()
    assert value not in out + err


def test_an_observable_ordinary_control_is_not_an_unsupported_backend():
    require_credential_observation(
        SimpleNamespace(focused_field_credential=lambda: None)
    )


def test_losing_the_seam_after_preflight_still_refuses_declared_input(tmp_path):
    backend = _NoFieldObservation([])
    backend.focused_field_credential = lambda: None
    recorder = Recorder(backend, tmp_path)
    recorder.withhold_credentials({"password": "synthetic-only"})
    backend.focused_field_credential = None
    with pytest.raises(CredentialFieldError):
        recorder.type_text("synthetic-only")
    assert not list(tmp_path.rglob("*.png"))
    assert all(not path.read_bytes() for path in tmp_path.rglob("events.jsonl"))


def test_explicit_person_secret_handoff_needs_no_automatic_credential_seam(tmp_path):
    from tests.test_authoring import ScriptedBackend

    backend = ScriptedBackend()
    assert not callable(getattr(backend, "focused_field_credential", None))
    session = AuthoringSession(backend, tmp_path, backend_kind="macos")
    session.remember_node("credential", {"x": 0, "y": 0, "w": 10, "h": 10})
    backend.values_at[(5, 5)] = None
    session.start_record()
    session.pause_for_input(param="password", secret=True, node_id="credential")
    assert session.continue_input(operator_confirmed=True) == {
        "recorded": True,
        "param": "password",
    }
    recording = session.stop_record()
    import json

    events = [
        json.loads(line)
        for line in (recording / "events.jsonl").read_text().splitlines()
    ]
    typed = [event for event in events if event["kind"] == "type"]
    assert len(typed) == 1 and typed[0]["secret"] is True
    assert "text" not in typed[0]
    assert not any(call[0] == "type_text" for call in backend.calls)


def test_a_seam_becoming_unreadable_does_not_reflect_its_error(tmp_path):
    class LosingObservation(_NoFieldObservation):
        available = True

        @property
        def focused_field_credential(self):
            if self.available:
                return lambda: None
            raise RuntimeError("synthetic-only")

    backend = LosingObservation([])
    recorder = Recorder(backend, tmp_path)
    recorder.withhold_credentials({"password": "synthetic-only"})
    backend.available = False
    with pytest.raises(CredentialFieldError) as caught:
        recorder.type_text("synthetic-only")
    assert "synthetic-only" not in str(caught.value)
    assert not list(tmp_path.rglob("*.png"))
    assert all(not path.read_bytes() for path in tmp_path.rglob("events.jsonl"))
