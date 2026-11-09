"""An API step under a production qualification admission (docs/API_TIER.md).

A signed production qualification admission binds the bundle digest, which
seals the API step's binding, and the qualified effect contract, which names
the paths the campaign qualified. The API step runs under the admission only
when that contract names the save's API path; a contract qualified on the GUI
alone keeps the qualified GUI path. A qualification case runs the path its
contract names for the save, so a campaign can qualify the API path: the
folded steps it stands for are covered by the save's API result.

The record system is a scripted verifier at INDEPENDENT_SYSTEM; the values
are made up.
"""

from __future__ import annotations

import pytest

from openadapt_flow import compatibility as matrix
from openadapt_flow.ir import QualifiedEffectRequirement
from openadapt_flow.qualification import api_folded_step_ids
from tests.test_compatibility_matrix import Run


def _with_api_path(run: Run):
    save = run.workflow.steps[-1]
    effect = save.api_binding.effects[0]
    return run.authorization.model_copy(
        update={
            "qualified_effect_requirements": (
                *run.authorization.qualified_effect_requirements,
                QualifiedEffectRequirement(
                    step_id=save.id,
                    actuation_path="api",
                    effect_index=0,
                    effect_contract_hash=effect.contract_hash(),
                    minimum_tier=1,
                ),
            )
        }
    )


def test_the_api_step_runs_when_the_admission_qualified_its_path(tmp_path):
    run = Run(tmp_path, "qualified")
    run.authorization = _with_api_path(run)
    report = run.run()

    assert [r.actuation for r in report.results] == ["api_folded", "api"], [
        r.api_fallback or r.error for r in report.results
    ]
    assert [path for path, _ in run.transport.posts] == ["/save.php"]


def test_a_gui_only_qualification_keeps_the_qualified_gui_path(tmp_path):
    run = Run(tmp_path, "qualified")
    assert not run.authorization.effect_requirements(run.workflow.steps[-1].id, "api")
    report = run.run()

    assert run.transport.posts == []
    assert report.results[0].api_fallback == (
        "the production qualification admission didn't qualify this API step's "
        "path, so the qualified GUI path runs"
    )


def _case(run: Run, path: str):
    save = run.workflow.steps[-1]
    return run.authorization.model_copy(
        update={
            "qualification_case_id": "representative-api",
            "qualification_case_action_paths": {save.id: path},
        }
    )


def test_a_qualification_case_runs_the_path_its_contract_names():
    import tempfile
    from pathlib import Path

    run = Run(Path(tempfile.mkdtemp()), "governed")
    replayer = run.replayer()

    replayer.governed_authorization = _case(run, "api")
    span = replayer._web_form_span_at(run.workflow, 0, durable=True)
    assert span is not None and span[2] is None

    replayer.governed_authorization = _case(run, "gui")
    span = replayer._web_form_span_at(run.workflow, 0, durable=True)
    assert span is not None
    assert span[2] == "a qualification case runs the GUI path it names"


def test_folded_steps_are_covered_only_by_an_api_path_save(tmp_path):
    run = Run(tmp_path, "governed")
    save = run.workflow.steps[-1]
    folded = set(save.api_binding.web_form.replaces_steps[:-1])

    assert api_folded_step_ids(run.workflow, {save.id: "api"}) == folded
    assert api_folded_step_ids(run.workflow, {save.id: "gui"}) == set()
    assert api_folded_step_ids(run.workflow, {}) == set()


def test_the_matrix_says_the_api_step_works_under_a_qualification_admission():
    assert matrix.cell("api_step", "qualified").status == "works"


# Every person in these tests signs with a registered approver key.
pytestmark = pytest.mark.usefixtures("approver")
