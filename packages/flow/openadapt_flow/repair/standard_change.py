"""Standard changes: self-repairs a person approved in advance.

ITIL calls a low-risk change that a person approves once, as a class, a
*standard change*. This module brings that idea to UI drift. A person signs a
repair policy once (``standard_repairs`` on the signed
:class:`~openadapt_flow.jit.authority.AdmissionPolicy`). After that, a
self-repair (:mod:`openadapt_flow.repair.self_repair`) applies without asking
anyone when all of these hold:

* the policy verifies (registered signer, signature, expiry), names the repair
  agent, and covers the workflow;
* the save step, its identity evidence and its record check are byte-identical
  to the admitted bundle, including every file they use;
* no step's identity evidence changed, and the patch weakens nothing;
* the change fits a class the policy allows:

  - ``relabel_or_move``: one control that is not the save has a new label,
    place or locator;
  - ``dismiss_dialog``: one new click dismisses a new dialog whose text
    matches a declared pattern, with a declared dismiss label;
  - ``menu_path``: up to ``menu_steps`` new clicks open a menu that now holds
    a moved control that is not the save;

* the probe up to the save passed, and both campaigns passed;
* no person reverted an automatic repair of this step;
* the repaired bundle ran once on the deployment's **test record**, saved, and
  the record check read the save back (``VERIFIED``).

Anything else goes to a person, exactly as without a policy. The store
re-checks the policy and the bundles itself
(:meth:`~openadapt_flow.repair.lifecycle.RepairStore.approve_standard_change`),
so a caller can't skip a check.

Every automatic repair is written to the run report, the run receipt, and the
store's ledger (``<store>/standard-changes/ledger.jsonl``), which the daily
digest reads. ``openadapt-flow repair revert CANDIDATE --by NAME`` undoes one,
as does the console route the phone portal calls. A revert also stops
automatic repair of that step until a person clears it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional

from openadapt_flow.ir import (
    ActionKind,
    RunReport,
    SelfRepairStepRecord,
    StandardChangeEvidence,
    Step,
    Workflow,
)
from openadapt_flow.repair.candidate import Actor, RepairCandidate

if TYPE_CHECKING:
    from openadapt_flow.jit.authority import SignedAdmissionPolicy, StandardRepairs
    from openadapt_flow.repair.lifecycle import RepairStore
    from openadapt_flow.repair.self_repair import (
        ProbeOutcome,
        SelfRepair,
        SelfRepairTarget,
        _Patch,
    )

__all__ = [
    "ChangeClass",
    "ChangeVerdict",
    "Ledger",
    "PreflightResult",
    "StandardChangeGrant",
    "StandardChangeRefused",
    "StandardChanges",
    "TestRecord",
    "authorize",
    "classify_change",
    "daily_digest",
    "receipt_fields",
    "revert",
    "run_preflight",
]

ChangeClass = Literal["relabel_or_move", "dismiss_dialog", "menu_path"]
LEDGER_DIR = "standard-changes"
LEDGER_FILE = "ledger.jsonl"
#: The approval route recorded on the run report.
ROUTE = "standard_change"
_POINTER = (ActionKind.CLICK, ActionKind.DOUBLE_CLICK)
#: Step fields that make up the record check.
_RECORD_CHECK_FIELDS = ("effects", "submit_binding", "api_binding")


class StandardChangeRefused(RuntimeError):
    """The repair is not a standard change; a person decides it."""


# -- what changed --------------------------------------------------------------


@dataclass(frozen=True)
class ChangeVerdict:
    """Which allowed class a patch fits, or why it fits none."""

    change_class: Optional[ChangeClass]
    problems: tuple[str, ...]
    detail: str = ""
    step_id: Optional[str] = None

    @property
    def allowed(self) -> bool:
        return self.change_class is not None and not self.problems


def _dump(model: Any) -> Any:
    return model.model_dump(mode="json")


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


def _file_digests(step: Step, *bundle_dirs: Path) -> dict[str, tuple[str, ...]]:
    """Each bundle file the step names, with its digest in each bundle."""

    out: dict[str, tuple[str, ...]] = {}
    for text in _strings(_dump(step)):
        if not text or text.startswith("/") or ".." in text or len(text) > 512:
            continue
        if "/" not in text and "." not in text:
            continue
        paths = [d / text for d in bundle_dirs]
        try:
            if not any(p.is_file() for p in paths):
                continue
        except OSError:
            continue
        out[text] = tuple(
            hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "missing"
            for p in paths
        )
    return out


def _changed_fields(old: Step, new: Step) -> set[str]:
    a, b = _dump(old), _dump(new)
    return {k for k in set(a) | set(b) if a.get(k) != b.get(k)}


def _identity_changed(old: Step, new: Step) -> list[str]:
    from openadapt_flow.runtime.healing.patch import IDENTITY_FIELDS

    if old.anchor is None or new.anchor is None:
        return ["anchor"] if (old.anchor is None) != (new.anchor is None) else []
    return [
        f
        for f in IDENTITY_FIELDS
        if _canon(getattr(old.anchor, f, None)) != _canon(getattr(new.anchor, f, None))
    ]


def _label(step: Step) -> Optional[str]:
    anchor = step.anchor
    if anchor is not None and anchor.structural is not None and anchor.structural.name:
        return anchor.structural.name
    if anchor is not None and anchor.ocr_text:
        return anchor.ocr_text
    match = re.match(
        r"^\s*(?:double[_ ])?click\s+['\"](.+)['\"]\s*$", step.intent or ""
    )
    return match.group(1) if match else None


def _save_problems(
    prior: Workflow, patched: Workflow, prior_dir: Path, patched_dir: Path
) -> tuple[list[str], set[str]]:
    from openadapt_flow.run_gate import is_consequential

    problems: list[str] = []
    saves = {s.id for s in prior.steps if is_consequential(s, prior)}
    if not saves:
        problems.append("the admitted bundle has no save step to compare")
    by_id = {s.id: s for s in patched.steps}
    for step in prior.steps:
        if step.id not in saves:
            continue
        new = by_id.get(step.id)
        if new is None:
            problems.append(f"{step.id}: the save step is gone")
            continue
        if _identity_changed(step, new):
            problems.append(f"{step.id}: the save step's identity evidence changed")
        if any(
            _canon(_dump(step).get(f)) != _canon(_dump(new).get(f))
            for f in _RECORD_CHECK_FIELDS
        ):
            problems.append(f"{step.id}: the save step's record check changed")
        if _canon(_dump(step)) != _canon(_dump(new)) and not problems:
            fields = ", ".join(sorted(_changed_fields(step, new)))
            problems.append(f"{step.id}: the save step changed ({fields})")
        for path, digests in _file_digests(step, prior_dir, patched_dir).items():
            if len(set(digests)) != 1:
                problems.append(f"{step.id}: the save step's file {path} changed")
    new_saves = {s.id for s in patched.steps if is_consequential(s, patched)}
    if new_saves - saves:
        problems.append(
            "the repair adds a save: " + ", ".join(sorted(new_saves - saves))
        )
    return problems, saves


def classify_change(
    prior: Workflow,
    patched: Workflow,
    prior_dir: Path,
    patched_dir: Path,
    repairs: "StandardRepairs",
) -> ChangeVerdict:
    """The class ``patched`` fits against ``prior`` under ``repairs``.

    Works on the two bundles alone, so it is the same on every backend: web,
    Windows, macOS, Linux, RDP and Citrix bundles are all compared step by
    step, field by field, and file by file.
    """

    from openadapt_flow.risk import is_meaningful_label, is_write_shaped
    from openadapt_flow.run_gate import is_consequential

    problems, saves = _save_problems(prior, patched, Path(prior_dir), Path(patched_dir))
    skip = {"steps", "manifest"}
    a = {k: v for k, v in _dump(prior).items() if k not in skip}
    b = {k: v for k, v in _dump(patched).items() if k not in skip}
    changed_settings = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    if changed_settings:
        problems.append(
            "the workflow's own settings changed: " + ", ".join(changed_settings)
        )
    prior_ids = [s.id for s in prior.steps]
    kept = [s.id for s in patched.steps if s.id in set(prior_ids)]
    if kept != prior_ids:
        problems.append("the repair removed or reordered recorded steps")
    if problems:
        return ChangeVerdict(None, tuple(problems))

    old_by_id = {s.id: s for s in prior.steps}
    changed: dict[str, set[str]] = {}
    for step in patched.steps:
        old = old_by_id.get(step.id)
        if old is None:
            continue
        fields = _changed_fields(old, step)
        if fields:
            changed[step.id] = fields
        identity = _identity_changed(old, step)
        if identity:
            problems.append(
                f"{step.id}: identity evidence changed ({', '.join(identity)})"
            )
    positions = [i for i, s in enumerate(patched.steps) if s.id not in old_by_id]
    inserted = [patched.steps[i] for i in positions]
    for step in inserted:
        label = _label(step)
        if step.action not in _POINTER:
            problems.append(f"{step.id}: a new step that is not a click")
        elif is_consequential(step, patched) or (label and is_write_shaped(label)):
            problems.append(f"{step.id}: a new step reads as a save ({label!r})")
        elif not is_meaningful_label(label):
            problems.append(f"{step.id}: a new click has no readable label")
    if problems:
        return ChangeVerdict(None, tuple(problems))
    if len(changed) != 1:
        return ChangeVerdict(
            None,
            (
                f"the repair changes {len(changed)} recorded steps; a standard "
                "change touches exactly one",
            ),
        )
    ((step_id, fields),) = changed.items()
    if step_id in saves:  # unreachable: the save is byte-identical
        return ChangeVerdict(None, (f"{step_id} is the save",))
    new_step = next(s for s in patched.steps if s.id == step_id)
    new_label = _label(new_step)
    if new_label and is_write_shaped(new_label):
        return ChangeVerdict(None, (f"{step_id} now reads as a save ({new_label!r})",))

    if not inserted:
        if fields != {"anchor"}:
            return ChangeVerdict(
                None,
                (
                    f"{step_id}: fields other than the target changed ({', '.join(sorted(fields))})",
                ),
            )
        old_label = _label(old_by_id[step_id])
        detail = f"{step_id}: {old_label!r} is now {new_label!r}"
        if not repairs.relabel_or_move:
            return ChangeVerdict(
                None, ("the policy does not allow relabel_or_move",), detail, step_id
            )
        return ChangeVerdict("relabel_or_move", (), detail, step_id)

    if positions != list(range(positions[0], positions[0] + len(positions))):
        return ChangeVerdict(None, ("the new steps are not in one place",))
    before = patched.steps[positions[0] - 1] if positions[0] > 0 else None
    after_index = positions[-1] + 1
    after = patched.steps[after_index] if after_index < len(patched.steps) else None
    labels = [repr(_label(s)) for s in inserted]

    if after is not None and after.id == step_id and fields == {"anchor"}:
        detail = f"{step_id}: reached through {', '.join(labels)}, now {new_label!r}"
        if len(inserted) > repairs.menu_steps:
            return ChangeVerdict(
                None,
                (
                    f"the menu path has {len(inserted)} new click(s); the policy "
                    f"allows {repairs.menu_steps}",
                ),
                detail,
                step_id,
            )
        return ChangeVerdict("menu_path", (), detail, step_id)

    if before is not None and before.id == step_id and fields == {"expect"}:
        if len(inserted) != 1:
            return ChangeVerdict(None, ("a dialog is dismissed by exactly one click",))
        dismiss = inserted[0]
        if _canon(_dump(dismiss).get("expect")) != _canon(
            _dump(old_by_id[step_id]).get("expect")
        ):
            return ChangeVerdict(
                None, ("the dismiss click does not lead to the recorded screen",)
            )
        texts = [p.text for p in before.expect if p.text]
        label = _label(dismiss)
        for pattern in repairs.dialogs:
            if pattern.matches(texts) and pattern.dismisses_with(label):
                return ChangeVerdict(
                    "dismiss_dialog",
                    (),
                    f"after {step_id}: dialog {pattern.name!r} dismissed with {label!r}",
                    step_id,
                )
        return ChangeVerdict(
            None,
            (f"after {step_id}: no declared dialog pattern matches ({label!r})",),
            step_id=step_id,
        )
    return ChangeVerdict(None, ("the new steps fit no allowed class",))


# -- authority -----------------------------------------------------------------


@dataclass(frozen=True)
class StandardChangeGrant:
    """What a verified repair policy grants one candidate."""

    policy_id: str
    policy_sha256: str
    signed_by: str
    change_class: ChangeClass
    detail: str
    step_id: Optional[str]

    @property
    def actor(self) -> Actor:
        return Actor(kind="automation", id=f"standard-change:{self.policy_sha256[:16]}")

    @property
    def label(self) -> str:
        return f"standard-change:{self.policy_sha256[:16]} (signed by {self.signed_by})"


def authorize(
    candidate: RepairCandidate,
    *,
    policy: "SignedAdmissionPolicy",
    key_dir: Path,
    store_root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> StandardChangeGrant:
    """Decide whether ``candidate`` is a standard change under ``policy``.

    Raises:
        StandardChangeRefused: with the first reason it is not.
    """

    from openadapt_flow.jit.authority import (
        AuthorityError,
        PrincipalRegistry,
        verify_policy,
    )

    try:
        verified = verify_policy(policy, PrincipalRegistry.load(Path(key_dir)), now=now)
    except AuthorityError as exc:
        raise StandardChangeRefused(
            f"the repair policy does not verify: {exc}"
        ) from exc
    repairs = verified.standard_repairs
    if repairs is None:
        raise StandardChangeRefused("the signed policy allows no standard repairs")
    provenance = candidate.provenance
    if (
        candidate.source != "model_suggestion"
        or provenance is None
        or provenance.repaired_by != "self_repair"
    ):
        raise StandardChangeRefused("only a self-repair can be a standard change")
    if provenance.agent_id not in repairs.repair_agents:
        raise StandardChangeRefused(
            f"the policy does not name repair agent {provenance.agent_id!r}"
        )
    if repairs.workflows and candidate.workflow_name not in repairs.workflows:
        raise StandardChangeRefused(
            f"the policy does not cover workflow {candidate.workflow_name!r}"
        )
    if candidate.contract_weakenings:
        raise StandardChangeRefused("the patch weakens the safety contract")
    if any(change.identity for change in candidate.binding_changes):
        raise StandardChangeRefused("the patch touches identity evidence")
    if not (candidate.prior_bundle_path and candidate.proposed_bundle_path):
        raise StandardChangeRefused("the candidate names no bundles to compare")
    prior_dir = Path(candidate.prior_bundle_path)
    patched_dir = Path(candidate.proposed_bundle_path)
    verdict = classify_change(
        Workflow.load(prior_dir),
        Workflow.load(patched_dir),
        prior_dir,
        patched_dir,
        repairs,
    )
    if not verdict.allowed:
        raise StandardChangeRefused("; ".join(verdict.problems)[:900])
    assert verdict.change_class is not None
    if store_root is not None:
        held = Ledger(store_root).held(candidate.workflow_name, verdict.step_id)
        if held is not None:
            raise StandardChangeRefused(
                f"{held.get('reverted_by', 'a person')} reverted an automatic "
                f"repair of {verdict.step_id} on {str(held.get('at', ''))[:10]}; "
                "a person decides repairs of this step until they clear it"
            )
    return StandardChangeGrant(
        policy_id=verified.policy_id,
        policy_sha256=verified.digest().removeprefix("sha256:"),
        signed_by=verified.signer,
        change_class=verdict.change_class,
        detail=verdict.detail,
        step_id=verdict.step_id,
    )


# -- the test record -----------------------------------------------------------


@dataclass
class PreflightResult:
    """One run of a bundle on the test record."""

    record_id: str
    passed: bool
    outcome: str
    run_dir: Path
    reason: str = ""
    report: Optional[RunReport] = None
    halted_step_id: Optional[str] = None


@dataclass
class TestRecord:
    """The record the deployment set aside for pre-flight runs (e.g. TEST-001).

    ``target`` opens sessions on that record and carries its parameters, so
    a pre-flight saves to the test record only. The record check must read
    the save back: a pre-flight passes only when the run is ``VERIFIED``.
    """

    __test__ = False  # not a pytest class

    record_id: str
    target: "SelfRepairTarget"


def _verified(report: RunReport) -> bool:
    if not report.success:
        return False
    if report.execution_outcome is not None:
        return report.execution_outcome == "VERIFIED"
    return report.transaction_outcome == "VERIFIED"


def run_preflight(
    test_record: TestRecord, bundle_dir: Path | str, run_dir: Path | str
) -> PreflightResult:
    """Run ``bundle_dir`` once on the test record, with the full read-back."""

    from openadapt_flow.repair.self_repair import _close

    bundle_dir, run_dir = Path(bundle_dir), Path(run_dir)
    session = None
    try:
        workflow = Workflow.load(bundle_dir)
        session = test_record.target.open()
        report = test_record.target.execute(workflow, bundle_dir, session, run_dir)
    except Exception as exc:  # noqa: BLE001 - a run that can't start fails
        return PreflightResult(
            test_record.record_id,
            False,
            "NOT_RUN",
            run_dir,
            f"the run could not start ({type(exc).__name__}: {exc})"[:300],
        )
    finally:
        _close(session)
    passed = _verified(report)
    halt = report.halt
    return PreflightResult(
        test_record.record_id,
        passed,
        str(report.execution_outcome or report.transaction_outcome or "UNKNOWN"),
        run_dir,
        ""
        if passed
        else (halt.reason if halt is not None and halt.reason else "not verified")[
            :300
        ],
        report,
        halt.state_id if halt is not None else None,
    )


# -- the ledger ----------------------------------------------------------------


class Ledger:
    """Append-only log of standard changes and reverts, in the repair store."""

    def __init__(self, store_root: Path | str) -> None:
        self.path = Path(store_root) / LEDGER_DIR / LEDGER_FILE

    def append(self, entry: dict[str, Any]) -> dict[str, Any]:
        row = {"at": datetime.now(timezone.utc).isoformat(), **entry}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        return row

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        return rows

    def applied(self) -> list[dict[str, Any]]:
        """Every automatic repair, newest last, with its revert if any."""

        reverts = {
            r["candidate_id"]: r for r in self.entries() if r.get("event") == "reverted"
        }
        out = []
        for row in self.entries():
            if row.get("event") == "applied":
                out.append({**row, "reverted": reverts.get(row["candidate_id"])})
        return out

    def find(self, candidate_id: str) -> Optional[dict[str, Any]]:
        return next(
            (r for r in self.applied() if r["candidate_id"] == candidate_id), None
        )

    def held(self, workflow: str, step_id: Optional[str]) -> Optional[dict[str, Any]]:
        """The revert that holds automatic repair of this step, if any."""

        held = None
        for row in self.entries():
            if row.get("workflow") != workflow or row.get("step_id") != step_id:
                continue
            if row.get("event") == "reverted":
                held = row
            elif row.get("event") == "hold_cleared":
                held = None
        return held


def daily_digest(store_root: Path | str, day: Optional[str] = None) -> str:
    """The day's automatic repairs as Markdown (UTC day, ``YYYY-MM-DD``)."""

    day = day or datetime.now(timezone.utc).date().isoformat()
    rows = [
        r for r in Ledger(store_root).applied() if str(r.get("at", "")).startswith(day)
    ]
    lines = [f"# Automatic repairs on {day}", ""]
    if not rows:
        lines.append("No repair applied without a person.")
        return "\n".join(lines) + "\n"
    lines.append(
        f"{len(rows)} repair(s) applied without a person, each under a signed policy "
        "and after a verified run on the test record."
    )
    lines.append("")
    lines.append(
        "| Time (UTC) | Workflow | Step | Class | Policy | Test record | Status | Undo |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        reverted = r.get("reverted")
        status = f"reverted by {reverted.get('reverted_by')}" if reverted else "in use"
        lines.append(
            f"| {str(r.get('at', ''))[11:19]} | {r.get('workflow')} | {r.get('step_id')} "
            f"| {r.get('change_class')} | {r.get('policy_id')} (signed by "
            f"{r.get('signed_by')}) | {r.get('test_record')} "
            f"{r.get('preflight_outcome')} | {status} | `{r.get('revert_command')}` |"
        )
    return "\n".join(lines) + "\n"


def revert_command(candidate_id: str, store_root: Path | str) -> str:
    return (
        f"openadapt-flow repair revert {candidate_id} --store {store_root} "
        "--by <your-name>"
    )


def revert(
    store: "RepairStore", candidate_id: str, *, by: str, route: str = "cli"
) -> RepairCandidate:
    """Undo one automatic repair: put the prior bundle back, hold the step.

    Only a person reverts (``by`` is their name). Until a person clears the
    hold (:func:`clear_hold`), repairs of the same step go to a person.
    """

    from openadapt_flow.repair.lifecycle import RepairLifecycleError

    ledger = Ledger(store.root)
    entry = ledger.find(candidate_id)
    if entry is None:
        raise RepairLifecycleError(
            f"refused: {candidate_id} is not an automatic repair; use "
            "`openadapt-flow repair rollback`"
        )
    if entry.get("reverted"):
        raise RepairLifecycleError(f"refused: {candidate_id} is already reverted")
    candidate = store.load_candidate(candidate_id)
    if candidate.state in ("canary", "active"):
        candidate = store.rollback(Actor(kind="human", id=by), candidate_id)
    elif candidate.state not in ("staged", "rolled_back"):
        raise RepairLifecycleError(
            f"refused: candidate {candidate_id} is {candidate.state!r}"
        )
    ledger.append(
        {
            "event": "reverted",
            "candidate_id": candidate_id,
            "workflow": entry.get("workflow"),
            "step_id": entry.get("step_id"),
            "reverted_by": by,
            "route": route,
        }
    )
    return candidate


def clear_hold(store_root: Path | str, workflow: str, step_id: str, *, by: str) -> None:
    """A person lets automatic repair of this step resume after a revert."""

    Ledger(store_root).append(
        {
            "event": "hold_cleared",
            "workflow": workflow,
            "step_id": step_id,
            "cleared_by": by,
        }
    )


def receipt_fields(report: Any) -> Optional[dict[str, str]]:
    """The closed fields a run receipt carries for a standard change."""

    evidence = getattr(report, "self_repair", None)
    change = getattr(evidence, "standard_change", None)
    candidate_id = getattr(evidence, "candidate_id", None)
    if change is None or not candidate_id:
        return None
    return {
        "change_class": change.change_class,
        "policy_sha256": change.policy_sha256,
        "candidate_id": candidate_id,
    }


# -- the hook self-repair calls ------------------------------------------------


@dataclass
class StandardChanges:
    """Apply a self-repair without a person when the signed policy allows it.

    Pass one to :class:`~openadapt_flow.repair.self_repair.SelfRepair` as
    ``standard_changes``. After both campaigns pass, self-repair calls
    :meth:`apply`. It returns the candidate in its canary window when the
    repair applied, or ``None`` when a person must decide; the reason is on
    the run's self-repair ledger either way.

    Args:
        policy: The signed policy (``standard_repairs`` set).
        key_dir: The key directory with the principal registry.
        test_record: Where the repaired bundle runs before it applies.
    """

    policy: "SignedAdmissionPolicy"
    key_dir: Path
    test_record: TestRecord
    now: Optional[Callable[[], datetime]] = None
    last_preflight: Optional[PreflightResult] = field(default=None, init=False)

    def _now(self) -> Optional[datetime]:
        return self.now() if self.now is not None else None

    def apply(
        self,
        repair: "SelfRepair",
        *,
        patch: "_Patch",
        probe: "ProbeOutcome",
        candidate: RepairCandidate,
        work: Path,
        evidence: Any,
    ) -> Optional[RepairCandidate]:
        from openadapt_flow.repair.lifecycle import RepairLifecycleError
        from openadapt_flow.repair.self_repair import AUTOMATION

        def to_person(why: str) -> None:
            evidence.steps.append(
                SelfRepairStepRecord(
                    stage="approval",
                    passed=False,
                    detail=f"not a standard change ({why}); asking a person"[:1024],
                )
            )

        if not probe.passed:
            to_person("the probe did not pass")
            return None
        try:
            grant = authorize(
                candidate,
                policy=self.policy,
                key_dir=self.key_dir,
                store_root=repair.store.root,
                now=self._now(),
            )
        except StandardChangeRefused as exc:
            to_person(str(exc))
            return None
        preflight = run_preflight(
            self.test_record, patch.bundle_dir, work / "preflight"
        )
        self.last_preflight = preflight
        if not preflight.passed:
            to_person(
                f"the test record {self.test_record.record_id} did not verify "
                f"({preflight.outcome}: {preflight.reason})"
            )
            return None
        try:
            approved, grant = repair.store.approve_standard_change(
                candidate.candidate_id,
                policy=self.policy,
                key_dir=self.key_dir,
                now=self._now(),
            )
            repair.store.stage(approved.candidate_id, AUTOMATION)
            approved = repair.store.start_canary(
                approved.candidate_id, AUTOMATION, max_runs=repair.canary_runs
            )
        except RepairLifecycleError as exc:
            to_person(f"the repair store refused: {exc}")
            return None
        undo = revert_command(approved.candidate_id, repair.store.root)
        record = StandardChangeEvidence(
            change_class=grant.change_class,
            detail=grant.detail[:512],
            policy_id=grant.policy_id,
            policy_sha256=grant.policy_sha256,
            policy_signed_by=grant.signed_by,
            test_record=self.test_record.record_id,
            preflight_outcome=preflight.outcome,
            preflight_run_dir=str(preflight.run_dir),
            applied_at=datetime.now(timezone.utc).isoformat(),
            revert_command=undo,
        )
        evidence.standard_change = record
        evidence.approval_route = ROUTE
        evidence.approved_by = (
            approved.approval.approved_by if approved.approval else None
        )
        Ledger(repair.store.root).append(
            {
                "event": "applied",
                "candidate_id": approved.candidate_id,
                "workflow": approved.workflow_name,
                "step_id": grant.step_id,
                "change_class": grant.change_class,
                "detail": grant.detail[:300],
                "policy_id": grant.policy_id,
                "policy_sha256": grant.policy_sha256,
                "signed_by": grant.signed_by,
                "agent_id": repair.agent.agent_id,
                "test_record": self.test_record.record_id,
                "preflight_outcome": preflight.outcome,
                "run_dir": str(work.parent),
                "prior_digest": approved.prior_content_digest,
                "proposed_digest": approved.proposed_content_digest,
                "revert_command": undo,
            }
        )
        evidence.steps.append(
            SelfRepairStepRecord(
                stage="approval",
                passed=True,
                detail=(
                    f"standard change {grant.change_class} ({grant.detail}) under "
                    f"policy {grant.policy_id} signed by {grant.signed_by}; test "
                    f"record {self.test_record.record_id} {preflight.outcome}; "
                    f"undo: {undo}"
                )[:1024],
            )
        )
        return approved
