"""API steps compiled from a recording's network capture (docs/API_TIER.md).

Covers the four parts of the feature without a browser or a model:

- capture redaction (secrets, session tokens, echoed cookies);
- the compiler's proposal from a synthetic capture, and its refusals;
- admission (person only, bound to the exact binding, needs a record check);
- the live-session actuator against a fake transport, and the replayer's
  span: an admitted API step replaces its GUI steps, and when a check fails
  before the save is sent the GUI steps run instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from openadapt_flow import network_capture as nc
from openadapt_flow.compiler.api_proposal import (
    ApiProposalError,
    admit_api_step,
    describe_api_step,
    propose_api_step,
)
from openadapt_flow.ir import (
    ActionKind,
    ApiBinding,
    Postcondition,
    PostconditionKind,
    Step,
    WebFormField,
    WebFormPrecheck,
    WebFormSpec,
    Workflow,
)
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.actuators import (
    ActuationStatus,
    ApiActuationResult,
    ApiHaltKind,
)
from openadapt_flow.runtime.actuators.web_form import (
    WebFormActuator,
    client_rules,
    folded_checks_digest,
    form_controls,
)
from openadapt_flow.runtime.effects import Effect, EffectKind, RestRecordVerifier
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import FakeBackend, FakeVision

FORM_HTML = """<html><body>
<script> var constraints = {"form_lname":{"presence":true}}; </script>
<form action='save.php' id='DEM'>
  <input type="hidden" name="csrf_token_form" value="<session:csrf_token_form>">
  <input name="form_lname" value="">
  <select name="form_state"><option value="">Unassigned</option>
    <option value="MA">Massachusetts</option><option value="CA">California</option></select>
  <input name="form_note" value="">
  <form id="nested"><select name="form_extra"><option value="" selected>-</option></select></form>
  <select name="form_status"><option value="active" selected>Active</option></select>
</form></body></html>"""


def test_form_parser_keeps_nested_controls_and_reads_rules():
    controls = form_controls(FORM_HTML, "/app/save.php")
    assert controls is not None
    assert set(controls) == {
        "csrf_token_form",
        "form_lname",
        "form_state",
        "form_note",
        "form_extra",
        "form_status",
    }
    assert controls["form_status"]["default"] == "active"
    assert controls["form_state"]["default"] == ""
    assert client_rules(FORM_HTML) == {"form_lname": {"presence": True}}


# -- capture redaction -----------------------------------------------------------


def test_redact_withholds_secrets_tokens_and_echoed_cookies():
    lines = [
        {
            "url": "http://app/login",
            "method": "POST",
            "form": [["authUser", "nurse"], ["clearPass", "pppppppppp"]],
        },
        {
            "url": "http://app/page?token_main=aaaaaaaaaaaa",
            "method": "GET",
            "response_text": 'document.cookie="SID=cccccccccccc"; csrf="tok-bbbbbbbbbb"; pw=pppppppppp',
        },
        {
            "url": "http://app/save",
            "method": "POST",
            "form": [
                ["csrf_token_form", "tok-bbbbbbbbbb"],
                ["ssn", "123-45-6789"],
                ["lname", "Marsh"],
            ],
        },
    ]
    out = nc.redact(lines, secret_names=["ssn"], session_values=["cccccccccccc"])
    text = json.dumps(out)
    for leaked in (
        "pppppppppp",
        "tok-bbbbbbbbbb",
        "aaaaaaaaaaaa",
        "cccccccccccc",
        "123-45-6789",
    ):
        assert leaked not in text
    assert ["clearPass", nc.SECRET] in out[0]["form"]
    assert ["csrf_token_form", "<session:csrf_token_form>"] in out[2]["form"]
    assert ["lname", "Marsh"] in out[2][
        "form"
    ]  # business values stay, as in events.jsonl
    assert "<session:cookie>" in out[1]["response_text"]


class _FakeResponse:
    status = 200
    headers = {"content-type": "text/html; charset=utf-8"}

    def body(self):
        return b"<html>ok</html>"


class _FakeRequest:
    def __init__(self, url, method="GET", body=None, resource_type="document"):
        self.url, self.method, self.resource_type = url, method, resource_type
        self.post_data_buffer = body
        self.headers = (
            {"content-type": "application/x-www-form-urlencoded"} if body else {}
        )
        self.frame = type("F", (), {"url": url})()

    def all_headers(self):
        return {**self.headers, "cookie": "SID=cccccccccccc"}

    def response(self):
        return _FakeResponse()


class _FakeContext:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler

    def remove_listener(self, event, handler):
        self.handlers.pop(event, None)


def test_capture_writes_and_loads_without_static_assets(tmp_path):
    ctx = _FakeContext()
    cap = nc.NetworkCapture(ctx).start()
    ctx.handlers["requestfinished"](_FakeRequest("http://app/form"))
    ctx.handlers["requestfinished"](
        _FakeRequest("http://app/a.png", resource_type="image")
    )
    ctx.handlers["requestfinished"](
        _FakeRequest("http://app/save", "POST", b"lname=Marsh&csrf=tok-bbbbbbbbbb")
    )
    cap.stop()
    cap.write(tmp_path, t0_monotonic=0.0)
    rows = nc.load(tmp_path)
    assert [r["url"] for r in rows] == ["http://app/form", "http://app/save"]
    assert rows[1]["form"] == [["lname", "Marsh"], ["csrf", "<session:csrf>"]]
    assert "cccccccccccc" not in (tmp_path / nc.NETWORK_FILE).read_text()


# -- proposal --------------------------------------------------------------------

PARAMS = {"lname": "Marsh", "state_label": "Massachusetts", "note": "Referred"}


def _effect():
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"lname": "Marsh"},
        expected_count=1,
        timeout_s=1.0,
    )


def _recording(
    tmp_path: Path,
    *,
    save_form=None,
    popup_text="No matches were found.",
    form_page=True,
) -> Path:
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "meta.json").write_text(json.dumps({"params": PARAMS}))
    targets = {
        0: {"selector": '[name="form_lname"]', "role": "textbox", "name": "Last"},
        2: {"selector": '[name="form_state"]', "role": "combobox", "name": "State"},
        4: {"selector": '[name="form_note"]', "role": "textbox", "name": "Note"},
    }
    events = [
        {"i": i, "kind": k, "t": float(i + 1)}
        | ({"structural": targets[i]} if i in targets else {})
        for i, k in enumerate(
            ["click", "type", "click", "type", "click", "type", "click", "click"]
        )
    ]
    (rec / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    lines = []
    if form_page:
        lines.append(
            {
                "t": -1.0,
                "method": "GET",
                "url": "http://app/app/new.php",
                "status": 200,
                "response_text": FORM_HTML,
                "resource_type": "document",
            }
        )
    lines.append(
        {
            "t": 7.5,
            "method": "GET",
            "url": "http://app/app/dup.php?lname=Marsh&close",
            "status": 200,
            "response_text": f"<td>{popup_text}</td>",
            "resource_type": "document",
        }
    )
    lines.append(
        {
            "t": 8.2,
            "method": "POST",
            "url": "http://app/app/save.php",
            "status": 200,
            "resource_type": "document",
            "form": save_form
            or [
                ["csrf_token_form", "<session:csrf_token_form>"],
                ["form_lname", "Marsh"],
                ["form_state", "MA"],
                ["form_note", "Referred"],
                ["form_extra", ""],
                ["form_status", "active"],
            ],
            "response_text": "<script>window.location='/app/view.php?set_pid=7&is_new=1';</script>",
        }
    )
    (rec / nc.NETWORK_FILE).write_text(
        json.dumps({"schema": nc.SCHEMA, "exchanges": len(lines)})
        + "\n"
        + "".join(json.dumps(ln) + "\n" for ln in lines)
    )
    return rec


def _workflow(*, dialog_expect=True, effects=True) -> Workflow:
    steps = []
    for i in range(8):
        action = ActionKind.TYPE if i in (1, 3, 5) else ActionKind.CLICK
        steps.append(
            Step(
                id=f"step_{i:03d}",
                intent=f"s{i}",
                action=action,
                param={1: "lname", 3: "state_label", 5: "note"}.get(i),
            )
        )
    if dialog_expect:
        steps[6] = steps[6].model_copy(
            update={
                "expect": [
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="No matches were found.",
                    )
                ]
            }
        )
    if effects:
        steps[7] = steps[7].model_copy(
            update={"effects": [_effect()], "risk": "irreversible"}
        )
    return Workflow(name="referral", steps=steps, params=dict(PARAMS))


def test_proposal_explains_every_field_and_carries_the_duplicate_check(tmp_path):
    wf, step, reasons = propose_api_step(_workflow(), _recording(tmp_path))
    assert reasons == [] and step is not None and step.id == "step_007"
    b = step.api_binding
    assert b.kind == "web_form" and b.on_unavailable == "gui" and b.admission is None
    by = {f.name: f for f in b.web_form.fields}
    assert by["form_lname"].source == "param" and by["form_lname"].param == "lname"
    assert (
        by["form_state"].source == "select_label"
        and by["form_state"].param == "state_label"
    )
    assert by["csrf_token_form"].source == "session"
    assert (
        by["form_status"].source == "form_default"
        and by["form_status"].value == "active"
    )
    (check,) = b.web_form.prechecks
    assert (
        check.require_text == ["No matches were found."]
        and ("lname", "{lname}") in check.query
    )
    assert b.web_form.replaces_steps == [f"step_{i:03d}" for i in range(8)]
    assert b.web_form.success_pattern and "set_pid" in b.web_form.success_pattern
    assert b.effects == [_effect()]
    assert "in place of 8 GUI steps" in describe_api_step(step)


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        (
            {
                "save_form": [
                    ["form_lname", "Marsh"],
                    ["form_state", "MA"],
                    ["form_note", "Referred"],
                    ["mystery", "x9"],
                ]
            },
            "mystery",
        ),
        (
            {
                "save_form": [
                    ["form_lname", "Marsh"],
                    ["form_state", "MA"],
                    ["form_note", "Referred"],
                    ["pin", nc.SECRET],
                ]
            },
            "secret",
        ),
        ({"form_page": False}, "start recording before opening it"),
        ({"popup_text": "1 match: Marsh"}, "no response it caused carries it"),
    ],
)
def test_proposal_refuses_what_it_cannot_explain(tmp_path, kwargs, reason):
    wf_in = _workflow()
    wf, step, reasons = propose_api_step(wf_in, _recording(tmp_path, **kwargs))
    assert step is None and wf == wf_in
    assert reason in " ".join(reasons)


# -- admission -------------------------------------------------------------------


def test_admission_is_a_person_bound_to_the_exact_binding(tmp_path):
    wf, _, _ = propose_api_step(_workflow(), _recording(tmp_path))
    with pytest.raises(ApiProposalError, match="not a person"):
        admit_api_step(wf, admitted_by="agent:claude")
    admitted, ids = admit_api_step(wf, admitted_by="reviewer")
    b = admitted.steps[-1].api_binding
    assert ids == ["step_007"] and b.admitted()
    tampered = b.model_copy(update={"url_template": "/app/delete.php"})
    assert not tampered.admitted()


def test_admission_needs_a_record_check(tmp_path):
    wf, _, _ = propose_api_step(_workflow(effects=False), _recording(tmp_path))
    with pytest.raises(ApiProposalError, match="no record check"):
        admit_api_step(wf, admitted_by="reviewer")


# -- actuator --------------------------------------------------------------------


class _Resp:
    def __init__(self, status=200, text="", headers=None):
        self.status, self.text, self.headers = status, text, headers or {}


class FakeTransport:
    base_url = "http://app"

    def __init__(
        self,
        form=FORM_HTML.replace("<session:csrf_token_form>", "live-session-value"),
        dup="No matches were found.",
        save=None,
        raise_on_post=False,
    ):
        self.form, self.dup, self.raise_on_post = form, dup, raise_on_post
        self.save = save or _Resp(
            200, "<script>window.location='/app/view.php?set_pid=9&is_new=1'</script>"
        )
        self.gets: list[str] = []
        self.posts: list[tuple[str, str]] = []

    def get(self, path):
        self.gets.append(path)
        return _Resp(200, self.form if path.startswith("/app/new.php") else self.dup)

    def post_form(self, path, body, *, referer):
        if self.raise_on_post:
            raise requests.exceptions.ConnectionError("reset after send")
        self.posts.append((path, body))
        return self.save


def _admitted_binding(tmp_path) -> ApiBinding:
    wf, _, _ = propose_api_step(_workflow(), _recording(tmp_path))
    wf, _ = admit_api_step(wf, admitted_by="reviewer")
    return wf.steps[-1].api_binding


RUN = {"lname": "Feld", "state_label": "California", "note": "Referred"}


def test_actuator_sends_run_values_and_the_live_token(tmp_path):
    t = FakeTransport()
    out = WebFormActuator(t).actuate(_admitted_binding(tmp_path), RUN)
    assert out.status is ActuationStatus.ACTUATED
    ((path, body),) = t.posts
    assert path == "/app/save.php"
    assert (
        "csrf_token_form=live-session-value" in body
        and "form_lname=Feld" in body
        and "form_state=CA" in body
    )
    assert any(g.startswith("/app/dup.php?lname=Feld") for g in t.gets)


@pytest.mark.parametrize(
    "transport, run, reason",
    [
        (FakeTransport(dup="1 match found"), RUN, "did not pass"),
        (
            FakeTransport(
                form=FORM_HTML.replace('name="form_note"', 'name="form_note2"')
            ),
            RUN,
            "form changed",
        ),
        (
            FakeTransport(
                form=FORM_HTML.replace(
                    '"form_lname":{"presence":true}', '"form_note":{"presence":true}'
                )
            ),
            RUN,
            "validation rules changed",
        ),
        (
            FakeTransport(
                form=FORM_HTML.replace(
                    'value="active" selected', 'value="inactive" selected'
                )
            ),
            RUN,
            "default changed",
        ),
        (FakeTransport(), {**RUN, "state_label": "Atlantis"}, "no single option"),
        (FakeTransport(), {**RUN, "lname": ""}, "required by the form"),
        (FakeTransport(), {"lname": "Feld"}, "no value for"),
    ],
)
def test_actuator_refuses_before_sending(tmp_path, transport, run, reason):
    out = WebFormActuator(transport).actuate(_admitted_binding(tmp_path), run)
    assert out.status is ActuationStatus.UNAVAILABLE and reason in out.reason
    assert transport.posts == []


def test_actuator_halts_after_sending(tmp_path):
    binding = _admitted_binding(tmp_path)
    rejected = FakeTransport(save=_Resp(200, "Authentication Error"))
    out = WebFormActuator(rejected).actuate(binding, RUN)
    assert (
        out.status is ActuationStatus.HALT
        and out.halt_kind is ApiHaltKind.RESPONSE_REJECTED
    )
    lost = FakeTransport(raise_on_post=True)
    out = WebFormActuator(lost).actuate(binding, RUN)
    assert (
        out.status is ActuationStatus.HALT
        and out.halt_kind is ApiHaltKind.DELIVERY_UNCERTAIN
    )


# -- replayer span ---------------------------------------------------------------

TARGET = {"patient_id": "p1", "type": "Triage"}


class EnterWritesBackend(FakeBackend):
    """The GUI path: pressing Enter on the save step writes the record."""

    def __init__(self, sor_url):
        super().__init__(viewport=(300, 200))
        self.sor_url = sor_url

    def press(self, key):
        super().press(key)
        if key == "Enter":
            requests.post(
                f"{self.sor_url}/api/encounter",
                json={**TARGET, "note": "gui"},
                timeout=5,
            )


class ScriptedActuator:
    def __init__(self, sor_url, status):
        self.sor_url, self.status, self.calls = sor_url, status, 0

    def actuate(self, binding, params):
        self.calls += 1
        if self.status is ActuationStatus.ACTUATED:
            requests.post(
                f"{self.sor_url}/api/encounter",
                json={**TARGET, "note": "api"},
                timeout=5,
            )
            return ApiActuationResult(
                status=ActuationStatus.ACTUATED, reason="POST /save -> 200"
            )
        if self.status is ActuationStatus.HALT:
            return ApiActuationResult(
                status=ActuationStatus.HALT,
                halt_kind=ApiHaltKind.RESPONSE_REJECTED,
                reason="POST /save -> 200 without the recorded success response",
            )
        return ApiActuationResult(
            status=ActuationStatus.UNAVAILABLE,
            reason="the check before saving did not pass; the save was not sent",
        )


def _span_workflow(*, admitted=True) -> Workflow:
    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN, match=TARGET, expected_count=1, timeout_s=2.0
    )
    steps = [
        Step(id="step_000", intent="next field", action=ActionKind.KEY, key="Tab"),
        Step(
            id="step_001",
            intent="save",
            action=ActionKind.KEY,
            key="Enter",
            effects=[effect],
        ),
    ]
    spec = WebFormSpec(
        form_path="/new.php",
        fields=[WebFormField(name="lname", source="param", param="lname")],
        prechecks=[
            WebFormPrecheck(path="/dup.php", require_text=["No matches were found."])
        ],
        replaces_steps=["step_000", "step_001"],
        folded_checks_digest=folded_checks_digest(steps),
    )
    binding = ApiBinding(
        kind="web_form", url_template="/save.php", effects=[effect], web_form=spec
    )
    steps[1] = steps[1].model_copy(update={"api_binding": binding})
    wf = Workflow(name="span", steps=steps, params={"lname": "Marsh"})
    if admitted:
        wf, _ = admit_api_step(wf, admitted_by="reviewer")
    return wf


def _run(tmp_path, wf, status, **kw):
    url, db, stop = fault_serve()
    url = url.rstrip("/")
    try:
        backend = EnterWritesBackend(url)
        actuator = ScriptedActuator(url, status)
        bundle = tmp_path / "bundle"
        (bundle / "templates").mkdir(parents=True, exist_ok=True)
        report = Replayer(
            backend,
            vision=FakeVision(),
            effect_verifier=RestRecordVerifier(url),
            web_form_actuator=actuator,
            poll_interval_s=0.01,
            **kw,
        ).run(wf, bundle_dir=bundle, run_dir=tmp_path / "run")
        return report, backend, actuator, db.snapshot()["records"]
    finally:
        stop()


def test_admitted_api_step_replaces_its_gui_steps(tmp_path):
    report, backend, actuator, records = _run(
        tmp_path, _span_workflow(), ActuationStatus.ACTUATED
    )
    assert report.success is True and actuator.calls == 1
    folded, save = report.results
    assert folded.skipped and folded.actuation == "api_folded"
    assert save.actuation == "api" and save.effect_verified is True
    assert backend.actions == []
    assert [r["note"] for r in records] == ["api"]


def test_a_failed_check_before_the_save_runs_the_gui_steps(tmp_path):
    report, backend, actuator, records = _run(
        tmp_path, _span_workflow(), ActuationStatus.UNAVAILABLE
    )
    assert actuator.calls == 1
    assert [a[1] for a in backend.actions if a[0] == "press"] == ["Tab", "Enter"]
    assert "did not pass" in report.results[0].api_fallback
    assert [r["note"] for r in records] == ["gui"]


def test_a_rejected_save_halts_without_gui_retry(tmp_path):
    report, backend, actuator, records = _run(
        tmp_path, _span_workflow(), ActuationStatus.HALT
    )
    assert report.success is False and backend.actions == [] and records == []


@pytest.mark.parametrize(
    "mutate, kw, reason",
    [
        (lambda wf: _span_workflow(admitted=False), {}, "not admitted"),
        (lambda wf: wf, {"api_steps": False}, "off for this run"),
        (
            lambda wf: wf.model_copy(
                update={
                    "steps": [
                        wf.steps[0],
                        wf.steps[1].model_copy(
                            update={
                                "effects": [
                                    *wf.steps[1].effects,
                                    wf.steps[1].effects[0],
                                ]
                            }
                        ),
                    ]
                }
            ),
            {},
            "record check differs",
        ),
        (
            lambda wf: wf.model_copy(
                update={
                    "steps": [
                        wf.steps[0].model_copy(
                            update={
                                "expect": [
                                    Postcondition(
                                        kind=PostconditionKind.TEXT_PRESENT,
                                        text="Added later",
                                    )
                                ]
                            }
                        ),
                        wf.steps[1],
                    ]
                }
            ),
            {},
            "replaces changed",
        ),
    ],
)
def test_gui_steps_run_when_the_api_step_may_not(tmp_path, mutate, kw, reason):
    wf = mutate(_span_workflow())
    report, backend, actuator, records = _run(
        tmp_path, wf, ActuationStatus.ACTUATED, **kw
    )
    assert actuator.calls == 0
    assert reason in report.results[0].api_fallback
    assert ("press", "Tab") in [(a[0], a[1]) for a in backend.actions]


@pytest.mark.parametrize("mode", ["each_item", "problems_only", "not_gated"])
@pytest.mark.parametrize("can_prepare", [True, False])
def test_a_gated_run_uses_api_steps_that_can_show_their_request(mode, can_prepare):
    from openadapt_flow.approval_mode.model import ApprovalMode

    actuator = WebFormActuator(FakeTransport()) if can_prepare else object()
    replayer = Replayer(FakeBackend(), vision=FakeVision(), web_form_actuator=actuator)
    replayer.approval_gate = type("Gate", (), {"mode": ApprovalMode(mode)})()
    _end, _save, reason = replayer._web_form_span_at(_span_workflow(), 0, durable=False)
    # Under an approval mode the person approves the exact request
    # (tests/test_api_step_approval.py), so the actuator must prepare it.
    runs = can_prepare or mode == "not_gated"
    assert (reason is None) is runs
    if not runs:
        assert "approval mode needs the exact request" in reason


class LostResponseActuator(ScriptedActuator):
    """The save request went out and its response was lost."""

    def actuate(self, binding, params):
        self.calls += 1
        return ApiActuationResult(
            status=ActuationStatus.HALT,
            halt_kind=ApiHaltKind.DELIVERY_UNCERTAIN,
            reason="the save request failed after it was sent; it is not retried",
        )


def test_an_uncertain_save_needs_reconciliation_and_is_never_redone(tmp_path):
    url, db, stop = fault_serve()
    url = url.rstrip("/")
    try:
        backend = EnterWritesBackend(url)
        actuator = LostResponseActuator(url, ActuationStatus.HALT)
        bundle = tmp_path / "bundle"
        (bundle / "templates").mkdir(parents=True)
        report = Replayer(
            backend,
            vision=FakeVision(),
            effect_verifier=RestRecordVerifier(url),
            web_form_actuator=actuator,
            poll_interval_s=0.01,
        ).run(_span_workflow(), bundle_dir=bundle, run_dir=tmp_path / "run")
    finally:
        stop()
    assert actuator.calls == 1 and backend.actions == []
    assert report.success is False
    assert report.transaction_outcome == "RECONCILIATION_REQUIRED"
    assert report.results[-1].actuation == "api"


def test_the_fallback_reports_the_rung_that_ran(tmp_path):
    report, backend, actuator, records = _run(
        tmp_path, _span_workflow(), ActuationStatus.UNAVAILABLE
    )
    first, save = report.results
    # The reason is on the first covered step; the save ran on the GUI path
    # (a key step here, so it reports no resolution rung; a click reports
    # the rung it resolved on, such as "dom").
    assert "did not pass" in first.api_fallback
    assert first.actuation != "api_folded" and save.actuation != "api"
    assert ("press", "Enter") in [(a[0], a[1]) for a in backend.actions]


def test_an_identity_checked_save_needs_a_right_record_contract(tmp_path):
    # The save checks the right record on screen. An API step that names no
    # right-record contract can't make that check, so the GUI steps run (a
    # durable checkpoint of the save would otherwise lack identity evidence).
    wf = _span_workflow(admitted=False)
    save = wf.steps[-1].model_copy(update={"identity_armed": True})
    wf = wf.model_copy(update={"steps": [*wf.steps[:-1], save]})
    wf, _ = admit_api_step(wf, admitted_by="reviewer")
    report, backend, actuator, records = _run(tmp_path, wf, ActuationStatus.ACTUATED)
    assert actuator.calls == 0
    assert "names no right-record contract" in report.results[0].api_fallback
    assert ("press", "Enter") in [(a[0], a[1]) for a in backend.actions]
