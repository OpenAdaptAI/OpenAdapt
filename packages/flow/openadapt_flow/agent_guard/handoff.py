"""Run 2: turn a guarded run 1 that read back correctly into a draft program.

The agent did the task once in its own browser; the guard recorded it with the
network capture and verify-run read the save back as ``VERIFIED``.
:func:`handoff` puts that run where the MCP teach flow (#1335) and
``compile-agent`` keep theirs, so the same people and the same rules take it
from there:

1. **Compile** the recording (distilled, as ``compile-agent`` does) into the
   task's ``program/bundle``. ``compile`` proposes an API step for the save
   when the captured form request replays cleanly (``api_proposal.json`` says
   which step, or why not); the GUI steps stay as the fallback, and the API
   step runs only after a person admits it (``openadapt-flow api admit``).
2. **Check run 1's record** the way ``compile-agent`` does
   (:func:`~openadapt_flow.compile_agent.pipeline.check_run_record`): exactly
   one new record, nothing else changed, the record the task named, every
   input in it. Otherwise the draft is set aside.
3. **Propose the record check** from the app's own reads with
   ``scaffold-verifier`` (:mod:`openadapt_flow.record_check_synthesis`): the
   reads the app made after the save, a database read path the operator gave
   (``teach.db``), or the ``verify.yaml`` read, fault-tested against the
   ``effect_e2e`` fault classes. When that check misses a fault it is kept
   next to the draft for review and the plain proposal from the before/after
   reads is proposed instead.
4. **Write the draft** as a JIT ``DRAFT`` signed as recorded by the attached
   agent, with no agent principal: only a person or a registered admitter
   key can admit it.

Nothing here admits anything. A person confirms the record check
(``teach_confirm_checks`` or ``compile-agent admit``), canaries run, and a
person (or an admitter key) admits. ``openadapt-agent serve --teach VERIFY
--teach-dir DIR`` then serves the admitted program as ``taught_<name>``, which
runs with no model calls and reads every save back.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Optional

from openadapt_flow.agent_guard.config import GuardConfig
from openadapt_flow.guard import GuardTask

__all__ = ["HandoffError", "HandoffResult", "handoff", "typed_inputs"]

HANDOFF_FILE = "agent_guard.json"
HANDOFF_SCHEMA = "openadapt.agent-guard-handoff/v1"
#: The dialect a recording made by the attached recorder is stamped with.
ATTACHED_DIALECT = "attached-browser"


class HandoffError(ValueError):
    """Run 1 can't become a draft. Nothing was written to the teach dir."""


@dataclass
class HandoffResult:
    status: str
    task_dir: Path
    task_key: str
    detail: Optional[str] = None
    inputs: list[str] = field(default_factory=list)
    expect: dict[str, str] = field(default_factory=dict)
    record_check: Optional[dict[str, Any]] = None
    proposed_check: Optional[dict[str, Any]] = None
    api_step: Optional[dict[str, Any]] = None
    next_steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        out["task_dir"] = str(self.task_dir)
        return out


def _events(recording: Path) -> list[dict[str, Any]]:
    path = recording / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line]


def typed_inputs(recording: Path, task: GuardTask) -> list[str]:
    """The task's ``intended_fields`` whose whole value the agent typed."""

    typed = {
        str(event.get("text"))
        for event in _events(recording)
        if event.get("kind") == "type" and event.get("text")
    }
    return sorted(
        name for name, value in task.intended_fields.items() if str(value) in typed
    )


def _record_match(cfg: GuardConfig) -> dict[str, Any]:
    """The match of the one ``record_written`` effect in ``verify.yaml``."""

    from openadapt_flow.verify_run import load_verify_config

    config = load_verify_config(cfg.verify)
    written = [
        e
        for e in config.effects
        if getattr(e.kind, "value", e.kind) == "record_written"
        and int(e.expected_count) > 0
    ]
    if len(written) != 1:
        raise HandoffError(
            "verify.yaml must claim exactly one new record (a record_written "
            "effect) for run 1 to become a program"
        )
    return dict(written[0].match)


def _expect(
    match: Mapping[str, Any], task: GuardTask, inputs: list[str]
) -> dict[str, str]:
    """The record's fields every run writes the same: literals in the check,
    and task values that aren't inputs."""

    expect: dict[str, str] = {}
    for fld, expr in match.items():
        param = getattr(expr, "param", None)
        literal = getattr(expr, "literal", None)
        if param:
            if param not in inputs and param in task.params:
                expect[fld] = str(task.params[param])
        elif literal is not None:
            expect[fld] = str(literal)
    return expect


def _instruction(task: GuardTask, inputs: list[str], given: Optional[str]) -> str:
    if given:
        return given
    record = ", ".join(f"{k} {v}" for k, v in sorted(task.expected_record.items()))
    values = ", ".join(f"{name} {{{name}}}" for name in inputs)
    return f"On the record {record}, save {values}"


def _pin(node: Any, fixed: Mapping[str, str]) -> Any:
    """``{param: name}`` -> ``{literal: value}``, and ``{name}`` in a path
    template -> the value, for each fixed value in a verifier section (reads
    scoped by the record's identity)."""

    if isinstance(node, dict):
        if set(node) == {"param"} and node["param"] in fixed:
            return {"literal": fixed[node["param"]]}
        return {k: _pin(v, fixed) for k, v in node.items()}
    if isinstance(node, list):
        return [_pin(v, fixed) for v in node]
    if isinstance(node, str):
        # A read's path template (``/patients/{patient_id}/notes``).
        from urllib.parse import quote

        for name, value in fixed.items():
            node = node.replace("{" + name + "}", quote(value, safe=""))
    return node


def _propose_from_reads(
    recording: Path,
    cfg: GuardConfig,
    values: Mapping[str, str],
    fixed: Mapping[str, str],
    out_dir: Path,
) -> tuple[Optional[str], dict[str, Any]]:
    """scaffold-verifier's check from the app's reads. Returns the rendered
    file when it caught every fault, and what it found either way.

    ``fixed`` are the task values every run writes the same (the record's
    identity): scaffold-verifier finds the record by them too, and the check
    pins them as literals, so the program can only save on that record.
    """

    import yaml

    from openadapt_flow import record_check_synthesis as rcs

    teach = cfg.teach
    assert teach is not None
    section = (yaml.safe_load(cfg.verify.read_text("utf-8")) or {}).get("verifier")
    try:
        proposal = rcs.synthesize(
            recording,
            params={**fixed, **values},
            db_path=teach.db,
            verifier=section if isinstance(section, dict) else None,
            complete_read=teach.complete_read,
        )
    except rcs.SynthesisError as exc:
        return None, {"source": "scaffold-verifier", "working": False, "why": str(exc)}
    report: dict[str, Any] = {
        "source": "scaffold-verifier",
        "working": proposal.working,
        "reads": [r["source"] for r in proposal.proof()["reads"]],
        "faults_caught": sum(1 for r in proposal.faults if r.ok and not r.correct),
        "faults": sum(1 for r in proposal.faults if not r.correct),
        "misses": proposal.misses,
    }
    if not proposal.effects:
        return None, report
    from openadapt_flow.runtime.effects.effect import ValueExpr

    proposal.effects = [
        effect.model_copy(
            update={
                "match": {
                    fld: (
                        ValueExpr(literal=fixed[expr.param])
                        if expr.param is not None and expr.param in fixed
                        else expr
                    )
                    for fld, expr in effect.match.items()
                }
            }
        )
        for effect in proposal.effects
    ]
    proposal.verifier = _pin(proposal.verifier, fixed)
    check, proof = rcs.write(proposal, out_dir / "scaffold")
    report["file"] = str(check)
    report["proof"] = str(proof)
    return (rcs.render(proposal) if proposal.working else None), report


def handoff(
    recording: Path | str,
    *,
    cfg: GuardConfig,
    task: GuardTask,
) -> HandoffResult:
    """Make run 1 a draft in ``cfg.teach.dir`` (see the module docstring).

    ``recording`` is the guard's run-1 recording; call this only for a run the
    guard read back as ``VERIFIED``.
    """

    from openadapt_flow.compile_agent.pipeline import (
        PROPOSED_FILE,
        FirstRunCompiler,
        _write_json,
        check_run_record,
    )
    from openadapt_flow.compiler.compile import compile_recording
    from openadapt_flow.compiler.distill import distill_recording
    from openadapt_flow.record_sor import SNAPSHOTS_NAME, _write_private

    teach = cfg.teach
    if teach is None:
        raise HandoffError("guard.yaml has no teach: section")
    source = Path(recording)
    if not (source / "events.jsonl").is_file():
        raise HandoffError(f"{source} is not a recording")
    inputs = (
        sorted(teach.inputs) if teach.inputs is not None else typed_inputs(source, task)
    )
    unknown = sorted(set(inputs) - set(task.intended_fields))
    if unknown:
        raise HandoffError(f"inputs {unknown} are not intended_fields of the task")
    expect = _expect(_record_match(cfg), task, inputs)
    if not expect:
        raise HandoffError(
            "the check names no field every run writes the same (such as the "
            "patient id); a program for any record is not made"
        )
    values = {name: str(task.intended_fields[name]) for name in inputs}
    compiler = FirstRunCompiler(
        _instruction(task, inputs, teach.instruction),
        params=inputs,
        url=cfg.app_url,
        verify=cfg.verify,
        expect=expect,
        on_screen=[str(v) for v in task.expected_record.values()],
        task_id=teach.name,
        policy=teach.policy,
        cache_dir=teach.dir,
        key_dir=teach.key_dir,
    )
    jit = compiler._jit()
    if jit.admitted or jit.candidate() is not None:
        raise HandoffError(
            f"{compiler.dir} already has a draft or an admitted program; reject "
            "the draft to hand a new run over"
        )
    compiler.save_spec()
    program = jit.program_dir
    if program.exists():
        shutil.rmtree(program)
    program.mkdir(parents=True)
    kept = program / "recording"
    shutil.copytree(source, kept)
    for stale in (PROPOSED_FILE, "verify.yaml", "record_checks.json", "admission.json"):
        (compiler.out_dir / stale).unlink(missing_ok=True)

    # 1. Compile, as compile-agent does: distilled, effects mined from the
    # record reads the recording kept.
    distilled = program / "recording-distilled"
    report = distill_recording(kept, distilled)
    # The recorder kept record reads when verify.yaml's read works (as
    # compile-agent's record_reader does): mine the effect contract from them.
    mine = (kept / SNAPSHOTS_NAME).is_file()
    workflow = compile_recording(
        distilled, jit.bundle_dir, name=compiler.task.task_id, mine_effects=mine
    )
    actions = len(_events(kept))
    # _write_draft reads only these attributes of the capture session and
    # computer the JIT's own recording would pass.
    shim_session: Any = SimpleNamespace(
        recording_dir=kept, compiled_recording_dir=distilled
    )
    shim_computer: Any = SimpleNamespace(
        actions=actions,
        log=[{"source": "agent_guard", "recorded_events": actions}],
    )
    compiled = {
        "workflow_id": f"wf_{workflow.recording_id or compiler.task.key}",
        "distilled": {
            "steps_in": report.steps_in,
            "steps_out": report.steps_out,
            "dropped": [
                {"index": d.index, "kind": d.kind, "rule": d.rule}
                for d in report.dropped
            ],
        },
    }
    jit._write_draft(
        shim_session, compiled, shim_computer, dict(values), ATTACHED_DIALECT
    )
    api_step = None
    api_file = jit.bundle_dir / "api_proposal.json"
    if api_file.is_file():
        api_step = json.loads(api_file.read_text("utf-8"))

    def finish(status: str, detail: Optional[str], **kw: Any) -> HandoffResult:
        result = HandoffResult(
            status=status,
            task_dir=compiler.dir,
            task_key=compiler.task.key,
            detail=detail,
            inputs=inputs,
            expect=expect,
            api_step=api_step,
            **kw,
        )
        _write_json(
            compiler.dir / HANDOFF_FILE,
            {
                "schema": HANDOFF_SCHEMA,
                "guard_task_id": task.task_id,
                **result.to_dict(),
            },
        )
        return result

    # 2. Run 1's record check, from the reads before and after it.
    snapshots_path = kept / SNAPSHOTS_NAME
    snapshots = (
        json.loads(snapshots_path.read_text("utf-8"))
        if snapshots_path.is_file()
        else {}
    )
    before = (snapshots.get("before") or {}).get("records")
    after = (snapshots.get("after") or {}).get("records")
    check = check_run_record(before, after, expect=expect, values=values)
    if not check.passed:
        kept_at = compiler._set_aside(jit, "record_check_failed", check.reasons)
        return finish(
            "record_check_failed",
            "; ".join(check.reasons) + f" (draft set aside at {kept_at})",
            record_check=check.to_dict(),
        )

    # 3. The proposed record check: the app's own reads first.
    fixed = {
        str(expr.param): str(task.params[expr.param])
        for expr in _record_match(cfg).values()
        if getattr(expr, "param", None)
        and expr.param not in inputs
        and expr.param in task.params
    }
    rendered, found = _propose_from_reads(kept, cfg, values, fixed, compiler.out_dir)
    if rendered is not None:
        compiler.out_dir.mkdir(parents=True, exist_ok=True)
        _write_private(compiler.out_dir / PROPOSED_FILE, rendered)
        found["proposed"] = True
    else:
        compiler._propose(before or [], after or [], values)
        found["proposed"] = False
        found["fallback"] = "the before/after reads (record --verify's proposer)"
    found["proposed_file"] = str(compiler.out_dir / PROPOSED_FILE)
    params = " ".join(f"--canary-param {p}=<safe value>" for p in inputs)
    return finish(
        "draft",
        "run 1 read back correctly; a person confirms the record check, then admits",
        record_check=check.to_dict(),
        proposed_check=found,
        next_steps=[
            f"openadapt-flow compile-agent admit {compiler.dir} --admit-as "
            f"'<your name>' {params}",
            f"openadapt-agent serve --teach {cfg.verify} --teach-dir {teach.dir} "
            "(serves the admitted program as a tool)",
        ],
    )
