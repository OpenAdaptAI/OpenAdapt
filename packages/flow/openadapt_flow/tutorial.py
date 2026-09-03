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

#: The guided tutorial pauses before each scripted demonstration action and
#: each replay step. The fast tutorial keeps a zero delay.
GUIDED_PRESENTATION_DELAY_S = 1.0

#: A tutorial delay is a presentation aid, not an unbounded runtime wait.
MAX_PRESENTATION_DELAY_S = 5.0

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

#: The fault mode ``--break-it`` injects.  ``optimistic`` is the sharpest
#: demonstration of the product's claim: the backend REJECTS the write AFTER
#: the application has already painted its success banner, so every on-screen
#: check passes while nothing landed in the system of record.  Only an
#: independent read of that system can catch it -- which is the point.
TUTORIAL_BREAK_FAULT = "optimistic"

#: The entry query for the ``--break-it`` rerun.  Identical to the clean
#: query except for the fault mode; the bundle, the policy, the gate, and the
#: verifier are all unchanged.
TUTORIAL_BREAK_ENTRY_QUERY = f"?fault={TUTORIAL_BREAK_FAULT}&idempotency=demo#tasks"


class TutorialError(RuntimeError):
    """A tutorial stage did not produce the evidence it claims."""


@dataclass
class BreakItResult:
    """What the ``--break-it`` rerun proved, for the CLI's narrative.

    Every field is read from the halted run's own report or from the fault
    server's ground-truth store -- nothing here is scripted output.
    """

    run_dir: Path
    report_path: Path
    fault: str
    execution_outcome: str
    transaction_outcome: Optional[str]
    transaction_billable: Optional[bool]
    #: The lie, observed: the consequential step's on-screen postconditions
    #: all passed (the application painted its success banner) even though the
    #: write never landed.
    screen_claimed_success: bool
    #: The success banner's text as observed on screen at the halt, if any.
    screen_claim_text: Optional[str]
    effects_required: int
    effects_refuted: int
    #: The engine's own explanation of the halt, verbatim from the report.
    halt_reason: str
    #: Rows in the independent system of record after the run (0: the write
    #: the screen claimed was never persisted).
    system_of_record_records: int


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
    # The report can classify a VERIFIED production-profile transaction as
    # billable. This local-only tutorial never reports usage to Cloud.
    reported_to_metering: bool = False
    receipt_paths: dict[str, Path] = field(default_factory=dict)
    #: Present only when the tutorial ran with ``break_it=True``: the same
    #: certified bundle, rerun against a backend that lies, and the engine's
    #: halt that caught it.
    break_it: Optional[BreakItResult] = None


def _next_steps_block() -> str:
    """The closing block the CLI prints after a plain VERIFIED tutorial run.

    Only the success rail earns it: a halt or a ``--break-it`` rerun ends on
    its own narrative instead.  The links match the flagship OpenAdapt
    README so every surface points at the same three destinations.
    """

    return (
        "That run made no model call and sent nothing off this computer.\n"
        "  What it proves        https://openadapt.ai/execute\n"
        "  Run it on your work   https://openadapt.ai/qualify\n"
        "  Community             https://discord.gg/yF527cQbDG"
    )


def outcome_epilogue_lines(
    *,
    what: str,
    why_safe: str,
    next_command: str,
) -> list[str]:
    """The three-line outcome epilogue: what / why-safe / exact next command.

    Presentation only -- every caller keeps its own exit code and fail-closed
    semantics unchanged. Shared by the replay finisher, the lint failure path,
    and the tutorial so every non-VERIFIED ending speaks with one voice.
    """

    return [
        f"What happened: {what}.",
        f"Why this is safe: {why_safe}.",
        f"Next command: {next_command}",
    ]


def tutorial_epilogue(result: "TutorialResult") -> list[str]:
    """The epilogue for a NON-VERIFIED tutorial run (never printed on success).

    A halt or unverified completion is a correct result but earns no receipt;
    these lines say what happened, why that is the safe behavior, and give the
    exact next command instead of leaving the operator at a dead end.
    """

    outcome = result.execution_outcome
    if outcome == "COMPLETED_UNVERIFIED":
        what = (
            f"the run completed on screen but ended {outcome} -- no independent "
            "system-of-record proof, so no receipt was issued"
        )
        why_safe = (
            "a demo-profile completion can never claim success under Flow; "
            "only independently confirmed writes earn VERIFIED"
        )
        next_command = (
            "openadapt-flow scaffold-verifier "
            f"{result.recording_dir}   # draft an oracle, wire deployment.yaml "
            "effects:, re-run under the standard profile"
        )
    else:  # HALTED / FAILED / ROLLED_BACK
        what = (
            f"the run stopped at a governed check and ended {outcome} -- no "
            "receipt was issued for an unproven run"
        )
        why_safe = (
            "the engine reports only what independent evidence proves; halting "
            "on a failed check is the fail-closed contract working"
        )
        next_command = f"openadapt-flow explain {result.run_dir}"
    return outcome_epilogue_lines(
        what=what, why_safe=why_safe, next_command=next_command
    )


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


def _validated_presentation_delay(value: float) -> float:
    delay = float(value)
    if not 0.0 <= delay <= MAX_PRESENTATION_DELAY_S:
        raise TutorialError(
            "presentation delay must be between 0 and "
            f"{MAX_PRESENTATION_DELAY_S:g} seconds"
        )
    return delay


def _presentation_pause(
    delay_s: float, *, sleep: Callable[[float], None] = time.sleep
) -> None:
    """Pause one tutorial stage without changing production runtime timing."""

    delay = _validated_presentation_delay(delay_s)
    if delay:
        sleep(delay)


def _presentation_replayer_type(replayer_type: type[Any], delay_s: float) -> type[Any]:
    """Return a tutorial-only replayer type that pauses once per IR step."""

    delay = _validated_presentation_delay(delay_s)
    if not delay:
        return replayer_type

    class PresentationReplayer(replayer_type):
        def _run_step(self, *args: Any, **kwargs: Any) -> Any:
            _presentation_pause(delay)
            return super()._run_step(*args, **kwargs)

    return PresentationReplayer


def record_tutorial(
    base_url: str,
    recording_dir: Path,
    *,
    headed: bool = False,
    presentation_delay_s: float = 0.0,
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

    delay = _validated_presentation_delay(presentation_delay_s)
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

        def demonstrate(action: Callable[[], None]) -> None:
            _presentation_pause(delay)
            action()

        demonstrate(lambda: recorder.click(*_center(page, ".open-btn")))
        demonstrate(lambda: recorder.click(*_center(page, "#new-encounter")))
        demonstrate(lambda: recorder.click(*_center(page, "#type-triage")))
        demonstrate(lambda: recorder.click(*_center(page, "#note")))
        demonstrate(lambda: recorder.type_text(TUTORIAL_NOTE, param="note"))
        demonstrate(lambda: recorder.click(*_center(page, "#save-encounter")))
        page.wait_for_selector("#saved-banner", state="visible")
        page.wait_for_timeout(250)
        return recorder.finish()
    finally:
        close()


def record_tutorial_interactive(base_url: str, recording_dir: Path) -> Path:
    """Record the bundled workflow from a real human browser demonstration."""

    from openadapt_flow.interactive_recorder import record_interactive

    _http_json(f"{base_url.rstrip('/')}/api/reset", method="POST", body={})
    baseline_records = len(_records(base_url))
    entry_url = f"{base_url.rstrip('/')}/{TUTORIAL_ENTRY_QUERY}"
    print(
        "\nGuided recording\n"
        "  1. Open the first task.\n"
        "  2. Select New encounter, then Triage.\n"
        "  3. Select the Note field and type a short synthetic note.\n"
        "  4. Select Save encounter.\n"
        "  5. Wait for the saved message. The recording browser closes "
        "automatically.\n"
        "\nOpenAdapt records your actions and observes the separate local "
        "system of record."
    )
    return record_interactive(
        entry_url,
        recording_dir,
        param_fields=("note",),
        headless=False,
        system_of_record_reader=lambda: _records(base_url),
        stop_when=lambda: len(_records(base_url)) > baseline_records,
    )


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
    entry_query: Optional[str] = None,
    presentation_delay_s: float = 0.0,
) -> Any:
    """Admit and execute the tutorial under the ``standard`` profile.

    ``entry_query`` defaults to the clean :data:`TUTORIAL_ENTRY_QUERY`; the
    ``--break-it`` rerun passes :data:`TUTORIAL_BREAK_ENTRY_QUERY` instead.
    Nothing else differs between the two runs: same bundle, same policy, same
    gate, same verifier.
    """

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.deployment import DeploymentConfig, PolicySection
    from openadapt_flow.execution_profiles import (
        ExecutionProfile,
        execution_profile_contract,
    )
    from openadapt_flow.run_gate import build_runtime_authorization, evaluate_run_gate
    from openadapt_flow.runtime import Replayer
    from openadapt_flow.runtime.effects import RestRecordVerifier

    if entry_query is None:
        entry_query = TUTORIAL_ENTRY_QUERY
    delay = _validated_presentation_delay(presentation_delay_s)
    _http_json(f"{base_url.rstrip('/')}/api/reset", method="POST", body={})
    entry_url = f"{base_url.rstrip('/')}/{entry_query}"

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
    replay_params = {
        "note": str(getattr(workflow, "params", {}).get("note", TUTORIAL_NOTE))
    }
    authorization = build_runtime_authorization(
        workflow,
        gate,
        approval_source="openadapt-flow-tutorial",
        params=replay_params,
    )

    backend, close = PlaywrightBackend.launch(entry_url, headless=not headed)
    try:
        replayer_type = _presentation_replayer_type(Replayer, delay)
        return replayer_type(
            backend,
            effect_verifier=verifier,
            governed_authorization=authorization,
            durable=True,
            require_settled=True,
        ).run(
            workflow.model_copy(deep=True),
            params=replay_params,
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
    interactive_record: bool = False,
    presentation_delay_s: float = 0.0,
    echo: Optional[Callable[[str], None]] = None,
    break_it: bool = False,
) -> TutorialResult:
    """Run the complete free path and return its evidence.

    Stages: serve -> record -> compile -> certify -> run (standard profile,
    independent effect verification) -> receipt.

    With ``break_it=True`` the SAME certified bundle is then rerun against a
    backend that injects the :data:`TUTORIAL_BREAK_FAULT` fault -- the server
    rejects the write after the application has already painted its success
    banner -- and the engine is expected to HALT rather than believe the
    screen.  The rerun's evidence lands in ``<work_dir>/run-broken`` and on
    :attr:`TutorialResult.break_it`.  If the engine does NOT halt, this
    function raises: an uncaught injected fault is a product failure, never a
    tutorial variant.

    Raises:
        TutorialError: a stage produced insufficient evidence.  Nothing here
            downgrades a requirement to make a stage pass.
    """

    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.mockmed.fault_server import serve
    from openadapt_flow.report import render_run_report

    say = echo or (lambda message: None)
    delay = _validated_presentation_delay(presentation_delay_s)
    root = Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    recording_dir = root / "recording"
    bundle_dir = root / "bundle"
    run_dir = root / "run"

    base_url, _db, stop = serve()
    try:
        visible = headed or interactive_record or delay > 0
        say("[1/5] Record the demonstration against a real persistence boundary")
        if interactive_record:
            record_tutorial_interactive(base_url, recording_dir)
        else:
            record_tutorial(
                base_url,
                recording_dir,
                headed=visible,
                presentation_delay_s=delay,
            )

        say("[2/5] Compile and propose a fixture-specific effect contract")
        workflow = compile_recording(
            recording_dir, bundle_dir, name=name, mine_effects=True
        )
        save = consequential_step(workflow)
        say(
            f"      {len(save.effects)} system-of-record effect proposal(s) "
            f"derived from the fixture's record delta on {save.id}; review "
            "is required for a real deployment"
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
            headed=visible,
            presentation_delay_s=delay,
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
            from openadapt_flow.receipt import _build_tutorial_receipt, write_receipt

            say("[5/5] Emit the local run receipt")
            receipt = _build_tutorial_receipt(report)
            result.receipt_paths = write_receipt(receipt, run_dir)

    if break_it:
        result.break_it = _run_break_it(
            workflow=workflow,
            bundle_dir=bundle_dir,
            run_dir=root / "run-broken",
            headed=headed,
            say=say,
        )
    return result


def _run_break_it(
    *,
    workflow: Any,
    bundle_dir: Path,
    run_dir: Path,
    headed: bool,
    say: Callable[[str], None],
) -> BreakItResult:
    """Rerun the certified bundle against a lying backend and prove the halt.

    The backend now runs in ``optimistic`` fault mode: it REJECTS the write,
    but only after the application has already painted its success banner.
    Every on-screen check therefore passes.  The engine's independent read of
    the system of record is the only thing standing between that screen and a
    claimed success -- and the run must end HALTED because of it.
    """

    from openadapt_flow.mockmed.fault_server import serve
    from openadapt_flow.report import render_run_report

    say("")
    say("[break-it] Rerun the SAME certified bundle, but this time the backend")
    say(f"[break-it] lies: fault mode {TUTORIAL_BREAK_FAULT!r} rejects the write")
    say("[break-it] AFTER the app has painted its success banner.")
    base_url, _db, stop = serve()
    try:
        report = run_tutorial_workflow(
            base_url=base_url,
            workflow=workflow,
            bundle_dir=bundle_dir,
            run_dir=run_dir,
            headed=headed,
            entry_query=TUTORIAL_BREAK_ENTRY_QUERY,
        )
        record_count = len(_records(base_url))
    finally:
        stop()
    report_path = render_run_report(run_dir)

    if report.execution_outcome != "HALTED":
        raise TutorialError(
            "the engine FAILED to catch the injected fault: the broken run "
            f"ended {report.execution_outcome} instead of HALTED. This is a "
            "product failure, not a tutorial variant; do not trust this build "
            "with a consequential write."
        )

    save_step = consequential_step(workflow)
    step_result = next(
        (result for result in report.results if result.step_id == save_step.id), None
    )
    refuted = sum(
        1
        for result in report.results
        for evidence in result.effect_evidence
        if evidence.final_verdict == "refuted"
    )
    claim_text: Optional[str] = None
    if report.halt is not None:
        claim_text = next(
            (text for text in report.halt.observed_texts if "saved" in text.lower()),
            None,
        )
    envelope = report.outcome_envelope
    say(
        f"[break-it] {report.execution_outcome}: the system of record holds "
        f"{record_count} record(s); the screen said otherwise."
    )
    return BreakItResult(
        run_dir=run_dir,
        report_path=report_path,
        fault=TUTORIAL_BREAK_FAULT,
        execution_outcome=str(report.execution_outcome),
        transaction_outcome=report.transaction_outcome,
        transaction_billable=report.transaction_billable,
        screen_claimed_success=bool(step_result and step_result.postconditions_ok),
        screen_claim_text=claim_text,
        effects_required=(
            int(envelope.required_contracts.effect) if envelope is not None else 0
        ),
        effects_refuted=refuted,
        halt_reason=(report.halt.reason if report.halt is not None else ""),
        system_of_record_records=record_count,
    )
