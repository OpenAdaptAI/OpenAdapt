"""A cached program is reused only for the record check and environment it was
admitted under.

The task id and the parameter schema used to be the whole key, so a task
whose record check changed, or an app that moved to a new version at the same
origin, silently reused the old program. Now the admission pins the record
check's digest and the canary's environment, a caller can pin both in the
key, and a run refuses a mismatch.

No browser and no model.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.ir import ActionKind, Anchor, Step, Workflow
from openadapt_flow.jit import AdmissionError, WebApp
from openadapt_flow.jit.compile_agent import _reuse_refusal
from openadapt_flow.jit.signature import (
    TaskSignature,
    record_check_digest,
    record_check_of,
)
from openadapt_flow.runtime.effects.effect import Effect, EffectKind
from tests.test_jit_lifecycle import (  # noqa: F401  (autouse fixture)
    REVIEWER,
    _admitted,
    _jit,
    _policy_passes,
    _report,
    _review,
    _stub_runs,
)

URL = "http://127.0.0.1:9/"


def _workflow(*, anchor_x: int = 10, effect_field: str = "note") -> Workflow:
    return Workflow(
        name="t",
        steps=[
            Step(id="step_000", intent="type the note", action=ActionKind.TYPE, text="x"),
            Step(
                id="step_001",
                intent="click Save",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template=f"templates/step_001_{anchor_x}.png",
                    region=(anchor_x, 10, 40, 20),
                    click_point=(anchor_x + 5, 15),
                ),
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"type": "note"},
                        field=effect_field,
                    )
                ],
            ),
        ],
    )


# -- the key ---------------------------------------------------------------------


def test_the_key_is_unchanged_when_nothing_is_pinned() -> None:
    task = TaskSignature("t.add_note", "Add a note", {"note": str})
    canonical = json.dumps(
        {"task_id": "t.add_note", "params": {"note": "string"}},
        sort_keys=True,
        separators=(",", ":"),
    )
    old = "t.add_note-" + hashlib.sha256(canonical.encode()).hexdigest()[:12]
    assert task.key == old
    assert "record_check" not in task.to_dict()


def test_pinning_a_record_check_or_environment_changes_the_key() -> None:
    base = TaskSignature("t.add_note", "Add a note", {"note": str})
    check = TaskSignature(
        "t.add_note", "Add a note", {"note": str}, record_check="a" * 64
    )
    other_check = dataclasses.replace(check, record_check="b" * 64)
    env = TaskSignature(
        "t.add_note", "Add a note", {"note": str}, environment=URL + "#7.0.2"
    )
    other_env = dataclasses.replace(env, environment=URL + "#7.0.3")
    keys = {base.key, check.key, other_check.key, env.key, other_env.key}
    assert len(keys) == 5
    assert check.to_dict()["record_check"] == "a" * 64


@pytest.mark.parametrize(
    "kw", [{"record_check": "not-a-digest"}, {"environment": "  "}]
)
def test_a_malformed_pin_is_refused(kw: dict) -> None:
    with pytest.raises(ValueError):
        TaskSignature("t.add_note", "Add a note", {}, **kw)


# -- the record check digest -----------------------------------------------------


def test_a_re_anchored_save_keeps_its_record_check() -> None:
    assert record_check_digest(_workflow(anchor_x=10)) == record_check_digest(
        _workflow(anchor_x=300)
    )


def test_a_changed_contract_changes_the_record_check() -> None:
    assert record_check_digest(_workflow()) != record_check_digest(
        _workflow(effect_field="summary")
    )


def test_the_record_check_names_only_saving_steps() -> None:
    check = record_check_of(_workflow())
    assert [s["step_id"] for s in check["saves"]] == ["step_001"]
    assert check["saves"][0]["effects"][0]["field"] == "note"


# -- admission pins both, runs refuse a mismatch --------------------------------------


def test_the_admission_pins_the_record_check_and_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jit = _admitted(tmp_path, monkeypatch)
    manifest = json.loads((jit.bundle_dir / "admission.json").read_text())
    assert manifest["record_check_digest"] == record_check_digest(
        Workflow.load(jit.bundle_dir)
    )
    assert manifest["canary"]["environment"] == "http://127.0.0.1:9"
    assert jit.run().status == "VERIFIED"


def test_a_new_app_version_at_the_same_origin_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jit = _jit(tmp_path)
    jit = dataclasses.replace(jit, app=WebApp(url=URL, environment_id="7.0.2"))
    _review(jit)
    _stub_runs(jit, monkeypatch, _report())
    jit.canary(principal_id=REVIEWER)
    from tests.test_jit_lifecycle import _ci

    assert jit.admit_with_key(_ci(jit, monkeypatch))["outcome"] == "admitted"
    assert jit.run().status == "VERIFIED"

    upgraded = dataclasses.replace(jit, app=WebApp(url=URL, environment_id="7.0.3"))
    _stub_runs(upgraded, monkeypatch, _report())
    result = upgraded.run()
    assert result.status == "refused"
    assert "admitted in environment" in (result.detail or "")
    assert "7.0.3" in (result.detail or "")


def test_an_environment_that_cannot_be_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jit = _admitted(tmp_path, monkeypatch)

    def broken() -> str:
        raise RuntimeError("version endpoint down")

    other = dataclasses.replace(jit, app=WebApp(url=URL, environment_id=broken))
    _stub_runs(other, monkeypatch, _report())
    result = other.run()
    assert result.status == "refused"
    assert "could not be read" in (result.detail or "")


def test_a_pinned_record_check_the_program_does_not_have_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = TaskSignature("t.add_note", "Add a note", {}, record_check="0" * 64)
    jit = _admitted(tmp_path, monkeypatch, task=task)
    result = jit.run()
    assert result.status == "refused"
    assert "pins record check" in (result.detail or "")


def test_the_pinned_record_check_of_the_admitted_program_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _jit(tmp_path / "probe")
    digest = record_check_digest(Workflow.load(probe.bundle_dir))
    task = TaskSignature("t.add_note", "Add a note", {}, record_check=digest)
    jit = _admitted(tmp_path, monkeypatch, task=task)
    assert jit.run().status == "VERIFIED"


def test_a_canary_outside_the_pinned_environment_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = TaskSignature("t.add_note", "Add a note", {}, environment=URL + "#8")
    jit = _jit(tmp_path, task=task)
    _review(jit)
    _stub_runs(jit, monkeypatch, _report())
    with pytest.raises(AdmissionError, match="pinned to environment"):
        jit.canary(principal_id=REVIEWER)


def test_an_admitted_record_check_that_differs_is_refused() -> None:
    workflow = _workflow()
    app = WebApp(url=URL)
    task = TaskSignature("t.add_note", "Add a note", {})
    record = {"record_check_digest": "f" * 64, "canary": {"environment": app.origin}}
    assert "not the one admitted" in (_reuse_refusal(record, workflow, task, app) or "")
    record["record_check_digest"] = record_check_digest(workflow)
    assert _reuse_refusal(record, workflow, task, app) is None
    # An admission from before the pins existed: only the caller's pins apply.
    assert _reuse_refusal({"canary": {}}, workflow, task, app) is None
