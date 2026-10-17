"""Every path that executes a bundle refuses a JIT draft that is not admitted.

The shared check is in ``Replayer.run`` (``jit.lifecycle.ensure_runnable``).
These tests drive each entry point with a draft bundle and show that nothing
runs: the replayer itself, a copy of the bundle, the Execute dispatcher behind
``serve-execute``, the Execute service receipt, and the server that
``emit-mcp`` writes. ``emit-skill`` and ``emit-mcp`` still describe a draft and
label it DRAFT. No browser and no model.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow.ir import Workflow
from openadapt_flow.jit import lifecycle
from openadapt_flow.jit.lifecycle import DraftNotAdmitted, ensure_runnable
from openadapt_flow.runtime import Replayer
from tests.test_jit_lifecycle import (  # noqa: F401  (autouse fixture)
    REVIEWER,
    _admitted,
    _jit,
    _policy_passes,
)


class UntouchedBackend:
    """Fails the test if the replayer acts before the draft check."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the backend was used ({name}) before the check")


def _replay(bundle: Path, tmp_path: Path, **kw: Any) -> Any:
    workflow = Workflow.load(bundle)
    return Replayer(UntouchedBackend(), vision=SimpleNamespace(), **kw).run(
        workflow, bundle_dir=bundle, run_dir=tmp_path / "run"
    )


def test_the_replayer_refuses_a_draft_and_its_copy(tmp_path: Path) -> None:
    jit = _jit(tmp_path)
    with pytest.raises(DraftNotAdmitted, match="is a DRAFT"):
        _replay(jit.bundle_dir, tmp_path)
    copy = tmp_path / "copied"
    shutil.copytree(jit.bundle_dir, copy)
    with pytest.raises(DraftNotAdmitted, match="is a DRAFT"):
        _replay(copy, tmp_path)
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("who", ["agent", "model:x", "cua-bot-7", "", " "])
def test_an_agent_cannot_attend_a_draft_run(tmp_path: Path, who: str) -> None:
    jit = _jit(tmp_path)
    with pytest.raises(DraftNotAdmitted):
        _replay(jit.bundle_dir, tmp_path, attended_draft_by=who)


def test_an_attended_or_admitted_draft_passes_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jit = _jit(tmp_path)
    ensure_runnable(jit.bundle_dir, attended_by=REVIEWER)
    admitted = _admitted(tmp_path / "ok", monkeypatch)
    ensure_runnable(admitted.bundle_dir, key_dir=admitted.key_dir)
    # Checked against another admission key, the same bundle is refused.
    with pytest.raises(DraftNotAdmitted):
        ensure_runnable(admitted.bundle_dir, key_dir=tmp_path / "other-keys")
    # A bundle that is not a JIT draft is not affected.
    plain = tmp_path / "plain"
    Workflow(name="p", steps=[]).save(plain)
    ensure_runnable(plain)
    assert lifecycle.draft_label(plain) is None


def _admission(bundle: Path) -> Any:
    from openadapt_flow.execute.models import AdmittedBundle
    from openadapt_flow.execute.registry import (
        MOCKMED_EFFECT_STRENGTH,
        MOCKMED_ENVIRONMENT_OK,
        MOCKMED_QUALIFICATION_ID,
        MOCKMED_WORKFLOW_DIGEST,
        MOCKMED_WORKFLOW_VERSION,
    )

    return AdmittedBundle(
        qualification_id=MOCKMED_QUALIFICATION_ID,
        workflow_version=MOCKMED_WORKFLOW_VERSION,
        workflow_digest=MOCKMED_WORKFLOW_DIGEST,
        environment_id=MOCKMED_ENVIRONMENT_OK,
        minimum_effect_strength=MOCKMED_EFFECT_STRENGTH,
        bundle_dir=str(bundle),
    )


def test_serve_execute_refuses_a_draft_before_any_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("openadapt_types")
    from openadapt_types.execute import ExecuteRequestV1, ExecuteTerminalOutcomeV1

    import openadapt_flow.tutorial as tutorial
    from openadapt_flow.execute.dispatch import DispatchError, live_replay
    from openadapt_flow.execute.service import ExecuteService
    from tests.test_execute_post_dispatch_faults import _request

    def never(**kw: Any) -> Any:
        raise AssertionError("the replay started")

    monkeypatch.setattr(tutorial, "run_tutorial_workflow", never)
    bundle = _jit(tmp_path).bundle_dir
    admission = _admission(bundle)
    request = ExecuteRequestV1.model_validate(_request())
    with pytest.raises(DispatchError, match="is a DRAFT") as raised:
        live_replay(admission, request, tmp_path / "run")
    assert raised.value.pre_effect is True

    # Through the Execute service: a failed platform receipt, nothing delivered.
    def runner(_admission: Any, req: Any, run_dir: Path) -> Any:
        return live_replay(admission, req, run_dir)

    store = ExecuteService(
        tmp_path / "svc", token="t", runner=runner, seed_mockmed=True
    )
    receipt = store.get_receipt(store.create_execution(_request()).execution_id)
    assert receipt.outcome is ExecuteTerminalOutcomeV1.FAILED_PLATFORM
    assert receipt.delivery_uncertain is False


def test_emit_mcp_labels_a_draft_and_its_server_refuses_it(tmp_path: Path) -> None:
    from openadapt_flow.emit.mcp_tool import emit_mcp_server

    bundle = _jit(tmp_path).bundle_dir
    server = emit_mcp_server(bundle, tmp_path / "mcp" / "server.py")
    assert "DRAFT, not admitted" in server.read_text()
    # The emitted server replays its copy of the bundle through Replayer.run,
    # and the copy still carries the draft marker.
    copy = server.parent / "bundle"
    assert (copy / lifecycle.DRAFT_MARKER).is_file()
    with pytest.raises(DraftNotAdmitted):
        _replay(copy, tmp_path)


def test_emit_skill_labels_a_draft_but_not_an_admitted_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow.emit.skill import emit_skill

    draft = emit_skill(_jit(tmp_path / "d").bundle_dir, tmp_path / "skills-d")
    text = (draft / "SKILL.md").read_text()
    assert "Status: DRAFT, not admitted" in text
    assert "description: DRAFT" in text

    admitted = _admitted(tmp_path / "a", monkeypatch)
    monkeypatch.setenv(lifecycle.KEY_DIR_ENV, str(admitted.key_dir))
    done = emit_skill(admitted.bundle_dir, tmp_path / "skills-a")
    assert "DRAFT" not in (done / "SKILL.md").read_text()
