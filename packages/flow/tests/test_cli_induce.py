"""``openadapt-flow induce`` — multi-trace induction via the CLI.

End-to-end (no mocks): the handler feeds compiled bundle dirs to the real
``induce_program``, writes a program bundle when CERTIFIED, and REFUSES
(nonzero exit, no bundle) when intent stays underdetermined — the ask-don't-
guess posture surfaced honestly at the CLI.
"""

from __future__ import annotations

from pathlib import Path

from openadapt_flow.__main__ import build_parser, main
from openadapt_flow.ir import ActionKind, Step, Workflow


def _bundle(tmp: Path, name: str, steps: list[Step]) -> str:
    Workflow(name="src", steps=steps).save(tmp / name)
    return str(tmp / name)


def _dose_trace(patient: str) -> list[Step]:
    return [
        Step(
            id="s_patient", intent="type patient", action=ActionKind.TYPE, text=patient
        ),
        Step(id="s_dose", intent="type dose", action=ActionKind.TYPE, text="5mg"),
    ]


def test_induce_certified_writes_program_bundle(tmp_path: Path, capsys) -> None:
    b1 = _bundle(tmp_path, "t1", _dose_trace("Alice"))
    b2 = _bundle(tmp_path, "t2", _dose_trace("Bob"))
    out = tmp_path / "induced"

    rc = main(["induce", b1, b2, "--out", str(out), "--name", "dose-prog"])
    assert rc == 0

    captured = capsys.readouterr().out
    assert "CERTIFIED" in captured
    assert "param 'patient'" in captured

    workflow = Workflow.load(out)
    assert workflow.name == "dose-prog"
    assert workflow.program is not None  # a real PROGRAM bundle
    assert "patient" in workflow.param_specs  # the varying value became a param


def test_induce_refuses_contradiction_nonzero_no_bundle(tmp_path: Path, capsys) -> None:
    b1 = _bundle(
        tmp_path,
        "c1",
        [
            Step(
                id="a",
                intent="click approve",
                action=ActionKind.CLICK,
                risk="irreversible",
            )
        ],
    )
    b2 = _bundle(
        tmp_path,
        "c2",
        [
            Step(
                id="b",
                intent="click reject",
                action=ActionKind.CLICK,
                risk="irreversible",
            )
        ],
    )
    out = tmp_path / "should_not_exist"

    rc = main(["induce", b1, b2, "--out", str(out)])
    assert rc == 2  # refused

    captured = capsys.readouterr().out
    assert "NOT CERTIFIED" in captured
    assert not (out / "workflow.json").is_file()  # nothing written


def test_induce_held_out_flag_prints_validation(tmp_path: Path, capsys) -> None:
    b1 = _bundle(tmp_path, "t1", _dose_trace("Alice"))
    b2 = _bundle(tmp_path, "t2", _dose_trace("Bob"))
    rc = main(["induce", b1, b2, "--out", str(tmp_path / "o"), "--held-out"])
    assert rc == 0
    # The held-out check reports STRUCTURAL trace-shape coverage (honest naming
    # from the induction-hardening pass), not behavioral "validation".
    assert "Held-out STRUCTURAL coverage" in capsys.readouterr().out


def test_induce_parser_accepts_multiple_recordings() -> None:
    args = build_parser().parse_args(["induce", "r1", "r2", "r3", "--out", "bundle"])
    assert args.command == "induce"
    assert args.recording == ["r1", "r2", "r3"]
    assert args.out == "bundle"
