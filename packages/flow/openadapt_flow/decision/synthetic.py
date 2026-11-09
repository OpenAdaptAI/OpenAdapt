"""Synthetic saves with known faults, for tests and the evaluation script.

The generator models one data-entry workflow (a name, a date of birth, a
record number, a visit type, a unit count and a free note) and injects the
faults the decline reasons name. Some faults are visible to the record check
(it refutes the save, so the engine halts), and some aren't: a wrong value
typed into the note, which the read-back doesn't read, or a value the
document itself got wrong, which the read-back confirms as typed. Those
silent faults are what triage has to route to a person.

Every value is fake. The rates are invented; results on this data say how
the mechanism behaves, not how a real workflow will.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from openadapt_flow.decision.features import (
    IntakeFieldEvidence,
    ParamHistory,
    ParamKind,
    SaveObservation,
    WorkflowHistory,
    group_key,
)
from openadapt_flow.decision.minimize import FieldComparison

VISIT_TYPES = ("new", "follow_up", "annual", "urgent")
RARE_VISIT = "telehealth"
PARAM_KINDS: dict[str, ParamKind] = {
    "patient_name": "free",
    "dob": "free",
    "mrn": "free",
    "visit_type": "choice",
    "units": "number",
    "note": "free",
}
GROUP = group_key("synthetic-intake", "save")


@dataclass(frozen=True)
class FaultRates:
    wrong_record_detected: float = 0.02
    wrong_record_silent: float = 0.004
    wrong_values_detected: float = 0.02
    wrong_values_silent: float = 0.01
    screen_problem: float = 0.01
    #: Good saves that still look unusual (benign noise).
    benign_low_confidence: float = 0.06
    benign_outlier: float = 0.02
    benign_new_choice: float = 0.01
    benign_unread_field: float = 0.03


@dataclass(frozen=True)
class SyntheticSave:
    observation: SaveObservation
    #: ``approved`` or the decline reason a careful person would pick.
    truth: str

    @property
    def bad(self) -> bool:
        return self.truth in ("wrong_values", "wrong_record")


def _id(rng: np.random.Generator) -> str:
    return "".join(rng.choice(list("0123456789abcdef"), 24))


def generate(
    n: int,
    *,
    seed: int = 0,
    rates: Optional[FaultRates] = None,
    history: Optional[ParamHistory] = None,
    recent: Optional[list[str]] = None,
) -> list[SyntheticSave]:
    """``n`` saves in order. History and decline counts roll forward."""

    r = rates or FaultRates()
    rng = np.random.default_rng(seed)
    hist = history.model_copy(deep=True) if history else ParamHistory()
    window = list(recent or [])
    out: list[SyntheticSave] = []
    for _ in range(n):
        name = f"Patient {int(rng.integers(1, 10**6))}"
        dob = f"19{int(rng.integers(30, 99))}-0{int(rng.integers(1, 9))}-1{int(rng.integers(0, 9))}"
        mrn = f"MRN-{int(rng.integers(10**5, 10**6))}"
        visit = str(rng.choice(VISIT_TYPES))
        units = float(np.round(rng.normal(10, 2), 1))
        note = f"note {int(rng.integers(0, 1000))}"
        conf = float(rng.uniform(0.93, 1.0))
        source = "ocr" if rng.random() < 0.3 else "document_text"
        readback = "confirmed"
        run_clean = True
        unread_note = rng.random() < 0.5
        truth = "approved"

        u = rng.random()
        edges = np.cumsum(
            [
                r.wrong_record_detected,
                r.wrong_record_silent,
                r.wrong_values_detected,
                r.wrong_values_silent,
                r.screen_problem,
            ]
        )
        observed = {
            "patient_name": name,
            "dob": dob,
            "mrn": mrn,
            "visit_type": visit,
            "units": str(units),
            "note": note,
        }
        if u < edges[0]:
            truth, readback = "wrong_record", "refuted"
            observed["mrn"] = f"MRN-{int(rng.integers(10**5, 10**6))}"
        elif u < edges[1]:
            # The document named the wrong patient; everything typed matches
            # what was read back. Usually an OCR read with lower confidence.
            truth = "wrong_record"
            if rng.random() < 0.7:
                conf = float(rng.uniform(0.6, 0.9))
                source = "ocr"
        elif u < edges[2]:
            truth, readback = "wrong_values", "refuted"
            observed["units"] = str(units + 5)
        elif u < edges[3]:
            truth = "wrong_values"
            kind = rng.random()
            if kind < 0.4:
                observed["note"] = "something else"
                unread_note = True
            elif kind < 0.7:
                units = float(np.round(units * 10, 1))
                observed["units"] = str(units)
            else:
                conf = float(rng.uniform(0.5, 0.9))
                if rng.random() < 0.5:
                    visit = RARE_VISIT
                    observed["visit_type"] = visit
        elif u < edges[4]:
            truth, run_clean = "screen_problem", False
        else:
            if rng.random() < r.benign_low_confidence:
                conf = float(rng.uniform(0.7, 0.9))
            if rng.random() < r.benign_outlier:
                units = float(np.round(rng.normal(10, 2) + 8, 1))
                observed["units"] = str(units)
            if rng.random() < r.benign_new_choice:
                visit = RARE_VISIT
                observed["visit_type"] = visit
            if rng.random() < r.benign_unread_field:
                observed["dob"] = None  # type: ignore[assignment]

        params = {
            "patient_name": name,
            "dob": dob,
            "mrn": mrn,
            "visit_type": visit,
            "units": str(units),
            "note": note,
        }
        kinds = {
            "patient_name": "identity",
            "dob": "date",
            "mrn": "id",
            "visit_type": "choice",
            "units": "number",
            "note": "value",
        }
        comparisons = [
            FieldComparison(
                name=field,
                kind=kinds[field],  # type: ignore[arg-type]
                expected=params[field],
                observed=observed[field],
            )
            for field in params
            if not (field == "note" and unread_note)
        ]
        intake = [
            IntakeFieldEvidence(name=field, source=source, confidence=conf)  # type: ignore[arg-type]
            for field in ("patient_name", "dob", "mrn", "units")
        ]
        declines = sum(1 for t in window[-20:] if t != "approved")
        obs = SaveObservation(
            item_id=_id(rng),
            group_key=GROUP,
            outcome="VERIFIED" if readback == "confirmed" else "HALTED",
            readback=readback,  # type: ignore[arg-type]
            run_clean=run_clean,
            comparisons=comparisons,
            params=params,
            param_kinds=dict(PARAM_KINDS),
            intake=intake,
            history=hist.model_copy(deep=True),
            workflow=WorkflowHistory(
                recent_items=min(20, len(window)), recent_declines=declines
            ),
        )
        out.append(SyntheticSave(observation=obs, truth=truth))
        if readback == "confirmed":
            window.append(truth)
            if truth == "approved":
                hist.observe(params, PARAM_KINDS)
    return out


__all__ = ["GROUP", "FaultRates", "SyntheticSave", "generate"]
