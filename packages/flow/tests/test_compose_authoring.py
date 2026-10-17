"""Authoring a parent composition from two compiled child bundles."""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.compiler.compose_authoring import (
    author_composition,
    effect_bound_param_names,
)
from openadapt_flow.composition import (
    CompositionError,
    HandoffBinding,
    is_composition_artifact,
    topological_order,
)
from openadapt_flow.ir import ActionKind, ParamSpec, Step, Workflow
from openadapt_flow.runtime.effects.effect import Effect, EffectKind, ValueExpr


def _writer(name: str = "intake") -> Workflow:
    return Workflow(
        name=name,
        surface="web",
        steps=[
            Step(
                id="type_patient",
                intent="type <patient_id>",
                action=ActionKind.TYPE,
                param="patient_id",
            ),
            Step(
                id="save",
                intent="save encounter",
                action=ActionKind.KEY,
                key="Enter",
                risk="irreversible",
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"patient_id": ValueExpr(param="patient_id")},
                        expected_count=1,
                    )
                ],
            ),
        ],
        param_specs={"patient_id": ParamSpec(name="patient_id", example="p1")},
    )


def _reader(name: str = "posting") -> Workflow:
    return Workflow(
        name=name,
        surface="linux",
        steps=[
            Step(
                id="type_patient",
                intent="type <patient_id>",
                action=ActionKind.TYPE,
                param="patient_id",
            )
        ],
        param_specs={"patient_id": ParamSpec(name="patient_id", example="p1")},
    )


def _save(tmp_path: Path, workflow: Workflow, folder: str) -> Path:
    path = tmp_path / folder
    path.mkdir()
    workflow.save(path)
    return path


def test_effect_bound_params_are_the_only_handoff_sources():
    writer = _writer()
    assert effect_bound_param_names(writer) == {"patient_id"}
    assert effect_bound_param_names(_reader()) == set()


def test_author_copies_children_and_writes_composition(tmp_path: Path):
    a = _save(tmp_path, _writer(), "intake")
    b = _save(tmp_path, _reader(), "posting")
    out = tmp_path / "composed"
    composition = author_composition(
        [("intake", a), ("posting", b)],
        handoffs=[
            HandoffBinding(
                from_child="intake",
                source="patient_id",
                to_child="posting",
                target="patient_id",
            )
        ],
        name="claim-post",
        out=out,
    )
    assert is_composition_artifact(out)
    assert composition.name == "claim-post"
    assert topological_order(composition) == ["intake", "posting"]
    assert (out / "children" / "intake" / "workflow.json").is_file()
    assert (out / "children" / "posting" / "workflow.json").is_file()
    reloaded = type(composition).load(out)
    assert reloaded.children[0].surface == "web"
    assert reloaded.children[1].surface == "linux"


def test_author_refuses_handoff_not_bound_by_an_effect(tmp_path: Path):
    a = _save(tmp_path, _reader("first"), "first")
    b = _save(tmp_path, _reader("second"), "second")
    with pytest.raises(CompositionError, match="not a parameter bound"):
        author_composition(
            [("first", a), ("second", b)],
            handoffs=[
                HandoffBinding(
                    from_child="first",
                    source="patient_id",
                    to_child="second",
                    target="patient_id",
                )
            ],
            out=tmp_path / "composed",
        )
    assert not (tmp_path / "composed").exists()


def test_author_refuses_unknown_target_param(tmp_path: Path):
    a = _save(tmp_path, _writer(), "intake")
    b = _save(tmp_path, _reader(), "posting")
    with pytest.raises(CompositionError, match="not a parameter of"):
        author_composition(
            [("intake", a), ("posting", b)],
            handoffs=[
                HandoffBinding(
                    from_child="intake",
                    source="patient_id",
                    to_child="posting",
                    target="claim_id",
                )
            ],
            out=tmp_path / "bad",
        )


def test_author_refuses_one_child(tmp_path: Path):
    a = _save(tmp_path, _writer(), "intake")
    with pytest.raises(CompositionError, match="at least two"):
        author_composition([("intake", a)], out=tmp_path / "composed")


def test_author_refuses_cycle(tmp_path: Path):
    a = _save(tmp_path, _writer(), "intake")
    b = _save(tmp_path, _reader(), "posting")
    with pytest.raises(CompositionError, match="cycle"):
        author_composition(
            [("intake", a), ("posting", b)],
            after={"intake": ["posting"], "posting": ["intake"]},
            out=tmp_path / "cycled",
        )


def test_explicit_after_orders_dag(tmp_path: Path):
    a = _save(tmp_path, _writer(), "intake")
    b = _save(tmp_path, _reader(), "posting")
    out = tmp_path / "composed"
    composition = author_composition(
        [("posting", b), ("intake", a)],
        after={"posting": ["intake"]},
        out=out,
    )
    assert topological_order(composition) == ["intake", "posting"]


def test_author_refuses_backwards_handoff(tmp_path: Path):
    a = _save(tmp_path, _writer("intake"), "intake")
    b = _save(tmp_path, _writer("posting"), "posting")
    with pytest.raises(CompositionError, match="runs backwards"):
        author_composition(
            [("intake", a), ("posting", b)],
            handoffs=[
                HandoffBinding(
                    from_child="posting",
                    source="patient_id",
                    to_child="intake",
                    target="patient_id",
                )
            ],
            out=tmp_path / "backwards",
        )
