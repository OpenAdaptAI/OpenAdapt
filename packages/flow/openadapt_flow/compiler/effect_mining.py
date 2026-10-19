"""Auto-derive system-of-record ``Effect`` specs from a demonstration.

The compiler already mines *vision* postconditions (``compile._postconditions``)
and *structural* ones (URL/title/new-tab). It does NOT, on its own, produce the
typed ``Effect`` contracts (``record_written`` / ``field_equals``) that the
``EffectVerifier`` checks against the REAL system of record — those have been
hand-authored per workflow (see ``benchmark/silent_wrong_action._effect_verify``
and every ``tests/test_effect_*``). Both external reviews flagged exactly this:

    "the compiler emits Postconditions; auto-deriving the system-of-record
     binding (which field/endpoint maps to which step) is the missing piece."

This module closes that gap *honestly*, respecting the boundary the RFC draws
in ``docs/design/WORKFLOW_PROGRAM_IR.md`` §7:

- **Derivable now** — when the demonstration actually OBSERVED the system of
  record (a ``/api/db``-style JSON snapshot captured before and after a step,
  or a structured DOM field map), the concrete binding *is* in the recording:
  which record appeared, which field took the typed value, and (if present)
  the idempotency key. We mine those into real, machine-checkable ``Effect``s.

- **Not derivable — "irreducibly app-specific"** (§7) — when a step is a
  consequential write but the demonstration captured NO system-of-record
  observation, *which* API / record / idempotency-key the write maps to is
  customer-specific and simply is not in the recording. We refuse to INVENT an
  endpoint: we emit a clearly-flagged PLACEHOLDER effect
  (:attr:`Effect.needs_operator_confirmation`) that a run will not silently
  trust, and log the gap.

- **No signal at all** — a step with no observed SoR delta that is not marked
  consequential gets NO effect and an honest "no verifiable effect derivable"
  log line (never a fabricated green).

The miner makes ZERO model / network calls — it is pure heuristics over what
the demonstration already recorded (the compile-time $0 guarantee; a model at
compile time is a separate, later item).

Where the demonstration's SoR snapshots come from
-------------------------------------------------
The recorder attaches a snapshot of the system of record to each event when
the recording backend exposes one (``backend.SystemOfRecordBackend`` →
``recorder`` writes ``sor_before`` / ``sor_after`` on the event, exactly as it
already does for ``url_before`` / ``url_after``). A structured DOM field map,
when a backend captures one, arrives as ``dom_fields_before`` /
``dom_fields_after``. Backends that expose neither (pixel-only substrates)
simply never populate them, and the miner falls back to the honest placeholder
/ no-effect paths above.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from openadapt_flow.ir import ActionKind, PostconditionKind, Region, Step
from openadapt_flow.runtime.effects.effect import (
    MIN_RENAV_ACTIONS,
    Effect,
    EffectKind,
    ReadbackGuard,
    ReadbackNav,
    ReadbackReference,
    ReadbackSpec,
    ValueExpr,
    stable_id,
)


def _ve(value: str) -> ValueExpr:
    """Wrap a mined literal as a :class:`ValueExpr` (the demonstrated example)."""
    return ValueExpr(literal=value)


def _vemap(selector: dict[str, str]) -> dict[str, ValueExpr]:
    """Wrap a mined selector's literal values as :class:`ValueExpr`."""
    return {k: ValueExpr(literal=v) for k, v in selector.items()}


# Event keys carrying a captured system-of-record snapshot (list of record
# dicts) before / after the step. Written by the recorder from a
# ``SystemOfRecordBackend``; mirror the existing ``url_before`` convention.
SOR_BEFORE_KEY = "sor_before"
SOR_AFTER_KEY = "sor_after"

# Event keys carrying a structured DOM field map ({field_name: value}) before
# / after the step, when a backend captured one.
DOM_FIELDS_BEFORE_KEY = "dom_fields_before"
DOM_FIELDS_AFTER_KEY = "dom_fields_after"

# Record fields never used as an identity SELECTOR in a mined ``match``: a
# surrogate primary key is assigned by the system of record at write time, so
# it is unknown at compile time and cannot be part of the intended-record
# selector (it would only ever match the demo's row). Kept deliberately tiny —
# we mine the fields the demonstration actually observed and let the operator
# prune, rather than guess which app fields are "incidental".
SURROGATE_ID_FIELDS = frozenset({"id"})

# Field that commonly carries an at-most-once / idempotency key in a JSON
# system of record (the MockMed fault server uses ``key``). Only mined when the
# observed record actually carries a non-null value there — never invented.
IDEMPOTENCY_KEY_FIELD = "key"

# Sentinel selector for a PLACEHOLDER effect: a value no real record can carry,
# so even if a misconfigured run tried to verify it (it must not — the replayer
# HALTs on needs_operator_confirmation first), it could never falsely CONFIRM.
PLACEHOLDER_MATCH: dict[str, str] = {
    "__unbound_system_of_record__": "operator-must-bind"
}


@dataclass
class StepEffectMining:
    """The miner's decision for one step (for attachment + audit logging).

    ``disposition`` is one of:

    - ``"derived"``     — real effect(s) mined from an OBSERVED SoR / DOM delta;
    - ``"readback"``    — no SoR observed, but the demonstration viewed the
      saved value → an auto-derived on-screen read-back oracle (the no-API
      default for GUI-only recordings), instead of an unconditional HALT;
    - ``"placeholder"`` — consequential step, binding not derivable → a flagged
      placeholder effect the run must not silently trust;
    - ``"none"``        — no verifiable effect derivable (honest gap).
    """

    step_id: str
    effects: list[Effect] = field(default_factory=list)
    disposition: str = "none"
    reason: str = ""

    @property
    def derived(self) -> bool:
        return self.disposition == "derived"

    @property
    def readback(self) -> bool:
        return self.disposition == "readback"

    @property
    def placeholder(self) -> bool:
        return self.disposition == "placeholder"


def _as_records(value: Any) -> Optional[list[dict]]:
    """Coerce a captured SoR snapshot to a list of record dicts, or None.

    None means "no snapshot captured" (fall through to placeholder / none) —
    distinct from an empty list, which means "the SoR was observed and was
    empty" (a legitimate baseline: a first write appears against nothing).
    """
    if value is None:
        return None
    if isinstance(value, list):
        return [r for r in value if isinstance(r, dict)]
    return None


def _new_records(before: list[dict], after: list[dict]) -> list[dict]:
    """Records present in ``after`` that were not in ``before`` (by identity).

    Uses ``effects.stable_id`` — the same identity function the verifier's
    collateral-loss accounting uses — so "new" here means exactly what the
    runtime means by it.
    """
    before_ids = {stable_id(r) for r in before}
    return [r for r in after if stable_id(r) not in before_ids]


def _param_value_fields(record: dict, exclude_texts: tuple[str, ...]) -> dict[str, str]:
    """Fields of ``record`` whose value equals a demonstrated parameter value.

    These are the write's PAYLOAD (the typed note): each becomes a
    ``field_equals`` read-back. They are left out of the constant selector;
    once the parameters are confirmed, the ones bound to a parameter join the
    ``match`` selector bound to it (see :func:`_mine_from_sor_delta`).
    Comparison is on ``str()`` and exact (the value was typed verbatim;
    unlike OCR text there is no glyph ambiguity to fuzz over).
    """
    wanted = {v for v in exclude_texts if v}
    out: dict[str, str] = {}
    for key, value in record.items():
        if value is None:
            continue
        if str(value) in wanted:
            out[key] = str(value)
    return out


def _match_selector(record: dict, payload_fields: set[str]) -> dict[str, str]:
    """Identity selector for ``record``: its observed fields minus the
    surrogate id, the per-run payload fields, and null/empty fields.

    Everything left is a stable, observed identifier of the intended record
    (e.g. ``{"patient_id": "p1", "type": "Triage"}``). We keep ALL such
    observed fields rather than guess which are "incidental" — the mined
    workflow is reviewable, and an over-specific selector fails safe (it can
    only refuse, never falsely confirm a different record).
    """
    selector: dict[str, str] = {}
    for key, value in record.items():
        if key in SURROGATE_ID_FIELDS or key in payload_fields:
            continue
        if value is None or str(value) == "":
            continue
        selector[key] = str(value)
    return selector


def _mine_from_sor_delta(
    step: Step,
    before: list[dict],
    after: list[dict],
    exclude_texts: tuple[str, ...],
    param_bindings: Optional[Mapping[str, str]] = None,
) -> Optional[StepEffectMining]:
    """Mine real effects from an OBSERVED before/after system-of-record delta.

    Returns a ``derived`` :class:`StepEffectMining` when exactly one new record
    appeared (the consequential write we can bind concretely), or None when the
    snapshot shows no single-record write to bind (the caller then decides
    placeholder vs. none).
    """
    new_records = _new_records(before, after)
    if len(new_records) != 1:
        # 0 new records: this step wrote nothing observable. >1: the demo
        # conflated multiple writes into one step — we cannot bind a single
        # at-most-once contract honestly. Either way, nothing derivable here.
        return None
    record = new_records[0]
    payload = _param_value_fields(record, exclude_texts)
    selector = _match_selector(record, set(payload))
    if not selector:
        # A new record appeared but every non-payload field is the surrogate
        # id or empty — there is no stable identity to assert against.
        return None

    effects: list[Effect] = []

    # After a person confirms the parameters, a field holding a confirmed
    # parameter's value narrows the selector to records carrying this run's
    # value (a claim number, an order id). Match on it too, bound to the
    # parameter: otherwise the selector is only the demonstration's constant
    # fields, and on a table that already holds such records it matches every
    # earlier one. This does not make the check count only new records: a
    # record that already carried the same value would still match.
    bindings = dict(param_bindings or {})
    bound = {
        fname: ValueExpr(param=bindings[fval])
        for fname, fval in payload.items()
        if fval in bindings
    }
    match = {**_vemap(selector), **bound}

    written = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match=match,
        expected_count=1,
        risk=step.risk,
        probe=(
            "a record matching "
            + ", ".join(
                [f"{k}={v!r}" for k, v in selector.items()]
                + [f"{k}=<{bindings[v]}>" for k, v in payload.items() if v in bindings]
            )
            + " exists exactly once (mined from the demonstration's "
            "system-of-record delta)"
        ),
    )
    # Idempotency key: derivable ONLY when the observed record actually carries
    # one. Never invented — §7 names the idempotency key as app-specific.
    key_val = record.get(IDEMPOTENCY_KEY_FIELD)
    if key_val is not None and str(key_val) != "":
        written.idempotency_key = _ve(str(key_val))
        written.key_field = IDEMPOTENCY_KEY_FIELD
    effects.append(written)

    # field_equals per payload field: the typed value actually persisted (the
    # partial-save catch). Value is the DEMONSTRATED example (as the
    # hand-authored effects already bake ``value=NOTE``); per-run parameter
    # substitution of effect values is a separate runtime item.
    for fname, fval in payload.items():
        param = bindings.get(fval)
        effects.append(
            Effect(
                kind=EffectKind.FIELD_EQUALS,
                match=dict(match),
                field=fname,
                value=ValueExpr(param=param) if param else _ve(fval),
                risk=step.risk,
                probe=(
                    f"field {fname!r} of the written record equals the run's "
                    f"{param!r} parameter (mined; the demonstration typed that "
                    "parameter's value into this field)"
                    if param
                    else f"field {fname!r} of the written record equals the "
                    "demonstrated value (mined; the value is the recorded "
                    "example — substitute the run's parameter if this field "
                    "is parameterized)"
                ),
            )
        )

    reason = (
        f"derived {len(effects)} effect(s) from an observed system-of-record "
        f"delta (1 new record; selector {selector}"
        + (
            f"; idempotency key from field {IDEMPOTENCY_KEY_FIELD!r}"
            if written.idempotency_key is not None
            else "; no idempotency key observed"
        )
        + (
            f"; {len(payload)} field_equals read-back(s)"
            if payload
            else "; no parameterized field to read back"
        )
        + ")"
    )
    return StepEffectMining(
        step_id=step.id, effects=effects, disposition="derived", reason=reason
    )


def _mine_from_dom_delta(
    step: Step,
    before: dict,
    after: dict,
    exclude_texts: tuple[str, ...],
) -> Optional[StepEffectMining]:
    """Mine a ``field_equals`` from a structured DOM field map delta.

    When no system-of-record snapshot exists but a backend captured a
    structured field map, a field that CHANGED to a demonstrated (typed) value
    is a weaker, form-level effect: it asserts the value reached the field, not
    that it reached the record. Mined only for a field whose new value is a
    parameter value (the payload) and differs from its before value.
    """
    wanted = {v for v in exclude_texts if v}
    effects: list[Effect] = []
    changed: list[str] = []
    for fname, new_val in after.items():
        if new_val is None or str(new_val) not in wanted:
            continue
        if str(before.get(fname)) == str(new_val):
            continue  # already held the value before the action
        changed.append(fname)
        effects.append(
            Effect(
                kind=EffectKind.FIELD_EQUALS,
                match=_vemap({"field": fname}),
                field="value",
                value=_ve(str(new_val)),
                risk=step.risk,
                needs_operator_confirmation=True,
                probe=(
                    f"DOM field {fname!r} took the demonstrated value — a "
                    "form-level signal, NOT a system-of-record write. The "
                    "operator must bind this to the real record (which "
                    "endpoint/row persists this field) before it is trusted."
                ),
            )
        )
    if not effects:
        return None
    return StepEffectMining(
        step_id=step.id,
        effects=effects,
        # Marked placeholder: a DOM field write is not a system-of-record
        # write. We mined a concrete field/value, but the binding to the record
        # is still app-specific, so it must not be silently trusted.
        disposition="placeholder",
        reason=(
            f"mined {len(effects)} DOM field_equals candidate(s) for "
            f"{changed} — form-level only; flagged needs_operator_confirmation "
            "(no system-of-record snapshot to bind against)"
        ),
    )


def _norm_text(text: str) -> str:
    return " ".join(text.lower().split())


def _mine_onscreen_readback(
    step: Step,
    exclude_texts: tuple[str, ...],
) -> Optional[StepEffectMining]:
    """Auto-derive a SAME-SURFACE on-screen read-back effect from the demo.

    The out-of-the-box, no-API oracle: when the demonstration shows the person
    VIEWING the saved value after a consequential GUI write (a stable region
    that rendered the typed value), that view IS the oracle. We mine it into an
    ``onscreen`` ``field_equals`` effect whose :class:`ReadbackSpec` names the
    region to re-OCR and the value expected there — so the run re-reads the
    saved value instead of an unconditional HALT.

    Derived ONLY when BOTH are present in what the recording already captured:

    - a BOUNDED region that stabilized after the action (a ``REGION_STABLE``
      postcondition — where the saved value/status rendered), so the re-read is
      not an unbounded whole-frame guess; and
    - the demonstrated saved VALUE (a mined ``TEXT_PRESENT`` whose text carries
      a typed payload value, or — failing an explicit text match — the single
      demonstrated payload value paired with that stable region).

    Returned as ``different_path=False`` (SAME-SURFACE): it is the weak,
    NON-default signal. The compiler's cross-step pass
    (:func:`derive_different_path_readback`) UPGRADES it to the strong,
    default-eligible different-path oracle when the recording also contains a
    re-open of the record. When neither a bounded region nor a value is present
    we return ``None`` (the caller falls back to the honest placeholder HALT —
    there is nothing on screen to read back).
    """
    payloads = tuple(v for v in exclude_texts if v)
    if not payloads:
        return None

    stable_region: Optional[Region] = None
    for pc in step.expect:
        if pc.kind is PostconditionKind.REGION_STABLE and pc.region is not None:
            stable_region = pc.region
            break
    if stable_region is None:
        return None

    # The saved value: a payload value that a TEXT_PRESENT postcondition shows
    # is on screen, else (no explicit text_present) the single demonstrated
    # payload value bounded by the stable region.
    saved_value: Optional[str] = None
    present_texts = [
        _norm_text(pc.text)
        for pc in step.expect
        if pc.kind is PostconditionKind.TEXT_PRESENT and pc.text
    ]
    for payload in payloads:
        norm = _norm_text(payload)
        if norm and any(norm in t for t in present_texts):
            saved_value = payload
            break
    if saved_value is None and len(payloads) == 1:
        saved_value = payloads[0]
    if saved_value is None:
        return None

    effect = Effect(
        kind=EffectKind.FIELD_EQUALS,
        value=_ve(saved_value),
        risk=step.risk,
        readback=ReadbackSpec(region=stable_region, different_path=False),
        probe=(
            "SAME-SURFACE on-screen read-back: re-OCR the saved-value region "
            f"{stable_region} and require the demonstrated value "
            f"{saved_value!r}. Auto-derived from the demonstration's view of "
            "the saved record. A presence/consistency signal only — it re-reads "
            "the write's own surface and CANNOT catch a phantom/optimistic "
            "save; non-default until upgraded to a different-path re-open."
        ),
    )
    return StepEffectMining(
        step_id=step.id,
        effects=[effect],
        disposition="readback",
        reason=(
            "auto-derived a SAME-SURFACE on-screen read-back (no system-of-"
            f"record observed): region {stable_region}, expected value "
            f"{saved_value!r} (the demonstration viewed the saved value). "
            "Non-default (weak) until a different-path re-open is derived."
        ),
    )


def _placeholder(step: Step) -> StepEffectMining:
    """A flagged placeholder for a consequential step whose SoR binding is not
    derivable from the demonstration (§7 "irreducibly app-specific").

    We emit a ``record_written`` candidate with a SENTINEL selector (never a
    real endpoint) and ``needs_operator_confirmation=True``. The run refuses to
    silently trust it.
    """
    effect = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match=_vemap(dict(PLACEHOLDER_MATCH)),
        expected_count=1,
        risk=step.risk,
        needs_operator_confirmation=True,
        probe=(
            f"PLACEHOLDER for consequential step {step.id!r} ({step.intent}): "
            "the demonstration captured no system-of-record observation, so "
            "which API / record / idempotency-key this write maps to is "
            "app-specific and was NOT derived. Operator must bind it (and "
            "clear needs_operator_confirmation) before the run will verify it."
        ),
    )
    return StepEffectMining(
        step_id=step.id,
        effects=[effect],
        disposition="placeholder",
        reason=(
            "consequential (irreversible) step with no captured "
            "system-of-record delta — emitted a flagged placeholder "
            "(binding is app-specific, not derivable from the demo)"
        ),
    )


def _onscreen_readback_effect(step: Step) -> Optional[Effect]:
    """The step's mined SAME-SURFACE on-screen read-back effect, or None."""
    for eff in step.effects:
        rb = eff.readback
        if rb is not None and not rb.different_path and not rb.renavigation:
            return eff
    return None


def _as_readback_nav(step: Step) -> Optional[ReadbackNav]:
    """Turn a compiled step into a deterministic re-navigation action, or None.

    Only actions that replay WITHOUT per-run parameters or ambiguity qualify: a
    click at a recorded point, a literal (non-parameterized, non-secret) type,
    or a key press. A parameterized/secret type or an anchor-less click returns
    None — the caller then declines to build a different-path re-navigation
    (staying with the weaker same-surface read-back) rather than emit a
    re-navigation that would drive the wrong target at run time.
    """
    if step.action in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK):
        if step.anchor is None:
            return None
        px, py = step.anchor.click_point
        return ReadbackNav(action="click", point=(int(px), int(py)))
    if step.action is ActionKind.TYPE:
        if step.secret or step.param is not None or not step.text:
            return None
        return ReadbackNav(action="type", text=step.text)
    if step.action is ActionKind.SELECT_OPTION:
        # ReadbackNav cannot express the atomic focus/typeahead/commit contract.
        # Refuse to turn it into a lossy TYPE+KEY sequence during effect mining.
        return None
    if step.action is ActionKind.KEY:
        if not step.key:
            return None
        return ReadbackNav(action="key", key=step.key)
    return None


def _value_view_region(step: Step, norm_value: str) -> Optional[Region]:
    """A bounded region on ``step`` that RE-SHOWS ``norm_value``, or None.

    The re-view signal: the step's postconditions both assert the value is on
    screen (a ``TEXT_PRESENT`` carrying it) AND name a stabilized region (a
    ``REGION_STABLE``) to bound the re-read. Both are required so the
    different-path read is not an unbounded whole-frame guess.
    """
    shows_value = any(
        pc.kind is PostconditionKind.TEXT_PRESENT
        and pc.text is not None
        and norm_value in _norm_text(pc.text)
        for pc in step.expect
    )
    if not shows_value:
        return None
    for pc in step.expect:
        if pc.kind is PostconditionKind.REGION_STABLE and pc.region is not None:
            return pc.region
    return None


def _as_learned_nav(
    step: Step, demo_reference: Optional[str], secret_params: frozenset[str]
) -> Optional[ReadbackNav]:
    """A typing step that only replays with this run's data, or None.

    Typing the reference number the save showed becomes a ``reference`` step;
    typing a run input (a search for this run's patient) becomes a ``param``
    step. Secrets never replay.
    """
    if step.action is not ActionKind.TYPE or step.secret:
        return None
    if (
        demo_reference
        and step.text
        and _norm_text(step.text) == _norm_text(demo_reference)
    ):
        return ReadbackNav(action="type", reference=True)
    if step.param is not None and step.param not in secret_params:
        return ReadbackNav(action="type", param=step.param)
    return None


def _types_reference(nav: list[ReadbackNav]) -> bool:
    return any(step.reference for step in nav)


def _commit_labels(steps: list[Step]) -> list[str]:
    """Labels of the write flow's consequential controls, for the guard."""
    labels: list[str] = []
    for step in steps:
        if step.risk != "irreversible" or step.anchor is None:
            continue
        text = (step.anchor.ocr_text or "").strip()
        if text and text not in labels:
            labels.append(text)
    return labels


def _line_region(lines: Optional[list[Any]], norm_value: str) -> Optional[Region]:
    """The region of the OCR line that shows ``norm_value``, if exactly one."""
    hits = [
        line.region
        for line in lines or []
        if norm_value and norm_value in _norm_text(getattr(line, "text", ""))
    ]
    if len(hits) != 1:
        return None
    x, y, w, h = hits[0]
    return (int(x), int(y), int(w), int(h))


def derive_different_path_readback(
    steps: list[Step],
    *,
    references: Optional[Mapping[str, str]] = None,
    view_lines: Optional[Mapping[str, list[Any]]] = None,
    param_bindings: Optional[Mapping[str, str]] = None,
    secret_params: frozenset[str] = frozenset(),
) -> int:
    """Cross-step pass: UPGRADE a same-surface read-back to DIFFERENT-PATH.

    A same-surface read-back (mined by :func:`_mine_onscreen_readback`) is the
    weak, non-default signal. When the demonstration ALSO re-opened the written
    record by an independent path — navigating away from the write flow and
    back to re-view the saved value — that re-navigation is the strong,
    default-eligible oracle. This pass finds that pattern in the ALREADY-mined
    linear step list and rewrites the effect's :class:`ReadbackSpec` in place:
    ``different_path=True`` with the intervening navigation as
    :attr:`ReadbackSpec.renavigation` and the re-view's bounded region.

    Two shapes are recognized:

    - **Fixed navigation.** Every intervening step is a deterministic,
      non-parameterized action (:func:`_as_readback_nav`), there are at least
      :data:`MIN_RENAV_ACTIONS` of them, and the re-view re-shows the value in
      a bounded region (:func:`_value_view_region`). Default-eligible as before.
    - **Learned search.** The person found the record again by typing a run
      input (a search for this run's patient) or the reference number the
      save showed (``references``, from :func:`mine_reference`). The typing
      replays with this run's value. The navigation must contain a state
      reset (:func:`~openadapt_flow.compiler.readback_authoring.detect_reset`);
      without one the effect stays same-surface. The check is marked
      ``demonstrated`` with no admission, so it confirms nothing until a
      person admits it (``openadapt-flow check admit``). When the saved value
      is one run input's value (``param_bindings``), the check reads that
      input back, not the demonstrated literal.

    Every upgraded check carries a read-only :class:`ReadbackGuard` with the
    labels of the write flow's consequential controls. ``view_lines`` maps a
    step id to the OCR lines of its after-frame; it bounds the re-view when
    the step asserts no text (parameter values are never asserted).

    Conservative and fail-safe: an over-eager upgrade could only cause a false
    HALT, never a false CONFIRM. Returns the number of effects upgraded.

    Makes ZERO model / network calls (pure structure over the compiled steps).
    """
    from openadapt_flow.compiler.readback_authoring import detect_reset

    refs = dict(references or {})
    lines_by_step = dict(view_lines or {})
    bindings = dict(param_bindings or {})
    upgraded = 0
    for i, step in enumerate(steps):
        effect = _onscreen_readback_effect(step)
        if effect is None or effect.value is None:
            continue
        value = effect.value.resolve({})
        if not value:
            continue
        norm_value = _norm_text(value)
        prior = effect.readback
        reference = prior.reference if prior is not None else None
        demo_reference = refs.get(step.id) if reference is not None else None
        guard = ReadbackGuard(commit_labels=_commit_labels(steps[: i + 1]))
        nav: list[ReadbackNav] = []
        nav_steps: list[Step] = []
        learned = False
        for later in steps[i + 1 :]:
            navstep = _as_learned_nav(later, demo_reference, secret_params)
            if navstep is not None:
                learned = True
            else:
                navstep = _as_readback_nav(later)
            if navstep is None:
                break  # non-deterministic step — cannot build a safe re-nav
            nav.append(navstep)
            nav_steps.append(later)
            region = _value_view_region(later, norm_value)
            if region is None and learned:
                region = _line_region(lines_by_step.get(later.id), norm_value)
            if region is None:
                continue
            if learned:
                reset = detect_reset(nav_steps)
                if reset is None:
                    break  # no state reset: stays same-surface
                effect.readback = ReadbackSpec(
                    region=region,
                    different_path=True,
                    renavigation=list(nav),
                    demonstrated=True,
                    reset=reset,
                    guard=guard,
                    # Kept for a search by a run input too: the reopened
                    # record must show this save's number, or an older
                    # record with the same value could pass.
                    reference=reference,
                )
                if value in bindings:
                    effect.value = ValueExpr(param=bindings[value])
                effect.probe = (
                    "LEARNED-SEARCH on-screen read-back: find the record again "
                    f"by the demonstrated search ({len(nav)} action(s), "
                    f"{reset.kind.replace('_', ' ')} detected), then re-OCR "
                    f"region {region}. Needs a person's admission before it "
                    "can confirm a save."
                )
                upgraded += 1
                break
            if len(nav) >= MIN_RENAV_ACTIONS:
                # Reopen credit needs a state reset inside the navigation;
                # without one the spec stays different-path but earns only
                # the same-screen level (onscreen._is_persisted_reacquisition).
                effect.readback = ReadbackSpec(
                    region=region,
                    different_path=True,
                    renavigation=list(nav),
                    reset=detect_reset(nav_steps),
                    guard=guard,
                    reference=reference if _types_reference(nav) else None,
                )
                effect.probe = (
                    "DIFFERENT-PATH on-screen read-back: re-open the record by "
                    f"an independent path ({len(nav)} recorded re-navigation "
                    f"action(s)) then re-OCR region {region} for the "
                    f"demonstrated value {value!r}. Re-opening forces a real "
                    "fetch, defeating the phantom/optimistic-save class; "
                    "default-eligible. Still same-APPLICATION — see "
                    "docs/LIMITS.md."
                )
                upgraded += 1
                break
    return upgraded


# -- reference numbers -----------------------------------------------------

_TOKEN = re.compile(r"[A-Za-z0-9#][A-Za-z0-9#._-]*[A-Za-z0-9]")
_DATE_LIKE = re.compile(r"^\d{1,4}[-.]\d{1,2}[-.]\d{1,4}$")


def _reference_candidates(text: str) -> list[str]:
    out = []
    for match in _TOKEN.finditer(text):
        token = match.group(0).lstrip("#")
        digits = sum(ch.isdigit() for ch in token)
        if digits < 3 or not 4 <= len(token) <= 32 or _DATE_LIKE.match(token):
            continue
        out.append(token)
    return out


def reference_pattern(token: str) -> str:
    """A pattern for numbers shaped like ``token``, with one capture group.

    Letter runs and separators are kept as shown (``CLM-``); each digit run
    may be a little shorter or longer than the demonstrated one.
    """
    body = []
    for run in re.findall(r"\d+|[A-Za-z]+|[^A-Za-z0-9]", token):
        if run.isdigit():
            body.append(r"\d{%d,%d}" % (max(1, len(run) - 2), len(run) + 3))
        else:
            body.append(re.escape(run))
    return r"(?<![A-Za-z0-9])(" + "".join(body) + r")(?![A-Za-z0-9])"


def mine_reference(
    before_lines: Optional[list[Any]],
    after_lines: Optional[list[Any]],
    exclude_texts: tuple[str, ...] = (),
) -> Optional[tuple[ReadbackReference, str]]:
    """The reference number the save's screen newly shows, or None.

    A candidate is a token with at least three digits that is on the
    after-frame, on no line of the before-frame, and not typed by the person
    (``exclude_texts``). Exactly one distinct candidate must remain;
    otherwise nothing is learned. Returns the capture and the demonstrated
    value (used only at compile time, to recognize a later search by it).
    """
    if not after_lines:
        return None
    before_text = " ".join(getattr(line, "text", "") for line in before_lines or [])
    before_tokens = set(_reference_candidates(before_text))
    typed = {_norm_text(v) for v in exclude_texts if v}
    found: dict[str, Any] = {}
    for line in after_lines:
        for token in _reference_candidates(getattr(line, "text", "")):
            if token in before_tokens or _norm_text(token) in typed:
                continue
            if any(_norm_text(token) in t for t in typed):
                continue
            found.setdefault(token, line)
    if len(found) != 1:
        return None
    token, line = next(iter(found.items()))
    x, y, w, h = (int(v) for v in line.region)
    pad_x = max(w // 2, 40)
    region = (max(0, x - pad_x), max(0, y - h), w + 2 * pad_x, 3 * h)
    return ReadbackReference(region=region, pattern=reference_pattern(token)), token


def mine_step_effects(
    event: dict,
    step: Step,
    *,
    exclude_texts: tuple[str, ...] = (),
    param_bindings: Optional[Mapping[str, str]] = None,
) -> StepEffectMining:
    """Derive candidate system-of-record effects for one compiled step.

    Precedence (honest, most-concrete-first):

    1. An OBSERVED system-of-record delta (``sor_before`` / ``sor_after`` on
       the event) with exactly one new record → real ``record_written`` (+
       ``field_equals`` per typed field, + idempotency key iff observed).
    2. Otherwise a structured DOM field map (``dom_fields_*``) whose field took
       the typed value → a form-level ``field_equals``, flagged
       ``needs_operator_confirmation`` (not a record write).
    3. Otherwise, if the demonstration VIEWED the saved value (a stable region
       rendered the typed payload) → an auto-derived on-screen READ-BACK effect
       (:func:`_mine_onscreen_readback`) — the no-API default oracle, instead
       of an unconditional HALT. This is what replaces today's dead-end
       placeholder for a GUI-only consequential write whose value is on screen.
    4. Otherwise, if the step is consequential (``risk == "irreversible"``) →
       a flagged PLACEHOLDER ``record_written`` (binding app-specific; there is
       nothing on screen to read back).
    5. Otherwise → NO effect and a "no verifiable effect derivable" reason.

    Args:
        event: The recorded event dict (may carry ``sor_before`` /
            ``sor_after`` / ``dom_fields_*``).
        step: The compiled step (its ``risk`` and ``id`` are used).
        exclude_texts: Demonstrated parameter values (the typed payload), used
            to split a record's payload fields (→ ``field_equals``) from its
            identity fields (→ ``match``).
        param_bindings: Optional demonstrated value -> parameter name. A
            ``field_equals`` whose value is one of these reads back the run's
            parameter (``ValueExpr(param=...)``) instead of the demonstrated
            literal. Only system-of-record deltas use it. ``None`` keeps the
            literal, as before.

    Returns:
        A :class:`StepEffectMining` describing the decision. ``effects`` is
        empty for the ``"none"`` disposition.
    """
    before = _as_records(event.get(SOR_BEFORE_KEY))
    after = _as_records(event.get(SOR_AFTER_KEY))
    if before is not None and after is not None:
        mined = _mine_from_sor_delta(step, before, after, exclude_texts, param_bindings)
        if mined is not None:
            return mined
        # A snapshot existed but showed no single-record write. If the step is
        # consequential, that is itself worth surfacing; otherwise it is an
        # honest no-op (e.g. a navigation click that touched no record).
        if step.risk == "irreversible":
            ph = _placeholder(step)
            ph.reason = (
                "system-of-record snapshot captured but showed no single new "
                "record for this consequential step — emitted a flagged "
                "placeholder (the write may be an update/multi-row the miner "
                "cannot bind to one at-most-once contract)"
            )
            return ph
        return StepEffectMining(
            step_id=step.id,
            disposition="none",
            reason=(
                "system-of-record snapshot captured but no new record appeared "
                "after this step — no verifiable effect derivable"
            ),
        )

    dom_before = event.get(DOM_FIELDS_BEFORE_KEY)
    dom_after = event.get(DOM_FIELDS_AFTER_KEY)
    if isinstance(dom_before, dict) and isinstance(dom_after, dict):
        mined = _mine_from_dom_delta(step, dom_before, dom_after, exclude_texts)
        if mined is not None:
            return mined

    # No independent system-of-record and no structured DOM map (the pilot-
    # shaped GUI-only case: Citrix / legacy EMR). If the demonstration VIEWED
    # the saved value, derive an on-screen read-back oracle instead of an
    # unconditional HALT — the out-of-the-box, no-connector default.
    readback = _mine_onscreen_readback(step, exclude_texts)
    if readback is not None:
        return readback

    if step.risk == "irreversible":
        return _placeholder(step)

    return StepEffectMining(
        step_id=step.id,
        disposition="none",
        reason=(
            "no system-of-record observation captured and the step is not "
            "marked consequential — no verifiable effect derivable"
        ),
    )
