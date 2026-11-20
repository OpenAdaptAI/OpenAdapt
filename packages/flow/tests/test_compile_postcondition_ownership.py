"""A postcondition remains on the producing step across later selections."""

from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path

import pytest
from PIL import Image

from openadapt_flow.compiler import compile as compiler
from openadapt_flow.ir import ActionKind, Postcondition, PostconditionKind, Workflow


@pytest.mark.parametrize("producer", ["read", "selection", "write_click", "write_key"])
@pytest.mark.parametrize("chain", [False, True], ids=["A-B", "A-B-C"])
def test_compilation_preserves_postcondition_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, producer: str, chain: bool
) -> None:
    """Later persistence does not transfer a producer's obligation.

    Reads retain their read contract; other producers have a mined screen
    check. Writes also have a synthetic SoR delta mined by the real compiler.
    These records test ownership, not OCR or runtime write qualification.
    """
    marker = "Producer completion shown"
    recording, bundle = tmp_path / "recording", tmp_path / "bundle"
    (recording / "frames").mkdir(parents=True)
    count = 3 if chain else 2
    events = []
    for index in range(count):
        event = {
            "i": index,
            "kind": "click",
            "x": 30,
            "y": 25,
            "t": float(index + 1),
            "structured_identity": "record 23",
        }
        if index == 0:
            if producer == "read":
                event.update(
                    kind="read",
                    region=[2, 2, 20, 10],
                    schema_name="item",
                    field={"name": "status"},
                )
            elif producer == "write_key":
                event.update(kind="key", key="Enter")
            if producer.startswith("write"):
                event.update(
                    sor_before=[], sor_after=[{"key": "r-1", "status": "saved"}]
                )
        events.append(event)
        for suffix in ("before", "after"):
            Image.new("RGB", (128, 96), "white").save(
                recording / "frames" / f"{index:04d}_{suffix}.png"
            )
    (recording / "meta.json").write_text(
        json.dumps({"id": "ownership", "viewport": [128, 96], "params": {}})
    )
    (recording / "events.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n"
    )
    mined = {}

    def postconditions(_before, _after, *, step_id, **_kwargs):
        conditions = [
            Postcondition(
                kind=PostconditionKind.REGION_STABLE,
                region=(0, 0, 10, 10),
                phash="0" * 16,
            )
        ]
        if step_id == "step_000":
            conditions.append(
                Postcondition(kind=PostconditionKind.TEXT_PRESENT, text=marker)
            )
        mined[step_id] = [condition.model_dump(mode="json") for condition in conditions]
        return conditions

    monkeypatch.setattr(compiler, "ocr", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(compiler, "_postconditions", postconditions)
    # Even perfect persistence evidence grants no predicate timing authority.
    monkeypatch.setattr(
        import_module("openadapt_flow.vision.ocr"),
        "text_present",
        lambda *_args, **_kwargs: True,
    )
    workflow = compiler.compile_recording(
        recording,
        bundle,
        name="ownership",
        mine_effects=True,
        risk_overrides={f"step_{index:03d}": "reversible" for index in range(1, count)},
    )
    persisted = Workflow.model_validate_json((bundle / "workflow.json").read_text())
    for compiled in (workflow, persisted):
        assert len(compiled.steps) == count
        for step in compiled.steps:
            assert [
                condition.model_dump(mode="json") for condition in step.expect
            ] == mined.get(step.id, [])
        assert all(
            step.identity_armed is True and step.risk == "reversible"
            for step in compiled.steps[1:]
        )
        if producer == "read":
            assert compiled.steps[0].action is ActionKind.READ
            assert compiled.steps[0].read is not None
        elif producer.startswith("write"):
            assert compiled.steps[0].effects
            assert any(
                condition.text == marker for condition in compiled.steps[0].expect
            )
    notes_path = bundle / "compile_notes.json"
    notes = (
        json.loads(notes_path.read_text()) if notes_path.exists() else {"changes": []}
    )
    assert all(
        note["change"] != "defer_selection_text_check" for note in notes["changes"]
    )
