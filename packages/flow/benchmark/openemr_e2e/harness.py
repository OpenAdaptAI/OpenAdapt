"""Integrated OpenEMR end-to-end proof harness (compiled arm; agent arm gated off).

One entry point -- :func:`run_openemr_e2e` -- that ties the WHOLE compiled
runtime pipeline together against the OpenEMR add-patient-note flagship task,
reproducibly and for $0:

    compile -> replay -> effect-verify -> (silent-wrong-write catch) ->
    inject drift -> HALT -> teach the fix -> re-run clean

Unlike the existing OpenEMR *benchmark*
(:mod:`openadapt_flow.benchmark.openemr_benchmark`), which measures the compiled
arm vs. a paid agent arm on the LIVE public demo and is explicitly NOT
CI-reproducible, this harness proves the pipeline *wiring* end to end on a
deterministic, offline fixture (the in-process MockMed ``fault_server`` as the
system of record) so CI can run the whole loop on every push. It does not
duplicate the benchmark; it orchestrates the runtime components the benchmark
assumes already work, and asserts they compose.

Live vs. fixture (never a silent skip)
--------------------------------------
The end-to-end loop ALWAYS runs on the fixture substrate -- that is the
CI-reproducible proof, and every result is labelled ``substrate="fixture"``.
When ``OPENEMR_FHIR_BASE_URL`` is set the harness additionally probes the REAL
OpenEMR FHIR system of record for reachability (a genuine
``FhirEffectVerifier.capture_pre_state`` against live state) and records the
outcome honestly under ``live_probe``. With ``require_live=True`` an unreachable
live SoR is a hard error, never a quiet pass.

Cost guardrail (do not violate)
-------------------------------
The compiled arm is model-free: ZERO API calls, ``cost_usd == 0.0``, enforced
by construction (no client is ever constructed). The paid computer-use agent
arm -- the money-spending comparison -- is wired ONLY as a gate: it requires an
explicit opt-in plus a hard per-run USD cap, and even then THIS harness refuses
to invoke it, pointing at ``scripts/openemr_demo.py benchmark`` (the audited
paid path with full cost guardrails). The head-to-head ratio is reported from
PREVIOUSLY-recorded numbers (``benchmark/openemr/results.json``), never by
spending money now. See :func:`agent_arm_gate`.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from benchmark.openemr_e2e import simulation as sim
from openadapt_flow.learning import (
    ExecutionTrace,
    SkillLibrary,
    TraceStep,
    execution_trace_from_halt,
    learn_from_halt,
    promoted_workflow,
    resolution_demonstration,
)
from openadapt_flow.learning.synth_stream import StructuralDiffInducer
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.effects import RestRecordVerifier
from openadapt_flow.runtime.replayer import Replayer

#: Distinct per-phase note values (unique so a pass proves parameter
#: substitution against live state, not replay of a baked-in literal).
NOTE_CLEAN = "Renal panel ordered ahead of the next quarterly visit."
NOTE_PARTIAL = "Low-sodium meal plan handout given and explained."
NOTE_DRIFT = "Walking program begun, thirty minutes on weekday mornings."
NOTE_RERUN = "Home blood-pressure log shows stable readings all month."

#: Where the previously-recorded head-to-head numbers live (NEVER regenerated
#: here -- regenerating them costs money; see the module docstring).
_REPO_ROOT = Path(__file__).resolve().parents[2]
RECORDED_RATIO_PATH = _REPO_ROOT / "benchmark" / "openemr" / "results.json"


class AgentArmRefused(RuntimeError):
    """Raised when the paid agent arm is requested; it is never invoked here."""


def agent_arm_gate(*, enable: bool, max_cost_usd: Optional[float]) -> None:
    """Validate the paid-agent-arm opt-in -- then refuse to run it.

    Standing policy: this harness never spends money. The agent arm exists only
    as a gated, capped comparison path. This gate enforces the wiring the policy
    requires (an explicit opt-in AND a hard USD cap) and then refuses regardless,
    so a caller cannot accidentally bill an API from the end-to-end harness. The
    audited paid path is ``scripts/openemr_demo.py benchmark`` (per-run + total
    cost caps, preflight, billing-error abort).

    Args:
        enable: The explicit ``--agent-arm`` opt-in.
        max_cost_usd: The mandatory hard per-run cost cap. Required whenever
            ``enable`` is set.

    Raises:
        ValueError: ``enable`` set without a positive ``max_cost_usd`` cap.
        AgentArmRefused: whenever ``enable`` is set (the arm is never invoked).
    """
    if not enable:
        return
    if max_cost_usd is None or max_cost_usd <= 0:
        raise ValueError(
            "the paid agent arm requires a hard --max-cost-usd cap (> 0); "
            "refusing to consider it without one"
        )
    raise AgentArmRefused(
        "the paid computer-use agent arm is disabled in this end-to-end "
        f"harness by standing policy (a ${max_cost_usd:.2f}/run cap was "
        "supplied, but no API is ever called here). Run the audited paid "
        "benchmark instead: `python scripts/openemr_demo.py benchmark` "
        "(per-run + total cost caps, preflight, billing-error abort)."
    )


def recorded_ratio(path: Path | str = RECORDED_RATIO_PATH) -> Optional[dict[str, Any]]:
    """Load the previously-recorded compiled-vs-agent numbers (no spend).

    Reads the committed OpenEMR benchmark ``results.json`` and returns a small
    ratio summary for the harness to print. Returns None (not an error) when the
    file is absent -- the harness still proves the compiled loop.
    """
    p = Path(path)
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    arms = data.get("arms", {})
    c, a = arms.get("compiled", {}), arms.get("agent", {})
    agent_cost = a.get("cost_usd_per_run") or 0.0
    return {
        "source": str(p.relative_to(_REPO_ROOT))
        if p.is_relative_to(_REPO_ROOT)
        else str(p),
        "generated_at": data.get("generated_at"),
        "target": data.get("target"),
        "compiled": {
            "n": c.get("n"),
            "success_count": c.get("success_count"),
            "cost_usd_per_run": c.get("cost_usd_per_run", 0.0),
        },
        "agent": {
            "n": a.get("n"),
            "success_count": a.get("success_count"),
            "cost_usd_per_run": agent_cost,
        },
        "framing": (
            f"compiled {c.get('success_count')}/{c.get('n')} at $0/run vs "
            f"agent {a.get('success_count')}/{a.get('n')} at "
            f"${agent_cost:.4f}/run (recorded on the live demo; not spent now)"
        ),
    }


# -- phase bookkeeping --------------------------------------------------------


@dataclass
class Phase:
    """One end-to-end phase's outcome."""

    name: str
    passed: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)
    wall_s: float = 0.0


@dataclass
class E2EResult:
    """The full harness result (also serialized to ``result.json``)."""

    substrate: str
    task: str
    passed: bool
    phases: list[Phase]
    cost_usd: float
    model_calls: int
    agent_arm: dict[str, Any]
    recorded_ratio: Optional[dict[str, Any]]
    live_probe: Optional[dict[str, Any]]
    generated_at: str
    total_wall_s: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "substrate": self.substrate,
            "task": self.task,
            "passed": self.passed,
            "cost_usd": self.cost_usd,
            "model_calls": self.model_calls,
            "agent_arm": self.agent_arm,
            "recorded_ratio": self.recorded_ratio,
            "live_probe": self.live_probe,
            "generated_at": self.generated_at,
            "total_wall_s": self.total_wall_s,
            "phases": [
                {
                    "name": p.name,
                    "passed": p.passed,
                    "detail": p.detail,
                    "wall_s": p.wall_s,
                    **p.data,
                }
                for p in self.phases
            ],
        }


# -- the pipeline -------------------------------------------------------------


def _replay(
    workflow: Any,
    *,
    bundle_dir: Path,
    run_dir: Path,
    note: str,
    fault: str = "",
    modal: Optional[str] = None,
) -> tuple[Any, sim.SimBackend, dict[str, Any]]:
    """Drive one REAL ``Replayer.run`` against a fresh fixture system of record.

    A fresh ``fault_server`` per replay keeps the record-written count exact and
    isolates phases. Returns ``(RunReport, backend, sor_snapshot)``.
    """
    url, db, stop = fault_serve()
    try:
        backend = sim.SimBackend(url, fault=fault)
        vision = sim.AddNoteVision(backend, modal_text=modal)
        replayer = Replayer(
            backend,
            vision=vision,
            effect_verifier=RestRecordVerifier(url.rstrip("/")),
            poll_interval_s=0.01,
        )
        report = replayer.run(
            workflow,
            params={sim.NOTE_PARAM: note},
            bundle_dir=bundle_dir,
            run_dir=run_dir,
        )
        return report, backend, db.snapshot()
    finally:
        stop()


def _timed(fn: Callable[[], Phase]) -> Phase:
    start = time.monotonic()
    phase = fn()
    phase.wall_s = time.monotonic() - start
    return phase


def _live_probe(
    *, require_live: bool, log: Callable[[str], None]
) -> Optional[dict[str, Any]]:
    """Probe the REAL OpenEMR FHIR system of record for reachability.

    Genuine ``FhirEffectVerifier.capture_pre_state`` against live state when
    ``OPENEMR_FHIR_BASE_URL`` is set; never fabricated. Returns None when no live
    endpoint is configured (the fixture loop still ran and is labelled as such).
    """
    base = os.environ.get("OPENEMR_FHIR_BASE_URL")
    if not base:
        if require_live:
            raise RuntimeError(
                "require_live=True but OPENEMR_FHIR_BASE_URL is not set -- "
                "refusing to report a live result the harness did not obtain"
            )
        return None
    from openadapt_flow.runtime.effects import FhirEffectVerifier

    token = os.environ.get("OPENEMR_FHIR_TOKEN")
    verifier = FhirEffectVerifier(base.rstrip("/"), access_token=token)
    try:
        state = verifier.capture_pre_state()
        reachable = bool(state.reachable)
        detail = f"reachable={reachable}, records_observed={len(state.records)}"
    except Exception as exc:  # noqa: BLE001 - reachability is data, not a crash
        reachable, detail = False, f"{type(exc).__name__}: {exc}"
    log(f"LIVE PROBE ({base}): {detail}")
    if require_live and not reachable:
        raise RuntimeError(
            f"require_live=True but live OpenEMR SoR unreachable: {detail}"
        )
    return {"base_url": base, "reachable": reachable, "detail": detail}


def run_openemr_e2e(
    out_dir: Path | str,
    *,
    enable_agent_arm: bool = False,
    max_cost_usd: Optional[float] = None,
    require_live: bool = False,
    log: Callable[[str], None] = print,
) -> E2EResult:
    """Run the integrated OpenEMR add-patient-note pipeline end to end ($0).

    Phases, each a REAL runtime call, all deterministic and model-free:

    1. **compile** -- materialize the add-note demonstration as a compiled
       bundle (``workflow.json`` + templates) with the Save step's
       system-of-record effect contracts.
    2. **clean replay + effect-verify** -- replay against a clean SoR; the note
       write is CONFIRMED against the record and the run succeeds.
    3. **silent-wrong-write catch** -- replay against a ``partial`` fault (the
       screen paints "Saved" but the note is dropped in the SoR); effect
       verification REFUTES and the run HALTS where screen-only checking passes.
    4. **inject drift -> HALT** -- replay with an unexpected consent modal; the
       never-demonstrated confirm step cannot resolve and the run HALTS,
       emitting a learnable ``HaltObservation``.
    5. **teach** -- feed the operator's dismiss-then-confirm correction to the
       GOVERNED learn/promote loop, which induces a guarded branch, gates it
       (identity/effect/risk may not regress), and promotes on improved held-out
       coverage.
    6. **re-run clean** -- replay the SAME drift through the taught program: it
       now dismisses the modal and completes; a clean run skips the new branch,
       proving no regression.

    Args:
        out_dir: Directory for ``result.json`` and ``SUMMARY.md`` (and the
            compiled bundle + per-phase run artifacts).
        enable_agent_arm: The paid-agent-arm opt-in. Validated by
            :func:`agent_arm_gate`, which then REFUSES to run it (no spend).
        max_cost_usd: Mandatory hard per-run cap when ``enable_agent_arm`` is
            set. The arm is still never invoked.
        require_live: Fail hard if a live OpenEMR SoR is requested but
            unreachable (never a silent pass). The fixture loop runs regardless.
        log: Progress logger.

    Returns:
        The :class:`E2EResult` (also written to ``out_dir/result.json``).

    Raises:
        ValueError / AgentArmRefused: from :func:`agent_arm_gate`.
        RuntimeError: from the live probe under ``require_live``.
    """
    agent_arm_gate(enable=enable_agent_arm, max_cost_usd=max_cost_usd)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    bundle_dir = out / "bundle"
    runs = out / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    task = (
        "OpenEMR add-patient-note: open patient messages, enter a "
        "parameterized note, save, confirm -- verified against the system "
        "of record, drift-halted, taught, and re-run clean."
    )

    phases: list[Phase] = []

    # -- Phase 1: compile ---------------------------------------------------
    def _compile() -> Phase:
        workflow = sim.write_bundle(bundle_dir)
        assert workflow.program is not None
        n_states = sum(
            1 for s in workflow.program.states.values() if s.step is not None
        )
        save = workflow.program.states["s_save"].step
        n_effects = len(save.effects) if save else 0
        ok = (bundle_dir / "workflow.json").exists() and n_effects == 2
        return Phase(
            "compile",
            ok,
            f"compiled bundle written ({n_states} action steps, "
            f"{n_effects} system-of-record effects on Save)",
            {
                "bundle_dir": str(bundle_dir),
                "action_steps": n_states,
                "effects": n_effects,
            },
        )

    p1 = _timed(_compile)
    phases.append(p1)
    log(f"[1/6] compile: {'PASS' if p1.passed else 'FAIL'} -- {p1.detail}")
    workflow = sim.write_bundle(bundle_dir)  # reload for downstream phases

    # -- Phase 2: clean replay + effect-verify ------------------------------
    def _clean() -> Phase:
        r, be, snap = _replay(
            workflow,
            bundle_dir=bundle_dir,
            run_dir=runs / "clean",
            note=NOTE_CLEAN,
        )
        save = next((x for x in r.results if x.step_id == "s_save"), None)
        confirmed = bool(save and save.effect_verified) and all(
            "CONFIRMED" in line for line in (save.effect_results if save else [])
        )
        rec = snap["records"][0] if snap["records"] else {}
        ok = r.success and confirmed and rec.get("note") == NOTE_CLEAN
        return Phase(
            "clean_replay_effect_verify",
            ok,
            "clean replay succeeded; note write CONFIRMED against the "
            f"system of record ({len(snap['records'])} record)"
            if ok
            else f"unexpected: success={r.success} confirmed={confirmed}",
            {
                "run_success": r.success,
                "effect_verified": bool(save and save.effect_verified),
                "effect_results": save.effect_results if save else [],
                "sor_note": rec.get("note"),
                "model_calls": r.model_calls,
            },
        )

    p2 = _timed(_clean)
    phases.append(p2)
    log(
        f"[2/6] clean replay + effect-verify: {'PASS' if p2.passed else 'FAIL'} -- {p2.detail}"
    )

    # -- Phase 3: silent-wrong-write catch ----------------------------------
    def _catch() -> Phase:
        r, be, snap = _replay(
            workflow,
            bundle_dir=bundle_dir,
            run_dir=runs / "partial",
            note=NOTE_PARTIAL,
            fault="partial",
        )
        save = next((x for x in r.results if x.step_id == "s_save"), None)
        refuted = save is not None and any(
            "REFUTED" in line for line in save.effect_results
        )
        rec = snap["records"][0] if snap["records"] else {}
        # The screen painted "Saved" (postcondition passed) yet the record's
        # note is empty -- the exact silent wrong-write the record check exists
        # to catch.
        screen_said_saved = bool(save and save.postconditions_ok)
        ok = (
            (not r.success)
            and r.terminal_outcome == "halt"
            and refuted
            and rec.get("note") == ""
        )
        return Phase(
            "silent_wrong_write_catch",
            ok,
            "screen showed 'Saved' but the record dropped the note -- effect "
            "verification REFUTED and the run HALTED (screen-only checking "
            "would have passed)"
            if ok
            else f"unexpected: success={r.success} refuted={refuted} note={rec.get('note')!r}",
            {
                "run_success": r.success,
                "terminal_outcome": r.terminal_outcome,
                "screen_postcondition_ok": screen_said_saved,
                "effect_results": save.effect_results if save else [],
                "sor_note": rec.get("note"),
                "halt_intent": getattr(r.halt, "intent", None),
            },
        )

    p3 = _timed(_catch)
    phases.append(p3)
    log(
        f"[3/6] silent-wrong-write catch: {'PASS' if p3.passed else 'FAIL'} -- {p3.detail}"
    )

    # -- Phase 4: inject drift -> HALT --------------------------------------
    def _drift() -> Phase:
        r, be, snap = _replay(
            workflow,
            bundle_dir=bundle_dir,
            run_dir=runs / "drift",
            note=NOTE_DRIFT,
            modal=sim.CONSENT_MODAL_FACT,
        )
        halt = r.halt
        ok = (
            (not r.success)
            and r.terminal_outcome == "halt"
            and halt is not None
            and halt.intent == sim.INTENT_CONFIRM
            and sim.CONSENT_MODAL_FACT in halt.observed_texts
            and sim.INTENT_SAVE in halt.completed_intents
        )
        # store the halt report for the teach phase
        _drift.report = r  # type: ignore[attr-defined]
        return Phase(
            "inject_drift_halt",
            ok,
            "an unexpected consent modal blocked the confirm step; the "
            "workflow HALTED and emitted a learnable halt observation "
            f"(observed {halt.observed_texts if halt else []})"
            if ok
            else f"unexpected: success={r.success} halt={halt}",
            {
                "run_success": r.success,
                "terminal_outcome": r.terminal_outcome,
                "halt_intent": getattr(halt, "intent", None),
                "halt_observed_texts": list(halt.observed_texts) if halt else [],
                "halt_completed_intents": list(halt.completed_intents) if halt else [],
            },
        )

    p4 = _timed(_drift)
    phases.append(p4)
    log(f"[4/6] inject drift -> HALT: {'PASS' if p4.passed else 'FAIL'} -- {p4.detail}")
    halt_report = getattr(_drift, "report", None)

    # -- Phase 5: teach the fix (governed learn/promote) --------------------
    learned = None

    def _teach() -> Phase:
        nonlocal learned
        if halt_report is None:
            return Phase("teach", False, "no halt report from the drift phase", {})
        library = SkillLibrary(out / "skills")
        library.create_skill(sim.SKILL_ID, sim.build_add_note_program())
        halt_trace = execution_trace_from_halt(halt_report, trace_id="probe")
        # A prior clean run of the skill (the deployment already has these);
        # the loop needs one to isolate the branch-guard fact.
        clean_baseline = ExecutionTrace(
            trace_id=f"{sim.SKILL_ID}-clean",
            outcome="success",
            steps=[
                TraceStep(intent=i)
                for i in (
                    sim.INTENT_OPEN,
                    sim.INTENT_NOTE,
                    sim.INTENT_SAVE,
                    sim.INTENT_CONFIRM,
                )
            ],
        )
        correction = resolution_demonstration(
            halt_trace,
            resolution_steps=[
                TraceStep(intent=sim.INTENT_DISMISS, action=sim.ActionKind.CLICK)
            ],
            tail_intents=(sim.INTENT_CONFIRM,),
            trace_id=f"{sim.SKILL_ID}-correction",
        )
        outcome, _ = learn_from_halt(
            library,
            sim.SKILL_ID,
            halt_report=halt_report,
            correction=correction,
            inducer=StructuralDiffInducer(),
            baseline=[clean_baseline],
        )
        if outcome.action == "promoted":
            learned = promoted_workflow(library, sim.SKILL_ID, name=sim.SKILL_ID)
        return Phase(
            "teach",
            outcome.action == "promoted",
            f"governed learn loop: {outcome.action} "
            f"(held-out coverage {outcome.coverage_before:.2f} -> {outcome.coverage_after:.2f}); "
            f"{outcome.reason}",
            {
                "action": outcome.action,
                "coverage_before": outcome.coverage_before,
                "coverage_after": outcome.coverage_after,
                "gate_passed": bool(outcome.gate and outcome.gate.passed),
            },
        )

    p5 = _timed(_teach)
    phases.append(p5)
    log(f"[5/6] teach the fix: {'PASS' if p5.passed else 'FAIL'} -- {p5.detail}")

    # -- Phase 6: re-run clean (taught program) -----------------------------
    def _rerun() -> Phase:
        if learned is None:
            return Phase("rerun_clean", False, "teach did not promote a program", {})
        r_drift, be_d, snap_d = _replay(
            learned,
            bundle_dir=bundle_dir,
            run_dir=runs / "rerun_drift",
            note=NOTE_RERUN,
            modal=sim.CONSENT_MODAL_FACT,
        )
        r_clean, be_c, snap_c = _replay(
            learned,
            bundle_dir=bundle_dir,
            run_dir=runs / "rerun_clean",
            note=NOTE_CLEAN,
        )
        skipped = [x.step_id for x in r_clean.results if x.skipped]
        dismissed = any(
            a[0] == "click" and abs(a[1] - 100) <= 15 and abs(a[2] - 100) <= 15
            for a in be_d.actions
        )
        ok = r_drift.success and dismissed and r_clean.success and bool(skipped)
        return Phase(
            "rerun_clean",
            ok,
            "taught program re-ran the SAME drift to success (dismissed the "
            "modal, confirmed) and a clean run skipped the new branch -- no "
            "regression"
            if ok
            else f"unexpected: drift_success={r_drift.success} clean_success={r_clean.success}",
            {
                "rerun_drift_success": r_drift.success,
                "modal_dismissed": dismissed,
                "rerun_clean_success": r_clean.success,
                "clean_skipped_branch": skipped,
            },
        )

    p6 = _timed(_rerun)
    phases.append(p6)
    log(f"[6/6] re-run clean: {'PASS' if p6.passed else 'FAIL'} -- {p6.detail}")

    # -- live-vs-fixture labelling (never a silent skip) --------------------
    live = _live_probe(require_live=require_live, log=log)
    substrate = "fixture" if live is None else "fixture (+live SoR reachability probe)"

    result = E2EResult(
        substrate=substrate,
        task=task,
        passed=all(p.passed for p in phases),
        phases=phases,
        cost_usd=0.0,
        model_calls=sum(p.data.get("model_calls", 0) for p in phases),
        agent_arm={
            "invoked": False,
            "policy": "gated off; never invoked in this harness",
            "paid_path": "scripts/openemr_demo.py benchmark",
        },
        recorded_ratio=recorded_ratio(),
        live_probe=live,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        total_wall_s=time.monotonic() - started,
    )
    write_artifacts(result, out)
    log(
        f"DONE: {'ALL PHASES PASSED' if result.passed else 'SOME PHASES FAILED'} "
        f"on substrate={result.substrate}; cost=${result.cost_usd:.2f}, "
        f"model_calls={result.model_calls}. Wrote {out / 'result.json'} and SUMMARY.md"
    )
    return result


# -- artifacts ----------------------------------------------------------------


def render_summary(result: E2EResult) -> str:
    """Render the human-readable ``SUMMARY.md`` from the result."""
    rows = "\n".join(
        f"| {i + 1} | {p.name} | {'PASS' if p.passed else 'FAIL'} | "
        f"{p.wall_s * 1000:.0f} ms | {p.detail} |"
        for i, p in enumerate(result.phases)
    )
    ratio = result.recorded_ratio
    ratio_block = (
        f"\n## Compiled vs. agent (recorded, not spent now)\n\n"
        f"> {ratio['framing']}\n\n"
        f"Source: `{ratio['source']}` (generated {ratio.get('generated_at', '?')[:10]}). "
        "The paid agent arm is NOT run by this harness; these numbers come from "
        "the audited live benchmark.\n"
        if ratio
        else ""
    )
    live = result.live_probe
    live_block = (
        f"\n## Live vs. fixture\n\n"
        f"The end-to-end loop ran on substrate **{result.substrate}**. "
        + (
            f"A live OpenEMR FHIR system-of-record probe was attempted at "
            f"`{live['base_url']}`: reachable={live['reachable']} ({live['detail']}).\n"
            if live
            else "No live OpenEMR endpoint was configured "
            "(`OPENEMR_FHIR_BASE_URL` unset), so only the CI-reproducible "
            "fixture loop ran -- and is labelled as such, never a silent pass.\n"
        )
    )
    return f"""# OpenEMR end-to-end proof -- integrated harness (compiled arm)

Generated: {result.generated_at}. Substrate: **{result.substrate}**.
Result: **{"ALL PHASES PASSED" if result.passed else "SOME PHASES FAILED"}**.
Model calls: **{result.model_calls}**. Cost: **${result.cost_usd:.2f}**.

**Task:** {result.task}

This harness ties the whole compiled runtime pipeline together against the
OpenEMR add-patient-note flagship task and asserts the components compose:
compile -> replay -> effect-verify against the system of record -> catch a
silent wrong write -> HALT on injected drift -> teach the fix (governed
learn/promote) -> re-run clean. Every phase is a real runtime call; the whole
loop is deterministic, offline (in-process MockMed `fault_server` as the system
of record), and makes zero model calls.

## Phases

| # | phase | result | wall | detail |
|---|---|---|---|---|
{rows}
{ratio_block}{live_block}
## Cost guardrail

The compiled arm is model-free by construction (no API client is ever
constructed): **$0**, {result.model_calls} model calls. The paid computer-use
agent arm is wired only as a gate -- it requires an explicit opt-in AND a hard
per-run USD cap, and even then this harness refuses to invoke it, pointing at
the audited paid benchmark (`{result.agent_arm["paid_path"]}`). The head-to-head
ratio above is quoted from previously-recorded numbers, never spent now.
"""


def write_artifacts(result: E2EResult, out_dir: Path | str) -> None:
    """Write ``result.json`` and ``SUMMARY.md`` into ``out_dir``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(json.dumps(result.to_dict(), indent=2) + "\n")
    (out / "SUMMARY.md").write_text(render_summary(result))
