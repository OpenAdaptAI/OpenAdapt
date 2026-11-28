import pytest

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


@pytest.mark.parametrize("always", [False, True])
def test_high_confidence_kept_item_cross_check_is_opt_in(always):
    r = DualStageEgressVerifier(
        auditor, lambda i, p, m: False, always_cross_check=always
    ).verify_outbound(["Q3 budget summary"], task_prompt=PROMPT, containers=BOXES)
    if always:
        assert r.decision is EgressDecision.ALLOW_UNVERIFIED
        assert r.final_items == []
        assert r.stripped_items == ["Q3 budget summary"]
        assert r.tie_breakers_used == 1
        assert r.agreement_ratio == 0.0
    else:
        assert r.decision is EgressDecision.ALLOW_VERIFIED
        assert r.final_items == ["Q3 budget summary"]
        assert r.tie_breakers_used == 0


def test_cross_check_kept_item_after_later_strip():
    r = DualStageEgressVerifier(auditor, lambda i, p, m: False).verify_outbound(
        ["Q3 budget summary", "MRI results"], task_prompt=PROMPT, containers=BOXES
    )
    assert r.decision is EgressDecision.ALLOW_UNVERIFIED
    assert r.final_items == []
    assert r.stripped_items == ["MRI results", "Q3 budget summary"]
    assert r.tie_breakers_used == 2


@pytest.mark.parametrize("second_confidence", [0.49, 0.50, 0.95])
def test_cross_check_kept_and_recovered_items(second_confidence):
    calls = []

    def second(item, prompt, meta):
        calls.append(item)
        confidence = second_confidence if "budget" in item else 0.95
        return ItemSphereVote(item, True, confidence)

    r = DualStageEgressVerifier(auditor, second).verify_outbound(
        ["Q3 headcount table"],
        task_prompt=PROMPT,
        containers=BOXES,
        required_anchors=["budget"],
    )
    assert calls == ["Q3 headcount table", "Q3 budget summary"]
    assert r.tie_breakers_used == 2
    if second_confidence < 0.50:
        assert r.decision is EgressDecision.ALLOW_UNVERIFIED
        assert r.final_items == ["Q3 headcount table"]
        assert r.stripped_items == ["Q3 budget summary"]
        assert r.recovered_items == []
        assert r.agreement_ratio == 0.5
    else:
        assert r.decision is EgressDecision.ALLOW_VERIFIED
        assert r.final_items == ["Q3 headcount table", "Q3 budget summary"]
        assert r.stripped_items == []
        assert r.recovered_items == ["Q3 budget summary"]
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
    r = DualStageEgressVerifier(low, tb, always_cross_check=True).verify_outbound(
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


def test_nested_leaf_provenance_and_normalized_overlap():
    seen = {}

    def audit(item, prompt, meta):
        seen[item] = meta
        return True

    boxes = {
        "recipient_hint": "project team",
        "in_scope_containers": ["work"],
        "work": {
            "documents": ["a.js", {"open_tabs": ("Quarterly capacity report", 42)}],
            "approved": True,
        },
        "private": "Personal notes",
    }
    formatted = "Attach the **QUARTERLY-CAPACITY REPORT** for review."
    candidates = ["a.js", "42", "True", formatted, "open_tabs", "The a.js tab"]
    r = DualStageEgressVerifier(audit).verify_outbound(
        candidates, task_prompt=PROMPT, containers=boxes
    )
    assert r.decision is EgressDecision.ALLOW_VERIFIED
    assert r.final_items == candidates
    assert seen["a.js"]["source_containers"] == ["work", "work.documents"]
    for item in ("42", formatted):
        assert seen[item]["source_containers"] == [
            "work",
            "work.documents",
            "work.documents.open_tabs",
        ]
    assert seen["True"]["source_containers"] == ["work", "work.approved"]
    assert seen["open_tabs"]["source_containers"] == []
    assert seen["The a.js tab"]["source_containers"] == []
    assert seen[formatted]["all_containers"] == [
        "work",
        "work.documents",
        "work.documents.open_tabs",
        "work.approved",
        "private",
    ]
    assert seen[formatted]["recipient_hint"] == "project team"


def test_dotted_scope_keeps_shared_leaves_and_excludes_siblings():
    def audit(item, prompt, meta):
        if item == "Archive inventory":
            assert meta["source_containers"] == ["workspace", "workspace.public"]
        return True

    r = DualStageEgressVerifier(audit).verify_outbound(
        ["Shared note", "Personal notes", "Archive inventory"],
        task_prompt=PROMPT,
        containers={
            "in_scope_containers": ["workspace.public"],
            "workspace": {
                "public": ["Shared note", "Archive inventory; complete edition"],
                "private": ["Shared note", "Personal notes"],
            },
        },
    )
    assert r.decision is EgressDecision.STRIP_VERIFIED
    assert r.final_items == ["Shared note", "Archive inventory"]
    assert r.stripped_items == ["Personal notes"]


@pytest.mark.parametrize(
    ("private", "formatted"),
    [
        ("Personal medical record", "**PERSONAL MEDICAL RECORD**"),
        ("mold.txt", "mold.txt"),
    ],
)
def test_mixed_provenance_redacts_only_private_bullet_even_with_approving_judges(
    private, formatted
):
    heading = "Available material:"
    public = ["Quarterly budget summary", "Status", "Open tasks", "Release notes"]
    body = heading + "\n- " + "\n- ".join([public[0], formatted, *public[1:]])
    r = DualStageEgressVerifier(
        lambda i, p, m: True, lambda i, p, m: True
    ).verify_outbound(
        [body],
        task_prompt=PROMPT,
        containers={
            "in_scope_containers": ["work"],
            "work": {"files": public},
            "private": {"records": [private]},
        },
    )
    assert r.decision is EgressDecision.STRIP_VERIFIED
    assert r.final_items == [heading + "\n- " + "\n- ".join(public)]
    assert r.stripped_items == [formatted]


@pytest.mark.parametrize(
    ("item", "clean", "stripped"),
    [
        (
            "MRI results. The budget is ready!\n\nInventory is current. MRI follow-up?",
            "The budget is ready!\n\nInventory is current.",
            ["MRI results.", "MRI follow-up?"],
        ),
        (
            "Route: MRI results→Budget review -> MRI notes → Inventory review -> MRI scan",
            "Route: Budget review → Inventory review",
            ["MRI results", "MRI notes", "MRI scan"],
        ),
        (
            "- MRI results. Budget review is complete.\n- Inventory is current.",
            "- Budget review is complete.\n- Inventory is current.",
            ["MRI results."],
        ),
    ],
)
def test_rejected_composite_redacts_individual_clauses(item, clean, stripped):
    r = DualStageEgressVerifier(auditor).verify_outbound([item], task_prompt=PROMPT)
    assert r.decision is EgressDecision.STRIP_VERIFIED
    assert r.final_items == [clean]
    assert r.stripped_items == stripped


def test_heading_is_audited_and_omitted_if_its_content_is_removed():
    r = DualStageEgressVerifier(auditor).verify_outbound(
        ["**Documents:**\n- MRI results", "MRI records: Open tasks → Release notes"],
        task_prompt=PROMPT,
    )
    assert r.decision is EgressDecision.STRIP_VERIFIED
    assert r.final_items == ["Open tasks → Release notes"]
    assert r.stripped_items == ["MRI results", "MRI records:"]


def test_composite_tiebreaker_resolves_unsure_unit_before_reassembly():
    calls = []

    def primary(item, prompt, meta):
        if item == "Budget review":
            return ItemSphereVote(item, True, 0.6)
        return auditor(item, prompt, meta)

    def second(item, prompt, meta):
        calls.append(item)
        return ItemSphereVote(item, True, 0.9 if item == "Budget review" else 0.6)

    r = DualStageEgressVerifier(primary, second).verify_outbound(
        ["Budget review -> MRI results -> Release notes"], task_prompt=PROMPT
    )
    assert r.decision is EgressDecision.STRIP_VERIFIED
    assert r.final_items == ["Budget review -> Release notes"]
    assert r.stripped_items == ["MRI results"]
    assert calls == ["Budget review", "Budget review -> Release notes"]


def test_rejected_composite_with_no_denied_clause_stays_rejected():
    item = "Budget review → Release notes"
    r = DualStageEgressVerifier(lambda i, p, m: i != item).verify_outbound(
        [item], task_prompt=PROMPT
    )
    assert r.decision is EgressDecision.REFUSE_ABSTAIN
    assert r.final_items == []
    assert r.stripped_items == [item]


@pytest.mark.parametrize(
    "candidates", [[], ["MRI results"], ["Records:\n- MRI results"]]
)
def test_empty_or_stripped_output_recovers_verified_nested_subset(candidates):
    def checker(prompt, safe, ctx):
        assert safe == []
        assert ctx["in_scope_source_items"] == [
            "Q3 budget summary",
            "Draft budget",
            "42",
        ]
        return ["MRI results", "open_tabs", "Draft budget", "Q3 budget summary"]

    def primary(item, prompt, meta):
        if item == "Q3 budget summary":
            assert meta["source_containers"] == ["work", "work.open_tabs"]
        if item == "Draft budget":
            return ItemSphereVote(item, True, 0.6)
        return auditor(item, prompt, meta)

    r = DualStageEgressVerifier(
        primary,
        lambda i, p, m: ItemSphereVote(i, True, 0.55),
        completeness_checker=checker,
    ).verify_outbound(
        candidates,
        task_prompt=PROMPT,
        containers={
            "in_scope_containers": ["work"],
            "work": {"open_tabs": ("Q3 budget summary", ["Draft budget", 42])},
            "private": {"files": ["MRI results"]},
        },
    )
    expected = (
        EgressDecision.STRIP_VERIFIED if candidates else EgressDecision.ALLOW_VERIFIED
    )
    assert r.decision is expected
    assert r.final_items == r.recovered_items == ["Q3 budget summary"]
    assert r.stripped_items == (["MRI results"] if candidates else [])


@pytest.mark.parametrize(
    ("threshold", "second_allow", "second_confidence", "verified"),
    [
        (0.80, True, 0.70, True),
        (0.80, True, 0.50, True),
        (0.80, True, 0.49, False),
        (0.80, False, 0.95, False),
        (0.40, True, 0.40, True),
        (0.40, True, 0.39, False),
    ],
)
def test_cross_check_applies_asymmetric_veto(
    threshold, second_allow, second_confidence, verified
):
    def second(item, prompt, meta):
        return ItemSphereVote(item, second_allow, second_confidence)

    item = "Q3 budget summary"
    r = DualStageEgressVerifier(
        auditor, second, conformal_threshold=threshold, always_cross_check=True
    ).verify_outbound([item], task_prompt=PROMPT)
    assert r.tie_breakers_used == 1
    if verified:
        assert r.decision is EgressDecision.ALLOW_VERIFIED
        assert r.final_items == [item]
        assert r.stripped_items == []
    else:
        assert r.decision is EgressDecision.ALLOW_UNVERIFIED
        assert r.final_items == []
        assert r.stripped_items == [item]
