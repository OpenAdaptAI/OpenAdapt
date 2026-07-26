"""Governed repair promotion lifecycle (roadmap Section 9).

Covers the full happy path (candidate -> reviewed -> replay campaign -> fault
campaign -> approved -> staged -> canary -> active), every gate's refusal
(failed fault campaign, weakened identity contract, unapproved promotion,
canary regression -> automatic revert), one-command rollback restoring the
prior hash, and the hard rule that a model suggestion can never actuate a
repair or promote itself.

Everything is deterministic and hermetic: bundles are tiny synthetic
workflows, campaign frames are PIL-drawn, and the resolver / band sampler are
pixel-scanning fakes wired through the same injection seams production uses.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from openadapt_flow.ir import ActionKind, Anchor, Step, Workflow
from openadapt_flow.qualification import EnvironmentBoundary, init_project
from openadapt_flow.repair import (
    Actor,
    CanaryRunObservation,
    ModelActuationError,
    RepairInvariantError,
    RepairStore,
    enforce_contract_invariants,
    register_bundle_candidate,
    run_fault_campaign,
    run_replay_campaign,
)
from openadapt_flow.repair.campaign import merge_campaign_results
from openadapt_flow.repair.lifecycle import RepairLifecycleError
from openadapt_flow.repair.registration import (
    build_candidate,
    detached_candidate_path,
    load_detached_candidate,
)
from openadapt_flow.verification import VerificationTier

# --------------------------------------------------------------------------- #
# Synthetic geometry: a red target block with a green identity-band stripe.
# --------------------------------------------------------------------------- #

VIEWPORT = (300, 200)
BLOCK = (135, 90, 30, 20)  # x, y, w, h
CLICK = (150, 100)
ARMED_BAND = "Jane Sample DOB 1980-01-01"
WRONG_BAND = "WRONG ENTITY 1999-12-31"

RED = (200, 30, 30)
GREEN = (0, 180, 0)

HUMAN = Actor(kind="human", id="dr-reviewer")
AUTOMATION = Actor(kind="automation", id="ci")
MODEL = Actor(kind="model", id="runtime-vlm")


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_frame() -> bytes:
    """White frame, red target block, green band stripe left of the target."""
    image = Image.new("RGB", VIEWPORT, (255, 255, 255))
    x, y, w, h = BLOCK
    for px in range(x, x + w):
        for py in range(y, y + h):
            image.putpixel((px, py), RED)
    for px in range(5, 60):
        for py in range(95, 106):
            image.putpixel((px, py), GREEN)
    return _png(image)


def _is_marker(pixel: tuple[int, int, int]) -> bool:
    r, g, b = pixel
    return (r > 150 and g < 80 and b < 80) or (r < 100 and g > 170 and b > 170)


def fake_resolve(frame_png: bytes):
    """Pixel-scanning stand-in for the resolution ladder.

    Finds the red block (or its re-themed inverse). Raises on an ambiguous
    frame (two well-separated columns of hits, like the real ladder's
    ambiguity refusal); picks the largest vertical cluster otherwise (a
    reflow moves most of the block; a template matcher follows it).
    """
    image = Image.open(io.BytesIO(frame_png)).convert("RGB")
    xs: list[int] = []
    ys: list[int] = []
    for py in range(image.height):
        for px in range(image.width):
            if _is_marker(image.getpixel((px, py))):
                xs.append(px)
                ys.append(py)
    if not xs:
        return None
    if max(xs) - min(xs) > 2 * BLOCK[2]:
        raise RuntimeError("ambiguous: two indistinguishable candidates")
    # Largest contiguous row cluster (gap > 3 rows splits clusters).
    rows = sorted(set(ys))
    clusters: list[list[int]] = [[rows[0]]]
    for row in rows[1:]:
        if row - clusters[-1][-1] > 3:
            clusters.append([])
        clusters[-1].append(row)
    best = max(clusters, key=len)
    keep = set(best)
    cx = [px for px, py in zip(xs, ys) if py in keep]
    cy = [py for py in ys if py in keep]
    return (sum(cx) // len(cx), sum(cy) // len(cy))


def fake_sample_band(frame_png: bytes, point):
    """Read the synthetic identity band near ``point``.

    Green stripe (or its re-themed inverse) -> the armed band text; a gray
    wash -> unreadable (None); anything else -> a different entity's band.
    """
    image = Image.open(io.BytesIO(frame_png)).convert("RGB")
    saw_gray = False
    y0 = max(0, point[1] - 8)
    y1 = min(image.height, point[1] + 9)
    for py in range(y0, y1):
        for px in range(0, min(80, image.width)):
            r, g, b = image.getpixel((px, py))
            if (g > 150 and r < 100 and b < 100) or (r > 200 and g < 120 and b > 200):
                return ARMED_BAND
            if 120 <= r <= 136 and r == g == b:
                saw_gray = True
    return None if saw_gray else WRONG_BAND


def simple_verifier(recorded: str, observed: str) -> str:
    return "verified" if observed == recorded else "mismatch"


# --------------------------------------------------------------------------- #
# Bundle + evidence fixtures
# --------------------------------------------------------------------------- #


def _block_crop_png(color=RED) -> bytes:
    image = Image.new("RGB", (BLOCK[2], BLOCK[3]), color)
    return _png(image)


def make_bundle(
    path: Path,
    *,
    ocr_text: str = "Submit",
    context_text: str | None = ARMED_BAND,
    risk: str = "reversible",
    crop_color=RED,
) -> Path:
    anchor = Anchor(
        template="templates/target.png",
        region=BLOCK,
        click_point=CLICK,
        ocr_text=ocr_text,
        context_text=context_text,
    )
    workflow = Workflow(
        name="repair-demo",
        viewport=VIEWPORT,
        steps=[
            Step(
                id="step-1",
                intent="Submit the encounter",
                action=ActionKind.CLICK,
                anchor=anchor,
                risk=risk,
            )
        ],
    )
    path.mkdir(parents=True, exist_ok=True)
    (path / "templates").mkdir(exist_ok=True)
    (path / "templates" / "target.png").write_bytes(_block_crop_png(crop_color))
    workflow.save(path)
    return path


def make_evidence_run_dir(path: Path) -> Path:
    heal_dir = path / "heals" / "step-1"
    heal_dir.mkdir(parents=True, exist_ok=True)
    (heal_dir / "screen.png").write_bytes(make_frame())
    (heal_dir / "patch.json").write_text(
        json.dumps({"step_id": "step-1", "rung_used": "ocr"})
    )
    return path


@pytest.fixture()
def bundles(tmp_path: Path) -> tuple[Path, Path, Path]:
    prior = make_bundle(tmp_path / "prior")
    proposed = make_bundle(
        tmp_path / "proposed",
        ocr_text="Submit Encounter",  # the repaired locator evidence
        crop_color=(201, 31, 31),  # refreshed crop bytes -> new digest
    )
    run_dir = make_evidence_run_dir(tmp_path / "run")
    return prior, proposed, run_dir


def registered(
    store: RepairStore, prior: Path, proposed: Path, run_dir: Path, source="heal"
):
    register_bundle_candidate(prior, proposed, source=source, evidence_run_dir=run_dir)
    return store.add_candidate(load_detached_candidate(proposed))


def _run_campaigns(store: RepairStore, candidate, proposed: Path):
    frame = make_frame()
    anchor = Workflow.load(proposed).steps[0].anchor
    assert anchor is not None
    replay = run_replay_campaign(
        "step-1",
        anchor,
        frame,
        resolve=fake_resolve,
        sample_band=fake_sample_band,
        band_verifier=simple_verifier,
    )
    candidate = store.record_campaign(candidate.candidate_id, replay, AUTOMATION)
    fault = run_fault_campaign(
        "step-1",
        anchor,
        frame,
        resolve=fake_resolve,
        sample_band=fake_sample_band,
        band_verifier=simple_verifier,
    )
    return store.record_campaign(candidate.candidate_id, fault, AUTOMATION)


# --------------------------------------------------------------------------- #
# Campaigns
# --------------------------------------------------------------------------- #


def test_replay_campaign_passes_on_healthy_battery(bundles):
    prior, proposed, run_dir = bundles
    anchor = Workflow.load(proposed).steps[0].anchor
    result = run_replay_campaign(
        "step-1",
        anchor,
        make_frame(),
        resolve=fake_resolve,
        sample_band=fake_sample_band,
        band_verifier=simple_verifier,
    )
    assert result.passed, [c.model_dump() for c in result.cases if not c.passed]
    labels = {case.label for case in result.cases}
    assert "step-1:baseline" in labels
    assert len(result.cases) == 5  # baseline + shift/scale/retheme/reflow


def test_fault_campaign_requires_refusal_on_every_adversarial_case(bundles):
    prior, proposed, run_dir = bundles
    anchor = Workflow.load(proposed).steps[0].anchor
    result = run_fault_campaign(
        "step-1",
        anchor,
        make_frame(),
        resolve=fake_resolve,
        sample_band=fake_sample_band,
        band_verifier=simple_verifier,
    )
    assert result.passed, [c.model_dump() for c in result.cases if not c.passed]
    kinds = {case.kind for case in result.cases}
    assert kinds == {
        "ambiguity",
        "wrong_entity",
        "stale_target",
        "unexpected_dialog",
        "verifier_failure",
    }


def test_fault_campaign_fails_when_binding_acts_on_wrong_entity(bundles):
    """A binding whose identity check would pass on a wrong-entity frame is a
    silent wrong action; the campaign must fail it."""
    prior, proposed, run_dir = bundles
    anchor = Workflow.load(proposed).steps[0].anchor
    result = run_fault_campaign(
        "step-1",
        anchor,
        make_frame(),
        resolve=fake_resolve,
        sample_band=lambda frame, point: ARMED_BAND,  # blind sampler
        band_verifier=simple_verifier,
    )
    assert not result.passed
    failed_kinds = {case.kind for case in result.cases if not case.passed}
    assert "wrong_entity" in failed_kinds


def test_fault_campaign_fails_unarmed_binding_that_acts(bundles):
    """No identity band at all: acting on any adversarial frame must fail."""
    prior, proposed, run_dir = bundles
    anchor = (
        Workflow.load(proposed)
        .steps[0]
        .anchor.model_copy(update={"context_text": None})
    )
    result = run_fault_campaign(
        "step-1",
        anchor,
        make_frame(),
        resolve=fake_resolve,
        sample_band=fake_sample_band,
        band_verifier=simple_verifier,
    )
    assert not result.passed


# --------------------------------------------------------------------------- #
# Registration + candidate record
# --------------------------------------------------------------------------- #


def test_registration_writes_privacy_safe_detached_candidate(bundles):
    prior, proposed, run_dir = bundles
    path = register_bundle_candidate(
        prior, proposed, source="heal", evidence_run_dir=run_dir
    )
    assert path == detached_candidate_path(proposed)
    candidate = load_detached_candidate(proposed)
    assert candidate.state == "candidate"
    assert candidate.prior_content_digest != candidate.proposed_content_digest
    assert any(change.field == "ocr_text" for change in candidate.binding_changes)
    assert candidate.failure_fingerprints, "heal evidence yields fingerprints"
    assert candidate.failure_fingerprints[0].failure_class == "anchor_drift:ocr"
    # Privacy: no raw observation (band text, OCR label) in the record.
    raw = path.read_text()
    assert ARMED_BAND not in raw
    assert "Submit Encounter" not in raw


def test_candidate_never_auto_activates(bundles, tmp_path):
    prior, proposed, run_dir = bundles
    store = RepairStore(tmp_path / "store")
    candidate = registered(store, prior, proposed, run_dir)
    assert candidate.state == "candidate"
    assert store.active_pointer() is None


# --------------------------------------------------------------------------- #
# Invariants (fail closed)
# --------------------------------------------------------------------------- #


def test_weakened_identity_contract_is_hard_refused(bundles, tmp_path):
    prior, _proposed, run_dir = bundles
    weakened = make_bundle(
        tmp_path / "weakened", context_text=None, crop_color=(199, 29, 29)
    )
    candidate = build_candidate(prior, weakened, source="heal")
    assert candidate.state == "rejected"
    assert "identity" in (candidate.rejection_reason or "")
    # A rejected candidate can never advance.
    store = RepairStore(tmp_path / "store")
    store.add_candidate(candidate)
    with pytest.raises(RepairLifecycleError):
        store.review(candidate.candidate_id, HUMAN)


def test_weakened_risk_contract_is_hard_refused(bundles, tmp_path):
    prior_wf = Workflow.load(make_bundle(tmp_path / "p2", risk="irreversible"))
    weakened_wf = Workflow.load(make_bundle(tmp_path / "q2", risk="reversible"))
    with pytest.raises(RepairInvariantError) as excinfo:
        enforce_contract_invariants(prior_wf, weakened_wf)
    assert "risk" in str(excinfo.value)
    assert "qualification revision" in str(excinfo.value)


def test_removed_step_is_hard_refused(tmp_path):
    prior_wf = Workflow.load(make_bundle(tmp_path / "p3"))
    proposed_wf = Workflow.load(make_bundle(tmp_path / "q3"))
    proposed_wf.steps = []
    with pytest.raises(RepairInvariantError):
        enforce_contract_invariants(prior_wf, proposed_wf)


def _with_qualification(workflow: Workflow) -> Workflow:
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="web",
            application="TestApp",
            application_version="1.0",
            environment_digest="a" * 64,
            runtime_version="1.0",
        ),
    )
    return workflow


def test_weakened_effect_tier_refused_without_new_revision(tmp_path):
    prior_wf = _with_qualification(Workflow.load(make_bundle(tmp_path / "p4")))
    proposed_wf = _with_qualification(Workflow.load(make_bundle(tmp_path / "q4")))
    # Silent weakening: minimum tier relaxed WITHOUT touching the revision.
    assert proposed_wf.qualification is not None
    proposed_wf.qualification.minimum_effect_tier = VerificationTier.IMMEDIATE_SCREEN
    with pytest.raises(RepairInvariantError) as excinfo:
        enforce_contract_invariants(prior_wf, proposed_wf)
    assert "minimum effect tier weakened" in str(excinfo.value)


def test_weakening_admitted_only_with_explicit_new_revision(tmp_path):
    from openadapt_flow.qualification import set_minimum_effect_tier

    prior_wf = _with_qualification(Workflow.load(make_bundle(tmp_path / "p5")))
    proposed_wf = _with_qualification(Workflow.load(make_bundle(tmp_path / "q5")))
    # The governed path: the tier change goes through the qualification API,
    # which advances the revision and chains the digest.
    set_minimum_effect_tier(proposed_wf, VerificationTier.IMMEDIATE_SCREEN)
    weakenings = enforce_contract_invariants(prior_wf, proposed_wf)
    assert weakenings and weakenings[0].dimension == "effect"


# --------------------------------------------------------------------------- #
# Lifecycle: gates refuse, happy path promotes
# --------------------------------------------------------------------------- #


def test_unapproved_promotion_is_refused(bundles, tmp_path):
    prior, proposed, run_dir = bundles
    store = RepairStore(tmp_path / "store")
    candidate = registered(store, prior, proposed, run_dir)
    # Approval without review/campaigns refuses.
    with pytest.raises(RepairLifecycleError):
        store.approve(candidate.candidate_id, HUMAN)
    # Staging and canary without approval refuse.
    with pytest.raises(RepairLifecycleError):
        store.stage(candidate.candidate_id, AUTOMATION)
    with pytest.raises(RepairLifecycleError):
        store.start_canary(candidate.candidate_id, AUTOMATION)


def test_failed_fault_campaign_blocks_approval(bundles, tmp_path):
    prior, proposed, run_dir = bundles
    store = RepairStore(tmp_path / "store")
    candidate = registered(store, prior, proposed, run_dir)
    store.review(candidate.candidate_id, HUMAN)
    frame = make_frame()
    anchor = Workflow.load(proposed).steps[0].anchor
    replay = run_replay_campaign(
        "step-1",
        anchor,
        frame,
        resolve=fake_resolve,
        sample_band=fake_sample_band,
        band_verifier=simple_verifier,
    )
    store.record_campaign(candidate.candidate_id, replay, AUTOMATION)
    # A blind band sampler makes the wrong-entity case a silent wrong action.
    fault = run_fault_campaign(
        "step-1",
        anchor,
        frame,
        resolve=fake_resolve,
        sample_band=lambda f, p: ARMED_BAND,
        band_verifier=simple_verifier,
    )
    candidate = store.record_campaign(candidate.candidate_id, fault, AUTOMATION)
    assert candidate.state == "replay_passed"  # did NOT advance
    with pytest.raises(RepairLifecycleError) as excinfo:
        store.approve(candidate.candidate_id, HUMAN)
    assert "gate sequence" in str(excinfo.value)


def test_full_lifecycle_happy_path(bundles, tmp_path):
    prior, proposed, run_dir = bundles
    store = RepairStore(tmp_path / "store")
    candidate = registered(store, prior, proposed, run_dir)

    candidate = store.review(candidate.candidate_id, HUMAN)
    assert candidate.state == "reviewed"

    candidate = _run_campaigns(store, candidate, proposed)
    assert candidate.state == "fault_passed"
    assert candidate.campaigns_passed()

    candidate = store.approve(candidate.candidate_id, HUMAN, non_interactive=True)
    assert candidate.state == "approved"
    assert candidate.approval is not None
    assert candidate.approval.approved_by == HUMAN.id
    assert (
        candidate.approval.proposed_content_digest == candidate.proposed_content_digest
    )

    candidate = store.stage(candidate.candidate_id, AUTOMATION)
    assert candidate.state == "staged"
    # BOTH bundles staged by exact hash (prior too, for rollback).
    assert store.bundle_path(candidate.proposed_content_digest).is_dir()
    assert store.bundle_path(candidate.prior_content_digest).is_dir()

    candidate = store.start_canary(candidate.candidate_id, AUTOMATION, max_runs=2)
    assert candidate.state == "canary"
    pointer = store.active_pointer()
    assert pointer is not None
    assert pointer.mode == "canary"
    assert pointer.active_digest == candidate.proposed_content_digest
    assert pointer.prior_digest == candidate.prior_content_digest

    for run_number in range(2):
        candidate = store.record_canary_run(
            candidate.candidate_id,
            CanaryRunObservation(
                run_id=f"run-{run_number}", verified=True, silent_incorrect=False
            ),
        )
    assert candidate.state == "active"
    pointer = store.active_pointer()
    assert pointer is not None
    assert pointer.mode == "active"
    assert pointer.active_digest == candidate.proposed_content_digest
    # Full lineage retained in the audit history.
    states = [record.to_state for record in candidate.history]
    assert states == [
        "reviewed",
        "replay_passed",
        "fault_passed",
        "approved",
        "staged",
        "canary",
        "active",
    ]


def _promote_to_canary(store, prior, proposed, run_dir, max_runs=3):
    candidate = registered(store, prior, proposed, run_dir)
    store.review(candidate.candidate_id, HUMAN)
    candidate = _run_campaigns(store, candidate, proposed)
    store.approve(candidate.candidate_id, HUMAN, non_interactive=True)
    store.stage(candidate.candidate_id, AUTOMATION)
    return store.start_canary(candidate.candidate_id, AUTOMATION, max_runs=max_runs)


def test_canary_regression_auto_reverts_to_prior(bundles, tmp_path):
    prior, proposed, run_dir = bundles
    store = RepairStore(tmp_path / "store")
    candidate = _promote_to_canary(store, prior, proposed, run_dir)
    candidate = store.record_canary_run(
        candidate.candidate_id,
        CanaryRunObservation(run_id="run-0", verified=True, silent_incorrect=False),
    )
    assert candidate.state == "canary"
    # A silent-incorrect run halts the canary IMMEDIATELY.
    candidate = store.record_canary_run(
        candidate.candidate_id,
        CanaryRunObservation(
            run_id="run-1",
            verified=True,
            silent_incorrect=True,
            detail="conflicting write observed",
        ),
    )
    assert candidate.state == "staged"
    assert candidate.canary_metrics.halted
    assert "silent-incorrect" in (candidate.canary_metrics.halt_reason or "")
    pointer = store.active_pointer()
    assert pointer is not None
    assert pointer.active_digest == candidate.prior_content_digest


def test_canary_verification_regression_also_reverts(bundles, tmp_path):
    prior, proposed, run_dir = bundles
    store = RepairStore(tmp_path / "store")
    candidate = _promote_to_canary(store, prior, proposed, run_dir)
    candidate = store.record_canary_run(
        candidate.candidate_id,
        CanaryRunObservation(run_id="run-0", verified=False, silent_incorrect=False),
    )
    assert candidate.state == "staged"
    pointer = store.active_pointer()
    assert pointer is not None
    assert pointer.active_digest == candidate.prior_content_digest


def test_rollback_restores_prior_hash(bundles, tmp_path):
    prior, proposed, run_dir = bundles
    store = RepairStore(tmp_path / "store")
    candidate = _promote_to_canary(store, prior, proposed, run_dir, max_runs=1)
    candidate = store.record_canary_run(
        candidate.candidate_id,
        CanaryRunObservation(run_id="run-0", verified=True, silent_incorrect=False),
    )
    assert candidate.state == "active"
    candidate = store.rollback(HUMAN)
    assert candidate.state == "rolled_back"
    pointer = store.active_pointer()
    assert pointer is not None
    assert pointer.active_digest == candidate.prior_content_digest
    assert pointer.mode == "active"


def test_activation_refuses_tampered_staged_bundle(bundles, tmp_path):
    prior, proposed, run_dir = bundles
    store = RepairStore(tmp_path / "store")
    candidate = registered(store, prior, proposed, run_dir)
    store.review(candidate.candidate_id, HUMAN)
    candidate = _run_campaigns(store, candidate, proposed)
    store.approve(candidate.candidate_id, HUMAN, non_interactive=True)
    candidate = store.stage(candidate.candidate_id, AUTOMATION)
    # Tamper with the staged copy AFTER staging.
    staged = store.bundle_path(candidate.proposed_content_digest)
    (staged / "templates" / "target.png").write_bytes(_block_crop_png((1, 2, 3)))
    with pytest.raises((RepairLifecycleError, Exception)):
        store.start_canary(candidate.candidate_id, AUTOMATION)
    assert store.active_pointer() is None


def test_approval_refuses_when_bundle_changed_after_registration(bundles, tmp_path):
    prior, proposed, run_dir = bundles
    store = RepairStore(tmp_path / "store")
    candidate = registered(store, prior, proposed, run_dir)
    store.review(candidate.candidate_id, HUMAN)
    candidate = _run_campaigns(store, candidate, proposed)
    # The proposed bundle mutates on disk between campaign and approval.
    workflow = Workflow.load(proposed)
    workflow.steps[0].intent = "Something else entirely"
    workflow.save(proposed)
    with pytest.raises(RepairLifecycleError) as excinfo:
        store.approve(candidate.candidate_id, HUMAN, non_interactive=True)
    assert "content digest changed" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Hard rule: a model suggestion never actuates or promotes itself
# --------------------------------------------------------------------------- #


def test_model_actor_cannot_perform_any_transition(bundles, tmp_path):
    prior, proposed, run_dir = bundles
    store = RepairStore(tmp_path / "store")
    candidate = registered(store, prior, proposed, run_dir, source="model_suggestion")
    for operation in (
        lambda: store.review(candidate.candidate_id, MODEL),
        lambda: store.approve(candidate.candidate_id, MODEL),
        lambda: store.stage(candidate.candidate_id, MODEL),
        lambda: store.start_canary(candidate.candidate_id, MODEL),
        lambda: store.rollback(MODEL, candidate_id=candidate.candidate_id),
        lambda: store.reject(candidate.candidate_id, MODEL, "nope"),
    ):
        with pytest.raises(ModelActuationError):
            operation()
    # Nothing moved, nothing activated.
    assert store.load_candidate(candidate.candidate_id).state == "candidate"
    assert store.active_pointer() is None


def test_automation_cannot_review_or_approve(bundles, tmp_path):
    prior, proposed, run_dir = bundles
    store = RepairStore(tmp_path / "store")
    candidate = registered(store, prior, proposed, run_dir)
    with pytest.raises(RepairLifecycleError):
        store.review(candidate.candidate_id, AUTOMATION)
    store.review(candidate.candidate_id, HUMAN)
    candidate = _run_campaigns(store, candidate, proposed)
    with pytest.raises(RepairLifecycleError):
        store.approve(candidate.candidate_id, AUTOMATION)


def test_model_suggestion_source_still_requires_full_human_gate(bundles, tmp_path):
    """A model-sourced candidate flows through the SAME human-gated path; it
    can never skip a state or self-promote."""
    prior, proposed, run_dir = bundles
    store = RepairStore(tmp_path / "store")
    candidate = registered(store, prior, proposed, run_dir, source="model_suggestion")
    # No transition can jump the queue.
    with pytest.raises(RepairLifecycleError):
        store.approve(candidate.candidate_id, HUMAN)
    with pytest.raises(RepairLifecycleError):
        store.start_canary(candidate.candidate_id, HUMAN)
    assert store.active_pointer() is None


# --------------------------------------------------------------------------- #
# Canary observation derivation + campaign merge
# --------------------------------------------------------------------------- #


def test_observation_from_report_detects_silent_incorrect():
    from openadapt_flow.ir import RunReport
    from openadapt_flow.repair import observation_from_report

    verified = RunReport(
        workflow_name="w", started_at="t", execution_outcome="VERIFIED"
    )
    observation = observation_from_report(verified)
    assert observation.verified and not observation.silent_incorrect

    silent = RunReport(
        workflow_name="w",
        started_at="t",
        execution_outcome="COMPLETED_UNVERIFIED",
        transaction_outcome="COMPLETED_UNVERIFIED",
    )
    observation = observation_from_report(silent)
    assert not observation.verified
    assert observation.silent_incorrect


def test_merge_campaign_results_refuses_mixed_kinds(bundles):
    prior, proposed, run_dir = bundles
    anchor = Workflow.load(proposed).steps[0].anchor
    frame = make_frame()
    replay = run_replay_campaign(
        "step-1",
        anchor,
        frame,
        resolve=fake_resolve,
        sample_band=fake_sample_band,
        band_verifier=simple_verifier,
    )
    fault = run_fault_campaign(
        "step-1",
        anchor,
        frame,
        resolve=fake_resolve,
        sample_band=fake_sample_band,
        band_verifier=simple_verifier,
    )
    with pytest.raises(ValueError):
        merge_campaign_results([replay, fault])
    merged = merge_campaign_results([replay, replay])
    assert merged.total == 2 * replay.total


# --------------------------------------------------------------------------- #
# Replayer integration: a healed bundle is registered, never auto-activated
# --------------------------------------------------------------------------- #


def test_replayer_heal_registers_detached_candidate(tmp_path):
    """The heal path's `save_healed_to` bundle carries a lifecycle candidate:
    the exact spot a repair used to become implicitly usable now emits a
    governed, never-auto-activating candidate record."""
    from openadapt_flow.runtime.replayer import Replayer
    from tests.test_heal import (
        FakeBackend,
        _drifted_vision,
        ocr_anchored_step,
    )

    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "templates").mkdir(parents=True)
    workflow = Workflow(name="wf", steps=[ocr_anchored_step()])
    workflow.save(bundle_dir)  # prior bundle is loadable for registration

    healed_dir = tmp_path / "healed"
    run_dir = tmp_path / "run"
    report = Replayer(FakeBackend(), vision=_drifted_vision()).run(
        Workflow.load(bundle_dir),
        bundle_dir=bundle_dir,
        run_dir=run_dir,
        save_healed_to=healed_dir,
    )
    assert report.success is True
    assert report.heal_count == 1

    candidate = load_detached_candidate(healed_dir)
    assert candidate.state == "candidate"
    assert candidate.source == "heal"
    assert candidate.prior_content_digest != candidate.proposed_content_digest
    changed_fields = {change.field for change in candidate.binding_changes}
    assert "click_point" in changed_fields
    assert candidate.failure_fingerprints
    # The heal evidence is referenced by path + hash, never embedded.
    assert any(
        ref.relative_path == "heals/s1/patch.json" for ref in candidate.failure_evidence
    )


# --------------------------------------------------------------------------- #
# CLI wiring smoke test
# --------------------------------------------------------------------------- #


def test_cli_repair_register_and_status(bundles, tmp_path, capsys):
    from openadapt_flow.__main__ import build_parser

    prior, proposed, run_dir = bundles
    store_dir = tmp_path / "cli-store"
    parser = build_parser()
    args = parser.parse_args(
        [
            "repair",
            "register",
            str(proposed),
            "--prior",
            str(prior),
            "--source",
            "heal",
            "--evidence",
            str(run_dir),
            "--store",
            str(store_dir),
        ]
    )
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert "recorded in" in out

    args = parser.parse_args(["repair", "list", "--store", str(store_dir)])
    assert args.func(args) == 0
    assert "candidate" in capsys.readouterr().out

    args = parser.parse_args(["repair", "status", "--store", str(store_dir)])
    assert args.func(args) == 0
    assert "no active repair pointer" in capsys.readouterr().out


def test_cli_non_interactive_approve_requires_approved_by(bundles, tmp_path, capsys):
    from openadapt_flow.__main__ import build_parser

    prior, proposed, run_dir = bundles
    store = RepairStore(tmp_path / "store")
    candidate = registered(store, prior, proposed, run_dir)
    parser = build_parser()
    args = parser.parse_args(
        [
            "repair",
            "approve",
            candidate.candidate_id,
            "--non-interactive",
            "--store",
            str(store.root),
        ]
    )
    assert args.func(args) == 1
    assert "--approved-by" in capsys.readouterr().out
