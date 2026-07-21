"""Focused adversarial evaluation for governed live identity enforcement.

This is a deterministic synthetic surface, not a production reliability
benchmark.  It measures the policy handoff itself: the same reversible target
with no readable live identity is executed by permissive ``replay`` but halted
by governed ``run``; a readable exact identity still executes without over-halt.

Run::

    python -m openadapt_flow.validation.governed_run \
        --out benchmark/governed_run
"""

from __future__ import annotations

import argparse
import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from openadapt_flow.ir import ActionKind, Anchor, Step, Workflow
from openadapt_flow.runtime.authorization import (
    GovernedRunAuthorization,
    runtime_inputs_digest,
)
from openadapt_flow.runtime.replayer import Replayer

_IDENTITY = "Jane Sample 1980-01-15 MRN RC79284 Active"


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (320, 200), "white").save(out, format="PNG")
    return out.getvalue()


class _Backend:
    def __init__(self, *, structured_identity: str | None, actual_entity: str):
        self.structured_identity = structured_identity
        self.actual_entity = actual_entity
        self.actions: list[tuple] = []

    @property
    def viewport(self) -> tuple[int, int]:
        return (320, 200)

    def screenshot(self) -> bytes:
        return _png()

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        self.actions.append(("click", self.actual_entity, x, y, double))

    def type_text(self, text: str) -> None:
        self.actions.append(("type", text))

    def press(self, key: str) -> None:
        self.actions.append(("press", key))

    def scroll(self, dx: int, dy: int) -> None:
        self.actions.append(("scroll", dx, dy))

    def structured_text_at(self, x: int, y: int) -> str | None:
        return self.structured_identity


class _Vision:
    @staticmethod
    def wait_settled(backend) -> bytes:
        return backend.screenshot()

    @staticmethod
    def find_template(*_args, **_kwargs):
        return SimpleNamespace(
            point=(110, 105), region=(100, 100, 40, 20), confidence=0.99
        )

    @staticmethod
    def ocr(*_args, **_kwargs):
        return []


def _workflow(bundle: Path) -> Workflow:
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "open.png").write_bytes(_png())
    workflow = Workflow(
        name="governed identity probe",
        steps=[
            Step(
                id="open-patient",
                intent="open the selected patient",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/open.png",
                    region=(100, 100, 40, 20),
                    click_point=(110, 105),
                    ocr_text="Open",
                    context_text=_IDENTITY,
                    structured_identity=_IDENTITY,
                ),
                identity_armed=True,
                risk="reversible",
            )
        ],
    )
    workflow.save(bundle)
    return Workflow.load(bundle)


def _trial(condition: str, root: Path, index: int) -> dict:
    bundle = root / f"{condition}-{index}-bundle"
    run_dir = root / f"{condition}-{index}-run"
    workflow = _workflow(bundle)
    governed = condition.startswith("governed")
    readable = condition == "governed_verified"
    backend = _Backend(
        structured_identity=_IDENTITY if readable else None,
        actual_entity="correct" if readable else "wrong",
    )
    authorization = None
    if governed:
        assert workflow.manifest is not None
        authorization = GovernedRunAuthorization(
            bundle_content_digest=workflow.manifest.content_digest,
            runtime_inputs_digest=runtime_inputs_digest(workflow, None, None),
            admitted_policy_name="validation",
            required_identity_step_ids=("open-patient",),
        )
    report = Replayer(
        backend,
        vision=_Vision(),
        governed_authorization=authorization,
        poll_interval_s=0.0,
    ).run(workflow, bundle_dir=bundle, run_dir=run_dir)
    clicked = bool(backend.actions)
    wrong = clicked and backend.actions[0][1] == "wrong"
    return {
        "trial": index + 1,
        "success": report.success,
        "acted": clicked,
        "correct_action": clicked and not wrong,
        "silent_wrong_action": report.success and wrong,
        "safe_halt": not report.success and not clicked,
        "over_halt": not report.success and readable,
        "identity_status": (
            report.results[0].identity.status
            if report.results and report.results[0].identity is not None
            else None
        ),
    }


def run_probe(trials: int = 3) -> dict:
    if trials < 3:
        raise ValueError("comparative evaluation requires at least 3 trials")
    conditions = (
        "permissive_unreadable",
        "governed_unreadable",
        "governed_verified",
    )
    with tempfile.TemporaryDirectory(prefix="openadapt-governed-run-") as tmp:
        root = Path(tmp)
        rows = {
            condition: [_trial(condition, root, i) for i in range(trials)]
            for condition in conditions
        }
    summary = {
        condition: {
            metric: sum(bool(row[metric]) for row in condition_rows)
            for metric in (
                "correct_action",
                "silent_wrong_action",
                "safe_halt",
                "over_halt",
            )
        }
        for condition, condition_rows in rows.items()
    }
    return {
        "scope": "synthetic deterministic governed-policy handoff",
        "trials_per_condition": trials,
        "oracle": "backend action log plus Replayer report",
        "conditions": rows,
        "summary": summary,
        "caveat": (
            "This isolates runtime authorization semantics; it does not measure "
            "OCR accuracy, application reliability, or production error rates."
        ),
    }


def write_report(report: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    n = report["trials_per_condition"]
    lines = [
        "# Governed run authorization probe",
        "",
        "Synthetic deterministic policy-handoff evaluation; not a production "
        "reliability benchmark.",
        "",
        f"Trials per condition: **{n}**. Oracle: backend action log plus the "
        "persisted `Replayer` result.",
        "",
        "| Condition | Correct action | Silent wrong action | Safe halt | Over-halt |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for condition, metrics in report["summary"].items():
        lines.append(
            f"| `{condition}` | {metrics['correct_action']}/{n} | "
            f"{metrics['silent_wrong_action']}/{n} | {metrics['safe_halt']}/{n} | "
            f"{metrics['over_halt']}/{n} |"
        )
    lines.extend(["", f"Caveat: {report['caveat']}", ""])
    (out / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=3)
    args = parser.parse_args()
    report = run_probe(args.trials)
    write_report(report, args.out)
    print(json.dumps(report["summary"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
