"""Sign-in values in a wrap recording become secret parameters.

Unit level, so they run on every pull request; the headless-browser proof is
``tests/e2e/test_wrap_sign_in_e2e.py``. A fake backend reports which field
has the focus, the way ``PlaywrightBackend.focused_field_credential`` reads it
from the page's own markup.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any, Optional

import pytest
from PIL import Image

from openadapt_flow.compiler.compile import _form_submit_steps
from openadapt_flow.ir import ActionKind, Step
from openadapt_flow.recorder import CredentialFieldError, Recorder
from openadapt_flow.secret_inputs import public_values, secret_environment

PASSWORD = "Synth-Pw-unit-5f1c9d"
USERNAME = "synthuser-unit-7a2b"
USER_BOX = (100, 100, 200, 20)
PASS_BOX = (100, 140, 200, 20)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (640, 400), (250, 250, 250)).save(buf, format="PNG")
    return buf.getvalue()


class SignInBackend:
    """A page with a sign-in form; ``focus`` names the focused field."""

    def __init__(self) -> None:
        self._png = _png()
        self.typed: list[str] = []
        self.focus: Optional[str] = None
        #: Whether the sign-in form is still on screen.
        self.form_shown = True

    @property
    def viewport(self) -> tuple[int, int]:
        return (640, 400)

    def screenshot(self) -> bytes:
        return self._png

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        pass

    def type_text(self, text: str) -> None:
        self.typed.append(text)

    def press(self, key: str) -> None:
        pass

    def scroll(self, dx: int, dy: int) -> None:
        pass

    def focused_field_credential(self) -> Optional[dict[str, Any]]:
        box = {"username": USER_BOX, "password": PASS_BOX}.get(self.focus or "")
        return {"kind": self.focus, "box": box} if box else None

    def credential_field_at(self, x: int, y: int) -> Optional[str]:
        if not self.form_shown:
            return None
        for kind, (bx, by, bw, bh) in (("username", USER_BOX), ("password", PASS_BOX)):
            if bx <= x < bx + bw and by <= y < by + bh:
                return kind
        return None


def _events(directory: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (directory / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]


def _black(png: bytes, box: tuple[int, int, int, int]) -> bool:
    x, y, w, h = box
    with Image.open(io.BytesIO(png)) as image:
        crop = image.convert("RGB").crop((x, y, x + w, y + h))
        return crop.getextrema() == ((0, 0), (0, 0), (0, 0))


def _sign_in(tmp_path: Path) -> tuple[SignInBackend, Path]:
    backend = SignInBackend()
    rec = Recorder(backend, tmp_path / "rec", app_url="http://fake.local/")
    rec.withhold_credentials(
        {"username": USERNAME, "password": PASSWORD, "note": "a note"}
    )
    backend.focus = "username"
    rec.type_text(USERNAME)
    backend.focus = "password"
    rec.type_text(PASSWORD)
    rec.click(150, 180)  # Sign in: the form is still on the before frame
    backend.form_shown = False
    backend.focus = None
    rec.type_text("a note")
    return backend, rec.finish()


def test_the_password_and_its_username_are_recorded_by_name_only(
    tmp_path: Path,
) -> None:
    backend, directory = _sign_in(tmp_path)
    # The app still received the real values.
    assert backend.typed == [USERNAME, PASSWORD, "a note"]
    events = _events(directory)
    secret = [e for e in events if e.get("secret")]
    assert [(e["param"], e["credential"]) for e in secret] == [
        ("username", "username"),
        ("password", "password"),
    ]
    assert all("text" not in e for e in secret)
    assert events[-1]["text"] == "a note" and not events[-1].get("secret")
    meta = json.loads((directory / "meta.json").read_text())
    assert meta["secret_params"] == ["password", "username"]
    assert "username" not in meta["params"] and "password" not in meta["params"]
    for path in directory.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert USERNAME.encode() not in data and PASSWORD.encode() not in data


def test_each_field_is_black_in_every_frame_that_still_shows_it(
    tmp_path: Path,
) -> None:
    _, directory = _sign_in(tmp_path)
    frames = directory / "frames"
    # The username field from its own event on; the password from its own.
    for index, boxes in ((0, [USER_BOX]), (1, [USER_BOX, PASS_BOX])):
        for side in ("before", "after"):
            png = (frames / f"{index:04d}_{side}.png").read_bytes()
            assert all(_black(png, box) for box in boxes)
    # The Sign-in click's before frame still shows the form.
    before = (frames / "0002_before.png").read_bytes()
    assert _black(before, USER_BOX) and _black(before, PASS_BOX)
    # After the form is gone nothing more is blacked out.
    after = (frames / "0003_after.png").read_bytes()
    assert not _black(after, USER_BOX) and not _black(after, PASS_BOX)


def test_a_value_that_is_no_declared_input_is_refused_before_typing(
    tmp_path: Path,
) -> None:
    backend = SignInBackend()
    rec = Recorder(backend, tmp_path / "rec", app_url="http://fake.local/")
    rec.withhold_credentials({"password": PASSWORD})
    backend.focus = "password"
    with pytest.raises(CredentialFieldError) as refused:
        rec.type_text("typed-from-elsewhere")
    assert "typed-from-elsewhere" not in str(refused.value)
    assert backend.typed == []


def test_two_inputs_with_one_value_cannot_name_a_secret(tmp_path: Path) -> None:
    backend = SignInBackend()
    rec = Recorder(backend, tmp_path / "rec", app_url="http://fake.local/")
    rec.withhold_credentials({"password": PASSWORD, "other": PASSWORD})
    backend.focus = "password"
    with pytest.raises(CredentialFieldError):
        rec.type_text(PASSWORD)
    assert backend.typed == []


def test_recordings_that_did_not_opt_in_keep_their_own_handling(
    tmp_path: Path,
) -> None:
    backend = SignInBackend()
    rec = Recorder(backend, tmp_path / "rec", app_url="http://fake.local/")
    backend.focus = "password"
    rec.type_text("literal")
    assert _events(rec.finish())[0]["text"] == "literal"


def _step(i: int, action: ActionKind, **kw: Any) -> Step:
    return Step(id=f"step_{i:03d}", intent="x", action=action, **kw)


def test_a_sign_in_submit_is_not_a_save_but_the_next_form_is(monkeypatch) -> None:
    from openadapt_flow.compiler import compile as compile_module

    monkeypatch.setattr(compile_module, "_is_form_save", lambda raw: True)
    steps = [
        _step(0, ActionKind.TYPE, param="username", secret=True),
        _step(1, ActionKind.TYPE, param="password", secret=True),
        _step(2, ActionKind.CLICK),
        _step(3, ActionKind.TYPE, text="a note"),
        _step(4, ActionKind.CLICK),
    ]
    found = _form_submit_steps(
        steps,
        {"step_002": {}, "step_004": {}},
        {"step_000": "username", "step_001": "password"},
    )
    assert [s.id for s in found] == ["step_004"]
    # A form with one ordinary field beside a secret is still a save.
    mixed = [
        _step(0, ActionKind.TYPE, param="password", secret=True),
        _step(1, ActionKind.TYPE, text="new display name"),
        _step(2, ActionKind.CLICK),
    ]
    assert [
        s.id
        for s in _form_submit_steps(mixed, {"step_002": {}}, {"step_000": "password"})
    ] == ["step_002"]


def test_a_secret_is_in_the_environment_only_during_its_run(monkeypatch) -> None:
    monkeypatch.delenv("OPENADAPT_FLOW_SECRET_PASSWORD", raising=False)
    monkeypatch.setenv("OPENADAPT_FLOW_SECRET_USERNAME", "operator-exported")
    values = {"password": PASSWORD, "username": "", "note": "n"}
    with secret_environment(values, ["password", "username"]):
        assert os.environ["OPENADAPT_FLOW_SECRET_PASSWORD"] == PASSWORD
        # An empty in-memory value leaves the operator's export in place.
        assert os.environ["OPENADAPT_FLOW_SECRET_USERNAME"] == "operator-exported"
    assert "OPENADAPT_FLOW_SECRET_PASSWORD" not in os.environ
    assert os.environ["OPENADAPT_FLOW_SECRET_USERNAME"] == "operator-exported"
    assert public_values(values, ["password", "username"]) == {"note": "n"}


def test_publish_examples_never_ask_a_caller_for_a_secret() -> None:
    from openadapt_flow.wrap.publish import curl_examples

    definition = {
        "binding": {
            "schema_version": "openadapt.execute-request/v1",
            "qualification_id": "q",
            "workflow_version": "v",
            "workflow_digest": "sha256:" + "a" * 64,
            "environment_id": "e" * 64,
            "effect_strength_schema_version": "1",
            "minimum_effect_strength": "independent_system_of_record",
        },
        "parameter_schema": [
            {"name": "note", "secret": False},
            {"name": "password", "secret": True},
        ],
    }
    (post, _) = curl_examples("https://cloud.test/x", definition)
    assert '"note": "<note>"' in post and "password" not in post


def test_a_missing_vault_set_is_named_when_the_endpoint_is_not_callable() -> None:
    from openadapt_flow.wrap.publish import VAULT_SET_REQUIRED, fetch_definition

    class Client:
        def request(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, Any]]:
            return 409, {"error": "workflow_not_ready", "code": VAULT_SET_REQUIRED}

    check = fetch_definition(
        Client(),  # type: ignore[arg-type]
        org_id="00000000-0000-4000-8000-000000000001",
        workflow_id="00000000-0000-4000-8000-000000000002",
        qualification_id="00000000-0000-4000-8000-000000000003",
    )
    assert check["callable"] is False and check["code"] == VAULT_SET_REQUIRED
    assert "vault set" in check["reason"]


def test_the_cli_reads_a_missing_sign_in_input_from_its_secret_variable(
    monkeypatch,
) -> None:
    from openadapt_flow.wrap.cli import _with_secret_env

    monkeypatch.setenv("OPENADAPT_FLOW_SECRET_PASSWORD", PASSWORD)
    values = _with_secret_env({"note": "n"}, ["note", "password", "username"])
    assert values == {"note": "n", "password": PASSWORD}


def test_a_withheld_value_typed_into_another_field_is_refused(tmp_path: Path) -> None:
    backend = SignInBackend()
    rec = Recorder(backend, tmp_path / "rec", app_url="http://fake.local/")
    rec.withhold_credentials({"password": PASSWORD, "note": "n"})
    backend.focus = "password"
    rec.type_text(PASSWORD)
    backend.focus = None
    with pytest.raises(CredentialFieldError):
        rec.type_text(f"my password is {PASSWORD}")
    assert backend.typed == [PASSWORD]


def test_a_password_change_or_a_lone_pin_stays_a_save(monkeypatch) -> None:
    from openadapt_flow.compiler import compile as compile_module

    monkeypatch.setattr(compile_module, "_is_form_save", lambda raw: True)
    steps = [
        _step(0, ActionKind.TYPE, param="username", secret=True),
        _step(1, ActionKind.TYPE, param="password", secret=True),
        _step(2, ActionKind.CLICK),
    ]
    observations = {"step_002": {}}
    sign_in = {"step_000": "username", "step_001": "password"}
    assert _form_submit_steps(steps, observations, sign_in) == []
    change = {"step_000": "username", "step_001": "new-password"}
    assert [s.id for s in _form_submit_steps(steps, observations, change)] == [
        "step_002"
    ]
    pin = [
        _step(0, ActionKind.TYPE, param="pin", secret=True),
        _step(1, ActionKind.CLICK),
    ]
    assert [
        s.id
        for s in _form_submit_steps(pin, {"step_001": {}}, {"step_000": "password"})
    ] == ["step_001"]


def test_a_field_that_cannot_be_read_takes_no_declared_input(tmp_path: Path) -> None:
    class Unreadable(SignInBackend):
        def focused_field_credential(self) -> Optional[dict[str, Any]]:
            return {"kind": "unknown", "box": None}

    backend = Unreadable()
    rec = Recorder(backend, tmp_path / "rec", app_url="http://fake.local/")
    rec.withhold_credentials({"password": PASSWORD, "note": "n"})
    with pytest.raises(CredentialFieldError):
        rec.type_text(PASSWORD)
    rec.type_text("free text")  # not a declared input: typed as before
    assert backend.typed == ["free text"]
