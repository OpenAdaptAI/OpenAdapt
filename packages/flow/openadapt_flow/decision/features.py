"""Deterministic features for one verified save.

Everything here is computed on this computer from evidence Flow already has:
the record check's read-back, the item's parameters against earlier approved
items of the same workflow, how the parameters were filled (``intake``), and
how often people declined this workflow recently. No model is called.

The output has two parts. :attr:`SaveFeatures.values` is the provider-safe
vector (see :mod:`openadapt_flow.decision.minimize`). :attr:`SaveFeatures.flags`
is local: it names the fields and parameters that failed a check so the
reviewer can see why an item was flagged. It is never put in a
:class:`~openadapt_flow.decision.provider.DecisionRequest`.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.decision.minimize import (
    FieldComparison,
    assert_minimized,
    minimize_comparisons,
    normalize,
)

#: How the record check ended for the save. ``none`` means no read-back ran.
ReadbackResult = Literal["confirmed", "refuted", "indeterminate", "none"]
#: How a parameter is compared with history. ``free`` parameters (names,
#: dates, record numbers, notes) differ on every item and are skipped.
ParamKind = Literal["number", "choice", "free"]
IntakeSource = Literal["document_text", "ocr", "agent", "manual", "none"]

_Z_CAP = 10.0
_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class IntakeFieldEvidence(BaseModel):
    """How one parameter was filled. Built from an ``IntakeResult`` field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=_ID)
    source: IntakeSource = "none"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    confirmed_by_person: bool = False


class NumericStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = 0
    mean: float = 0.0
    m2: float = 0.0
    minimum: Optional[float] = None
    maximum: Optional[float] = None

    def add(self, value: float) -> None:
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self.m2 += delta * (value - self.mean)
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)

    @property
    def std(self) -> float:
        return math.sqrt(self.m2 / (self.count - 1)) if self.count > 1 else 0.0


def value_digest(name: str, value: str) -> str:
    """Digest of a normalised choice value. Local history only."""

    payload = f"{name}\0{normalize('value', value)}".encode()
    return hashlib.sha256(payload).hexdigest()


class ParamHistory(BaseModel):
    """Parameters of earlier approved items of one workflow.

    Numbers are kept as running statistics and choices as value digests.
    Free-text parameters are never recorded.
    """

    model_config = ConfigDict(extra="forbid")

    items: int = 0
    numeric: dict[str, NumericStats] = Field(default_factory=dict)
    seen: dict[str, list[str]] = Field(default_factory=dict)

    def observe(self, params: dict[str, str], kinds: dict[str, ParamKind]) -> None:
        self.items += 1
        for name, raw in params.items():
            kind = kinds.get(name, "free")
            if kind == "number":
                number = _as_float(raw)
                if number is not None:
                    self.numeric.setdefault(name, NumericStats()).add(number)
            elif kind == "choice":
                digest = value_digest(name, raw)
                bucket = self.seen.setdefault(name, [])
                if digest not in bucket:
                    bucket.append(digest)


class WorkflowHistory(BaseModel):
    """Recent review outcomes for the workflow, as counts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recent_items: int = Field(default=0, ge=0)
    recent_declines: int = Field(default=0, ge=0)


class SaveObservation(BaseModel):
    """Everything triage knows about one save, before minimisation. Local."""

    model_config = ConfigDict(extra="forbid")

    #: Opaque id of the item (one run). Never a record key.
    item_id: str = Field(pattern=r"^[0-9a-f]{24}$")
    #: Opaque digest of the workflow and save step; the calibration group.
    group_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    #: The run's execution outcome (``VERIFIED``, ``HALTED_BEFORE_EFFECT``...).
    outcome: str = Field(default="UNKNOWN", max_length=64)
    readback: ReadbackResult = "none"
    readback_reconciled: bool = False
    #: No repair, model call, rescue or lower rung in the run (R2).
    run_clean: bool = False
    comparisons: list[FieldComparison] = Field(default_factory=list)
    params: dict[str, str] = Field(default_factory=dict)
    param_kinds: dict[str, ParamKind] = Field(default_factory=dict)
    intake: list[IntakeFieldEvidence] = Field(default_factory=list)
    history: ParamHistory = Field(default_factory=ParamHistory)
    workflow: WorkflowHistory = Field(default_factory=WorkflowHistory)

    def halted(self) -> bool:
        """True when the engine itself stopped on this save.

        A refuted or indeterminate read-back, or any outcome but
        ``VERIFIED``, is a halt. Triage reports it and never scores it.
        """

        return self.readback in ("refuted", "indeterminate") or self.outcome != (
            "VERIFIED"
        )

    def digest(self) -> str:
        """Binds what the reviewer is shown. Local; holds no value itself."""

        payload = self.model_dump_json().encode()
        return hashlib.sha256(payload).hexdigest()


class SaveFeatures(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    values: dict[str, bool | int | float]
    #: ``(field_or_param_name, code)``. Local reviewer copy only.
    flags: tuple[tuple[str, str], ...] = ()


def _as_float(raw: Any) -> Optional[float]:
    try:
        value = float(str(raw).replace(",", ""))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def extract_features(
    observation: SaveObservation, *, intake_floor: float = 0.9
) -> SaveFeatures:
    """Compute the feature vector and the reviewer's flags for one save."""

    facts, flags = minimize_comparisons(observation.comparisons)
    out: dict[str, Any] = dict(facts)
    out["readback_confirmed"] = observation.readback == "confirmed"
    out["readback_reconciled"] = observation.readback_reconciled
    out["run_clean"] = observation.run_clean

    history = observation.history
    max_z = 0.0
    out_of_range = 0
    unseen = 0
    for name, raw in sorted(observation.params.items()):
        kind = observation.param_kinds.get(name, "free")
        if kind == "number":
            number = _as_float(raw)
            stats = history.numeric.get(name)
            if number is None or stats is None or stats.count < 2:
                continue
            std = stats.std
            if std > 0:
                max_z = max(max_z, min(_Z_CAP, abs(number - stats.mean) / std))
            elif number != stats.mean:
                max_z = _Z_CAP
            assert stats.minimum is not None and stats.maximum is not None
            if number < stats.minimum or number > stats.maximum:
                out_of_range += 1
                flags.append((name, "param_out_of_range"))
        elif kind == "choice":
            if value_digest(name, raw) not in history.seen.get(name, []):
                unseen += 1
                flags.append((name, "param_unseen_value"))
    out["param_numeric_max_abs_z"] = float(max_z)
    out["param_out_of_range"] = out_of_range
    out["param_unseen_values"] = unseen
    out["param_history_items"] = history.items

    min_conf = 1.0
    below = 0
    unconfirmed = 0
    from_ocr = False
    from_agent = False
    for item in observation.intake:
        if item.source == "none":
            continue
        from_ocr = from_ocr or item.source == "ocr"
        from_agent = from_agent or item.source == "agent"
        if item.confirmed_by_person:
            continue
        unconfirmed += 1
        min_conf = min(min_conf, item.confidence)
        if item.confidence < intake_floor:
            below += 1
            flags.append((item.name, "intake_low_confidence"))
    out["intake_min_confidence"] = float(min_conf)
    out["intake_below_floor"] = below
    out["intake_unconfirmed"] = unconfirmed
    out["intake_from_ocr"] = from_ocr
    out["intake_from_agent"] = from_agent

    wf = observation.workflow
    out["workflow_recent_declines"] = wf.recent_declines
    out["workflow_decline_rate"] = (
        float(wf.recent_declines) / wf.recent_items if wf.recent_items else 0.0
    )
    return SaveFeatures(values=assert_minimized(out), flags=tuple(flags))


def opaque_item_id(run_dir: Path) -> str:
    return hashlib.sha256(str(Path(run_dir).resolve()).encode()).hexdigest()[:24]


def group_key(workflow_name: str, step_id: str) -> str:
    """The calibration group: one workflow's one save step."""

    return hashlib.sha256(f"{workflow_name}\0{step_id}".encode()).hexdigest()


def observation_from_report(
    report: Any,
    *,
    run_dir: Path,
    comparisons: Optional[list[FieldComparison]] = None,
    param_kinds: Optional[dict[str, ParamKind]] = None,
    intake: Optional[list[IntakeFieldEvidence]] = None,
    history: Optional[ParamHistory] = None,
    workflow: Optional[WorkflowHistory] = None,
) -> SaveObservation:
    """Build an observation from a :class:`~openadapt_flow.ir.RunReport`.

    The report gives the outcome, the record check's final verdicts and
    whether the run stayed on its recorded path. It doesn't keep the
    read-back values, so per-field ``comparisons`` come from the caller that
    held the verifier's results; without them, ``all_fields_exact`` is false
    and the v0 rule won't call the item clean.
    """

    evidence = [
        item for result in report.results for item in (result.effect_evidence or [])
    ]
    finals = {item.final_verdict for item in evidence}
    readback: ReadbackResult
    if not evidence:
        readback = "none"
    elif "refuted" in finals:
        readback = "refuted"
    elif "indeterminate" in finals:
        readback = "indeterminate"
    else:
        readback = "confirmed"
    reconciled = any(
        item.reconciliation_completed or item.initial_verdict != item.final_verdict
        for item in evidence
    )
    run_clean = report.model_calls == 0
    approval = getattr(report, "approval", None)
    if approval is not None:
        for check in approval.post_checks:
            if check.code == "whole_run_clean":
                run_clean = run_clean and check.passed
    save_steps = [r.step_id for r in report.results if r.effect_evidence]
    step = save_steps[-1] if save_steps else "<none>"
    params = {k: str(v) for k, v in (report.params or {}).items()}
    return SaveObservation(
        item_id=opaque_item_id(run_dir),
        group_key=group_key(report.workflow_name, step),
        outcome=report.execution_outcome or "UNKNOWN",
        readback=readback,
        readback_reconciled=reconciled,
        run_clean=run_clean,
        comparisons=list(comparisons or []),
        params=params,
        param_kinds=dict(param_kinds or {}),
        intake=list(intake or []),
        history=history or ParamHistory(),
        workflow=workflow or WorkflowHistory(),
    )


def intake_evidence(result: Any, *, source: IntakeSource) -> list[IntakeFieldEvidence]:
    """Per-parameter intake evidence from an ``intake.extract.IntakeResult``."""

    if result.mode == "agent":
        source = "agent"
    return [
        IntakeFieldEvidence(
            name=name,
            source=source,
            confidence=max(0.0, min(1.0, float(field.confidence))),
            confirmed_by_person=bool(field.confirmed),
        )
        for name, field in result.fields.items()
    ]


__all__ = [
    "IntakeFieldEvidence",
    "ParamHistory",
    "SaveFeatures",
    "SaveObservation",
    "WorkflowHistory",
    "extract_features",
    "group_key",
    "intake_evidence",
    "observation_from_report",
    "opaque_item_id",
]
