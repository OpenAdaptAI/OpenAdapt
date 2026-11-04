"""An API step under a managed (Cloud) dispatch (docs/API_TIER.md).

A managed dispatch's run authorization comes from Cloud, and its
``api_steps`` are inside the dispatch binding digest Cloud signs. The API step
runs when that authorization admits this exact API step (its binding, the
steps it folds and its record check), as under a local governed run. When
Cloud's authorization doesn't name it, the GUI steps run and say why. The CLI
refuses a dispatch that admits an API step the bundle itself doesn't.

The record system is a scripted verifier at INDEPENDENT_SYSTEM; the values
are made up.
"""

from __future__ import annotations

import pytest

from openadapt_flow import compatibility as matrix
from openadapt_flow.runner.dispatch_envelope import (
    _BINDING_FACTORY,
    ManagedDispatchBinding,
)
from openadapt_flow.runner.protocol import dispatch_binding_sha256
from tests.test_api_step_governed import PARAMS
from tests.test_compatibility_matrix import Run

# The person in the shared governed run signs with a registered approver key.
pytestmark = pytest.mark.usefixtures("approver")

RUN_ID = "00000000-0000-4000-8000-0000000000a1"


def _binding(authorization) -> ManagedDispatchBinding:
    return ManagedDispatchBinding(
        _BINDING_FACTORY,
        run_id=RUN_ID,
        authorization=authorization,
        binding_sha256=dispatch_binding_sha256(RUN_ID, authorization),
    )


def _managed_run(run: Run, authorization):
    replayer = run.replayer()
    replayer = type(replayer)(
        replayer.backend,
        vision=replayer.vision,
        effect_verifier=run.verifier,
        managed_dispatch_binding=_binding(authorization),
        durable=True,
        checkpoint_key=replayer.checkpoint_key,
        require_settled=True,
        approval_gate=replayer.approval_gate,
        web_form_actuator=replayer.web_form_actuator,
    )
    return replayer, replayer.run(
        run.workflow, params=PARAMS, bundle_dir=run.bundle, run_dir=run.run_dir
    )


def test_the_api_step_runs_when_clouds_authorization_admits_it(tmp_path):
    run = Run(tmp_path, "governed")
    assert run.authorization.api_steps
    replayer, report = _managed_run(run, run.authorization)

    # Cloud's authorization admits the API step, so it stands for the span.
    span = replayer._web_form_span_at(run.workflow, 0, durable=True)
    assert span is not None and span[2] is None
    assert report.results[0].actuation == "api_folded"
    assert report.results[0].api_fallback is None
    # The request still needs Cloud's per-delivery permit, checked before
    # anything is sent: without remote authority credentials nothing is sent.
    save = report.results[-1]
    assert "remote authority" in (save.error or "")
    assert run.transport.posts == []


def test_without_clouds_admission_the_gui_steps_run_and_say_why(tmp_path):
    run = Run(tmp_path, "governed")
    cloud = run.authorization.model_copy(update={"api_steps": ()})
    replayer, _report = _managed_run(run, cloud)

    span = replayer._web_form_span_at(run.workflow, 0, durable=True)
    assert span is not None and span[2] is not None
    assert "authorization" in span[2]
    assert run.transport.posts == []


def test_the_matrix_says_the_api_step_works_under_a_managed_dispatch():
    assert matrix.cell("api_step", "managed").status == "works"


def test_the_cli_refuses_a_dispatch_that_admits_an_api_step_the_bundle_doesnt(
    tmp_path,
):
    from openadapt_flow.__main__ import managed_authorization_mismatch
    from openadapt_flow.runtime.authorization import AdmittedApiStep

    run = Run(tmp_path, "governed")
    local = run.authorization
    assert not managed_authorization_mismatch(local, local)
    # Naming fewer API steps is allowed: those run on the GUI.
    fewer = local.model_copy(update={"api_steps": ()})
    assert not managed_authorization_mismatch(fewer, local)
    # An API step the bundle doesn't admit is refused before anything runs.
    extra = AdmittedApiStep(
        step_id="another",
        admission_digest="sha256:" + "1" * 64,
        folded_checks_digest=local.api_steps[0].folded_checks_digest,
        effect_contract_hashes=local.api_steps[0].effect_contract_hashes,
    )
    more = local.model_copy(update={"api_steps": (*local.api_steps, extra)})
    assert managed_authorization_mismatch(more, local)
