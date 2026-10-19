"""Local-only decision detail: what broke, and what "fixed" gets re-checked.

An operator three rooms from the runner cannot answer "is the live application
ready to verify and continue?" when nothing on the page says what stopped.  The
engine already knows: a typed halt category, the compiled step, the resolution
ladder's evidence, and the exact contracts a continuation must re-prove.  This
module projects those facts into the **local** decision detail.

Two projections, deliberately kept apart
----------------------------------------

* :class:`~openadapt_types.HumanDecisionTaskV1` -- the signed, Cloud-safe task.
  Closed enums, opaque ids, digests, counts and expiry only; every
  ``question.safe_slots`` value stays ``None`` and
  ``evidence.sensitive_evidence_local_only`` stays ``True``.  **Nothing here
  widens it.**  It is built in ``human_decisions._task_and_presentation`` and
  relayed verbatim to an authenticated remote AAL2 surface by
  ``human_decisions.portable_remote_decision_task``, which never reads this
  module's output.
* This module's :func:`halt_detail` -- returned only inside the local
  ``presentation`` block of ``GET /api/attention/{run}``.  That response is
  served by the loopback console and reaches a phone only through the
  customer-controlled runner-local portal, which is the same boundary that
  already serves protected screenshot crops.  ``portable_remote_decision_task``
  discards ``presentation`` entirely, so this can never ride the Cloud lane.

What may cross, and what may not
--------------------------------

Every field below is a closed enum, a bounded integer, or a boolean, with one
exception: ``target_label``.  An *intent string is not automatically safe* --
``click 'Open'`` is harmless, but a TYPE step's intent embeds the typed value,
which can be PHI.  So the step intent, ``result.error``, ``pending.reason`` and
the typed value are never read here at all.  ``target_label`` releases only the
target's **own accessible name**, and only when :func:`_safe_target_label`
proves it is static control chrome rather than record content.  When that proof
fails the label is withheld and the phone renders the target's *shape* instead
-- action kind plus control role -- so "could not find the field where you
typed <value>" degrades to "could not find the text field", never to a leak.

Vocabulary is reused, never reinvented: halt categories come from
``console.attention``, rung names from :data:`openadapt_flow.ir.Rung`, action
kinds from :class:`openadapt_flow.ir.ActionKind`.  There is deliberately no
free-text explanation field -- free text is how PHI escapes.  Operator prose is
composed by the client from these closed values.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional, get_args

from openadapt_flow.console import data
from openadapt_flow.ir import ActionKind, Anchor, Rung, Step, Workflow
from openadapt_flow.runtime import identity as _id
from openadapt_flow.runtime.authorization import runtime_params_for_gui
from openadapt_flow.runtime.durable.checkpoint import CheckpointStore, PendingEscalation

#: The resolution ladder in strongest-first order, taken from the engine's own
#: ``Rung`` literal so this can never drift into a parallel vocabulary.
#: ``tests/test_halt_detail.py`` pins it against ``resolver.RUNG_ORDER``.
RUNG_ORDER: tuple[Rung, ...] = tuple(get_args(Rung))

#: Whether the compiled bundle carries the evidence a rung consumes.
RungEvidence = Literal["recorded", "absent", "unknown"]

#: What became of a rung on the attempt that halted.
#:
#: ``failed`` -- the rung had its evidence, ran, and did not produce a target.
#: ``not_attempted`` -- the bundle carries no evidence for it.
#: ``unavailable`` -- the substrate cannot offer it (no accessibility tree
#:   across an RDP/Citrix pixel boundary), so it was skipped, not tried.
#: ``resolved`` -- this rung produced the target (the halt was later).
#: ``not_reached`` -- a stronger rung resolved first.
#: ``unknown`` -- the run retained no verdict for it.
RungVerdict = Literal[
    "resolved",
    "failed",
    "not_attempted",
    "not_reached",
    "unavailable",
    "unknown",
]

#: Contracts a continuation re-proves before it may resume.  Closed set; the
#: client owns the operator wording for each.
RecheckKind = Literal[
    "target_resolution",
    "record_identity",
    "postconditions",
    "system_of_record_effects",
    "delivery_reconciliation",
]

#: Substrates that expose a structural (DOM / UIA / AT-SPI) layer at all, in the
#: PORTABLE vocabulary the task already uses (``human_decisions._SUBSTRATE_MAP``
#: maps the report's ``web`` onto ``browser``).  On everything else -- rdp,
#: citrix, unknown -- the structural rung is *unavailable*, not failed: the
#: accessibility tree does not cross a remote display boundary (AGENTS.md,
#: "Capture Reuse Boundary").
_STRUCTURAL_SUBSTRATES = frozenset({"browser", "windows", "macos", "linux"})

#: Roles that name an interactive CONTROL.  A control's accessible name is its
#: own static label ("Open", "New Encounter"); it is a candidate for release.
#: Both ARIA spellings and Windows UIA ControlType names are listed because
#: ``StructuralLocator.role`` is deliberately backend-neutral.
_CONTROL_ROLES = frozenset(
    {
        "button",
        "checkbox",
        "combobox",
        "edit",
        "hyperlink",
        "link",
        "listbox",
        "menubutton",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "radio",
        "radiobutton",
        "searchbox",
        "slider",
        "spinbutton",
        "splitbutton",
        "switch",
        "tab",
        "tabitem",
        "textbox",
        "togglebutton",
    }
)

#: Roles that name a CONTAINER OF RECORD CONTENT.  Naming the role is safe and
#: useful ("the row"), but such an element's accessible name is the record --
#: a patient name, an MRN -- so its label is never released.
_CONTENT_ROLES = frozenset(
    {
        "article",
        "cell",
        "columnheader",
        "dataitem",
        "document",
        "grid",
        "gridcell",
        "group",
        "heading",
        "image",
        "img",
        "list",
        "listitem",
        "menu",
        "option",
        "region",
        "row",
        "rowheader",
        "table",
        "text",
        "tree",
        "treeitem",
    }
)

#: Actions whose payload is run data.  Their target label is never released:
#: the receiving field's accessible name can be the record it belongs to, and
#: the value itself is never read here at all.
_VALUE_BEARING_ACTIONS = frozenset({ActionKind.TYPE})

#: Bounds on a releasable control label.  A real control label is short; a
#: record value, a sentence of clinical text, or a concatenated row is not.
_MAX_LABEL_CHARS = 40
_MAX_LABEL_TOKENS = 4


def _normalized(value: Optional[str]) -> str:
    return " ".join(str(value or "").split()).casefold()


def _known_role(role: Optional[str]) -> Optional[str]:
    """Project a recorded role onto the closed role vocabulary, or drop it.

    An application can put an arbitrary string in ``aria-role``; only a role
    this module recognises is emitted, so the field can never become a
    free-text channel.
    """
    normalized = _normalized(role).replace(" ", "")
    if normalized in _CONTROL_ROLES or normalized in _CONTENT_ROLES:
        return normalized
    return None


def _label_shape_is_control_like(label: str) -> bool:
    """Reject shapes a control label does not have, using the engine's own
    identity classifiers rather than a second set of rules."""
    if not 0 < len(label) <= _MAX_LABEL_CHARS:
        return False
    tokens = label.split()
    if not tokens or len(tokens) > _MAX_LABEL_TOKENS:
        return False
    return not any(
        _id._has_digit(token)
        or _id._is_identifier_shaped(token)
        or _id._is_date_like(token)
        or _id._is_generational_suffix(token)
        for token in tokens
    )


def _is_run_data(label: str, params: dict[str, str]) -> bool:
    """Whether the label overlaps any of this run's bound parameter values."""
    squashed = _normalized(label).replace(" ", "")
    if not squashed:
        return True
    for value in params.values():
        other = _normalized(value).replace(" ", "")
        if other and (other in squashed or squashed in other):
            return True
    return False


def _carries_phi(label: str) -> bool:
    """Fail-closed reuse of the compiler's optional Presidio classification.

    ``compiler.compile._text_carries_phi`` already drops a geometry landmark
    the scrubber flags; this applies the identical test to a candidate label.
    Any failure of the scrubber itself counts as "carries PHI" here -- this
    surface is optional context, so refusing to guess costs nothing.
    """
    from openadapt_flow import privacy

    try:
        if not privacy.text_scrubbing_enabled():
            return False
        scrubbed = privacy.scrub_text(label)
    except Exception:  # noqa: BLE001 - an unusable classifier withholds
        return True
    return bool(scrubbed) and scrubbed != label


def _safe_target_label(
    step: Optional[Step], params: dict[str, str]
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(role, label)`` for the halted step's target.

    ``role`` is a closed-vocabulary value or None.  ``label`` is the target's
    accessible name, and is returned only when every one of these holds:

    1. the step's action does not carry run data (never a TYPE step);
    2. the recorded structural locator names an interactive CONTROL role, not
       a record container;
    3. the accessible name and the OCR of the target's **own template crop**
       agree -- two independently recorded sources both calling this string
       the element's own label, where the identity band is by construction the
       row text *excluding* that crop;
    4. the shape is control-like (:func:`_label_shape_is_control_like`);
    5. it does not overlap a bound run parameter value;
    6. the optional PHI classifier, when active, leaves it unchanged.

    Otherwise the caller reports the target's shape and withholds the label.
    """
    if step is None:
        return None, None
    anchor: Optional[Anchor] = step.anchor
    role = _known_role(anchor.structural.role) if anchor and anchor.structural else None
    if anchor is None or anchor.structural is None:
        return role, None
    if step.action in _VALUE_BEARING_ACTIONS or step.secret or step.param:
        return role, None
    if step.text is not None:
        return role, None
    if role not in _CONTROL_ROLES:
        return role, None
    name = str(anchor.structural.name or "").strip()
    if not name or _normalized(name) != _normalized(anchor.ocr_text):
        return role, None
    if not _label_shape_is_control_like(name):
        return role, None
    if _is_run_data(name, params):
        return role, None
    if _carries_phi(name):
        return role, None
    return role, name


def _rung_evidence(
    anchor: Optional[Anchor], bundle_dir: Optional[Path]
) -> dict[str, RungEvidence]:
    """Which rungs the compiled bundle actually carries evidence for."""
    if anchor is None:
        return {rung: "absent" for rung in RUNG_ORDER}
    if anchor.template and bundle_dir is not None:
        crop = bundle_dir / anchor.template
        template: RungEvidence = (
            "recorded"
            if crop.is_file() or crop.with_suffix(crop.suffix + ".enc").is_file()
            else "absent"
        )
    elif anchor.template:
        template = "unknown"
    else:
        template = "absent"
    return {
        "structural": "recorded" if anchor.structural is not None else "absent",
        "template": template,
        "template_global": template,
        "ocr": "recorded" if anchor.ocr_text else "absent",
        "geometry": "recorded" if anchor.landmarks else "absent",
        # The grounder is a deployment rung, not bundle evidence; whether one
        # was configured is not recorded on the run.
        "grounder": "unknown",
    }


def _resolution_ladder(
    *,
    anchor: Optional[Anchor],
    bundle_dir: Optional[Path],
    substrate: str,
    resolved_rung: Optional[str],
    ladder_exhausted: bool,
) -> list[dict[str, Any]]:
    """Per-rung evidence and verdict for the attempt that halted.

    ``resolver.resolve`` returns nothing only after walking every rung whose
    evidence exists, so on a ``resolution``/``disambiguation`` halt each rung
    with recorded evidence genuinely ran and genuinely failed.  When a rung DID
    resolve, everything above it failed and everything below was never reached.
    """
    evidence = _rung_evidence(anchor, bundle_dir)
    resolved_index = (
        RUNG_ORDER.index(resolved_rung)  # type: ignore[arg-type]
        if resolved_rung in RUNG_ORDER
        else None
    )
    rows: list[dict[str, Any]] = []
    for index, rung in enumerate(RUNG_ORDER):
        available = evidence[rung]
        verdict: RungVerdict
        if rung == "structural" and substrate not in _STRUCTURAL_SUBSTRATES:
            verdict = "unavailable" if available == "recorded" else "not_attempted"
        elif resolved_index is not None:
            if index == resolved_index:
                verdict = "resolved"
            elif index < resolved_index:
                verdict = "failed" if available == "recorded" else "not_attempted"
            else:
                verdict = "not_reached"
        elif not ladder_exhausted:
            verdict = "unknown"
        elif available == "recorded":
            verdict = "failed"
        elif available == "absent":
            verdict = "not_attempted"
        else:
            verdict = "unknown"
        rows.append({"rung": rung, "evidence": available, "verdict": verdict})
    return rows


def _will_recheck(
    *,
    step: Optional[Step],
    identity_required: Optional[int],
    identity_applicable: bool,
    delivery_state: str,
) -> list[dict[str, Any]]:
    """The contracts a continuation must re-prove, with their sizes.

    This is what "I fixed it" actually buys: the engine re-reads the live
    application and re-runs these, and refuses if any of them still fails.
    """
    checks: list[dict[str, Any]] = [{"check": "target_resolution", "count": None}]
    if identity_applicable:
        checks.append({"check": "record_identity", "count": identity_required})
    if step is not None:
        if step.expect:
            checks.append({"check": "postconditions", "count": len(step.expect)})
        if step.effects:
            checks.append(
                {"check": "system_of_record_effects", "count": len(step.effects)}
            )
    if delivery_state == "unknown":
        checks.append({"check": "delivery_reconciliation", "count": None})
    return checks


def _paused_step(
    run_dir: Path,
) -> tuple[Optional[PendingEscalation], Optional[Step], Optional[Path], int]:
    """Recover the halted step from the run's own durable manifest.

    Degrades to ``(pending, None, None, 0)`` for an encrypted or unreadable
    bundle: the detail loses the step's shape, never its correctness.  The
    bundle directory is used for reads only and is never projected -- a
    filesystem path is protected content on this boundary.
    """
    try:
        store = CheckpointStore(run_dir)
        pending = store.read_pending()
        manifest = store.read_manifest()
    except Exception:  # noqa: BLE001 - a locked or corrupt run stays answerable
        return None, None, None, 0
    if pending is None or manifest is None or not manifest.bundle_dir:
        return pending, None, None, 0
    bundle_dir = Path(manifest.bundle_dir)
    workflow: Optional[Workflow]
    workflow, _error = data.load_workflow_safe(bundle_dir)
    if workflow is None:
        return pending, None, None, 0
    step: Optional[Step] = None
    if 0 <= pending.step_index < len(workflow.steps):
        candidate = workflow.steps[pending.step_index]
        if candidate.id == pending.step_id:
            step = candidate
    if step is None:
        step = next((s for s in workflow.steps if s.id == pending.step_id), None)
    return pending, step, bundle_dir, len(workflow.steps)


def halt_detail(
    run_dir: Path,
    *,
    category: str,
    report: Any,
    failed: Any,
    substrate: str,
    delivery_state: str,
    identity_required: Optional[int],
) -> dict[str, Any]:
    """Project one open pause into the local, closed-vocabulary halt detail.

    Args:
        run_dir: The paused run's directory.
        category: The engine's typed halt category (``console.attention``).
        report: The loaded :class:`~openadapt_flow.ir.RunReport`, or None.
        failed: The last failed :class:`~openadapt_flow.ir.StepResult`, or None.
        substrate: The portable substrate label already derived for the task.
        delivery_state: ``not_delivered`` / ``delivered`` / ``unknown``.
        identity_required: Required identity signal count, when known.

    Returns:
        A JSON-safe mapping of closed enums, bounded integers and booleans.
    """
    pending, step, bundle_dir, step_count = _paused_step(run_dir)
    anchor = step.anchor if step is not None else None
    params: dict[str, str] = {}
    if report is not None and getattr(report, "params", None):
        params = runtime_params_for_gui(report.params)
    elif pending is not None:
        params = runtime_params_for_gui(pending.params)

    role, label = _safe_target_label(step, params)
    resolved_rung = None
    if failed is not None and getattr(failed, "resolution", None) is not None:
        resolved_rung = failed.resolution.rung
    identity_applicable = bool(
        step is not None
        and (
            step.identity_armed
            or (
                report is not None
                and step.id in getattr(report, "required_identity_step_ids", [])
            )
        )
    )
    return {
        # The engine's own typed category. Never a free-text reason.
        "category": category,
        "step_ordinal": (pending.step_index + 1) if pending is not None else None,
        "step_count": step_count or None,
        "action_kind": step.action.value if step is not None else None,
        "target_role": role,
        "target_label": label,
        # True when a label exists but could not be shown to be static control
        # chrome. The client says "the text field", not "the field you typed".
        "target_label_withheld": bool(
            anchor is not None
            and anchor.structural is not None
            and anchor.structural.name
            and label is None
        ),
        "resolution_ladder": _resolution_ladder(
            anchor=anchor,
            bundle_dir=bundle_dir,
            substrate=substrate,
            resolved_rung=resolved_rung,
            ladder_exhausted=category in {"resolution", "disambiguation"},
        ),
        "will_recheck": _will_recheck(
            step=step,
            identity_required=identity_required,
            identity_applicable=identity_applicable,
            delivery_state=delivery_state,
        ),
    }
