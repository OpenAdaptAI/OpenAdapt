"""First-class optional agent_used and verifier_adapter fields on EvidenceScopeV1."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from openadapt_types.check_level import CheckLevel
from openadapt_types.evidence_scope import (
    EvidenceScopeV1,
    build_evidence_scope,
    evidence_scope_problem,
    predicate_evidence_scope_problem,
)


def _base_scope(**overrides):
    data = build_evidence_scope(
        CheckLevel.SEPARATE_SESSION,
        basis="separate_session",
        not_covered=("read_back_time",),
    )
    data.update(overrides)
    return data


def test_omitted_provenance_fields_stay_backwards_compatible() -> None:
    block = _base_scope()
    assert "agent_used" not in block
    assert "verifier_adapter" not in block
    assert evidence_scope_problem(block) is None
    assert (
        predicate_evidence_scope_problem(
            {"outcome": "VERIFIED", "evidence_scope": block}
        )
        is None
    )


@pytest.mark.parametrize(
    ("agent_used", "verifier_adapter"),
    [
        ("claude-code", "deterministic"),
        ("gpt-5.4", "Veto-4B-r6"),
        ("codex", "taint-guard"),
        ("compiled-plan@v1.2", "deterministic+Veto-4B-r6"),
        ("Qwen/Qwen3.5-4B", "deterministic+Veto-4B-r6"),
        ("openai:gpt-5.4", "deterministic"),
    ],
)
def test_valid_agent_and_verifier_adapter_fields(
    agent_used: str, verifier_adapter: str
) -> None:
    block = build_evidence_scope(
        CheckLevel.SEPARATE_SESSION,
        basis="separate_session",
        not_covered=("read_back_time",),
        agent_used=agent_used,
        verifier_adapter=verifier_adapter,
    )
    assert block["agent_used"] == agent_used
    assert block["verifier_adapter"] == verifier_adapter
    assert evidence_scope_problem(block) is None
    parsed = EvidenceScopeV1.model_validate(block)
    assert parsed.agent_used == agent_used
    assert parsed.verifier_adapter == verifier_adapter


@pytest.mark.parametrize("field", ["agent_used", "verifier_adapter"])
@pytest.mark.parametrize(
    "bad_value",
    [
        "",
        "a" * 129,
        123,
        ["claude-code"],
        " leading-space",
        "bad\nnewline",
        "https://evil.example/payload",
    ],
)
def test_invalid_provenance_field_rejected(field: str, bad_value: object) -> None:
    block = _base_scope(**{field: bad_value})
    assert evidence_scope_problem(block) == "evidence_scope"
    with pytest.raises(ValidationError):
        EvidenceScopeV1.model_validate(block)


def test_extra_forbid_still_enforced() -> None:
    block = _base_scope(unknown_field="x")
    assert evidence_scope_problem(block) == "evidence_scope"
    with pytest.raises(ValidationError):
        EvidenceScopeV1.model_validate(block)
