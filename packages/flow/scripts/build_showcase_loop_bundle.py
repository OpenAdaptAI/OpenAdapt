#!/usr/bin/env python3
"""Regenerate the data-driven-LOOP showcase bundle (``docs/showcase-loop``).

Proof that the demonstration -> governed-loop AUTHORING WIRE is reachable
end-to-end on a REAL shipped bundle: it takes the existing single-demonstration
OpenEMR showcase bundle (``docs/showcase-openemr/bundle``, ``program:false``)
and wraps its demonstrated linear body in a ``LOOP`` over the worklist in
``docs/showcase-loop/worklist.csv`` -- emitting a ``program:true`` bundle the
Phase-2 interpreter runs once per record, bounded, ``$0``, identity-gated and
effect-verified per record.

Run from the repo root::

    python scripts/build_showcase_loop_bundle.py

Deterministic: the emitted bundle is a pure function of the source bundle and
the worklist, so re-running reproduces the committed artifact.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Run against THIS checkout's package, not a globally-installed one.
sys.path.insert(0, str(REPO))

from openadapt_flow.compiler.loop_authoring import author_data_driven_loop  # noqa: E402
from openadapt_flow.ir import Workflow  # noqa: E402

SOURCE = REPO / "docs" / "showcase-openemr" / "bundle"
WORKLIST = REPO / "docs" / "showcase-loop" / "worklist.csv"
OUT = REPO / "docs" / "showcase-loop" / "bundle"


def _read_worklist(path: Path) -> list[dict[str, str]]:
    import csv

    with path.open(newline="") as fh:
        return [
            {str(k): str(v) for k, v in row.items() if k is not None}
            for row in csv.DictReader(fh)
        ]


def main() -> int:
    body = Workflow.load(SOURCE)
    records = _read_worklist(WORKLIST)
    looped = author_data_driven_loop(
        body,
        records,
        loop_var="encounter_note",
        name="openemr-showcase-for-each",
    )
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    src_templates = SOURCE / "templates"
    if src_templates.is_dir():
        shutil.copytree(src_templates, OUT / "templates")
    looped.save(OUT)
    print(
        f"Wrote {OUT} (program:true): {len(records)} records over relation "
        f"'worklist', body = {len(body.steps)} demonstrated steps."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
