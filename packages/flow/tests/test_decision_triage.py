"""Save triage (openadapt_flow.decision): the four invariants and the parts.

1. Healthy replay makes no provider call (tests/test_decision_healthy_replay.py).
2. Triage never authorizes: halted saves stay halted whatever the score, a
   batch needs a person, and approving writes labels only.
3. Identity, date and record-number values never reach a provider.
4. Without calibration, and outside problems_only, every save is reviewed
   one at a time, as before.
"""

from __future__ import annotations

import ast
import json
import math
import stat
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from openadapt_flow.decision import synthetic
from openadapt_flow.decision.calibration import (
    ArtifactMismatch,
    CalibrationArtifact,
    apply_calibrator,
    binom_cdf,
    estimate_miss_rate,
    fit_calibration,
    fit_isotonic,
    fit_platt,
    load_artifact,
    ltt_threshold,
    min_bad_labels,
)
from openadapt_flow.decision.features import (
    IntakeFieldEvidence,
    ParamHistory,
    SaveObservation,
    WorkflowHistory,
    extract_features,
    group_key,
)
from openadapt_flow.decision.labels import (
    ApprovalLabelV1,
    LabelLog,
    label_fact_schema,
    to_judgment_case,
)
from openadapt_flow.decision.minimize import (
    FEATURE_NAMES,
    FieldComparison,
    PhiBoundaryError,
    assert_minimized,
    field_matches,
)
from openadapt_flow.decision.provider import Answer, DecisionRequest
from openadapt_flow.decision.review import ReviewRefused, approve_batch, decide_item
from openadapt_flow.decision.rules import rule_v0
from openadapt_flow.decision.triage import TriageConfig, build_plan, reviewer_view
from openadapt_flow.judgment_cases import LocalEvidenceRefV1

GROUP = group_key("wf", "save")
REPO = Path(__file__).resolve().parents[1]
NAME, DOB, MRN = "Zyxwvut Qopnml", "1961-07-14", "MRN-8840215"


def _history(n: int = 30) -> ParamHistory:
    history = ParamHistory()
    for i in range(n):
        history.observe(
            {"units": str(9 + (i % 3)), "visit_type": "new"},
            {"units": "number", "visit_type": "choice"},
        )
    return history


def _obs(i: int = 0, **overrides) -> SaveObservation:
    base = dict(
        item_id=f"{i:024x}",
        group_key=GROUP,
        outcome="VERIFIED",
        readback="confirmed",
        run_clean=True,
        comparisons=[
            FieldComparison(name="name", kind="identity", expected=NAME, observed=NAME),
            FieldComparison(
                name="dob", kind="date", expected=DOB, observed="07/14/1961"
            ),
            FieldComparison(
                name="mrn", kind="id", expected=MRN, observed="mrn 8840215"
            ),
            FieldComparison(
                name="units", kind="number", expected="10", observed="10.0"
            ),
        ],
        params={
            "name": NAME,
            "dob": DOB,
            "mrn": MRN,
            "units": "10",
            "visit_type": "new",
        },
        param_kinds={"units": "number", "visit_type": "choice"},
        intake=[IntakeFieldEvidence(name="mrn", source="ocr", confidence=0.97)],
        history=_history(),
        workflow=WorkflowHistory(recent_items=20, recent_declines=0),
    )
    base.update(overrides)
    return SaveObservation(**base)


def _approver() -> dict:
    return dict(decided_by="human", approver_id=None, auth_method="none")


# -- minimize (invariant 3) -------------------------------------------------------


def test_match_booleans_normalise_dates_ids_and_numbers():
    assert field_matches(
        FieldComparison(name="d", kind="date", expected=DOB, observed="Jul 14, 1961")
    )
    assert field_matches(
        FieldComparison(name="i", kind="id", expected=MRN, observed="mrn-8840215")
    )
    assert field_matches(
        FieldComparison(name="n", kind="number", expected="10", observed="10.00")
    )
    assert not field_matches(
        FieldComparison(name="d", kind="date", expected=DOB, observed="1961-07-15")
    )
    assert field_matches(FieldComparison(name="x", kind="value", expected="a")) is None


@pytest.mark.parametrize(
    "bad",
    [
        {"readback_confirmed": NAME},
        {"param_numeric_max_abs_z": math.nan},
        {"fields_compared": 1.5},
        {"readback_confirmed": 1},
    ],
)
def test_boundary_refuses_anything_but_typed_features(bad):
    features = dict(extract_features(_obs()).values)
    features.update(bad)
    with pytest.raises(PhiBoundaryError):
        assert_minimized(features)
    with pytest.raises(ValueError):
        DecisionRequest(group_key=GROUP, features=features)


def test_boundary_refuses_an_extra_key():
    features = dict(extract_features(_obs()).values)
    features["patient_name"] = True
    with pytest.raises(PhiBoundaryError):
        assert_minimized(features)


class _SpyProvider:
    provider_id = "test.spy"

    def __init__(self, p_bad: float = 0.0) -> None:
        self.requests: list[DecisionRequest] = []
        self.p_bad = p_bad

    def answer(self, request: DecisionRequest) -> Answer:
        self.requests.append(request)
        return Answer(provider_id=self.provider_id, p_bad=self.p_bad)


def _eligible_artifact() -> CalibrationArtifact:
    saves = synthetic.generate(6000, seed=3)
    labels = _labels_for(saves)
    artifact = fit_calibration(labels, group_key=synthetic.GROUP, window=len(labels))
    assert artifact.eligible
    return artifact


def _labels_for(saves) -> list[ApprovalLabelV1]:
    plan = build_plan([s.observation for s in saves], mode="each_item")
    truth = {s.observation.item_id: s.truth for s in saves}
    return [
        ApprovalLabelV1(
            label_id=f"{n:032x}",
            plan_id=plan.plan_id,
            item_id=item.item_id,
            group_key=item.group_key,
            observation_sha256=item.observation_sha256,
            decision="approved" if truth[item.item_id] == "approved" else "declined",
            decline_reason=None
            if truth[item.item_id] == "approved"
            else truth[item.item_id],
            bucket=item.bucket,
            route="review_item",
            inclusion_probability=1.0,
            features=item.features,
            approver_kind="unauthenticated",
            auth_method="none",
            recorded_at=f"2026-01-01T00:00:{n % 60:02d}+00:00",
        )
        for n, item in enumerate(plan.per_item)
    ]


@pytest.fixture(scope="module")
def artifact() -> CalibrationArtifact:
    return _eligible_artifact()


def test_identity_values_never_reach_a_provider(artifact):
    obs = [_obs(i, group_key=synthetic.GROUP) for i in range(5)]
    spy = _SpyProvider()
    build_plan(
        obs,
        mode="problems_only",
        config=TriageConfig(
            policy="learned", pinned_artifact_sha256=artifact.artifact_sha256
        ),
        artifacts={synthetic.GROUP: artifact},
        provider=spy,
    )
    assert len(spy.requests) == 5
    sent = "\n".join(r.model_dump_json() for r in spy.requests)
    for value in (NAME, DOB, MRN, "8840215", "1961", "zyxwvut", "07/14"):
        assert value.lower() not in sent.lower()
    for request in spy.requests:
        assert set(request.features) == set(FEATURE_NAMES)
        assert request.phi_boundary == "minimized_features_only"
        assert all(not isinstance(v, str) for v in request.features.values())


# -- features and the v0 rule ------------------------------------------------------


def test_clean_save_passes_rule_v0():
    feats = extract_features(_obs())
    assert feats.values["all_fields_exact"] is True
    assert rule_v0(feats.values) == ()


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (dict(readback="none"), "readback_not_confirmed"),
        (dict(run_clean=False), "run_not_clean"),
        (dict(comparisons=[]), "no_fields_compared"),
        (
            dict(comparisons=[FieldComparison(name="mrn", kind="id", expected=MRN)]),
            "field_unread",
        ),
        (
            dict(
                comparisons=[
                    FieldComparison(
                        name="name", kind="identity", expected=NAME, observed="Other"
                    )
                ]
            ),
            "identity_mismatch",
        ),
        (dict(history=_history(3)), "param_history_short"),
        (dict(params={"units": "40", "visit_type": "new"}), "param_out_of_range"),
        (dict(params={"units": "10", "visit_type": "rare"}), "param_unseen_value"),
        (
            dict(
                intake=[IntakeFieldEvidence(name="mrn", source="ocr", confidence=0.5)]
            ),
            "intake_low_confidence",
        ),
        (
            dict(workflow=WorkflowHistory(recent_items=20, recent_declines=1)),
            "recent_declines",
        ),
    ],
)
def test_each_rule_v0_check_names_its_reason(change, code):
    feats = extract_features(_obs(**change))
    assert code in rule_v0(feats.values)


def test_flags_name_the_field_not_its_value():
    obs = _obs(
        comparisons=[
            FieldComparison(name="mrn", kind="id", expected=MRN, observed="MRN-1")
        ]
    )
    flags = extract_features(obs).flags
    assert ("mrn", "id_mismatch") in flags
    assert MRN not in json.dumps(flags)


# -- invariant 4: unchanged outside problems_only, and when uncalibrated -----------


@pytest.mark.parametrize("mode", ["each_item", "not_gated"])
@pytest.mark.parametrize("policy", ["off", "rule_v0", "learned"])
def test_other_modes_review_every_save_in_order(mode, policy):
    obs = [_obs(i) for i in range(6)]
    plan = build_plan(obs, mode=mode, config=TriageConfig(policy=policy))
    assert plan.policy_applied == "off"
    assert plan.batch == () and plan.batch_sha256 is None
    assert [i.item_id for i in plan.per_item] == [o.item_id for o in obs]
    assert {i.bucket for i in plan.per_item} == {"unscored"}


def test_problems_only_defaults_to_rule_v0():
    plan = build_plan([_obs(i) for i in range(30)], mode="problems_only")
    assert plan.policy_applied == "rule_v0"
    assert plan.batch


def test_learned_without_enough_labels_reviews_every_save():
    labels = _labels_for(synthetic.generate(150, seed=1))
    artifact = fit_calibration(labels, group_key=synthetic.GROUP)
    assert not artifact.eligible and artifact.ineligible_reason == "too_few_labels"
    obs = [_obs(i, group_key=synthetic.GROUP) for i in range(10)]
    spy = _SpyProvider()
    plan = build_plan(
        obs,
        mode="problems_only",
        config=TriageConfig(
            policy="learned", pinned_artifact_sha256=artifact.artifact_sha256
        ),
        artifacts={synthetic.GROUP: artifact},
        provider=spy,
    )
    assert plan.policy_applied == "per_item_fallback"
    assert plan.batch == ()
    assert len(plan.per_item) == 10
    assert spy.requests == []


def test_too_few_bad_labels_is_uncalibrated():
    saves = synthetic.generate(
        400,
        seed=2,
        rates=synthetic.FaultRates(wrong_record_silent=0.0, wrong_values_silent=0.0),
    )
    artifact = fit_calibration(
        _labels_for(saves), group_key=synthetic.GROUP, min_labels=100
    )
    assert artifact.ineligible_reason == "too_few_bad_labels"


def test_learned_refuses_an_unpinned_or_changed_artifact(artifact):
    obs = [_obs(i, group_key=synthetic.GROUP) for i in range(4)]
    unpinned = build_plan(
        obs,
        mode="problems_only",
        config=TriageConfig(policy="learned"),
        artifacts={synthetic.GROUP: artifact},
    )
    assert unpinned.policy_applied == "per_item_fallback"
    assert unpinned.fallback_reason == "artifact_not_pinned"
    edited = artifact.model_copy(update={"t_low": 0.99})
    changed = build_plan(
        obs,
        mode="problems_only",
        config=TriageConfig(
            policy="learned", pinned_artifact_sha256=artifact.artifact_sha256
        ),
        artifacts={synthetic.GROUP: edited},
    )
    assert changed.fallback_reason == "artifact_mismatch" and changed.batch == ()


# -- invariant 2: triage never authorizes ---------------------------------------------


@pytest.mark.parametrize("readback", ["refuted", "indeterminate"])
def test_refuted_and_indeterminate_halt_whatever_the_score(artifact, readback):
    obs = [_obs(i, group_key=synthetic.GROUP) for i in range(4)]
    obs.append(_obs(99, group_key=synthetic.GROUP, readback=readback))
    obs.append(_obs(98, group_key=synthetic.GROUP, outcome="HALTED"))
    spy = _SpyProvider(p_bad=0.0)
    plan = build_plan(
        obs,
        mode="problems_only",
        config=TriageConfig(
            policy="learned",
            audit_rate=0.01,
            pinned_artifact_sha256=artifact.artifact_sha256,
        ),
        artifacts={synthetic.GROUP: artifact},
        provider=spy,
    )
    halted = {i.item_id for i in plan.halted}
    assert halted == {f"{99:024x}", f"{98:024x}"}
    assert not halted & {i.item_id for i in (*plan.batch, *plan.per_item)}
    assert len(spy.requests) == 4  # halted saves are never scored
    with pytest.raises(ReviewRefused) as exc:
        decide_item(
            plan,
            LabelLog(Path("/nonexistent/x")),
            item_id=f"{99:024x}",
            decision="approved",
            **_approver(),
        )
    assert exc.value.code == "halted"


def test_hard_reasons_are_never_cleared_by_a_score(artifact):
    obs = [_obs(i, group_key=synthetic.GROUP, run_clean=False) for i in range(5)]
    plan = build_plan(
        obs,
        mode="problems_only",
        config=TriageConfig(
            policy="learned", pinned_artifact_sha256=artifact.artifact_sha256
        ),
        artifacts={synthetic.GROUP: artifact},
        provider=_SpyProvider(p_bad=0.0),
    )
    assert plan.batch == ()
    assert all("run_not_clean" in i.reasons for i in plan.per_item)


def _plan_with_batch():
    plan = build_plan([_obs(i) for i in range(40)], mode="problems_only")
    assert plan.batch
    return plan


def test_batch_needs_a_person_and_the_exact_batch(tmp_path):
    plan = _plan_with_batch()
    log = LabelLog(tmp_path / "labels.jsonl")
    for kwargs, code in [
        (
            dict(decided_by="automation", approver_id=None, auth_method="none"),
            "not_decided_by_a_person",
        ),
        (dict(**_approver(), batch_sha256="0" * 64), "batch_digest_mismatch"),
        (dict(**_approver(), pull_back=["f" * 24]), "not_in_batch"),
    ]:
        kwargs.setdefault("batch_sha256", plan.batch_sha256)
        with pytest.raises(ReviewRefused) as exc:
            approve_batch(plan, log, **kwargs)
        assert exc.value.code == code
    tampered = plan.model_copy(update={"batch": plan.batch[:-1]})
    with pytest.raises(ReviewRefused) as exc:
        approve_batch(tampered, log, batch_sha256=plan.batch_sha256, **_approver())
    assert exc.value.code == "plan_changed"
    assert log.read() == []


def test_one_action_approves_the_batch_with_pull_back(tmp_path):
    plan = _plan_with_batch()
    log = LabelLog(tmp_path / "labels.jsonl")
    back = plan.batch[0].item_id
    before = sorted(p.name for p in tmp_path.iterdir())
    result = approve_batch(
        plan, log, batch_sha256=plan.batch_sha256, pull_back=[back], **_approver()
    )
    assert back not in result.approved and result.pulled_back == (back,)
    assert len(result.approved) == len(plan.batch) - 1
    # Approving writes the label log and nothing else.
    after = sorted(p.name for p in tmp_path.iterdir())
    assert set(after) - set(before) == {"labels.jsonl"}
    assert stat.S_IMODE((tmp_path / "labels.jsonl").stat().st_mode) == 0o600
    labels = log.read()
    assert {lab.route for lab in labels} == {"review_batch"}
    assert all(lab.decided_by == "human" for lab in labels)
    # The pulled-back save is answered on its own.
    label = decide_item(
        plan,
        log,
        item_id=back,
        decision="declined",
        decline_reason="wrong_values",
        **_approver(),
    )
    assert label.route == "review_item" and label.is_bad()
    with pytest.raises(ReviewRefused):
        approve_batch(plan, log, batch_sha256=plan.batch_sha256, **_approver())


def test_answer_has_no_authorizing_field():
    assert set(Answer.model_fields) == {
        "schema_version",
        "provider_id",
        "p_bad",
        "abstain",
        "abstain_reason",
        "artifact_sha256",
    }


_AUTHORITY_MODULES = (
    "openadapt_flow.approval_mode.item_approval",
    "openadapt_flow.approval_mode.gate",
    "openadapt_flow.approval_mode.switch",
    "openadapt_flow.runtime.authorization",
    "openadapt_flow.runtime.durable",
    "openadapt_flow.runtime.replayer",
    "openadapt_flow.runner",
)


def test_decision_package_imports_no_authority_module():
    for path in (REPO / "openadapt_flow" / "decision").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith(_AUTHORITY_MODULES), (path.name, name)


def test_authority_modules_do_not_load_the_decision_package():
    code = (
        "import sys\n"
        "import openadapt_flow.runtime.replayer, openadapt_flow.approval_mode.gate\n"
        "import openadapt_flow.approval_mode.item_approval\n"
        "import openadapt_flow.runtime.authorization\n"
        "import openadapt_flow.runtime.durable.resume, openadapt_flow.runtime.durable.approval\n"
        "import openadapt_flow.runtime.durable.attended\n"
        "import openadapt_flow.console.attention, openadapt_flow.console.human_decisions\n"
        "import openadapt_flow.__main__\n"
        "print(sorted(m for m in sys.modules if m == 'openadapt_flow.decision' or m.startswith('openadapt_flow.decision.')))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "[]"


# -- audit sample ------------------------------------------------------------------


def test_audit_sample_is_mandatory_and_unmarked():
    with pytest.raises(ValueError):
        TriageConfig(audit_rate=0.0)
    plan = build_plan(
        [_obs(i) for i in range(40)],
        mode="problems_only",
        config=TriageConfig(audit_rate=1.0),
    )
    assert plan.batch == ()
    assert {i.bucket for i in plan.per_item} == {"audit"}
    assert all(i.inclusion_probability == 1.0 for i in plan.per_item)
    view = json.dumps(reviewer_view(plan))
    for marker in ("audit", "bucket", "score", "inclusion"):
        assert marker not in view


def test_audit_rate_samples_about_that_share():
    plan = build_plan(
        [_obs(i) for i in range(2000)],
        mode="problems_only",
        config=TriageConfig(audit_rate=0.1),
    )
    audited = sum(1 for i in plan.per_item if i.bucket == "audit")
    assert 140 <= audited <= 260
    assert len(plan.batch) + audited == 2000


def test_reviewer_view_lists_the_batch_and_reasons():
    obs = [_obs(i) for i in range(30)] + [_obs(77, run_clean=False)]
    view = reviewer_view(build_plan(obs, mode="problems_only"))
    assert (
        view["batch"]["title"] == f"Approve {len(view['batch']['items'])} clean items"
    )
    flagged = [r for r in view["review_each"] if r["item_id"] == f"{77:024x}"]
    assert flagged[0]["reasons"][0]["code"] == "run_not_clean"
    assert flagged[0]["reasons"][0]["text"]


def test_inverse_probability_miss_estimate():
    labels = _labels_for(synthetic.generate(5, seed=4))
    caught = labels[0].model_copy(
        update={
            "bucket": "flagged",
            "decision": "declined",
            "decline_reason": "wrong_values",
        }
    )
    found = labels[1].model_copy(
        update={
            "bucket": "audit",
            "inclusion_probability": 0.1,
            "decision": "declined",
            "decline_reason": "wrong_record",
        }
    )
    clean = labels[2].model_copy(
        update={"bucket": "audit", "inclusion_probability": 0.1}
    )
    estimate = estimate_miss_rate([caught, found, clean])
    assert estimate.estimated_bad_missed == pytest.approx(10.0)
    assert estimate.miss_rate == pytest.approx(10.0 / 11.0)


# -- calibration --------------------------------------------------------------------


def test_bad_labels_needed_for_the_bound():
    assert min_bad_labels(0.05, 0.10) == 45
    assert min_bad_labels(0.005, 0.10) == 460
    assert binom_cdf(0, 45, 0.05) <= 0.10 < binom_cdf(0, 44, 0.05)


def test_ltt_threshold_keeps_the_miss_bound():
    rng = np.random.default_rng(0)
    good = rng.uniform(0.0, 0.5, 2000)
    bad = rng.uniform(0.3, 1.0, 200)
    scores = np.concatenate([good, bad])
    is_bad = np.concatenate([np.zeros(2000, bool), np.ones(200, bool)])
    t = ltt_threshold(scores, is_bad, target_miss=0.05, delta=0.1)
    assert 0.3 <= t < 0.5
    assert (bad < t).mean() <= 0.05
    assert (
        ltt_threshold(scores, np.zeros_like(is_bad), target_miss=0.05, delta=0.1) == 0.0
    )


def test_calibrators_are_monotone():
    rng = np.random.default_rng(1)
    raw = rng.uniform(0, 1, 500)
    y = rng.uniform(0, 1, 500) < raw
    grid = np.linspace(0.01, 0.99, 50)
    for params in (fit_platt(raw, y), fit_isotonic(raw, y)):
        out = apply_calibrator(params, grid)
        assert np.all(np.diff(out) >= -1e-12)
        assert np.all((out >= 0) & (out <= 1))


def test_artifact_is_pinned(tmp_path, artifact):
    path = tmp_path / "artifact.json"
    path.write_text(artifact.model_dump_json())
    assert load_artifact(path, pinned_sha256=artifact.artifact_sha256) == artifact
    with pytest.raises(ArtifactMismatch):
        load_artifact(path, pinned_sha256="1" * 64)
    data = json.loads(path.read_text())
    data["t_low"] = 0.9
    path.write_text(json.dumps(data))
    with pytest.raises(ArtifactMismatch):
        load_artifact(path, pinned_sha256=artifact.artifact_sha256)


def test_eligible_artifact_scores_and_stays_calibrated(artifact):
    assert artifact.calibrator is not None and artifact.model is not None
    assert 0.0 < artifact.t_low <= artifact.t_high
    assert artifact.oof_ece is not None and artifact.oof_ece < 0.05


# -- labels --------------------------------------------------------------------------


def test_labels_round_trip_and_convert_to_judgment_cases(tmp_path):
    log = LabelLog(tmp_path / "l.jsonl")
    labels = _labels_for(synthetic.generate(20, seed=5))
    for label in labels:
        log.append(label)
    assert log.read() == labels
    case = to_judgment_case(
        labels[0],
        workflow_contract_sha256="a" * 64,
        decision_contract_sha256="b" * 64,
        evidence=LocalEvidenceRefV1(
            relative_path="report.json", sha256="c" * 64, kind="report"
        ),
        reviewer_principal_ref_sha256="d" * 64,
    )
    assert case.fact_schema_sha256 == label_fact_schema().contract_sha256()
    assert case.disposition.value == "human_node"


def test_label_rules():
    label = _labels_for(synthetic.generate(1, seed=6))[0]
    with pytest.raises(ValueError):
        ApprovalLabelV1.model_validate(
            {
                **label.model_dump(),
                "decision": "approved",
                "decline_reason": "wrong_values",
            }
        )
    with pytest.raises(ValueError):
        ApprovalLabelV1.model_validate({**label.model_dump(), "route": "review_batch"})
    with pytest.raises(ValueError):
        ApprovalLabelV1.model_validate(
            {**label.model_dump(), "decided_by": "automation"}
        )


# -- CLI -------------------------------------------------------------------------------


def test_cli_plan_approve_and_decide(tmp_path, capsys):
    from openadapt_flow.__main__ import main

    obs_path = tmp_path / "obs.jsonl"
    obs_path.write_text(
        "\n".join(o.model_dump_json() for o in [_obs(i) for i in range(30)]) + "\n"
    )
    plan_path = tmp_path / "plan.json"
    assert (
        main(
            [
                "triage",
                "plan",
                "--observations",
                str(obs_path),
                "--mode",
                "problems_only",
                "--out",
                str(plan_path),
            ]
        )
        == 0
    )
    view = json.loads(capsys.readouterr().out)
    assert NAME not in json.dumps(view)
    labels = tmp_path / "labels.jsonl"
    assert (
        main(
            [
                "triage",
                "approve-batch",
                str(plan_path),
                "--batch-sha256",
                view["batch"]["batch_sha256"],
                "--labels",
                str(labels),
                "--decided-by",
                "human",
                "--unauthenticated",
            ]
        )
        == 0
    )
    capsys.readouterr()
    item = view["review_each"][0]["item_id"] if view["review_each"] else None
    if item:
        assert (
            main(
                [
                    "triage",
                    "decide",
                    str(plan_path),
                    "--item",
                    item,
                    "--decline",
                    "wrong_record",
                    "--labels",
                    str(labels),
                    "--decided-by",
                    "human",
                    "--approver-id",
                    "user_1",
                    "--auth-method",
                    "cloud_aal2",
                ]
            )
            == 0
        )
    assert (
        main(
            [
                "triage",
                "approve-batch",
                str(plan_path),
                "--batch-sha256",
                view["batch"]["batch_sha256"],
                "--labels",
                str(labels),
                "--decided-by",
                "automation",
                "--unauthenticated",
            ]
        )
        == 2
    )
