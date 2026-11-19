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

from openadapt_flow.runtime import replayer as replayer_mod
from openadapt_flow.runtime.replayer import Replayer


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    seen: list[bool] = []

    def fake(step: Any, workflow: Any, **kwargs: Any) -> bool:
        strict = kwargs.get("require_current_risk_certification", True)
        seen.append(strict)
        return strict  # write-shaped: consequential unless the review counts

    monkeypatch.setattr(replayer_mod, "is_consequential", fake)
    return seen


def _replayer(authorization: Any) -> Replayer:
    replayer = Replayer.__new__(Replayer)
    replayer.governed_authorization = authorization
    return replayer


def test_a_bound_qualification_case_counts_the_project_decision(calls) -> None:
    auth = SimpleNamespace(
        qualification_case_id="representative",
        _qualification_binding_error=lambda _wf: None,
    )
    assert _replayer(auth)._first_local_consequential(object(), object()) is False
    assert calls == [False]


@pytest.mark.parametrize(
    "auth",
    [
        None,  # an ordinary run
        SimpleNamespace(  # a governed run that is not a qualification case
            qualification_case_id=None, _qualification_binding_error=lambda _wf: None
        ),
        SimpleNamespace(  # a case bound to another project or revision
            qualification_case_id="representative",
            _qualification_binding_error=lambda _wf: "project revision changed",
        ),
    ],
)
def test_every_other_run_needs_a_current_certification(calls, auth) -> None:
    assert _replayer(auth)._first_local_consequential(object(), object()) is True
    assert calls == [True]
