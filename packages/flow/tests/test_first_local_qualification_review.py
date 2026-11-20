"""The first-local delivery fence uses the qualification run's risk view.

#2209 refuses a consequential GUI step with no effect contract on the first
leg of a durable local run. During a qualification case, an operator's
confirmed down-classification (a "Create New Patient" button that only opens
the duplicate dialog, classified reversible) is not yet backed by a current
certification, because the campaign is what certifies it. The outcome
classifier already counts the project's decisions in a case bound to the
bundle's own project; the fence now does the same, and only then.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow import run_gate
from openadapt_flow.runtime.clock_checks import first_local_consequential


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    seen: list[Any] = []

    def fake(step: Any, workflow: Any, **kwargs: Any) -> bool:
        strict = kwargs.get("require_current_risk_certification", True)
        seen.append((strict, kwargs.get("certifying_policy_sha256")))
        return strict  # write-shaped: consequential unless the review counts

    monkeypatch.setattr(run_gate, "is_consequential", fake)
    return seen


def test_a_bound_qualification_case_counts_the_project_decision(calls) -> None:
    auth = SimpleNamespace(
        qualification_case_id="representative",
        admitted_policy_contract_sha256="p" * 64,
        _qualification_binding_error=lambda _wf: None,
    )
    assert first_local_consequential(auth, object(), object()) is False
    assert calls == [(False, "p" * 64)]


def test_a_certified_run_checks_against_its_admitted_policy(calls) -> None:
    """Without the digest no certification reproduces (the bundle's own policy
    copy is never authority), so a certified run's operator decisions never
    counted and the quickstart stopped at the dialog-opening step."""
    auth = SimpleNamespace(
        qualification_case_id=None,
        admitted_policy_contract_sha256="c" * 64,
        _qualification_binding_error=lambda _wf: None,
    )
    first_local_consequential(auth, object(), object())
    assert calls == [(True, "c" * 64)]


@pytest.mark.parametrize(
    "auth",
    [
        None,  # an ordinary run
        SimpleNamespace(  # a governed run that is not a qualification case
            qualification_case_id=None,
            admitted_policy_contract_sha256=None,
            _qualification_binding_error=lambda _wf: None,
        ),
        SimpleNamespace(  # a case bound to another project or revision
            qualification_case_id="representative",
            admitted_policy_contract_sha256=None,
            _qualification_binding_error=lambda _wf: "project revision changed",
        ),
    ],
)
def test_every_other_run_needs_a_current_certification(calls, auth) -> None:
    assert first_local_consequential(auth, object(), object()) is True
    assert calls == [(True, None)]


def test_the_real_rule_on_the_certified_referral_program(tmp_path) -> None:
    """No stand-ins: the shipped program, the real ``is_consequential``.

    step_039 (Create New Patient) is reversible only by the operator's
    confirmed classification. Without the admitted policy digest, or with a
    different one, the fence treats it as a write; with the digest the
    certification was made under, the decision counts.
    """
    import shutil
    from pathlib import Path

    from openadapt_flow.ir import Workflow

    program = (
        Path(__file__).resolve().parents[1]
        / "benchmark"
        / "openemr_local"
        / "referral_program"
        / "program"
    )
    shutil.copytree(program, tmp_path / "bundle")
    workflow = Workflow.load(tmp_path / "bundle")
    step = next(s for s in workflow.steps if s.id == "step_039")
    certification = workflow.qualification.last_certification
    digest = certification.policy_contract_sha256

    def auth(policy_digest):
        return SimpleNamespace(
            qualification_case_id=None,
            admitted_policy_contract_sha256=policy_digest,
            _qualification_binding_error=lambda _wf: None,
        )

    assert first_local_consequential(None, step, workflow) is True
    assert first_local_consequential(auth("0" * 64), step, workflow) is True
    assert first_local_consequential(auth(digest), step, workflow) is False
    save = next(s for s in workflow.steps if s.id == "step_040")
    assert first_local_consequential(auth(digest), save, workflow) is True
