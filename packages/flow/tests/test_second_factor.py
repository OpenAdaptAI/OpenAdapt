"""A sign-in's second factor comes from a person while the run waits.

Unit level: a fake control plane seals codes exactly as the dashboard's
browser code does (P-256 ECDH, HKDF-SHA256, AES-256-GCM), and a fake backend
reports a one-time-code field the way ``PlaywrightBackend`` reads it.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Optional

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from openadapt_flow import second_factor as sf
from openadapt_flow.compiler.compile import _form_submit_steps
from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.recorder import CredentialFieldError, Recorder
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.runtime.second_factor_step import DeliveryLedger, Handoff
from openadapt_flow.runtime.secret_frames import persisted_run_frame

CODE = "482913"
ENV_KEYS = (sf.URL_ENV, sf.TOKEN_ENV, sf.COMMAND_ENV, sf.TIMEOUT_ENV)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def seal(public_key_b64: str, request_id: str, code: str) -> dict[str, str]:
    """What the dashboard does in the person's browser (WebCrypto)."""

    raw = base64.urlsafe_b64decode(public_key_b64 + "=" * (-len(public_key_b64) % 4))
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    ephemeral = ec.generate_private_key(ec.SECP256R1())
    shared = ephemeral.exchange(ec.ECDH(), peer)
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=sf.SEAL_INFO + request_id.encode(),
    ).derive(shared)
    iv = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(iv, code.encode(), request_id.encode())
    epk = ephemeral.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return {"epk": _b64(epk), "iv": _b64(iv), "ciphertext": _b64(ciphertext)}


class _Response:
    def __init__(self, status_code: int, body: Any = None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeControlPlane:
    """The Cloud second-factor routes, in memory. ``answers`` are the poll
    states in order; ``"code"`` becomes an answered state with a sealed code.
    """

    request_id = "sfr-0123456789abcdef"

    def __init__(self, answers: list[Any], *, create_status: int = 201) -> None:
        self.answers = list(answers)
        self.create_status = create_status
        self.created: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.polls = 0

    def post(self, url: str, *, json: Any, headers: dict[str, str]) -> _Response:
        self.created.append(json)
        self.headers.append(headers)
        if self.create_status != 201:
            return _Response(self.create_status, {"error": "refused"})
        return _Response(201, {"request_id": self.request_id})

    def get(self, url: str, *, headers: dict[str, str]) -> _Response:
        assert url.endswith("/" + self.request_id)
        self.polls += 1
        answer = self.answers.pop(0) if self.answers else "pending"
        if isinstance(answer, int):
            return _Response(answer, {"error": "x"})
        if answer == "code":
            sealed = seal(self.created[0]["public_key"], self.request_id, CODE)
            return _Response(200, {"state": "answered", "sealed": sealed})
        return _Response(200, {"state": answer})


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _http(plane: FakeControlPlane, clock: Optional[_Clock] = None) -> sf.HttpSource:
    clock = clock or _Clock()
    return sf.HttpSource(
        "https://control.example/api/runners/runs/run-1/second-factor",
        "run-token",
        client=plane,
        clock=clock,
        sleep=clock.sleep,
    )


# -- sources -----------------------------------------------------------------


def test_a_sealed_code_reaches_the_run_and_the_control_plane_holds_no_code() -> None:
    plane = FakeControlPlane(["pending", 503, "code"])
    code = sf.obtain("code", "step_004", source=_http(plane), heartbeat=lambda: None)
    assert isinstance(code, sf.OneTimeCode) and code.consume() == CODE
    (created,) = plane.created
    assert set(created) == {"schema", "kind", "step_id", "public_key", "timeout_s"}
    assert created["kind"] == "code" and created["step_id"] == "step_004"
    assert created["timeout_s"] == int(sf.DEFAULT_TIMEOUT_S)
    assert plane.headers[0]["authorization"] == "Bearer run-token"
    # The request carries only a public key: nothing that could recover a code.
    assert CODE not in json.dumps(plane.created)


@pytest.mark.parametrize(
    ("answers", "code"),
    [
        (["declined"], "second_factor_declined"),
        (["expired"], "second_factor_timeout"),
        ([], "second_factor_timeout"),  # pending until the local deadline
        ([403], "second_factor_unavailable"),
        ([404], "second_factor_unavailable"),
        (["approved"], "second_factor_invalid"),  # wrong kind of answer
    ],
)
def test_every_other_answer_stops_the_step_with_a_closed_code(
    answers: list[Any], code: str
) -> None:
    with pytest.raises(sf.SecondFactorRefused) as refused:
        sf.obtain("code", "step_004", source=_http(FakeControlPlane(answers)))
    assert refused.value.code == code


def test_a_control_plane_that_opens_no_request_is_unavailable() -> None:
    with pytest.raises(sf.SecondFactorRefused) as refused:
        sf.obtain("code", "s1", source=_http(FakeControlPlane([], create_status=404)))
    assert refused.value.code == "second_factor_unavailable"


def test_a_ciphertext_sealed_for_another_request_does_not_open() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    sealed = seal(_b64(public), "sfr-aaaaaaaaaaaaaaaa", CODE)
    assert sf.open_sealed_code(key, "sfr-aaaaaaaaaaaaaaaa", sealed) == CODE
    with pytest.raises(sf.SecondFactorRefused) as refused:
        sf.open_sealed_code(key, "sfr-bbbbbbbbbbbbbbbb", sealed)
    assert refused.value.code == "second_factor_invalid"
    assert CODE not in str(refused.value)


def test_an_approval_waits_for_the_person_and_returns_no_value() -> None:
    plane = FakeControlPlane(["pending", "approved"])
    assert sf.obtain("approval", "step_002", source=_http(plane)) is None
    assert plane.created[0]["kind"] == "approval"


def test_the_url_must_be_https_and_needs_the_run_token() -> None:
    for url in ("http://control.example/x", "https://u:p@control.example/x"):
        with pytest.raises(sf.SecondFactorRefused):
            sf.HttpSource(url, "t")
    with pytest.raises(sf.SecondFactorRefused):
        sf.HttpSource("https://control.example/x", "")
    sf.HttpSource("http://127.0.0.1:8765/x", "t")  # local test servers only


def _script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "source.py"
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return f"{sys.executable} {path}"


def test_a_local_command_supplies_the_code(tmp_path: Path) -> None:
    command = _script(
        tmp_path,
        "import sys\nassert sys.argv[1:] == ['code', 'step_004']\nprint('482 913')\n",
    )
    env = {sf.COMMAND_ENV: command}
    code = sf.obtain("code", "step_004", env=env)
    assert code is not None and code.consume() == CODE


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("raise SystemExit(3)\n", "second_factor_declined"),
        ("raise SystemExit(1)\n", "second_factor_unavailable"),
        ("print('not a code!')\n", "second_factor_invalid"),
        ("import time\ntime.sleep(30)\n", "second_factor_timeout"),
    ],
)
def test_a_local_command_that_fails_types_nothing(
    tmp_path: Path, body: str, code: str
) -> None:
    env = {sf.COMMAND_ENV: _script(tmp_path, body), sf.TIMEOUT_ENV: "10"}
    if code == "second_factor_timeout":
        source = sf.CommandSource(env[sf.COMMAND_ENV])
        with pytest.raises(sf.SecondFactorRefused) as refused:
            source.obtain(sf.Request("code", "s", 0.5), heartbeat=lambda: None)
    else:
        with pytest.raises(sf.SecondFactorRefused) as refused:
            sf.obtain("code", "s", env=env)
    assert refused.value.code == code
    assert "not a code" not in str(refused.value)


def test_no_source_and_bad_timeouts_are_refused() -> None:
    with pytest.raises(sf.SecondFactorRefused) as refused:
        sf.obtain("code", "s", env={})
    assert refused.value.code == "second_factor_unavailable"
    for raw in ("abc", "1", "100000"):
        with pytest.raises(sf.SecondFactorRefused):
            sf.timeout_from_env({sf.TIMEOUT_ENV: raw})
    assert sf.timeout_from_env({sf.TIMEOUT_ENV: "45"}) == 45.0


@pytest.mark.parametrize(
    "raw",
    [
        "123",
        "12345678901234567",
        "12 34 5!",
        42,
        "",
        "123456\n654321",  # two codes on two lines are not one code
        "123\r456",
        "123\t456",
        "123\u2028456",
    ],
)
def test_only_one_short_code_of_letters_and_digits_is_typed(raw: object) -> None:
    with pytest.raises(sf.SecondFactorRefused):
        sf.normalize_code(raw)
    assert sf.normalize_code(" ab-12 34 \n") == "ab1234"
    assert sf.normalize_code("123\u00a0456") == "123456"
    assert sf.normalize_code("123456\r\n") == "123456"  # a command's last line


class _LateSource:
    """A source that answers, but only after the deadline has passed."""

    def __init__(self, clock: "_Clock", late_by: float) -> None:
        self.clock = clock
        self.late_by = late_by

    def obtain(self, request: sf.Request, *, heartbeat: Any) -> Optional[str]:
        self.clock.now += request.timeout_s + self.late_by
        return CODE if request.kind == "code" else None


@pytest.mark.parametrize("kind", ["code", "approval"])
def test_an_answer_after_the_deadline_is_dropped_whatever_the_source(kind: str) -> None:
    clock = _Clock()
    with pytest.raises(sf.SecondFactorRefused) as refused:
        sf.obtain(kind, "s", source=_LateSource(clock, 0.5), env={}, clock=clock)
    assert refused.value.code == "second_factor_timeout"


class _SlowPlane(FakeControlPlane):
    """A poll that blocks past the deadline and then answers."""

    def __init__(self, answers: list[Any], clock: "_Clock") -> None:
        super().__init__(answers)
        self.clock = clock

    def get(self, url: str, *, headers: dict[str, str]) -> _Response:
        self.clock.now += sf.DEFAULT_TIMEOUT_S + 1
        return super().get(url, headers=headers)


@pytest.mark.parametrize(
    ("answer", "kind"), [("code", "code"), ("approved", "approval")]
)
def test_a_control_plane_answer_that_lands_late_is_dropped(
    answer: str, kind: str
) -> None:
    clock = _Clock()
    source = _http(_SlowPlane([answer], clock), clock)
    with pytest.raises(sf.SecondFactorRefused) as refused:
        source.obtain(
            sf.Request(kind, "s", sf.DEFAULT_TIMEOUT_S), heartbeat=lambda: None
        )
    assert refused.value.code == "second_factor_timeout"


# -- program shape -------------------------------------------------------------


def _code_step(**kw: Any) -> Step:
    values: dict[str, Any] = dict(
        id="step_004",
        intent="type the one-time sign-in code",
        action=ActionKind.SECOND_FACTOR,
        one_time="code",
    )
    values.update(kw)
    return Step(**values)


def _approval_step(**kw: Any) -> Step:
    return _code_step(
        **{"id": "step_002", "intent": "approve", "one_time": "approval", **kw}
    )


#: The action values a runtime released before this one knows. An older
#: runtime validates ``action`` against its own enum, so a one-time step,
#: which no longer uses any of these, makes it refuse the whole bundle.
RELEASED_ACTIONS = frozenset(
    {
        "click",
        "double_click",
        "right_click",
        "drag",
        "type",
        "select_option",
        "key",
        "hotkey",
        "wait",
        "scroll",
        "read",
        "switch_surface",
    }
)


def test_a_one_time_step_is_its_own_action_an_older_runtime_refuses() -> None:
    from openadapt_flow.ir import ApiBinding

    for step in (_code_step(), _approval_step()):
        dumped = step.model_dump(mode="json")
        assert dumped["action"] == "second_factor"
        assert dumped["action"] not in RELEASED_ACTIONS
    assert {k.value for k in ActionKind} - RELEASED_ACTIONS == {"second_factor"}
    # Nothing can route a one-time step around the person: no anchor, text,
    # param, secret, API binding or effects.
    for bad in (
        dict(text="123456"),
        dict(param=sf.ONE_TIME_CODE_PARAM),
        dict(secret=True),
        dict(key="Enter"),
        dict(api_binding=ApiBinding(method="POST", url_template="/verify")),
        dict(one_time=None),
    ):
        with pytest.raises(ValueError):
            _code_step(**bad)
    for action in (ActionKind.TYPE, ActionKind.WAIT):
        with pytest.raises(ValueError):
            Step(id="s", intent="x", action=action, one_time="code")
    plain = Step(id="s", intent="x", action=ActionKind.CLICK)
    assert "one_time" not in plain.model_dump(mode="json")


def test_the_code_is_no_workflow_input_and_its_frames_are_black() -> None:
    from openadapt_flow.bundle_validation import build_runtime_parameter_schema

    workflow = Workflow(name="w", steps=[_code_step()])
    assert build_runtime_parameter_schema(workflow) == []
    assert sf.has_one_time_steps(workflow)
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (250, 250, 250)).save(buf, format="PNG")
    saved = persisted_run_frame(buf.getvalue(), workflow=workflow)
    with Image.open(io.BytesIO(saved)) as frame:
        assert frame.convert("RGB").getextrema() == ((0, 0), (0, 0), (0, 0))


def test_a_code_only_submit_signs_in_only_right_after_a_sign_in(monkeypatch) -> None:
    from openadapt_flow.compiler import compile as compile_module

    monkeypatch.setattr(compile_module, "_is_form_save", lambda raw: True)

    def typed(step_id: str) -> Step:
        return Step(
            id=step_id, intent="x", action=ActionKind.TYPE, param="p", secret=True
        )

    def click(step_id: str) -> Step:
        return Step(id=step_id, intent="x", action=ActionKind.CLICK)

    kinds = {"s0": "username", "s1": "password"}
    signed_in_first = [
        typed("s0"),
        typed("s1"),
        click("s2"),
        _code_step(id="s3"),
        click("s4"),
        Step(id="s5", intent="x", action=ActionKind.TYPE, text="a note"),
        click("s6"),
    ]
    found = _form_submit_steps(
        signed_in_first, {s.id: {} for s in signed_in_first}, kinds
    )
    assert [s.id for s in found] == ["s6"]
    # The exemption is armed only by a username-and-password submit, used
    # once, and lost to anything but the click that focuses the code field.
    twice = [*signed_in_first[:5], _code_step(id="s5"), click("s6")]
    found = _form_submit_steps(twice, {s.id: {} for s in twice}, kinds)
    assert [s.id for s in found] == ["s6"]
    browsed = [
        *signed_in_first[:3],
        click("b0"),
        click("b1"),
        _code_step(id="s3"),
        click("s4"),
    ]
    observations = {"s2": {}, "s4": {}}
    found = _form_submit_steps(browsed, observations, kinds)
    assert [s.id for s in found] == ["s4"]
    focused = [*signed_in_first[:3], click("f0"), _code_step(id="s3"), click("s4")]
    found = _form_submit_steps(focused, {"s2": {}, "s4": {}}, kinds)
    assert found == []
    # A code alone with a "Verify" button can confirm a payment: escalated.
    alone = [_code_step(id="s0"), click("s1")]
    found = _form_submit_steps(alone, {"s1": {}}, {})
    assert [s.id for s in found] == ["s1"]


# -- the handoff -----------------------------------------------------------------


class FakeField:
    """A one-time-code field as :class:`PlaywrightOneTimeField` reports it."""

    def __init__(self, **state: Any) -> None:
        self.state: dict[str, Any] = dict(
            kind="one-time-code",
            empty=True,
            length=0,
            masked=False,
            connected=True,
            focused=True,
        )
        self.state.update(state)
        self.typed: list[str] = []
        self.after_type: dict[str, Any] = {}
        self.on_check: Optional[Any] = None

    def mask(self) -> None:
        self.state["masked"] = True

    def bind(self) -> dict[str, Any]:
        return dict(self.state)

    def check(self) -> Optional[dict[str, Any]]:
        if self.on_check is not None:
            hook, self.on_check = self.on_check, None
            hook(self)
        return dict(self.state)

    def type_once(self, code: str) -> None:
        self.typed.append(code)
        self.state.update(empty=False, length=len(code), **self.after_type)


def _asker(code: str = CODE, calls: Optional[list[str]] = None) -> Any:
    def obtain(kind: str, step_id: str) -> Optional[sf.OneTimeCode]:
        if calls is not None:
            calls.append(kind)
        return sf.OneTimeCode(code) if kind == "code" else None

    return obtain


def test_the_handoff_types_one_code_once_into_the_bound_field() -> None:
    field, calls = FakeField(), []
    handoff = Handoff(field, DeliveryLedger(), obtain=_asker(calls=calls))
    assert handoff.deliver_code("s", key="s") is True
    assert field.typed == [CODE] and calls == ["code"]
    # The same step asks and types at most once per run, whatever calls again.
    with pytest.raises(sf.SecondFactorRefused):
        handoff.deliver_code("s", key="s")
    assert field.typed == [CODE] and calls == ["code"]


@pytest.mark.parametrize(
    "state",
    [
        dict(kind="other"),  # focus is not on a code field
        dict(empty=False, length=3),  # a prefilled field
    ],
)
def test_a_field_that_does_not_bind_is_refused_before_anyone_is_asked(
    state: dict[str, Any],
) -> None:
    field, calls = FakeField(**state), []
    with pytest.raises(sf.SecondFactorRefused) as refused:
        Handoff(field, DeliveryLedger(), obtain=_asker(calls=calls)).deliver_code(
            "s", key="s"
        )
    assert refused.value.code == "second_factor_unbound"
    assert calls == [] and field.typed == []


def test_a_field_split_into_boxes_is_refused() -> None:
    calls: list[str] = []
    field = FakeField(max_length=1)
    with pytest.raises(sf.SecondFactorRefused) as refused:
        Handoff(field, DeliveryLedger(), obtain=_asker(calls=calls)).deliver_code(
            "s", key="s"
        )
    assert refused.value.code == "second_factor_unbound"
    assert calls == [] and field.typed == []
    # A box too short for this code (but not for every code) refuses after.
    field = FakeField(max_length=4)
    with pytest.raises(sf.SecondFactorRefused):
        Handoff(field, DeliveryLedger(), obtain=_asker()).deliver_code("s", key="s")
    assert field.typed == []


def test_a_field_that_cannot_be_masked_is_refused() -> None:
    field = FakeField()
    field.mask = lambda: None  # type: ignore[method-assign]
    with pytest.raises(sf.SecondFactorRefused) as refused:
        Handoff(field, DeliveryLedger(), obtain=_asker()).deliver_code("s", key="s")
    assert refused.value.code == "second_factor_unbound" and field.typed == []


@pytest.mark.parametrize(
    "change",
    [
        dict(focused=False),  # a dialog took focus while the person answered
        dict(empty=False, length=1),  # something typed into it meanwhile
        dict(connected=False),  # the field was replaced
        dict(masked=False),  # the mask was removed
    ],
)
def test_a_field_that_changed_while_waiting_gets_nothing(
    change: dict[str, Any],
) -> None:
    field = FakeField()
    field.on_check = lambda f: f.state.update(change)
    with pytest.raises(sf.SecondFactorRefused) as refused:
        Handoff(field, DeliveryLedger(), obtain=_asker()).deliver_code("s", key="s")
    assert refused.value.code == "second_factor_unbound"
    assert field.typed == []
    assert CODE not in str(refused.value)


def test_an_inconclusive_readback_halts_and_never_types_again() -> None:
    field = FakeField()
    field.after_type = {"length": 2}  # the page shows something else
    handoff = Handoff(field, DeliveryLedger(), obtain=_asker())
    with pytest.raises(sf.SecondFactorRefused) as refused:
        handoff.deliver_code("s", key="s")
    assert refused.value.code == "second_factor_unverified"
    assert field.typed == [CODE]
    with pytest.raises(sf.SecondFactorRefused):
        handoff.deliver_code("s", key="s")
    assert field.typed == [CODE]


def test_a_page_that_submits_itself_counts_as_delivered_not_verified() -> None:
    field = FakeField()
    field.after_type = {"kind": "gone", "connected": False}
    handoff = Handoff(field, DeliveryLedger(), obtain=_asker())
    assert handoff.deliver_code("s", key="s") is False
    assert field.typed == [CODE]


def test_no_field_binding_refuses_before_asking() -> None:
    calls: list[str] = []
    with pytest.raises(sf.SecondFactorRefused) as refused:
        Handoff(None, DeliveryLedger(), obtain=_asker(calls=calls)).deliver_code(
            "s", key="s"
        )
    assert refused.value.code == "second_factor_unbound" and calls == []


def test_a_one_time_code_never_shows_itself_and_is_used_once() -> None:
    import copy
    import pickle

    code = sf.OneTimeCode(CODE)
    assert CODE not in repr(code) and CODE not in str(code) and CODE not in f"{code}"
    for leak in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            leak(code)
    assert code.consume() == CODE
    with pytest.raises(RuntimeError):
        code.consume()


# -- replay ----------------------------------------------------------------------


def _replay(
    tmp_path: Path, workflow: Workflow, field: Optional[FakeField] = None
) -> tuple[Any, Any]:
    from tests.test_replayer import FakeBackend, FakeVision

    backend = FakeBackend()
    if field is not None:
        backend.one_time_field = lambda: field  # type: ignore[attr-defined]
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    report = Replayer(backend, vision=FakeVision()).run(
        workflow, params={}, bundle_dir=bundle, run_dir=tmp_path / "run"
    )
    return report, backend


def _run_files(tmp_path: Path) -> bytes:
    return b"".join(
        p.read_bytes() for p in (tmp_path / "run").rglob("*") if p.is_file()
    )


def test_replay_types_the_code_once_and_writes_it_nowhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(sf.COMMAND_ENV, _script(tmp_path, f"print('{CODE}')\n"))
    field = FakeField()
    report, backend = _replay(tmp_path, Workflow(name="w", steps=[_code_step()]), field)
    assert report.success, [r.error for r in report.results]
    assert field.typed == [CODE]
    # Not through the generic TYPE path: no backend type_text call at all.
    assert not any(a[0] == "type" for a in backend.actions)
    (result,) = report.results
    assert result.input_verified is True and result.actuation == "dom"
    assert CODE.encode() not in _run_files(tmp_path)


def test_replay_on_a_backend_that_cannot_bind_stops_before_asking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = tmp_path / "asked"
    monkeypatch.setenv(
        sf.COMMAND_ENV,
        _script(tmp_path, f"open({str(asked)!r}, 'w').close()\nprint('{CODE}')\n"),
    )
    report, backend = _replay(tmp_path, Workflow(name="w", steps=[_code_step()]))
    assert not report.success
    (result,) = report.results
    assert "second_factor_unbound" in (result.error or "")
    assert result.failure_category == "governed_refusal"
    assert not asked.exists()
    assert not any(a[0] == "type" for a in backend.actions)


def test_replay_with_no_source_stops_before_typing(tmp_path: Path) -> None:
    field = FakeField()
    report, _backend = _replay(
        tmp_path, Workflow(name="w", steps=[_code_step()]), field
    )
    assert not report.success
    (result,) = report.results
    assert "second_factor_unavailable" in (result.error or "")
    assert result.failure_category == "governed_refusal"
    assert field.typed == []


def test_replay_halts_on_an_inconclusive_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(sf.COMMAND_ENV, _script(tmp_path, f"print('{CODE}')\n"))
    field = FakeField()
    field.after_type = {"length": 0}
    report, _backend = _replay(
        tmp_path, Workflow(name="w", steps=[_code_step()]), field
    )
    assert not report.success
    (result,) = report.results
    assert "second_factor_unverified" in (result.error or "")
    assert result.safety_halt and field.typed == [CODE]


def test_replay_waits_for_a_push_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(sf.COMMAND_ENV, _script(tmp_path, "raise SystemExit(3)\n"))
    step = _approval_step(id="step_000")
    report, _backend = _replay(tmp_path, Workflow(name="w", steps=[step]))
    assert not report.success
    assert "second_factor_declined" in (report.results[0].error or "")
    monkeypatch.setenv(sf.COMMAND_ENV, _script(tmp_path, "pass\n"))
    report, _backend = _replay(tmp_path / "again", Workflow(name="w", steps=[step]))
    assert report.success, [r.error for r in report.results]


# -- recording ---------------------------------------------------------------------


class CodePageBackend:
    """A second sign-in page with a one-time-code field."""

    BOX = (100, 100, 120, 20)

    def __init__(self) -> None:
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (640, 400), (250, 250, 250)).save(buf, format="PNG")
        self._png = buf.getvalue()
        self.typed: list[str] = []
        self.focus: Optional[str] = "one-time-code"
        self.field = FakeField()

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
        return {"kind": self.focus, "box": self.BOX} if self.focus else None

    def credential_field_at(self, x: int, y: int) -> Optional[str]:
        return "one-time-code"

    def one_time_field(self) -> FakeField:
        return self.field


def test_recording_types_the_persons_code_and_keeps_only_the_param(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(sf.COMMAND_ENV, _script(tmp_path, f"print('{CODE}')\n"))
    backend = CodePageBackend()
    rec = Recorder(backend, tmp_path / "rec", app_url="http://fake.local/")
    rec.withhold_credentials({"note": "a note"})
    rec.type_text(sf.ONE_TIME_CODE_MARKER)
    directory = rec.finish()
    assert backend.field.typed == [CODE] and backend.typed == []
    events = [
        json.loads(line)
        for line in (directory / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    (typed,) = [e for e in events if e.get("kind") == "type"]
    assert typed["one_time"] == "code" and typed["param"] == sf.ONE_TIME_CODE_PARAM
    assert "text" not in typed
    meta = json.loads((directory / "meta.json").read_text())
    assert meta["secret_params"] == [] and sf.ONE_TIME_CODE_PARAM not in meta["params"]
    for path in directory.rglob("*"):
        if path.is_file():
            assert CODE.encode() not in path.read_bytes()


def test_recording_refuses_a_code_field_without_a_source_or_with_an_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = CodePageBackend()
    rec = Recorder(backend, tmp_path / "rec", app_url="http://fake.local/")
    rec.withhold_credentials({"note": "a note"})
    with pytest.raises(CredentialFieldError, match="second_factor_unavailable"):
        rec.type_text(sf.ONE_TIME_CODE_MARKER)
    monkeypatch.setenv(sf.COMMAND_ENV, _script(tmp_path, f"print('{CODE}')\n"))
    with pytest.raises(CredentialFieldError):
        rec.type_text("a note")
    assert backend.typed == [] and backend.field.typed == []


def test_recording_refuses_when_focus_moves_while_the_person_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(sf.COMMAND_ENV, _script(tmp_path, f"print('{CODE}')\n"))
    backend = CodePageBackend()
    backend.field.on_check = lambda f: f.state.update(focused=False)
    rec = Recorder(backend, tmp_path / "rec", app_url="http://fake.local/")
    rec.withhold_credentials({"note": "a note"})
    with pytest.raises(CredentialFieldError, match="second_factor_unbound"):
        rec.type_text(sf.ONE_TIME_CODE_MARKER)
    assert backend.field.typed == [] and backend.typed == []


def test_the_dashboard_seal_vector_opens_here() -> None:
    """The same vector openadapt-cloud's secondFactorSeal.ts produced."""

    import hashlib

    vector = json.loads(
        (
            Path(__file__).parent / "fixtures" / "second_factor_seal_vector.json"
        ).read_text()
    )
    scalar = int.from_bytes(
        hashlib.sha256(vector["run_key_seed"].encode()).digest(), "big"
    )
    key = ec.derive_private_key(scalar, ec.SECP256R1())
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    assert _b64(public) == vector["run_public_key"]
    opened = sf.open_sealed_code(key, vector["request_id"], vector["sealed"])
    assert sf.normalize_code(opened) == vector["code"]


def test_a_cached_runs_child_gets_the_url_with_its_run_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from openadapt_flow.integrations.agents import skills

    monkeypatch.setenv(
        sf.URL_ENV, "https://control.example/api/runners/runs/r/second-factor"
    )
    monkeypatch.setenv(sf.TOKEN_ENV, "run-token")
    monkeypatch.setenv("UNRELATED_SECRET", "x")
    seen: dict[str, Any] = {}

    class Stop(Exception):
        pass

    def fake_client(target: Any, errlog: Any = None) -> Any:
        seen["env"] = dict(target.env)
        raise Stop

    import mcp.client.stdio as stdio

    monkeypatch.setattr(stdio, "stdio_client", fake_client)
    with pytest.raises(Stop):
        asyncio.run(skills._call_skill(sys.executable, Path("server.py"), {}))
    assert seen["env"][sf.URL_ENV].startswith("https://control.example/")
    assert seen["env"][sf.TOKEN_ENV] == "run-token"
    assert "UNRELATED_SECRET" not in seen["env"]


def test_the_run_token_goes_to_a_child_only_with_the_url_that_uses_it() -> None:
    token = {sf.TOKEN_ENV: "run-token", sf.TIMEOUT_ENV: "30"}
    command = sf.source_env({**token, sf.COMMAND_ENV: "otp-tool"})
    assert command == {sf.COMMAND_ENV: "otp-tool", sf.TIMEOUT_ENV: "30"}
    assert sf.source_env(token) == {sf.TIMEOUT_ENV: "30"}
    remote = sf.source_env({**token, sf.URL_ENV: "https://c.example/x"})
    assert remote[sf.TOKEN_ENV] == "run-token" and sf.COMMAND_ENV not in remote
