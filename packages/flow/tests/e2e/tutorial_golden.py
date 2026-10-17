"""The golden shape of the bundled tutorial's run: what it does, not just how it ends.

Why a golden and not a handful of asserts
-----------------------------------------

``VERIFIED`` is a one-bit outcome, and a one-bit outcome is a weak regression
gate: a run can reach it for the wrong reason and still look healthy.  Concrete
ways that happens, all of which leave the outcome untouched:

* the structural rung stops resolving and every click silently falls to the
  OCR or geometry rung -- slower, weaker evidence, same green result;
* an actuation path stops going through the DOM and starts synthesising raw
  pointer events;
* the compiler stops classifying the save as ``irreversible``, so the identity
  gate and the effect contract are never armed;
* a healing pass starts firing on a clean, undrifted screen;
* the mined effect contract changes shape, so ``VERIFIED`` is asserted against
  a different promise than the one that was reviewed.

So the artifact pins the *mechanism*: per step, the rung that resolved it, how
the input was delivered, whether identity was checked, its risk class, and the
effect evidence it produced -- plus the run-level contract counts and the exact
mined effect-contract hashes.

Determinism
-----------

Every field here was observed byte-identical across repeated local runs.  The
one field of the tutorial's result that is NOT stable is
``TutorialResult.bundle_digest``: the bundle carries retained frames, so its
content digest differs per recording.  It is deliberately excluded.  Nothing
here is a timing, a path, a temporary directory, or a wall-clock value.

Regenerating
------------

Deliberately, and never as a reflex::

    OPENADAPT_UPDATE_GOLDEN=1 pytest tests/e2e/test_free_path_e2e.py

Then read the diff.  A changed rung, actuation, risk class, or contract count
is a product change and belongs in the commit message.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from openadapt_flow.ir import RunReport
from openadapt_flow.tutorial import TutorialResult

#: The committed artifact.  Lives beside the other golden artifacts rather than
#: inside ``tests/e2e`` so it is obvious that it is data under review, not code.
GOLDEN_PATH = Path(__file__).resolve().parents[1] / "golden" / "tutorial_run.json"

#: Set to regenerate rather than compare.  Named to match the other golden
#: artifacts in this repository.
UPDATE_ENV = "OPENADAPT_UPDATE_GOLDEN"


def _step_shape(step: Any) -> dict[str, Any]:
    """The durable, mechanism-bearing facts about one replayed step."""

    resolution = step.resolution
    identity = step.identity
    delivery = step.delivery_receipt
    return {
        "step_id": step.step_id,
        "intent": step.intent,
        "risk": step.risk,
        "ok": step.ok,
        "skipped": step.skipped,
        # Which rung of the resolution ladder found the target. A run that
        # reaches VERIFIED on a weaker rung than it used to is a regression
        # even though its outcome is unchanged.
        "resolution_rung": getattr(resolution, "rung", None),
        # How the input was delivered. `dom` means the structural handle drove
        # it; a change here means the substrate path changed.
        "actuation": step.actuation,
        "delivery_status": getattr(delivery, "status", None),
        # The pre-action identity gate: armed, and what it concluded.
        "identity_status": getattr(identity, "status", None),
        "identity_mode": getattr(identity, "mode", None),
        # TYPE/SELECT_OPTION use their own explicit readback contract. It is
        # not a screen postcondition and must not impersonate one.
        "input_verified": step.input_verified,
        "postconditions_ok": step.postconditions_ok,
        "safety_halt": step.safety_halt,
        "healed": bool(step.heal),
        "interstitial_actions": len(step.interstitial_actions),
        # Independent effect evidence, per declared effect.
        "effect_evidence": [
            {
                "verdict": evidence.final_verdict,
                "verification_tier": evidence.verification_tier,
                "substrate": evidence.substrate,
            }
            for evidence in step.effect_evidence
        ],
    }


def golden_shape(result: TutorialResult, report: RunReport) -> dict[str, Any]:
    """Project one tutorial run onto the shape the artifact pins.

    Everything in here is a closed-vocabulary name, an enum, a boolean, or a
    count, plus the mined effect-contract hashes. No parameter values, no
    record content, no screen text, no paths, no timings -- so the artifact is
    mechanism, not data, and stays on the public side of ``AGENTS.md`` 4.2.
    """

    envelope = report.outcome_envelope
    journal = [
        {
            "step_id": entry.step_id,
            "consequential": entry.consequential,
            "attempt_state": entry.attempt_state,
            "observed_effect": entry.observed_effect,
            "verification_performed": entry.verification_performed,
            "effect_verified": entry.effect_verified,
            "approved_unverified": entry.approved_unverified,
            "collateral_reconciliation_actions": (
                entry.collateral_reconciliation_actions
            ),
            "intended_effect_contract_hashes": list(
                entry.intended_effect_contract_hashes
            ),
        }
        for entry in (report.effect_journal or [])
    ]

    return {
        "schema": 1,
        # --- how the run ended -------------------------------------------
        "execution_outcome": result.execution_outcome,
        "transaction_outcome": result.transaction_outcome,
        "execution_profile": result.execution_profile,
        "transaction_billable": result.transaction_billable,
        "success": report.success,
        "production_eligible": report.production_eligible,
        "halt": report.halt,
        # --- what it cost --------------------------------------------------
        # The whole free-path claim: a healthy run reaches VERIFIED with no
        # model in the loop at all.
        "model_calls": result.model_calls,
        "est_model_cost_usd": report.est_model_cost_usd,
        "heal_count": report.heal_count,
        # --- what it proved ------------------------------------------------
        "effects_required": result.effects_required,
        "effects_confirmed": result.effects_confirmed,
        # 1 == INDEPENDENT_SYSTEM: the effect was read back out of band, not
        # inferred from the screen. The profile only demands tier 3.
        "effect_tier": result.effect_tier,
        "governed_minimum_effect_tier": report.governed_minimum_effect_tier,
        "governed_policy_name": report.governed_policy_name,
        "system_of_record_records": result.system_of_record_records,
        "required_contracts": (
            envelope.required_contracts.model_dump() if envelope is not None else None
        ),
        "passed_contracts": (
            envelope.passed_contracts.model_dump() if envelope is not None else None
        ),
        # --- how it got there ----------------------------------------------
        "step_count": len(report.results),
        "rung_counts": dict(report.rung_counts),
        "identity_applicable_steps": report.identity_applicable_steps,
        "identity_armed_steps": report.identity_armed_steps,
        "steps": [_step_shape(step) for step in report.results],
        "effect_journal": journal,
    }


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten to leaf paths so a failure can name one field, not two blobs."""

    if isinstance(value, dict):
        flat: dict[str, Any] = {}
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return flat
    if isinstance(value, list):
        flat = {}
        for index, item in enumerate(value):
            flat.update(_flatten(item, f"{prefix}[{index}]"))
        return flat or {prefix: []}
    return {prefix: value}


def assert_no_drift(observed: dict[str, Any], committed: dict[str, Any]) -> None:
    """Compare leaf by leaf and name the exact field that moved."""

    observed_flat = _flatten(observed)
    committed_flat = _flatten(committed)

    added = sorted(set(observed_flat) - set(committed_flat))
    removed = sorted(set(committed_flat) - set(observed_flat))
    changed = [
        (path, committed_flat[path], observed_flat[path])
        for path in sorted(set(observed_flat) & set(committed_flat))
        if committed_flat[path] != observed_flat[path]
    ]

    if not (added or removed or changed):
        return

    lines = [
        "the bundled tutorial no longer runs the way the golden artifact "
        f"records. Review the change, then regenerate deliberately with "
        f"{UPDATE_ENV}=1 pytest tests/e2e/test_free_path_e2e.py",
    ]
    lines += [
        f"  changed {path}: golden={golden!r} now={now!r}"
        for path, golden, now in changed
    ]
    lines += [f"  the run no longer emits {path}" for path in removed]
    lines += [f"  the run now emits {path}={observed_flat[path]!r}" for path in added]
    raise AssertionError("\n".join(lines))


def compare_or_update(shape: dict[str, Any]) -> None:
    """Compare ``shape`` with the committed artifact, or rewrite it on demand."""

    if os.environ.get(UPDATE_ENV):  # pragma: no cover - maintenance path
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(
            json.dumps(shape, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return

    assert GOLDEN_PATH.is_file(), f"missing golden artifact {GOLDEN_PATH}"
    committed = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert_no_drift(shape, committed)
