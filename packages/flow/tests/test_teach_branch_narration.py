"""teach-branch: narration names the parameter that narrows the branch.

Same MockMed prior-auth scenario as ``test_teach_branch``. Two enum
parameters separate the examples from the normal run, so without narration
teach-branch refuses. A narrated sentence picks one, and is used only when
every example and normal run agrees with it. Synthetic data only.
"""

from __future__ import annotations

from openadapt_flow.ir import ParamKind, ParamSpec, Workflow
from openadapt_flow.learning.branch_teach import BranchExample, teach_branch
from openadapt_flow.narration import Narration, NarrationLine, write_narration
from tests.test_teach_branch import (
    AETNA,
    CIGNA,
    HUMANA,
    _bundle,
    _dismissed,
    _fix,
    _halt,
    _normal_texts,
    _replay,
    _workflow,
)

RULE = "If the payer type is medicaid, dismiss the prior auth notice."


def _two_param_workflow() -> Workflow:
    wf = _workflow()
    return wf.model_copy(
        update={
            "param_specs": {
                "payer_type": ParamSpec(
                    name="payer_type",
                    type=ParamKind.ENUM,
                    choices=["medicaid", "commercial"],
                    example="commercial",
                ),
                "plan_tier": ParamSpec(
                    name="plan_tier",
                    type=ParamKind.ENUM,
                    choices=["silver", "gold"],
                    example="gold",
                ),
            }
        }
    )


def _setup(tmp_path, sentence):
    wf = _two_param_workflow()
    bundle = _bundle(tmp_path, wf)
    fix = _fix(tmp_path)
    branch = {"payer_type": "medicaid", "plan_tier": "silver"}
    normal = {"payer_type": "commercial", "plan_tier": "gold"}
    r1 = _halt(wf, bundle, tmp_path / "r1", AETNA, branch)
    r2 = _halt(wf, bundle, tmp_path / "r2", CIGNA, branch)
    if sentence:
        write_narration(
            r1, Narration(lines=[NarrationLine(t_start=0.0, t_end=2.0, text=sentence)])
        )
    normal_dir = tmp_path / "normal"
    report, _ = _replay(wf, bundle, normal_dir, text=AETNA, blocking=False, params=normal)
    assert report.success
    return wf, bundle, [BranchExample(run_dir=r1, fix=fix), BranchExample(run_dir=r2, fix=fix)], normal_dir


def test_without_narration_two_separating_parameters_are_refused(tmp_path):
    _, bundle, examples, normal_dir = _setup(tmp_path, None)
    result = teach_branch(
        examples,
        tmp_path / "out",
        bundle=bundle,
        negatives=[normal_dir],
        screen_texts=_normal_texts({normal_dir: [AETNA]}),
    )
    assert not result.promoted
    assert "several parameters" in result.reason


def test_narration_picks_the_parameter_when_every_run_agrees(tmp_path):
    _, bundle, examples, normal_dir = _setup(tmp_path, RULE)
    out = tmp_path / "out"
    result = teach_branch(
        examples,
        out,
        bundle=bundle,
        negatives=[normal_dir],
        screen_texts=_normal_texts({normal_dir: [AETNA]}),
    )
    assert result.promoted, result.summary()
    assert (result.signature.param, result.signature.value) == ("payer_type", "medicaid")
    assert (result.narration, result.narration_status) == (RULE, "certified")
    assert "narration:" in result.summary()
    learned = Workflow.load(out)
    (source,) = learned.guard_sources.values()
    assert source.sentence == RULE and source.certified_by == "recordings_agree"
    report, backend = _replay(
        learned, out, tmp_path / "m", text=HUMANA,
        params={"payer_type": "medicaid", "plan_tier": "gold"},
    )
    assert report.success and _dismissed(backend)
    assert report.model_calls == 0


def test_narration_a_run_contradicts_is_not_used(tmp_path):
    # The examples were silver; the sentence says gold.
    _, bundle, examples, normal_dir = _setup(
        tmp_path, "If the plan tier is gold, dismiss the notice."
    )
    result = teach_branch(
        examples,
        tmp_path / "out",
        bundle=bundle,
        negatives=[normal_dir],
        screen_texts=_normal_texts({normal_dir: [AETNA]}),
    )
    assert not result.promoted
    assert result.narration_status == "contradicted"
    assert "several parameters" in result.reason


def test_repair_review_shows_the_sentence_beside_the_guard(tmp_path, capsys):
    from openadapt_flow.repair.cli import _print_narrated_guards
    from openadapt_flow.repair.registration import load_detached_candidate

    _, bundle, examples, normal_dir = _setup(tmp_path, RULE)
    out = tmp_path / "out"
    assert teach_branch(
        examples,
        out,
        bundle=bundle,
        negatives=[normal_dir],
        screen_texts=_normal_texts({normal_dir: [AETNA]}),
    ).promoted
    candidate = load_detached_candidate(out)
    _print_narrated_guards(candidate)
    shown = capsys.readouterr().out
    assert "guards suggested by narration" in shown
    assert RULE in shown and "recordings_agree" in shown
    # The candidate record itself still carries digests, not the sentence.
    assert RULE not in candidate.model_dump_json()
