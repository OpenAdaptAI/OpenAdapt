"""An API step's span as a declared sequence of writes, then the save.

Other writes sent while the replaced steps ran used to refuse the API step.
Now each is declared with a role: ``ambient`` (the page sent it on its own,
from another document loaded before the form opened, to an endpoint the form
doesn't name; not sent by the API step) or ``step`` (a replaced step caused
it; sent in its recorded place before the save, read back by that step's
record check, and the run stops without retrying when it fails). Synthetic
values only.
"""

from __future__ import annotations

import json

import pytest
import requests

from openadapt_flow import network_capture as nc
from openadapt_flow.compiler.api_proposal import (
    ApiProposalError,
    admit_api_step,
    describe_api_step,
    propose_api_step,
)
from openadapt_flow.runtime.actuators import ActuationStatus, ApiHaltKind
from openadapt_flow.runtime.actuators.web_form import (
    WebFormActuator,
    _Resp,
    web_form_effects,
)
from openadapt_flow.runtime.authorization import admitted_api_steps
from tests.test_api_tier_web_form import (
    RUN,
    FakeTransport,
    _effect,
    _recording,
    _workflow,
)

SHELL = {
    "t": -3.0,
    "method": "GET",
    "url": "http://app/main.php",
    "status": 200,
    "resource_type": "document",
    "response_text": "<script>setTimeout(() => fetch('/library/ajax/bg.php', {method: 'POST'}), 10000)</script>",
}
BACKGROUND = {
    "t": 3.4,
    "method": "POST",
    "url": "http://app/library/ajax/bg.php",
    "status": None,
    "failed": True,
    "resource_type": "fetch",
    "frame_url": "http://app/main.php",
    "request_type": "multipart/form-data",
    "body_omitted": "multipart",
}
DRAFT = {
    "t": 4.2,
    "method": "POST",
    "url": "http://app/app/draft.php",
    "status": 200,
    "resource_type": "xhr",
    "frame_url": "http://app/app/new.php",
    "form": [["csrf_token_form", "<session:csrf_token_form>"], ["form_lname", "Marsh"]],
}


def _rec(tmp_path, *extra):
    rec = _recording(tmp_path)
    rows = [json.loads(x) for x in (rec / nc.NETWORK_FILE).read_text().splitlines()]
    body = sorted(rows[1:] + [dict(e) for e in extra], key=lambda r: r["t"])
    (rec / nc.NETWORK_FILE).write_text(
        json.dumps({"schema": nc.SCHEMA, "exchanges": len(body)})
        + "\n"
        + "".join(json.dumps(r) + "\n" for r in body)
    )
    return rec


def _with_step_effect(wf, step_id="step_003"):
    steps = [
        s.model_copy(update={"effects": [_effect()]}) if s.id == step_id else s
        for s in wf.steps
    ]
    return wf.model_copy(update={"steps": steps})


def test_a_request_the_page_sends_on_its_own_is_ambient_and_not_sent(tmp_path):
    wf, step, reasons = propose_api_step(_workflow(), _rec(tmp_path, SHELL, BACKGROUND))
    assert reasons == [] and step is not None
    (write,) = step.api_binding.web_form.writes
    assert write.role == "ambient" and write.path == "/library/ajax/bg.php"
    assert (
        write.sent_by == "/main.php"
        and "loaded before the form opened" in write.evidence
    )
    assert "not sent (the page's own request)" in describe_api_step(step)
    wf, _ = admit_api_step(wf, admitted_by="reviewer")
    t = FakeTransport()
    out = WebFormActuator(t).actuate(wf.steps[-1].api_binding, RUN)
    assert out.status is ActuationStatus.ACTUATED
    assert [p for p, _ in t.posts] == ["/app/save.php"]


@pytest.mark.parametrize(
    "edit, why",
    [
        # Sent by the form's own document: a replaced step may have caused it.
        (
            lambda b, s: (dict(b, frame_url="http://app/app/new.php"), s),
            "can't rebuild",
        ),
        # The sending page wasn't open before the form: a step may have opened it.
        (lambda b, s: (b, dict(s, t=0.5)), "can't rebuild"),
        # Navigation or form posts are never ambient.
        (lambda b, s: (dict(b, resource_type="document"), s), "can't rebuild"),
    ],
)
def test_a_write_without_the_evidence_is_a_step_write(tmp_path, edit, why):
    background, shell = edit(dict(BACKGROUND), dict(SHELL))
    _wf, step, reasons = propose_api_step(
        _workflow(), _rec(tmp_path, shell, background)
    )
    assert step is None and why in " ".join(reasons)


def test_an_endpoint_the_form_page_names_is_not_ambient(tmp_path):
    rec = _rec(tmp_path, SHELL, BACKGROUND)
    rows = [json.loads(x) for x in (rec / nc.NETWORK_FILE).read_text().splitlines()]
    for row in rows[1:]:
        if row.get("url") == "http://app/app/new.php":
            row["response_text"] += "<script>/* posts bg.php */</script>"
    (rec / nc.NETWORK_FILE).write_text("".join(json.dumps(r) + "\n" for r in rows))
    _wf, step, reasons = propose_api_step(_workflow(), rec)
    assert step is None and "bg.php" in " ".join(reasons)


def test_a_step_write_is_declared_in_order_and_needs_its_own_read_back(tmp_path):
    wf, step, reasons = propose_api_step(_workflow(), _rec(tmp_path, DRAFT))
    assert reasons == [] and step is not None
    (write,) = step.api_binding.web_form.writes
    assert (write.role, write.from_step, write.expected_status) == (
        "step",
        "step_003",
        [200],
    )
    assert [(f.name, f.source, f.param) for f in write.fields] == [
        ("csrf_token_form", "session", None),
        ("form_lname", "param", "lname"),
    ]
    assert "sent before the save, in this order" in describe_api_step(step)
    with pytest.raises(ApiProposalError, match="has no record check"):
        admit_api_step(wf, admitted_by="reviewer")
    wf = _with_step_effect(wf)
    wf, _ = admit_api_step(wf, admitted_by="reviewer")
    save = wf.steps[-1]
    # The binding's record check reads back the save and the step's write.
    assert save.api_binding.effects == web_form_effects(wf.steps, save)
    assert len(save.api_binding.effects) == len(save.effects) + 1
    assert [row.step_id for row in admitted_api_steps(wf)] == [save.id]


def _admitted_sequence(tmp_path):
    wf, _step, _ = propose_api_step(_workflow(), _rec(tmp_path, DRAFT))
    wf, _ = admit_api_step(_with_step_effect(wf), admitted_by="reviewer")
    return wf.steps[-1].api_binding


class SequenceTransport(FakeTransport):
    def __init__(self, write_status=200, raise_on_write=False, page_sends=True):
        super().__init__()
        self.write_status, self.raise_on_write = write_status, raise_on_write
        self.page_sends = page_sends

    def evaluate_form(self, form_path, action_path, fields, **kw):
        # The page's own script posts the draft as the person types (blocked
        # in the scratch run); the actuator compares it with the one it built.
        result = super().evaluate_form(form_path, action_path, fields, **kw)
        self.step_writes = kw.get("step_writes")
        if self.page_sends:
            result.writes = [
                (
                    "POST /app/draft.php",
                    [
                        ("csrf_token_form", "scratch-token"),
                        ("form_lname", dict(fields)["form_lname"]),
                    ],
                )
            ]
        return result

    def post_form(self, path, body, *, referer):
        if path == "/app/draft.php":
            if self.raise_on_write:
                raise requests.exceptions.ConnectionError("reset after send")
            self.posts.append((path, body))
            return _Resp(self.write_status, "", {})
        return super().post_form(path, body, referer=referer)


def test_the_sequence_sends_each_write_in_order_then_the_save(tmp_path):
    t = SequenceTransport()
    out = WebFormActuator(t).actuate(_admitted_sequence(tmp_path), RUN)
    assert out.status is ActuationStatus.ACTUATED
    assert [p for p, _ in t.posts] == ["/app/draft.php", "/app/save.php"]
    assert "form_lname=Feld" in t.posts[0][1] and "live-session-value" in t.posts[0][1]


@pytest.mark.parametrize(
    "transport, kind, said",
    [
        (
            lambda: SequenceTransport(write_status=500),
            ApiHaltKind.RESPONSE_REJECTED,
            "the save was not sent",
        ),
        (
            lambda: SequenceTransport(raise_on_write=True),
            ApiHaltKind.DELIVERY_UNCERTAIN,
            "may have written",
        ),
    ],
)
def test_a_failed_write_stops_the_sequence_and_nothing_is_retried(
    tmp_path, transport, kind, said
):
    t = transport()
    out = WebFormActuator(t).actuate(_admitted_sequence(tmp_path), RUN)
    assert (
        out.status is ActuationStatus.HALT
        and out.halt_kind is kind
        and said in out.reason
    )
    assert "/app/save.php" not in [p for p, _ in t.posts]
    assert len([p for p, _ in t.posts if p == "/app/draft.php"]) <= 1


def test_the_approved_digest_covers_every_write_and_keeps_the_old_one_without(tmp_path):
    seq = WebFormActuator(SequenceTransport()).prepare(
        _admitted_sequence(tmp_path), RUN
    )
    assert len(seq.writes) == 1
    bare = seq.__class__(
        method=seq.method,
        path=seq.path,
        body=seq.body,
        referer=seq.referer,
        session_fields=seq.session_fields,
    )
    assert bare.payload_sha256() != seq.payload_sha256()
    # Without writes the digest is the one the approval gate always used.
    import hashlib

    canonical = json.dumps(
        {
            "method": bare.method.upper(),
            "path": bare.path,
            "content_type": "application/x-www-form-urlencoded",
            "body": bare.encoded(),
            "referer": bare.referer,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    assert (
        bare.payload_sha256()
        == "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    )


@pytest.mark.parametrize(
    "write, why",
    [
        (dict(DRAFT, t=0.5), "before any replaced step"),
        (dict(DRAFT, url="http://other/app/draft.php"), "another origin"),
        (dict(DRAFT, url="http://app/app/draft.php?id=7"), "values in its URL"),
        (dict(DRAFT, status=500), "wasn't accepted"),
        (dict(DRAFT, form=[["form_lname", "Someone else"]]), "is not a typed value"),
        (dict(DRAFT, form=None, body_omitted="multipart"), "can't rebuild"),
    ],
)
def test_a_step_write_the_api_step_cant_repeat_refuses_the_proposal(
    tmp_path, write, why
):
    _wf, step, reasons = propose_api_step(_workflow(), _rec(tmp_path, write))
    assert step is None and why in " ".join(reasons)


def test_api_admit_shows_the_requests_left_to_the_page_before_the_person_types(
    tmp_path, capsys
):
    import argparse

    from openadapt_flow.cli_api import cmd_api_admit
    from openadapt_flow.ir import Workflow

    wf, _step, _ = propose_api_step(_workflow(), _rec(tmp_path, SHELL, BACKGROUND))
    bundle = tmp_path / "bundle"
    wf.save(bundle)
    prompts: list[str] = []
    args = argparse.Namespace(bundle=str(bundle), step=None, by="reviewer", identity=[])
    code = cmd_api_admit(
        args, ask=lambda p: prompts.append(p) or "admit", interactive=lambda: True
    )
    out = capsys.readouterr().out
    assert code == 0 and Workflow.load(bundle).steps[-1].api_binding.admitted()
    assert "will NOT send these requests" in out and "POST /library/ajax/bg.php" in out
    assert "leave the 1 request(s) above to the page" in prompts[0]


def test_a_step_write_the_page_wouldnt_send_keeps_the_save_on_the_gui(tmp_path):
    t = SequenceTransport(page_sends=False)
    out = WebFormActuator(t).actuate(_admitted_sequence(tmp_path), RUN)
    assert out.status is ActuationStatus.UNAVAILABLE
    assert "didn't send POST /app/draft.php" in out.reason
    assert t.posts == [] and t.step_writes == ("POST /app/draft.php",)


def test_two_declared_writes_need_two_sends_from_the_page(tmp_path):
    second = dict(DRAFT, t=6.2)
    wf, _step, reasons = propose_api_step(_workflow(), _rec(tmp_path, DRAFT, second))
    assert reasons == [] and len(wf.steps[-1].api_binding.web_form.writes) == 2
    wf = _with_step_effect(_with_step_effect(wf, "step_003"), "step_005")
    wf, _ = admit_api_step(wf, admitted_by="reviewer")
    # The page's script sends the draft once; one request can't match two.
    t = SequenceTransport()
    out = WebFormActuator(t).actuate(wf.steps[-1].api_binding, RUN)
    assert out.status is ActuationStatus.UNAVAILABLE and "didn't send" in out.reason
    assert t.posts == []


def test_the_scratch_run_captures_a_step_write_the_page_sends_and_blocks_it(tmp_path):
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    from openadapt_flow.runtime.actuators.web_form import PlaywrightSessionTransport
    from tests.test_api_step_page_script import _App

    app = _App()
    app.form(
        script="document.forms[0].lname.addEventListener('change', e => fetch('/draft.php', {"
        " method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'},"
        " body: 'form_lname=' + encodeURIComponent(e.target.value) }));"
    )
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(app.base + "/")
            result = PlaywrightSessionTransport(page).evaluate_form(
                "/new.php",
                "/save.php",
                [("lname", "Marsh"), ("dob", "1979-06-14"), ("state", "CA")],
                step_writes=("POST /draft.php",),
            )
            browser.close()
    finally:
        app.server.shutdown()
    assert result.status == "valid", result.reason
    assert result.writes == [("POST /draft.php", [("form_lname", "Marsh")])]
    assert app.writes == []
