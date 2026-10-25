"""Behavioral generalization: execute held-out items, check identity and effects.

``compiler.induction.structural_trace_coverage`` (and ``validate_held_out``)
compares the SHAPE of an induced program with a held-out trace. It executes
nothing and checks neither which record was acted on nor what was written, so
it cannot support a claim that a program generalizes. This module is the score
for that claim.

For each held-out item (inputs the program was never taught on):

1. reset the test copy (optional) and open a fresh session;
2. run the program with the item's inputs through the ordinary replayer, with
   the record check (effect verifier) bound to those inputs;
3. ask an independent check, which the caller supplies and the program never
   sees, whether the intended entity got exactly the intended change.

Each item lands in one class:

- ``generalized``: the engine ended VERIFIED and the independent check agrees;
- ``halted_safely``: the engine stopped and the independent check found no
  change (a safe failure, but not generalization);
- ``silent_wrong``: the engine ended VERIFIED and the independent check
  disagrees (wrong entity, wrong value, a duplicate, collateral damage);
- ``wrong_effect``: the engine did not end VERIFIED and the independent check
  found a change it should not have (a partial or wrong write);
- ``error``: the item could not be run.

``score`` is generalized / items. A program with any ``silent_wrong`` or
``wrong_effect`` item has not generalized, whatever its score.

Without an independent check, an item is judged by the engine's record check
alone and ``independent`` is false in the result. That is still behavioral
(the program ran and its read-back confirmed the record bound to the item's
inputs), but it is weaker: report it as such.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, Field

CaseClass = Literal["generalized", "halted_safely", "silent_wrong", "wrong_effect", "error"]
IndependentVerdict = Literal["VERIFIED", "MISSING", "WRONG"]

#: The engine outcomes that mean "every declared effect was read back".
_ENGINE_VERIFIED = {"VERIFIED"}


def classify_case(engine_outcome: str, independent: Optional[str]) -> CaseClass:
    """Class of one held-out item from the engine outcome and the independent check.

    ``independent`` is ``VERIFIED`` (exactly the intended change on the
    intended entity), ``MISSING`` (no change) or ``WRONG`` (anything else), or
    None when no independent check was supplied.
    """

    engine = str(engine_outcome or "").split(".")[-1].upper()
    verified = engine in _ENGINE_VERIFIED
    if engine in ("", "ERROR", "REFUSED"):
        if independent in (None, "MISSING"):
            return "error"
        return "wrong_effect"
    if independent is None:
        return "generalized" if verified else "halted_safely"
    if verified:
        return "generalized" if independent == "VERIFIED" else "silent_wrong"
    if independent == "MISSING":
        return "halted_safely"
    return "wrong_effect"


class HeldOutCaseResult(BaseModel):
    #: SHA-256 of the item's canonical inputs (values are not stored).
    inputs_sha256: str
    engine_outcome: str
    independent: Optional[str] = None
    case_class: CaseClass
    failed_step_id: Optional[str] = None
    model_calls: int = 0
    seconds: float = 0.0
    detail: dict[str, Any] = Field(default_factory=dict)


class BehavioralGeneralization(BaseModel):
    """Executed held-out items, judged on identity and effects."""

    kind: Literal["behavioral"] = "behavioral"
    n_items: int = 0
    generalized: int = 0
    halted_safely: int = 0
    silent_wrong: int = 0
    wrong_effect: int = 0
    errors: int = 0
    #: True when every item was also judged by an independent check.
    independent: bool = False
    model_calls: int = 0
    per_item: list[HeldOutCaseResult] = Field(default_factory=list)

    @property
    def score(self) -> float:
        return self.generalized / self.n_items if self.n_items else 0.0

    @property
    def generalizes(self) -> bool:
        """Every item generalized; nothing wrong was written."""
        return (
            self.n_items > 0
            and self.generalized == self.n_items
            and self.silent_wrong == 0
            and self.wrong_effect == 0
        )

    def render(self) -> str:
        check = (
            "engine read-back and an independent check"
            if self.independent
            else "engine read-back only (no independent check)"
        )
        return (
            f"Held-out BEHAVIORAL generalization: {self.generalized}/{self.n_items} "
            f"items ran VERIFIED on the intended record ({self.score:.2f}); "
            f"{self.halted_safely} halted safely, {self.silent_wrong} silent wrong, "
            f"{self.wrong_effect} wrong effect, {self.errors} errors. "
            f"Executed every item; judged by {check}."
        )


def inputs_digest(params: Mapping[str, Any]) -> str:
    canon = json.dumps({k: str(v) for k, v in params.items()}, sort_keys=True)
    return hashlib.sha256(canon.encode()).hexdigest()


def summarize(results: Sequence[HeldOutCaseResult]) -> BehavioralGeneralization:
    counts = {c: 0 for c in ("generalized", "halted_safely", "silent_wrong", "wrong_effect", "error")}
    for r in results:
        counts[r.case_class] += 1
    return BehavioralGeneralization(
        n_items=len(results),
        generalized=counts["generalized"],
        halted_safely=counts["halted_safely"],
        silent_wrong=counts["silent_wrong"],
        wrong_effect=counts["wrong_effect"],
        errors=counts["error"],
        independent=bool(results) and all(r.independent is not None for r in results),
        model_calls=sum(r.model_calls for r in results),
        per_item=list(results),
    )


def score_held_out(
    bundle_dir: Path | str,
    items: Sequence[Mapping[str, str]],
    *,
    open_session: Callable[[], tuple[Any, Callable[[], None]]],
    make_verifier: Callable[[Mapping[str, str]], Any],
    run_root: Path | str,
    independent: Optional[Callable[[Mapping[str, str], Any, Any], str]] = None,
    snapshot: Optional[Callable[[], Any]] = None,
    reset: Optional[Callable[[], Any]] = None,
    reset_each: bool = False,
    replayer_kwargs: Optional[Mapping[str, Any]] = None,
) -> BehavioralGeneralization:
    """Run every held-out item through the program and judge it.

    ``independent(item, before, after)`` returns ``VERIFIED`` / ``MISSING`` /
    ``WRONG`` from ``snapshot()`` taken before and after the item; both must be
    given together. ``make_verifier(params)`` builds the engine's record check
    for the item (the same one a governed run uses). With ``reset_each`` the
    test copy is reset before every item; otherwise once before the first.
    """

    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    if (independent is None) != (snapshot is None):
        raise ValueError("independent and snapshot are given together")
    bundle_dir = Path(bundle_dir)
    run_root = Path(run_root)
    run_root.mkdir(parents=True, exist_ok=True)
    workflow = Workflow.load(bundle_dir)
    results: list[HeldOutCaseResult] = []
    if reset is not None and not reset_each:
        reset()
    for n, item in enumerate(items, start=1):
        params = {k: str(v) for k, v in item.items()}
        if reset is not None and reset_each:
            reset()
        before = snapshot() if snapshot is not None else None
        t0 = time.time()
        engine, failed, calls, detail = "ERROR", None, 0, {}
        try:
            backend, close = open_session()
            try:
                report = Replayer(
                    backend, effect_verifier=make_verifier(params), **dict(replayer_kwargs or {})
                ).run(
                    workflow.model_copy(deep=True),
                    params=params,
                    bundle_dir=bundle_dir,
                    run_dir=run_root / f"item-{n:03d}",
                )
            finally:
                close()
            engine = str(report.transaction_outcome or report.execution_outcome or "UNKNOWN")
            bad = next((r for r in report.results if not r.ok), None)
            failed = bad.step_id if bad is not None else None
            calls = int(report.model_calls or 0)
        except Exception as exc:  # noqa: BLE001 - an item that cannot run is a result
            detail["error"] = f"{type(exc).__name__}: {exc}"[:500]
        verdict = None
        if independent is not None and snapshot is not None:
            verdict = independent(item, before, snapshot())
        results.append(
            HeldOutCaseResult(
                inputs_sha256=inputs_digest(params),
                engine_outcome=engine.split(".")[-1].upper(),
                independent=verdict,
                case_class=classify_case(engine, verdict),
                failed_step_id=failed,
                model_calls=calls,
                seconds=round(time.time() - t0, 2),
                detail=detail,
            )
        )
    return summarize(results)
