"""CLI wiring for compose."""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.__main__ import build_parser, main
from openadapt_flow.cli_compose import parse_handoff
from openadapt_flow.compiler.compose_authoring import author_composition
from openadapt_flow.composition import is_composition_artifact
from openadapt_flow.ir import ActionKind, ParamSpec, Step, Workflow
from openadapt_flow.runtime.effects.effect import Effect, EffectKind, ValueExpr


def test_parser_dispatches_compose() -> None:
    args = build_parser().parse_args(
        [
            "compose",
            "--child",
            "intake=./a",
            "--child",
            "posting=./b",
            "--handoff",
            "intake.patient_id=posting.patient_id",
            "--out",
            "composed",
        ]
    )
    assert args.func.__name__ == "_cmd_compose"
    assert args.child == ["intake=./a", "posting=./b"]
    assert args.handoff == ["intake.patient_id=posting.patient_id"]


def test_parse_handoff_roundtrip() -> None:
    binding = parse_handoff("intake.patient_id=posting.claim_id")
    assert binding.from_child == "intake"
    assert binding.source == "patient_id"
    assert binding.to_child == "posting"
    assert binding.target == "claim_id"


def test_parse_handoff_rejects_malformed() -> None:
    with pytest.raises(SystemExit, match="FROM.source=TO.target"):
        parse_handoff("intake=posting")


def _writer() -> Workflow:
    return Workflow(
        name="intake",
        steps=[
            Step(
                id="save",
                intent="save",
                action=ActionKind.KEY,
                key="Enter",
                param="patient_id",
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"patient_id": ValueExpr(param="patient_id")},
                    )
                ],
            )
        ],
        param_specs={"patient_id": ParamSpec(name="patient_id", example="p1")},
    )


def _reader() -> Workflow:
    return Workflow(
        name="posting",
        steps=[
            Step(
                id="type_patient",
                intent="type",
                action=ActionKind.TYPE,
                param="patient_id",
            )
        ],
        param_specs={"patient_id": ParamSpec(name="patient_id", example="p1")},
    )


def test_cli_compose_writes_artifact(tmp_path: Path) -> None:
    a = tmp_path / "intake"
    b = tmp_path / "posting"
    a.mkdir()
    b.mkdir()
    _writer().save(a)
    _reader().save(b)
    out = tmp_path / "composed"
    rc = main(
        [
            "compose",
            "--child",
            f"intake={a}",
            "--child",
            f"posting={b}",
            "--handoff",
            "intake.patient_id=posting.patient_id",
            "--out",
            str(out),
            "--name",
            "two-step",
        ]
    )
    assert rc == 0
    assert is_composition_artifact(out)


def test_replay_refuses_composition(tmp_path: Path) -> None:
    a = tmp_path / "intake"
    b = tmp_path / "posting"
    a.mkdir()
    b.mkdir()
    _writer().save(a)
    _reader().save(b)
    out = tmp_path / "composed"
    author_composition(
        [("intake", a), ("posting", b)],
        out=out,
    )
    with pytest.raises(SystemExit, match="replay refuses a composition"):
        main(["replay", str(out), "--url", "http://example.invalid"])
