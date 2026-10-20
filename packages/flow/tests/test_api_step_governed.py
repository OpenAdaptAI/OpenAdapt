"""An API step under the default production profile (docs/API_TIER.md).

The default profile is Regulated, durable, governed by a run authorization,
and approval-gated per item. The API step runs there only when the run's
authorization admitted that exact API step (its binding, its folded-checks
digest and its record check); a person approves the exact request; the save
is checked with the same record check as the GUI save.

The record system is a scripted verifier at INDEPENDENT_SYSTEM; the values
are made up.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from openadapt_flow.approval_mode.gate import ApprovalGate
from openadapt_flow.approval_mode.item_approval import ItemApprovalStore
from openadapt_flow.approval_mode.model import ItemDecisionKind
from openadapt_flow.compiler.api_proposal import admit_api_step
from openadapt_flow.execution_profiles import ExecutionProfile
from openadapt_flow.ir import (
    ActionKind,
    ApiBinding,
    Step,
    WebFormField,
    WebFormSpec,
    Workflow,
)
from openadapt_flow.run_gate import build_runtime_authorization
from openadapt_flow.runtime.actuators.web_form import (
    WebFormActuator,
    folded_checks_digest,
)
from openadapt_flow.runtime.effects import ValueExpr
from tests.test_api_step_approval import APPROVE, Person
from tests.test_approval_mode_replayer import RecordSystem, _create_workflow
from tests.test_execution_profiles import (
    _KEY,
    Replayer,
    _gate,
    _ReadyVision,
    _sealed,
)
from tests.test_replayer import FakeBackend

PARAMS = {"lname": "Marsh"}
FORM = """<html><body><form action='/save.php'>
<input type="hidden" name="csrf" value="session-1">
<input name="lname" value="">
</form></body></html>"""


class Transport:
    base_url = "http://app"

    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []

    def get(self, path: str) -> Any:
        return _Resp(200, FORM)

    def post_form(self, path: str, body: str, *, referer: str) -> Any:
        self.posts.append((path, body))
        return _Resp(200, "saved")


class _Resp:
    def __init__(self, status: int, text: str) -> None:
        self.status, self.text, self.headers = status, text, {}


def _workflow(*, admitted: bool = True, identity: bool = True) -> Workflow:
    """Type the last name, then submit; the API step stands for both."""

    base = _create_workflow("api-referral")
    submit = base.steps[0]
    # The record check finds the new record by the last name this run typed.
    effect = submit.effects[0].model_copy(
        update={"new_records": [{"lname": ValueExpr(param="lname")}]}
    )
    submit = submit.model_copy(update={"effects": [effect]})
    typing = Step(
        id="type_lname",
        intent="type the last name",
        action=ActionKind.TYPE,
        text="{lname}",
    )
    spec = WebFormSpec(
        form_path="/new.php",
        fields=[
            WebFormField(name="csrf", source="session"),
            WebFormField(name="lname", source="param", param="lname"),
        ],
        controls=["csrf", "lname"],
        replaces_steps=[typing.id, submit.id],
        folded_checks_digest=folded_checks_digest([typing, submit]),
    )
    submit = submit.model_copy(
        update={
            "api_binding": ApiBinding(
                kind="web_form",
                url_template="/save.php",
                effects=list(submit.effects),
                web_form=spec,
            )
        }
    )
    wf = Workflow(
        name=base.name,
        steps=[typing, submit],
        params={"lname": "Marsh"},
    )
    if admitted:
        wf, _ = admit_api_step(
            wf,
            admitted_by="reviewer",
            identity={"subject_name": "lname"} if identity else None,
        )
    return wf


class Item:
    def __init__(self, tmp_path, *, workflow: Optional[Workflow] = None) -> None:
        self.tmp_path = tmp_path
        self.workflow, self.bundle = _sealed(
            tmp_path, workflow or _workflow(), encrypted=True
        )
        self.verifier = RecordSystem()
        self.gate_result = _gate(
            self.workflow,
            self.bundle,
            ExecutionProfile.REGULATED,
            verifier=self.verifier,
            durable=True,
        )
        assert self.gate_result.passed, self.gate_result.render()
        self.authorization = build_runtime_authorization(
            self.workflow, self.gate_result, params=PARAMS
        )
        self.run_dir = tmp_path / "run"
        self.person = Person([APPROVE])
        self.store = ItemApprovalStore(self.run_dir)
        self.person.store = self.store
        self.transport = Transport()
        self.backend = FakeBackend()

    def run(self, **overrides: Any):
        gate = ApprovalGate(
            "each_item",
            approval_store=self.store,
            approval_wait_s=30.0,
            approval_poll_s=1.0,
            sleep=self.person.sleep,
            monotonic=self.person.monotonic,
        )
        kwargs: dict[str, Any] = dict(
            vision=_ReadyVision(),
            effect_verifier=self.verifier,
            governed_authorization=self.authorization,
            durable=True,
            checkpoint_key=_KEY,
            require_settled=True,
            approval_gate=gate,
            web_form_actuator=WebFormActuator(self.transport),
        )
        kwargs.update(overrides)
        return Replayer(self.backend, **kwargs).run(
            self.workflow,
            params=PARAMS,
            bundle_dir=self.bundle,
            run_dir=self.run_dir,
        )


def test_the_default_profile_runs_the_admitted_api_step(tmp_path):
    item = Item(tmp_path)
    report = item.run()

    assert report.success is True, (
        [
            (r.step_id, r.ok, r.actuation, r.api_fallback, r.error, r.effect_results)
            for r in report.results
        ],
        report.execution_outcome,
        report.transaction_outcome,
    )
    assert item.backend.actions == []
    assert [path for path, _ in item.transport.posts] == ["/save.php"]
    assert [r.actuation for r in report.results] == ["api_folded", "api"]
    assert report.transaction_outcome == "VERIFIED"
    assert report.approval.decision is ItemDecisionKind.APPROVED
    (request,) = item.store.requests()
    assert request.form_state_kind == "api_request"


def test_an_api_step_admitted_without_a_right_record_parameter_is_refused(tmp_path):
    from openadapt_flow.execution_profiles import ExecutionProfile as P

    workflow, bundle = _sealed(tmp_path, _workflow(identity=False), encrypted=True)
    gate = _gate(workflow, bundle, P.REGULATED, verifier=RecordSystem(), durable=True)
    assert not gate.passed
    assert "api admit --identity" in gate.render()


def test_an_unadmitted_api_step_leaves_the_run_on_the_gui(tmp_path):
    item = Item(tmp_path, workflow=_workflow(admitted=False))
    assert item.authorization.api_steps == ()
    report = item.run()

    assert item.transport.posts == []
    assert "not admitted" in report.results[0].api_fallback
    assert report.results[0].actuation != "api_folded"


def test_an_api_step_the_authorization_did_not_admit_runs_on_the_gui(tmp_path):
    item = Item(tmp_path)
    item.authorization = item.authorization.model_copy(update={"api_steps": ()})
    report = item.run()

    assert item.transport.posts == []
    assert "admitted no API step" in report.results[0].api_fallback


@pytest.mark.parametrize(
    "update, reason",
    [
        ({"qualification_case_id": "case-1"}, "qualification case"),
        (
            {"production_qualification_admission_id": "admission-1"},
            "production qualification admission",
        ),
    ],
)
def test_profiles_that_dont_bind_api_steps_yet_say_so(tmp_path, update, reason):
    item = Item(tmp_path)
    replayer = Replayer(
        FakeBackend(),
        vision=_ReadyVision(),
        governed_authorization=item.authorization.model_copy(update=update),
        web_form_actuator=WebFormActuator(item.transport),
    )
    span = replayer._web_form_span_at(item.workflow, 0, durable=True)
    assert span is not None and reason in span[2]


def test_a_managed_dispatch_doesnt_bind_api_steps_yet(tmp_path):
    item = Item(tmp_path)
    replayer = Replayer(
        FakeBackend(),
        vision=_ReadyVision(),
        governed_authorization=item.authorization,
        web_form_actuator=WebFormActuator(item.transport),
    )
    replayer.managed_dispatch_binding = object()
    span = replayer._web_form_span_at(item.workflow, 0, durable=True)
    assert span is not None and "managed dispatch" in span[2]


def test_the_authorization_lists_api_steps_only_when_there_are_some(tmp_path):
    item = Item(tmp_path)
    (row,) = item.authorization.api_steps
    assert row.step_id == "submit"
    assert "api_steps" in item.authorization.model_dump(mode="json")
    bare = item.authorization.model_copy(update={"api_steps": ()})
    assert "api_steps" not in bare.model_dump(mode="json")


def _admit_cli(tmp_path, *identity: str, answer: str = "admit"):
    import argparse

    from openadapt_flow.cli_api import cmd_api_admit

    bundle = tmp_path / "cli-bundle"
    _workflow(admitted=False).save(bundle)
    args = argparse.Namespace(
        bundle=str(bundle), step=None, by="reviewer", identity=list(identity)
    )
    code = cmd_api_admit(args, ask=lambda _prompt: answer, interactive=lambda: True)
    return code, Workflow.load(bundle)


def test_api_admit_names_the_right_record_parameter(tmp_path, capsys):
    code, workflow = _admit_cli(tmp_path, "subject_name=lname")
    assert code == 0
    binding = workflow.steps[-1].api_binding
    assert binding.admitted()
    (identity,) = binding.identity
    assert (identity.key, identity.param, identity.effect_field) == (
        "subject_name",
        "lname",
        "lname",
    )
    assert identity.request_pointers == ["/body/lname"]
    assert "right record (subject_name)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "identity, message",
    [
        ("subject_name=mrn", "sends no form field"),
        ("subject_name", "is not KEY=PARAM"),
        ("not_a_signal=lname", "api admit:"),
    ],
)
def test_api_admit_refuses_a_parameter_that_cant_identify_the_record(
    tmp_path, capsys, identity, message
):
    code, workflow = _admit_cli(tmp_path, identity)
    assert code == 2
    assert message in capsys.readouterr().err
    assert not workflow.steps[-1].api_binding.admitted()


def test_the_record_check_must_select_by_the_identity_parameter():
    from openadapt_flow.compiler.api_proposal import ApiProposalError, web_form_identity

    save = _workflow(admitted=False).steps[-1]
    unrelated = save.effects[0].model_copy(update={"new_records": []})
    with pytest.raises(ApiProposalError, match="selects no field"):
        web_form_identity(save.api_binding, [unrelated], "subject_name", "lname")
