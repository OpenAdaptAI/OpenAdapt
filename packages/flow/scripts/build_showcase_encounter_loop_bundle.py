#!/usr/bin/env python3
"""Regenerate the COMPACT data-driven-LOOP showcase (``docs/showcase-encounter-loop``).

A small, legible companion to ``docs/showcase-loop`` (which wraps the 18-step
OpenEMR recording). Where that bundle proves the authoring wire on a real
recording, this one is deliberately tiny -- three steps whose consequential
``save`` carries PARAM-BOUND system-of-record effects -- so its program graph
reads at a glance and its ``visualize`` output fits in a README while still
showing the load-bearing structure: the ``LOOP``, the per-record body, the
effect check, the irreversible-write halt points, and the loop-back edge.

It emits TWO bundles from ONE authored ``save-encounter`` body:

* ``body/``   -- the single demonstration, a linear ``program:false`` bundle
                (the straight-line case ``visualize`` renders as a chain);
* ``bundle/`` -- that same body wrapped by ``author_data_driven_loop`` in a
                ``LOOP`` over ``worklist.csv`` (``program:true``), which the
                Phase-2 interpreter runs once per record, bounded, ``$0``,
                identity-gated and effect-verified per record.

The body is a MockMed-shaped fixture (no PHI, no recorded pixels): the same
``save-encounter`` shape ``tests/test_loop_authoring.py`` replays end-to-end
against the in-process MockMed system of record, so the emitted loop is a real,
interpreted program -- not a hand-drawn diagram.

Run from the repo root::

    python scripts/build_showcase_encounter_loop_bundle.py

Deterministic: the emitted bundles are a pure function of the body defined here
and ``worklist.csv``, so re-running reproduces the committed artifacts.
"""

from __future__ import annotations

import csv
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Run against THIS checkout's package, not a globally-installed one.
sys.path.insert(0, str(REPO))

from openadapt_flow.compiler.loop_authoring import (  # noqa: E402
    author_data_driven_loop,
)
from openadapt_flow.ir import (  # noqa: E402
    ActionKind,
    ParamSpec,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.runtime.effects import (  # noqa: E402
    Effect,
    EffectKind,
    ValueExpr,
)

OUT_DIR = REPO / "docs" / "showcase-encounter-loop"
WORKLIST = OUT_DIR / "worklist.csv"
BODY_OUT = OUT_DIR / "body"
LOOP_OUT = OUT_DIR / "bundle"


def _encounter_body() -> Workflow:
    """A single-demonstration 'save encounter' body: type the patient id, type
    the note, then save. The save is a consequential, irreversible write whose
    typed, PARAM-BOUND system-of-record effects verify each record against the
    value it actually wrote (RFC docs/design/EFFECT_VERIFIER.md; P0-3)."""
    return Workflow(
        name="save-encounter",
        # Pinned so re-running this builder reproduces the committed bundle
        # byte-for-byte (the default would stamp the current time).
        created_at="2026-07-21T00:00:00+00:00",
        steps=[
            Step(
                id="type_patient",
                intent="type <patient_id>",
                action=ActionKind.TYPE,
                param="patient_id",
            ),
            Step(
                id="type_note",
                intent="type <note>",
                action=ActionKind.TYPE,
                param="note",
            ),
            Step(
                id="save",
                intent="save encounter",
                action=ActionKind.KEY,
                key="Enter",
                risk="irreversible",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="Saved",
                        timeout_s=0.2,
                    )
                ],
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={
                            "patient_id": ValueExpr(param="patient_id"),
                            "type": "Triage",
                        },
                        expected_count=1,
                        timeout_s=2.0,
                    ),
                    Effect(
                        kind=EffectKind.FIELD_EQUALS,
                        match={"patient_id": ValueExpr(param="patient_id")},
                        field="note",
                        value=ValueExpr(param="note"),
                        timeout_s=2.0,
                    ),
                ],
            ),
        ],
        param_specs={
            "patient_id": ParamSpec(name="patient_id", example="patient-1"),
            "note": ParamSpec(name="note", example="triage note"),
        },
    )


def _read_worklist(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as fh:
        return [
            {str(k): str(v) for k, v in row.items() if k is not None}
            for row in csv.DictReader(fh)
        ]


def _reset(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def main() -> int:
    body = _encounter_body()
    records = _read_worklist(WORKLIST)

    looped = author_data_driven_loop(
        body,
        records,
        loop_var="encounter",
        name="save-encounter-for-each",
    )

    _reset(BODY_OUT)
    body.save(BODY_OUT)
    _reset(LOOP_OUT)
    looped.save(LOOP_OUT)

    print(
        f"Wrote {BODY_OUT} (program:false): {len(body.steps)} demonstrated steps."
    )
    print(
        f"Wrote {LOOP_OUT} (program:true): {len(records)} records over relation "
        f"'worklist', body = {len(body.steps)} demonstrated steps."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
