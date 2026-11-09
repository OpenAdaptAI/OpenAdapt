"""An API step never replaces steps that also wrote.

The reviewer's case for #1269: an earlier POST (/reserve.php) inside the span
was only listed in other_writes_seen, and the API step still replaced the
whole span, so a run skipped the reservation. Synthetic values only.
"""

from __future__ import annotations

import json

import pytest

from openadapt_flow import network_capture as nc
from openadapt_flow.compiler.api_proposal import (
    ApiProposalError,
    admit_api_step,
    propose_api_step,
)
from openadapt_flow.runtime.replayer import Replayer
from tests.test_api_tier_web_form import (
    _recording,
    _span_workflow,
    _workflow,
)
from tests.test_replayer import FakeBackend, FakeVision


def _with_write(rec, *, t=4.5, method="POST", url="http://app/app/reserve.php"):
    rows = [json.loads(x) for x in (rec / nc.NETWORK_FILE).read_text().splitlines()]
    rows.append(
        {"t": t, "method": method, "url": url, "status": 200, "resource_type": "fetch"}
    )
    (rec / nc.NETWORK_FILE).write_text("".join(json.dumps(r) + "\n" for r in rows))
    return rec


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_a_write_inside_the_span_refuses_the_proposal(tmp_path, method):
    rec = _with_write(_recording(tmp_path), method=method)
    wf_in = _workflow()
    wf, step, reasons = propose_api_step(wf_in, rec)
    assert step is None and wf == wf_in
    assert f"{method} /app/reserve.php" in " ".join(reasons)


def test_a_read_inside_the_span_is_still_fine(tmp_path):
    rec = _with_write(_recording(tmp_path), method="GET")
    _wf, step, reasons = propose_api_step(_workflow(), rec)
    assert step is not None, reasons


def _old_binding():
    """A binding proposed before such spans were refused."""

    wf = _span_workflow(admitted=False)
    save = wf.steps[-1]
    spec = save.api_binding.web_form.model_copy(
        update={"other_writes_seen": ["POST /reserve.php"]}
    )
    binding = save.api_binding.model_copy(update={"web_form": spec})
    return wf.model_copy(
        update={
            "steps": [*wf.steps[:-1], save.model_copy(update={"api_binding": binding})]
        }
    )


def test_such_a_binding_can_not_be_admitted():
    with pytest.raises(ApiProposalError, match="POST /reserve.php"):
        admit_api_step(_old_binding(), admitted_by="reviewer")


def test_an_admitted_older_binding_runs_on_the_gui():
    from openadapt_flow.ir import ApiAdmission

    # Admitted before such spans were refused: its admission still matches.
    wf = _old_binding()
    save = wf.steps[-1]
    clean = save.api_binding.model_copy(update={"admission": None})
    binding = clean.model_copy(
        update={
            "admission": ApiAdmission(
                admitted_by="reviewer",
                admitted_at="2026-10-18T00:00:00Z",
                spec_digest=clean.admission_digest(),
            )
        }
    )
    assert binding.admitted()
    wf = wf.model_copy(
        update={
            "steps": [*wf.steps[:-1], save.model_copy(update={"api_binding": binding})]
        }
    )
    replayer = Replayer(FakeBackend(), vision=FakeVision(), web_form_actuator=object())
    _end, _save, why = replayer._web_form_span_at(wf, 0, durable=False)
    assert why is not None and "POST /reserve.php" in why
