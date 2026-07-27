"""The bundled tutorial: the free path, end to end, that actually VERIFIES.

Why this module exists
----------------------

``replay`` runs the ``demo`` execution profile, whose contract sets
``require_effect_contracts=False``.  ``classify_execution_outcome`` therefore
maps every completed demo run to ``COMPLETED_UNVERIFIED`` unconditionally --
correctly, because a demo run proves nothing about a system of record.  Under
that profile ``VERIFIED`` is unreachable by construction, and it MUST stay
unreachable: a profile that can print ``VERIFIED`` without effect evidence is
exactly the failure this product exists to prevent.

The honest fix is not to loosen the profile.  It is to give the tutorial the
evidence the ``standard`` profile already demands, and then run it under that
profile.  Concretely, three things change and nothing is relaxed:

1. **A real system of record.**  The bundled MockMed application is served
   through its transactional persistence boundary
   (:mod:`openadapt_flow.mockmed.fault_server`) with ``?fault=ok``, so a save
   reaches a backend store instead of only mutating an in-page object.  That
   store is readable out of band at ``GET /api/db`` -- a path the application
   itself never calls, so the screen cannot influence what the verifier reads.
2. **A real effect contract.**  The demonstration is recorded with a
   ``system_of_record_reader`` attached, so each event retains the OBSERVED
   before/after record delta.  The compiler then DERIVES the effect contract
   from that delta (:mod:`openadapt_flow.compiler.effect_mining`); it is not
   hand-written, and a placeholder effect is refused here rather than trusted.
3. **A real admission.**  The run is admitted through the unmodified
   fail-closed :func:`~openadapt_flow.run_gate.evaluate_run_gate` under the
   ``standard`` profile against the shipped ``clinical-write`` policy, and
   executed with a :class:`RestRecordVerifier` reading ``/api/db``.

The outcome is ``VERIFIED`` because the write was independently confirmed in a
system of record at ``INDEPENDENT_SYSTEM`` tier, above the tier the profile
requires.  If any contract is unmet the gate refuses and this module fails
loudly rather than degrading the claim.

One further deliberate difference from
:func:`openadapt_flow.demo_driver.record_triage_demo`: the tutorial does not
demonstrate the login screen.  Typing a credential into a recording produces an
artifact whose plaintext value is a secret carrier for no evidentiary gain, and
the demonstration proves exactly as much without it.  That removes a hazard
rather than redacting one.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.request import Request, urlopen

#: The note typed during the tutorial.  Synthetic, constant, and free of
#: anything that could be mistaken for a real clinical note.
TUTORIAL_NOTE = "Synthetic follow-up in two weeks"

#: Default workflow name, matching the name the launcher's quickstart uses.
TUTORIAL_WORKFLOW_NAME = "local-quickstart"

#: The policy the tutorial is certified against.  The shipped healthcare-write
#: policy, not a permissive one: the tutorial has nothing to hide from it.
TUTORIAL_POLICY = "clinical-write"

#: The tutorial's entry query.  ``fault=ok`` routes the save through the real
#: backend (the control mode: the write is persisted normally), and
#: ``idempotency=demo`` makes the application send a stable logical-operation
#: key so the mined contract can assert at-most-once honestly.
TUTORIAL_ENTRY_QUERY = "?fault=ok&idempotency=demo#tasks"


class TutorialError(RuntimeError):
    """A tutorial stage did not produce the evidence it claims."""


@dataclass
class TutorialResult:
    """What the tutorial produced, for the CLI and the regression test."""

    recording_dir: Path
    bundle_dir: Path
    run_dir: Path
    execution_outcome: str
    transaction_outcome: Optional[str]
    execution_profile: Optional[str]
    transaction_billable: Optional[bool]
    model_calls: int
    effects_required: int
    effects_confirmed: int
    effect_tier: Optional[int]
    bundle_digest: Optional[str]
    system_of_record_records: int
    receipt_paths: dict[str, Path] = field(default_factory=dict)


def _http_json(url: str, *, method: str = "GET", body: Any = None) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(  # noqa: S310 - loopback only, built from serve()'s URL
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urlopen(request, timeout=10) as response:  # noqa: S310
        payload = response.read().decode("utf-8")
    return json.loads(payload) if payload else None


def _records(base_url: str) -> list[dict[str, Any]]:
    """Read the independent system of record, out of band from the screen."""

    snapshot = _http_json(f"{base_url.rstrip('/')}/api/db")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("records"), list):
        raise TutorialError("system of record returned an unexpected shape")
    return [record for record in snapshot["records"] if isinstance(record, dict)]


def _center(page: Any, selector: str) -> tuple[int, int]:
    locator = page.locator(selector).first
    locator.wait_for(state="visible")
    box = locator.bounding_box()
    if box is None:  # pragma: no cover - visible implies a box
        raise TutorialError(f"no bounding box for {selector!r}")
    return int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)


def record_tutorial(
    base_url: str,
    recording_dir: Path,
    *,
    headed: bool = False,
) -> Path:
    """Record the triage demonstration WITH system-of-record observation.

    Every action goes through the ordinary :class:`~openadapt_flow.recorder.Recorder`,
    so frames and events are captured exactly as a human demonstration would be.
    The one addition is ``system_of_record_reader``: after each action the
    recorder snapshots ``GET /api/db``, and the compiler mines the effect
    contract from the delta those snapshots show.
    """

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.recorder import Recorder

    _http_json(f"{base_url.rstrip('/')}/api/reset", method="POST", body={})
    entry_url = f"{base_url.rstrip('/')}/{TUTORIAL_ENTRY_QUERY}"
    backend, close = PlaywrightBackend.launch(entry_url, headless=not headed)
    try:
        page = backend.page
        recorder = Recorder(
            backend,
            recording_dir,
            app_url=entry_url,
            system_of_record_reader=lambda: _records(base_url),
        )
        recorder.click(*_center(page, ".open-btn"))
        recorder.click(*_center(page, "#new-encounter"))
        recorder.click(*_center(page, "#type-triage"))
        recorder.click(*_center(page, "#note"))
        recorder.type_text(TUTORIAL_NOTE, param="note")
        recorder.click(*_center(page, "#save-encounter"))
        page.wait_for_selector("#saved-banner", state="visible")
        page.wait_for_timeout(250)
        return recorder.finish()
    finally:
        close()


def consequential_step(workflow: Any) -> Any:
    """The single consequential, effect-bound step the compiler derived.

    Fails loudly on a placeholder effect.  The compiler emits one when the
    demonstration showed a consequential write whose system-of-record binding
    was not derivable; trusting it would mean verifying an invented endpoint.
    """

    candidates = [
        step for step in workflow.steps if step.risk == "irreversible" and step.effects
    ]
    if len(candidates) != 1:
        raise TutorialError(
            "expected exactly one consequential effect-bound step; observed "
            f"{[step.id for step in candidates]}"
        )
    step = candidates[0]
    if any(effect.needs_operator_confirmation for effect in step.effects):
        raise TutorialError(
            "the compiler emitted an UNBOUND placeholder effect: the "
            "demonstration observed no system-of-record delta to bind, so "
            "there is nothing to verify against and the tutorial refuses to "
            "claim otherwise"
        )
    if not any(effect.kind.value == "record_written" for effect in step.effects):
        raise TutorialError(
            "mined effects do not assert a written record: "
            f"{sorted(effect.kind.value for effect in step.effects)}"
        )
    return step


def certify_tutorial(workflow: Any) -> Any:
    """Certify the compiled bundle against the shipped ``clinical-write`` policy.

    The same evaluation ``openadapt-flow certify <bundle> --policy clinical-write``
    performs.  Nothing here suppresses a violation.
    """

    from openadapt_flow.policy import evaluate_policy, load_policy

    report = evaluate_policy(
        workflow,
        load_policy(TUTORIAL_POLICY),
        require_current_risk_certification=True,
    )
    if not report.passed:
        raise TutorialError(
            f"the tutorial bundle is NOT certified under {TUTORIAL_POLICY!r}:\n"
            f"{report.render()}"
        )
    return report


def run_tutorial_workflow(
    *,
    base_url: str,
    workflow: Any,
    bundle_dir: Path,
    run_dir: Path,
    headed: bool = False,
) -> Any:
    """Admit and execute the tutorial under the ``standard`` profile."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.deployment import DeploymentConfig, PolicySection
    from openadapt_flow.execution_profiles import (
        ExecutionProfile,
        execution_profile_contract,
    )
    from openadapt_flow.run_gate import build_runtime_authorization, evaluate_run_gate
    from openadapt_flow.runtime import Replayer
    from openadapt_flow.runtime.effects import RestRecordVerifier

    _http_json(f"{base_url.rstrip('/')}/api/reset", method="POST", body={})
    entry_url = f"{base_url.rstrip('/')}/{TUTORIAL_ENTRY_QUERY}"

    # The independent oracle: it reads the backend store over HTTP, never the
    # screen, so an optimistic banner cannot make it confirm anything.
    verifier = RestRecordVerifier(
        base_url,
        records_path="/api/db",
        records_key="records",
        timeout_s=2.0,
        poll_interval_s=0.05,
    )
    gate = evaluate_run_gate(
        workflow,
        bundle_dir=bundle_dir,
        deployment=DeploymentConfig(policy=PolicySection(policy=TUTORIAL_POLICY)),
        effect_verifier=verifier,
        profile_contract=execution_profile_contract(ExecutionProfile.STANDARD),
        effective_durable=True,
        effective_require_settled=True,
    )
    if not gate.passed:
        raise TutorialError(
            "the standard run gate REFUSED the tutorial bundle; nothing was "
            f"executed:\n{gate.render()}"
        )
    authorization = build_runtime_authorization(
        workflow,
        gate,
        approval_source="openadapt-flow-tutorial",
        params={"note": TUTORIAL_NOTE},
    )

    backend, close = PlaywrightBackend.launch(entry_url, headless=not headed)
    try:
        return Replayer(
            backend,
            effect_verifier=verifier,
            governed_authorization=authorization,
            durable=True,
            require_settled=True,
        ).run(
            workflow.model_copy(deep=True),
            params={"note": TUTORIAL_NOTE},
            bundle_dir=bundle_dir,
            run_dir=run_dir,
            execution_target_kind="web",
            execution_origin=base_url.rstrip("/"),
            execution_entry_url=entry_url,
        )
    finally:
        close()


def run_tutorial(
    work_dir: Path | str,
    *,
    headed: bool = False,
    name: str = TUTORIAL_WORKFLOW_NAME,
    emit_receipt: bool = True,
    label: Optional[str] = None,
    launcher_version: Optional[str] = None,
    echo: Optional[Callable[[str], None]] = None,
) -> TutorialResult:
    """Run the complete free path and return its evidence.

    Stages: serve -> record -> compile -> certify -> run (standard profile,
    independent effect verification) -> receipt.

    Raises:
        TutorialError: a stage produced insufficient evidence.  Nothing here
            downgrades a requirement to make a stage pass.
    """

    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.mockmed.fault_server import serve
    from openadapt_flow.report import render_run_report

    say = echo or (lambda message: None)
    root = Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    recording_dir = root / "recording"
    bundle_dir = root / "bundle"
    run_dir = root / "run"

    base_url, _db, stop = serve()
    try:
        say("[1/5] Record the demonstration against a real persistence boundary")
        record_tutorial(base_url, recording_dir, headed=headed)

        say("[2/5] Compile, mining the effect contract from the observed delta")
        workflow = compile_recording(
            recording_dir, bundle_dir, name=name, mine_effects=True
        )
        save = consequential_step(workflow)
        say(
            f"      {len(save.effects)} system-of-record effect(s) derived from "
            f"the demonstration's record delta on {save.id}"
        )

        say(f"[3/5] Certify against the {TUTORIAL_POLICY} policy")
        certify_tutorial(workflow)

        say("[4/5] Admit and execute under the standard profile")
        started = time.monotonic()
        report = run_tutorial_workflow(
            base_url=base_url,
            workflow=workflow,
            bundle_dir=bundle_dir,
            run_dir=run_dir,
            headed=headed,
        )
        render_run_report(run_dir)
        elapsed = time.monotonic() - started
        record_count = len(_records(base_url))
        say(
            f"      {report.execution_outcome} in {elapsed:.1f}s; "
            f"{report.model_calls} model calls; the system of record holds "
            f"{record_count} record(s)"
        )
    finally:
        stop()

    envelope = report.outcome_envelope
    tier: Optional[int] = None
    for step_result in report.results:
        for evidence in step_result.effect_evidence:
            if (
                evidence.final_verdict == "confirmed"
                and evidence.verification_tier is not None
            ):
                value = int(evidence.verification_tier)
                tier = value if tier is None else min(tier, value)

    result = TutorialResult(
        recording_dir=recording_dir,
        bundle_dir=bundle_dir,
        run_dir=run_dir,
        execution_outcome=str(report.execution_outcome),
        transaction_outcome=report.transaction_outcome,
        execution_profile=report.execution_profile,
        transaction_billable=report.transaction_billable,
        model_calls=int(report.model_calls),
        effects_required=(
            int(envelope.required_contracts.effect) if envelope is not None else 0
        ),
        effects_confirmed=(
            int(envelope.passed_contracts.effect) if envelope is not None else 0
        ),
        effect_tier=tier,
        bundle_digest=report.bundle_content_digest,
        system_of_record_records=record_count,
    )

    if emit_receipt:
        # One rail, one rule: the shareable artifact is the SUCCESS rail, and
        # only a VERIFIED run may use it -- exactly what ``report-run``
        # enforces. A halt is a correct, useful result, but it is not a
        # success and must not be dressed as one.
        if report.execution_outcome != "VERIFIED":
            say(
                "[5/5] No receipt: the shareable artifact is the success rail, "
                f"and this run is {report.execution_outcome}. The run's own "
                "evidence is in REPORT.md."
            )
        else:
            from openadapt_flow.receipt import build_receipt, write_receipt

            say("[5/5] Emit the local run receipt")
            receipt = build_receipt(
                report,
                provenance="synthetic-tutorial",
                label=label,
                launcher_version=launcher_version,
            )
            result.receipt_paths = write_receipt(receipt, run_dir)
    return result
