"""A JIT draft stays a draft, and its recorder stays its recorder.

Each test is an attack on the DRAFT to ADMITTED gate that used to succeed:

- deleting ``jit_draft.json`` let a draft run unadmitted;
- a registered agent could admit a draft it recorded when the draft named
  no ``agent_principal_id``, or after it edited ``candidate.json``;
- a replay outside the JIT API ran an admitted bundle on another app, or
  without an independent read-back.

No browser and no model.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow.ir import Workflow
from openadapt_flow.jit import AdmissionError, lifecycle
from openadapt_flow.jit.authority import principal_from_key, sign_policy
from openadapt_flow.jit.lifecycle import DraftNotAdmitted, ensure_runnable
from openadapt_flow.runtime import Replayer
from tests.test_jit_admission_modes import (  # noqa: F401  (fixture)
    _agent,
    _policy,
    keys,
)
from tests.test_jit_lifecycle import (  # noqa: F401  (autouse fixture)
    REVIEWER,
    IndependentVerifier,
    _admitted,
    _jit,
    _policy_passes,
    _report,
    _stub_runs,
)


class UntouchedBackend:
    """Fails the test if the replayer acts before the draft check."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the backend was used ({name}) before the check")


def _replay(bundle: Path, tmp_path: Path, **run_kw: Any) -> Any:
    key_dir = run_kw.pop("key_dir", None)
    verifier = run_kw.pop("verifier", None)
    workflow = Workflow.load(bundle)
    return Replayer(
        UntouchedBackend(),
        vision=SimpleNamespace(),
        effect_verifier=verifier,
        draft_key_dir=key_dir,
    ).run(workflow, bundle_dir=bundle, run_dir=tmp_path / "run", **run_kw)


def _edit_candidate(jit: Any, **changes: Any) -> None:
    path = jit.program_dir / "candidate.json"
    candidate = json.loads(path.read_text())
    candidate.update(changes)
    path.write_text(json.dumps(candidate))


# -- 1. the draft state lives in the digested bundle ---------------------------------


def test_deleting_the_draft_marker_does_not_free_a_draft(tmp_path: Path) -> None:
    jit = _jit(tmp_path)
    (jit.bundle_dir / lifecycle.DRAFT_MARKER).unlink()
    with pytest.raises(DraftNotAdmitted, match="is a DRAFT"):
        _replay(jit.bundle_dir, tmp_path)
    with pytest.raises(SystemExit, match="is a DRAFT"):
        lifecycle.refuse_unadmitted_draft(jit.bundle_dir)
    assert lifecycle.draft_label(jit.bundle_dir) == "DRAFT"
    # A copy without the marker is refused too.
    copy = tmp_path / "copied"
    shutil.copytree(jit.bundle_dir, copy)
    with pytest.raises(DraftNotAdmitted, match="is a DRAFT"):
        _replay(copy, tmp_path)
    assert not (tmp_path / "run").exists()


def test_the_draft_origin_is_inside_the_content_digest(tmp_path: Path) -> None:
    jit = _jit(tmp_path)
    workflow = Workflow.load(jit.bundle_dir)
    assert workflow.origin == lifecycle.DRAFT_ORIGIN
    digest = lifecycle._digest(workflow)
    # Taking the origin out is a different program: nothing pinned to the
    # draft's digest (review, canary, admission) counts for it.
    workflow.origin = None
    workflow.save(jit.bundle_dir)
    assert lifecycle._digest(Workflow.load(jit.bundle_dir)) != digest


def test_an_admitted_bundle_stripped_of_marker_and_origin_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jit = _admitted(tmp_path, monkeypatch)
    (jit.bundle_dir / lifecycle.DRAFT_MARKER).unlink()
    workflow = Workflow.load(jit.bundle_dir)
    workflow.origin = None
    workflow.save(jit.bundle_dir)
    # The admission manifest is still there and no longer matches.
    with pytest.raises(DraftNotAdmitted, match="not the one that was admitted"):
        ensure_runnable(jit.bundle_dir, key_dir=jit.key_dir)


# -- 2. the recording agent never admits its own draft ---------------------------


def test_an_agent_cannot_act_when_the_recorder_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keys: dict[str, Any],  # noqa: F811
) -> None:
    # The agent that recorded the draft exposed no principal_id.
    jit = _jit(tmp_path, agent_principal_id=None)
    recorder = principal_from_key(
        keys["recorder"], keys["dir"], policy=sign_policy(_policy(), keys["signer"])
    )
    with pytest.raises(DraftNotAdmitted, match="recording agent is unknown"):
        ensure_runnable(jit.bundle_dir, attended_by=recorder, key_dir=keys["dir"])
    with pytest.raises(AdmissionError, match="recording agent is unknown"):
        jit.canary(principal_id=recorder)
    _stub_runs(jit, monkeypatch, _report())
    _review(jit)
    jit.canary(principal_id=REVIEWER)
    jit.canary(principal_id=REVIEWER)
    with pytest.raises(AdmissionError, match="recording agent is unknown"):
        jit.admit_with_key(recorder)
    assert not (jit.bundle_dir / lifecycle.ADMISSION_FILE).exists()


def test_editing_the_candidate_does_not_hide_the_recorder(
    tmp_path: Path,
    keys: dict[str, Any],  # noqa: F811
) -> None:
    jit = _jit(tmp_path)
    recorder = principal_from_key(
        keys["recorder"], keys["dir"], policy=sign_policy(_policy(), keys["signer"])
    )
    _edit_candidate(jit, agent_principal_id="someone-else", recorded_by="agent:x")
    with pytest.raises(DraftNotAdmitted, match="changed after it was recorded"):
        ensure_runnable(jit.bundle_dir, attended_by=recorder, key_dir=keys["dir"])
    with pytest.raises(AdmissionError, match="changed after it was recorded"):
        jit.admit_with_key(recorder)


def test_a_candidate_with_no_signed_recorder_is_refused(
    tmp_path: Path,
    keys: dict[str, Any],  # noqa: F811
) -> None:
    jit = _jit(tmp_path)
    _edit_candidate(jit, recorder=None)
    with pytest.raises(AdmissionError, match="not signed"):
        _review(jit)
    with pytest.raises(DraftNotAdmitted, match="not signed"):
        ensure_runnable(jit.bundle_dir, attended_by=_agent(keys), key_dir=keys["dir"])


def test_a_recorder_signed_for_another_bundle_is_refused(tmp_path: Path) -> None:
    # The bundle is edited after the recording, and the signed recorder block
    # is kept: the block no longer fits the bundle.
    jit = _jit(tmp_path)
    workflow = Workflow.load(jit.bundle_dir)
    workflow.steps[0].intent = "type something else"
    workflow.save(jit.bundle_dir)
    with pytest.raises(AdmissionError, match="signed for another bundle"):
        _review(jit)


def _review(jit: Any) -> dict:
    return jit.review(
        principal_id=REVIEWER, reviewed_steps=["step_000", "step_001"], confirm="ok"
    )


# -- 3. every replay of an admitted bundle checks what the JIT API checks ------------


def test_a_replay_on_another_app_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jit = _admitted(tmp_path, monkeypatch)
    with pytest.raises(DraftNotAdmitted, match="canary ran on"):
        _replay(
            jit.bundle_dir,
            tmp_path,
            key_dir=jit.key_dir,
            verifier=IndependentVerifier(),
            execution_origin="https://elsewhere.example",
        )
    with pytest.raises(DraftNotAdmitted, match="canary ran on"):
        _replay(
            jit.bundle_dir,
            tmp_path,
            key_dir=jit.key_dir,
            verifier=IndependentVerifier(),
        )


def test_a_replay_without_an_independent_readback_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jit = _admitted(tmp_path, monkeypatch)
    with pytest.raises(DraftNotAdmitted, match="independent read-back"):
        _replay(
            jit.bundle_dir,
            tmp_path,
            key_dir=jit.key_dir,
            execution_origin=jit.app.origin,
        )


def test_a_replay_on_the_admitted_app_with_its_readback_passes_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jit = _admitted(tmp_path, monkeypatch)
    ensure_runnable(
        jit.bundle_dir,
        workflow=Workflow.load(jit.bundle_dir),
        key_dir=jit.key_dir,
        execution_origin=jit.app.origin,
        effect_verifier=IndependentVerifier(),
        at_run=True,
    )
