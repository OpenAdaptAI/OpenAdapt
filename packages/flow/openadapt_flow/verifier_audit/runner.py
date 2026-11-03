"""Run the fault families against a target and classify every cell."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from openadapt_flow.verifier_audit.families import (
    AUTH_KEY,
    DEFAULT_FAMILIES,
    FORMAT_ID,
    Candidate,
    FamilyContext,
    FaultFamily,
    Polarity,
    Refusal,
)
from openadapt_flow.verifier_audit.model import (
    AuditTarget,
    Call,
    CallKind,
    Episode,
    IncidentalCalls,
    Verdict,
    effect_diff,
)
from openadapt_flow.verifier_audit.report import render_markdown, summarize

KIT_VERSION = "verifier-audit/1"


@dataclass
class AuditConfig:
    families: Sequence[FaultFamily] = DEFAULT_FAMILIES
    tasks: Optional[Sequence[str]] = None
    max_candidates: int = 6
    redact: bool = False
    """Replace call arguments and effect rows with digests in the report, for
    targets whose task data may not be redistributed in the clear."""
    out_dir: Optional[Path] = None
    single_replay: bool = False
    """Score the census replay instead of replaying the chosen edit again.
    The choice still reads only the effect: the verdict is taken from the
    episode after the candidate is chosen. Halves the replays for slow
    targets, at the cost of the scored-replay reproducibility check."""
    log: Callable[[str], None] = field(default=lambda _msg: None)


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()[:16]


def _identity_pool(
    target: AuditTarget, tasks: Sequence[str], refs: dict[str, list[Call]]
) -> dict[str, list[Any]]:
    pool: dict[str, list[Any]] = {}
    for task in tasks:
        for call in refs.get(task, []):
            if call.kind is not CallKind.WRITE:
                continue
            for key, value in call.args.items():
                if AUTH_KEY.search(key) or isinstance(value, (list, dict)):
                    continue
                values = pool.setdefault(key, [])
                if value not in values:
                    values.append(value)
    return pool


def _edited_calls_ok(ep: Episode, cand: Candidate) -> bool:
    return not any(i in ep.call_errors for i in cand.must_succeed)


def _differs(ep: Episode, control: Episode, cand: Candidate) -> bool:
    if cand.ordering_only:
        return ep.claim_effect is not None and ep.claim_effect != control.claim_effect
    return ep.final_effect != control.final_effect


def _cell(task: str, family: FaultFamily, status: str, **extra: Any) -> dict[str, Any]:
    return {
        "task": task,
        "family": family.id,
        "polarity": family.polarity.value,
        "status": status,
        **extra,
    }


def _audit_family(
    target: AuditTarget,
    task: str,
    family: FaultFamily,
    ctx: FamilyContext,
    control: Episode,
    cfg: AuditConfig,
) -> dict[str, Any]:
    built = family.build(ctx)
    if isinstance(built, Refusal):
        return _cell(
            task, family, "REFUSED", reason=built.reason, edit=None, verdict=None
        )
    if family.polarity is Polarity.LEGITIMATE:
        cand = built[0]
        ep = target.run(task, cand.calls, score=True)
        status = {Verdict.PASS: "ACCEPTED", Verdict.FAIL: "FALSE_REJECT"}.get(
            ep.verdict, "SCORER_ERROR"
        )
        return _cell(
            task,
            family,
            status,
            edit=cand.description,
            calls=[c.to_json() for c in cand.calls],
            verdict=ep.verdict.value if ep.verdict else None,
            effect_changed=ep.final_effect != control.final_effect,
            effect_diff=effect_diff(control.final_effect, ep.final_effect),
            call_errors=ep.call_errors,
            scorer_detail=ep.scorer_detail,
        )
    attempts = []
    for cand in built[: cfg.max_candidates]:
        if cand.ordering_only and not getattr(target, "post_claim_steps", True):
            return _cell(
                task,
                family,
                "REFUSED",
                reason="EPISODE_ENDS_AT_CLAIM",
                edit=cand.description,
                verdict=None,
            )
        if cand.ordering_only and control.claim_effect is None:
            return _cell(
                task,
                family,
                "REFUSED",
                reason="NO_CLAIM_TIME_READ",
                edit=cand.description,
                verdict=None,
            )
        census = target.run(task, cand.calls, score=cfg.single_replay)
        ok, differs = _edited_calls_ok(census, cand), _differs(census, control, cand)
        attempts.append(
            {
                "edit": cand.description,
                "edited_call_accepted": ok,
                "effect_differs": differs,
            }
        )
        if not (ok and differs):
            continue
        ep = census if cfg.single_replay else target.run(task, cand.calls, score=True)
        reproducible = (
            ep.final_effect == census.final_effect
            and ep.claim_effect == census.claim_effect
        )
        base = control.claim_effect if cand.ordering_only else control.final_effect
        seen = ep.claim_effect if cand.ordering_only else ep.final_effect
        if not reproducible:
            status = "EFFECT_NOT_REPRODUCIBLE"
        else:
            status = {Verdict.PASS: "FALSE_ACCEPT", Verdict.FAIL: "CAUGHT"}.get(
                ep.verdict, "SCORER_ERROR"
            )
        return _cell(
            task,
            family,
            status,
            edit=cand.description,
            calls=[c.to_json() for c in cand.calls],
            verdict=ep.verdict.value if ep.verdict else None,
            effect_diff=effect_diff(base or (), seen or ()),
            judged_on="claim_time_effect" if cand.ordering_only else "final_effect",
            call_errors=ep.call_errors,
            scorer_detail=ep.scorer_detail,
            census_attempts=attempts,
        )
    if (
        attempts
        and all(a["edited_call_accepted"] for a in attempts)
        and not any(a["effect_differs"] for a in attempts)
    ):
        status = "DEGENERATE"
    else:
        status = "CANDIDATES_EXHAUSTED"
    return _cell(
        task, family, status, edit=None, verdict=None, census_attempts=attempts
    )


def _value_named(
    cell: dict[str, Any], ref: Sequence[Call], instruction: Optional[str]
) -> Optional[bool]:
    """For wrong-value and wrong-record cells: does the task instruction
    name the original value the edit replaced? ``None`` when the target has
    no instruction text. A heuristic substring check, reported beside the
    rate so a reader can discount edits to fields the task never specified."""
    if instruction is None or cell.get("family") not in ("wrong_value", "wrong_record"):
        return None
    edited = cell.get("calls")
    if not edited:
        return None
    text = instruction.lower()
    for before, after in zip(ref, edited):
        for key, value in before.to_json()["args"].items():
            if after["args"].get(key) != value and isinstance(value, (str, int, float)):
                needle = str(value).lower().strip()
                if len(needle) < 3:
                    return False
                return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", text) is not None
    return False


def _redact(cell: dict[str, Any]) -> dict[str, Any]:
    cell = dict(cell)
    if "calls" in cell:
        cell["calls"] = [
            {
                "name": c["name"],
                "kind": c["kind"],
                "args": _digest(json.dumps(c["args"], sort_keys=True, default=str)),
            }
            for c in cell["calls"]
        ]
    if cell.get("effect_diff"):
        cell["effect_diff"] = {
            k: [_digest(r) for r in v] for k, v in cell["effect_diff"].items()
        }
    cell.pop("scorer_detail", None)
    return cell


def run_audit(target: AuditTarget, cfg: Optional[AuditConfig] = None) -> dict[str, Any]:
    """Audit ``target`` and return the report; also write it when
    ``cfg.out_dir`` is set. Completed cells in ``out_dir/cells.jsonl`` are
    reused, so an interrupted audit resumes without re-scoring a cell."""
    cfg = cfg or AuditConfig()
    tasks = list(cfg.tasks or target.tasks())
    ledger_path = cfg.out_dir / "cells.jsonl" if cfg.out_dir else None
    done: dict[tuple[str, str], dict[str, Any]] = {}
    if ledger_path and ledger_path.is_file():
        for line in ledger_path.read_text().splitlines():
            if line.strip():
                cell = json.loads(line)
                done[(cell["task"], cell["family"])] = cell
    if cfg.out_dir:
        cfg.out_dir.mkdir(parents=True, exist_ok=True)

    refs = {task: target.reference(task) for task in tasks}
    pool = _identity_pool(target, tasks, refs)
    cells: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    def record(cell: dict[str, Any]) -> None:
        cell = _redact(cell) if cfg.redact else cell
        cells.append(cell)
        if ledger_path:
            with ledger_path.open("a") as fh:
                fh.write(json.dumps(cell, sort_keys=True, default=str) + "\n")

    for n, task in enumerate(tasks, 1):
        pending = [f for f in cfg.families if (task, f.id) not in done]
        cells.extend(done[(task, f.id)] for f in cfg.families if (task, f.id) in done)
        if (task, "__control__") in done:
            excluded.append(done[(task, "__control__")])
            continue
        if not pending:
            continue
        t0 = time.monotonic()
        control = target.run(task, refs[task], score=True)
        again = target.run(task, refs[task], score=False)
        reason = None
        if control.verdict is not Verdict.PASS:
            reason = f"REFERENCE_NOT_ACCEPTED:{control.verdict.value if control.verdict else None}"
        elif (
            again.final_effect != control.final_effect
            or again.claim_effect != control.claim_effect
        ):
            reason = "REFERENCE_EFFECT_NOT_REPRODUCIBLE"
        elif not control.final_effect:
            reason = "REFERENCE_HAS_NO_EFFECT"
        if reason:
            row = {
                "task": task,
                "family": "__control__",
                "reason": reason,
                "scorer_detail": control.scorer_detail,
            }
            excluded.append(row)
            if ledger_path:
                with ledger_path.open("a") as fh:
                    fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")
            cfg.log(f"[{n}/{len(tasks)}] {task}: excluded ({reason})")
            continue
        incidental = None
        if isinstance(target, IncidentalCalls):
            incidental = target.incidental_calls(task, refs[task])
        instruction = (
            target.instruction(task) if hasattr(target, "instruction") else None
        )
        ctx = FamilyContext(
            task=task, reference=refs[task], identity_pool=pool, incidental=incidental
        )
        for family in pending:
            try:
                cell = _audit_family(target, task, family, ctx, control, cfg)
                named = _value_named(cell, refs[task], instruction)
                if named is not None:
                    cell["original_value_in_instruction"] = named
            except Exception as exc:  # noqa: BLE001 - one broken cell must not end the audit
                cell = _cell(
                    task,
                    family,
                    "HARNESS_ERROR",
                    edit=None,
                    verdict=None,
                    error=repr(exc)[:500],
                )
            record(cell)
        cfg.log(f"[{n}/{len(tasks)}] {task}: {time.monotonic() - t0:.1f}s")

    report = {
        "kit_version": KIT_VERSION,
        "families_format": FORMAT_ID,
        "target": target.name,
        "families": [f.spec() for f in cfg.families],
        "tasks_excluded": len(excluded),
        "excluded": excluded,
        "summary": summarize(cells, cfg.families),
        "cells": cells,
    }
    if cfg.out_dir:
        (cfg.out_dir / "report.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
        )
        (cfg.out_dir / "REPORT.md").write_text(render_markdown(report))
    return report
