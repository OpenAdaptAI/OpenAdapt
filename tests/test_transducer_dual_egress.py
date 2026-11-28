from openadapt.transducer_dual_egress import (
    DualStageEgressVerifier,
    EgressDecision,
    ItemSphereVote,
)

PROMPT = "email the Q3 budget summary to Dana"
BOXES = {
    "recipient_hint": "Dana",
    "in_scope_containers": ["work"],
    "work": ["Q3 budget summary", "Q3 headcount table"],
    "health": ["MRI results"],
}


def auditor(item, prompt, meta):
    if "MRI" in item:
        return ItemSphereVote(item, False, 0.95, "medical")
    return {"allow": True, "confidence": 0.95}


def test_clean_pass():
    r = DualStageEgressVerifier(auditor).verify_outbound(
        ["Q3 budget summary"], task_prompt=PROMPT, containers=BOXES
    )
    assert r.decision is EgressDecision.ALLOW_VERIFIED
    assert r.agreement_ratio == 1.0


def test_bool_auditor():
    r = DualStageEgressVerifier(lambda i, p, m: True).verify_outbound(
        ["x"], task_prompt=PROMPT
    )
    assert r.decision is EgressDecision.ALLOW_VERIFIED


def test_foreign_bleed_stripped():
    r = DualStageEgressVerifier(auditor).verify_outbound(
        ["Q3 budget summary", "MRI results"], task_prompt=PROMPT, containers=BOXES
    )
    assert r.decision is EgressDecision.STRIP_VERIFIED
    assert r.final_items == ["Q3 budget summary"]
    assert r.stripped_items == ["MRI results"]


def test_provenance_conflict_without_tiebreaker_strips():
    r = DualStageEgressVerifier(lambda i, p, m: True).verify_outbound(
        ["Q3 budget summary", "MRI results"], task_prompt=PROMPT, containers=BOXES
    )
    assert r.stripped_items == ["MRI results"]
    assert r.decision is EgressDecision.STRIP_VERIFIED


def test_low_confidence_tiebreaker_resolves():
    low = lambda i, p, m: {"allow": True, "confidence": 0.5}  # noqa: E731
    tb = lambda i, p, m: ItemSphereVote(i, True, 0.9)  # noqa: E731
    r = DualStageEgressVerifier(low, tb).verify_outbound(
        ["Q3 budget summary"], task_prompt=PROMPT
    )
    assert r.decision is EgressDecision.ALLOW_VERIFIED
    assert r.tie_breakers_used == 1


def test_tiebreaker_denies_strips():
    low = lambda i, p, m: {"allow": True, "confidence": 0.5}  # noqa: E731
    tb = lambda i, p, m: False  # noqa: E731
    r = DualStageEgressVerifier(low, tb).verify_outbound(["a", "b"], task_prompt=PROMPT)
    assert r.decision is EgressDecision.REFUSE_ABSTAIN
    assert r.stripped_items == ["a", "b"]


def test_tiebreaker_still_uncertain_strips():
    low = lambda i, p, m: {"allow": True, "confidence": 0.5}  # noqa: E731
    r = DualStageEgressVerifier(low, low).verify_outbound(["a"], task_prompt=PROMPT)
    assert r.final_items == []


def test_unresolvable_uncertainty_is_unverified():
    low = lambda i, p, m: {"allow": True, "confidence": 0.5}  # noqa: E731
    r = DualStageEgressVerifier(low).verify_outbound(["a"], task_prompt=PROMPT)
    assert r.decision is EgressDecision.ALLOW_UNVERIFIED
    assert r.final_items == ["a"]


def test_malformed_auditor_output_strips():
    r = DualStageEgressVerifier(lambda i, p, m: None).verify_outbound(
        ["a"], task_prompt=PROMPT
    )
    assert r.decision is EgressDecision.REFUSE_ABSTAIN


def test_stage2_recovers_safe_item_not_bleed():
    def checker(prompt, safe, ctx):
        assert "MRI results" not in ctx["in_scope_source_items"]
        return ["Q3 headcount table", "MRI results"]

    r = DualStageEgressVerifier(auditor, completeness_checker=checker).verify_outbound(
        ["Q3 budget summary", "MRI results"], task_prompt=PROMPT, containers=BOXES
    )
    assert r.recovered_items == ["Q3 headcount table"]
    assert "MRI results" not in r.final_items
    assert r.decision is EgressDecision.STRIP_VERIFIED


def test_stage2_recovery_needs_auditor_approval():
    def picky(item, prompt, meta):
        if "headcount" in item:
            return {"allow": True, "confidence": 0.4}
        return True

    r = DualStageEgressVerifier(
        picky, completeness_checker=lambda p, s, c: ["Q3 headcount table"]
    ).verify_outbound(["Q3 budget summary"], task_prompt=PROMPT, containers=BOXES)
    assert r.recovered_items == []


def test_anchor_recovered():
    r = DualStageEgressVerifier(auditor).verify_outbound(
        ["Q3 headcount table"],
        task_prompt=PROMPT,
        containers=BOXES,
        required_anchors=["budget"],
    )
    assert r.recovered_items == ["Q3 budget summary"]
    assert r.decision is EgressDecision.ALLOW_VERIFIED


def test_missing_anchor_blocks_verified():
    r = DualStageEgressVerifier(auditor).verify_outbound(
        ["Q3 headcount table"],
        task_prompt=PROMPT,
        containers=BOXES,
        required_anchors=["Dana"],
    )
    assert r.decision is EgressDecision.ALLOW_UNVERIFIED
    assert "Dana" in r.reason


def test_empty_output_refuses():
    r = DualStageEgressVerifier(auditor).verify_outbound([], task_prompt=PROMPT)
    assert r.decision is EgressDecision.REFUSE_ABSTAIN
    assert r.agreement_ratio == 0.0


def test_only_bleed_refuses():
    r = DualStageEgressVerifier(auditor).verify_outbound(
        ["MRI results"], task_prompt=PROMPT, containers=BOXES
    )
    assert r.decision is EgressDecision.REFUSE_ABSTAIN
