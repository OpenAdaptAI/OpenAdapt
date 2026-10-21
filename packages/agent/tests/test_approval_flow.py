"""A run that waits for a person's approval, with real openadapt-flow.

No fakes: Flow records the synthetic MockMed task, then this server runs it
through ``openadapt-flow run --approval-mode each_item`` in a real browser,
and a person's answer is given with ``openadapt-flow item-approval``, as
Desktop's Run it does. Opt in with OPENADAPT_AGENT_E2E=1; it needs an
openadapt-flow with approval modes, the tutorial extra and Chromium.

The run has no profile: a Standard run needs a signed production admission
this test doesn't have. So Flow's report has no transaction outcome, and a
stopped run reads as not_sure_if_saved here, the cautious answer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest

from openadapt_agent.approval import flow_supports_approval_modes, waiting_for_person
from openadapt_agent.bridge import AgentBridge
from openadapt_agent.runner import RunnerConfig

BUNDLE_KEY = "approval-e2e-synthetic-key"

pytestmark = pytest.mark.skipif(
    os.environ.get("OPENADAPT_AGENT_E2E") != "1" or not flow_supports_approval_modes(),
    reason="set OPENADAPT_AGENT_E2E=1 with an openadapt-flow that has approval modes",
)


@pytest.fixture(scope="module")
def mockmed(tmp_path_factory):
    pytest.importorskip("playwright.sync_api")
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.mockmed.fault_server import serve
    from openadapt_flow.tutorial import TUTORIAL_ENTRY_QUERY, record_tutorial

    os.environ.setdefault("DO_NOT_TRACK", "1")
    os.environ.setdefault("OPENADAPT_TELEMETRY", "0")
    work = tmp_path_factory.mktemp("approval-e2e")
    saved_key = os.environ.get("OPENADAPT_BUNDLE_KEY")
    os.environ["OPENADAPT_BUNDLE_KEY"] = BUNDLE_KEY
    base_url, _db, stop = serve()
    try:
        recording = record_tutorial(base_url, work / "recording")
        workflow = compile_recording(
            recording, work / "compiled", name="approval-e2e", mine_effects=True
        )
        bundles = work / "bundles"
        workflow.save(bundles / "add-note", encrypt=True, key=BUNDLE_KEY)
        yield {
            "base_url": base_url,
            "entry_url": f"{base_url.rstrip('/')}/{TUTORIAL_ENTRY_QUERY}",
            "bundles": bundles,
            "work": work,
        }
    finally:
        stop()
        if saved_key is None:
            os.environ.pop("OPENADAPT_BUNDLE_KEY", None)
        else:
            os.environ["OPENADAPT_BUNDLE_KEY"] = saved_key


def _records(base_url: str) -> list:
    from urllib.request import urlopen

    with urlopen(f"{base_url}api/db", timeout=10) as response:
        return list(json.loads(response.read()).get("records", []))


def _bridge(
    mockmed, name: str, *, approval_wait: str, timeout_s: float, approval_mode="each_item"
) -> AgentBridge:
    config = RunnerConfig(
        runs_dir=mockmed["work"] / name,
        url=mockmed["entry_url"],
        timeout_s=timeout_s,
        approval_mode=approval_mode,
        extra_run_args=(
            "--durable",
            "--approval-wait",
            approval_wait,
            "--backend",
            "web",
            "--effects-kind",
            "rest",
            "--effects-base-url",
            mockmed["base_url"],
        ),
    )
    return AgentBridge(mockmed["bundles"], config, allow_run=True, mode="production")


def _run(bridge: AgentBridge, request_id: str, wait: int) -> dict:
    from openadapt_flow.tutorial import TUTORIAL_NOTE

    return bridge.dispatch(
        "run_workflow",
        {
            "workflow": next(iter(bridge.catalog)),
            "inputs": {"note": TUTORIAL_NOTE},
            "request_id": request_id,
            "wait_seconds": wait,
        },
    )


def _flow(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "openadapt_flow", *argv],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_nobody_answers_needs_review_and_the_server_never_kills_the_wait(mockmed):
    import urllib.request

    urllib.request.urlopen(
        urllib.request.Request(f"{mockmed['base_url']}api/reset", data=b"{}", method="POST"),
        timeout=10,
    ).close()
    # Flow waits 25 s for a person. The server's own timeout is 20 s of
    # work, so counting the wait would kill the run.
    bridge = _bridge(mockmed, "late", approval_wait="25", timeout_s=20.0)
    started = time.monotonic()
    waiting = _run(bridge, "approval-late-0001", wait=120)
    assert waiting["outcome"] == "needs_review", waiting
    assert waiting["reason"] == "waiting_for_approval"
    assert waiting["technical"]["approval_mode"] == "each_item"
    assert time.monotonic() - started < 60

    stopped = bridge.dispatch("get_run", {"run_id": waiting["run_id"], "wait_seconds": 120})
    elapsed = time.monotonic() - started
    print("waited", round(elapsed, 1), "s:", stopped)
    # Flow gave up waiting by itself, after more than the server's timeout.
    assert elapsed > 25
    assert stopped["outcome"] not in {"running", "done"}, stopped
    assert stopped["reason"] not in {"timed_out", "waiting_for_approval"}, stopped
    assert _records(mockmed["base_url"]) == []


def _approver_key(tmp_path, monkeypatch):
    """A person's approver key, registered in a throwaway key directory.

    The run and the answer both read that directory, so only an answer this
    key signed counts as the person's.
    """
    from openadapt_flow.jit.authority import (
        KEY_PASSPHRASE_ENV,
        generate_encrypted_key,
        register_principal,
    )
    from openadapt_flow.jit.lifecycle import KEY_DIR_ENV

    key_dir = tmp_path / "keys"
    monkeypatch.setenv(KEY_DIR_ENV, str(key_dir))
    monkeypatch.setenv(KEY_PASSPHRASE_ENV, "approval-e2e-synthetic-passphrase")
    key_path = key_dir / "front-desk.approver.pem"
    public = generate_encrypted_key(key_path)
    register_principal(key_dir, role="approver", principal_id="front-desk", public_key_hex=public)
    return key_path


def test_a_person_approves_and_the_run_saves_once(mockmed, tmp_path, monkeypatch):
    import urllib.request

    key_path = _approver_key(tmp_path, monkeypatch)

    urllib.request.urlopen(
        urllib.request.Request(f"{mockmed['base_url']}api/reset", data=b"{}", method="POST"),
        timeout=10,
    ).close()
    bridge = _bridge(mockmed, "approved", approval_wait="120", timeout_s=120.0)
    result: dict = {}
    worker = threading.Thread(
        target=lambda: result.update(_run(bridge, "approval-ok-0001", wait=120))
    )
    worker.start()
    runs = mockmed["work"] / "approved"
    deadline = time.monotonic() + 90
    run_dir = None
    while time.monotonic() < deadline and run_dir is None:
        for candidate in runs.glob("run-*"):
            if waiting_for_person(candidate):
                run_dir = candidate
        time.sleep(0.25)
    assert run_dir is not None, "the run never asked for an approval"
    worker.join(30)
    assert result["reason"] == "waiting_for_approval", result

    # The person answers, as Desktop's Run it would.
    shown = _flow("item-approval", "show", str(run_dir))
    (request,) = json.loads(shown.stdout)["requests"]
    approved = _flow(
        "item-approval",
        "approve",
        str(run_dir),
        "--request-id",
        request["request_id"],
        "--request-sha256",
        request["request_sha256"],
        "--decided-by",
        "human",
        "--route",
        "desktop_run_it",
        "--unauthenticated",
        "--approver-key",
        str(key_path),
    )
    assert approved.returncode == 0, approved.stdout + approved.stderr
    final = bridge.dispatch("get_run", {"run_id": result["run_id"], "wait_seconds": 120})
    print("approved:", final)
    # Unprofiled, so Flow doesn't stamp a verified outcome; MockMed's record
    # store is the check that the save happened exactly once.
    assert final["outcome"] != "running", final
    assert final["reason"] not in {"waiting_for_approval", "timed_out"}, final
    assert len(_records(mockmed["base_url"])) == 1


def test_flow_2_gates_a_default_run_and_the_agent_reports_the_wait(mockmed):
    """Flow 2.0 gates every saving run by default. With no --approval-mode
    from the operator, the agent must surface the wait, not hang or save."""
    import urllib.request

    urllib.request.urlopen(
        urllib.request.Request(f"{mockmed['base_url']}api/reset", data=b"{}", method="POST"),
        timeout=10,
    ).close()
    bridge = _bridge(mockmed, "default", approval_wait="15", timeout_s=20.0, approval_mode=None)
    started = time.monotonic()
    waiting = _run(bridge, "approval-default-0001", wait=120)
    assert waiting["outcome"] == "needs_review", waiting
    assert waiting["reason"] == "waiting_for_approval"
    print("default run:", waiting)
    # The operator named no mode, so the agent does not claim one Flow may
    # have taken from deployment config; it only reports a named mode.
    assert waiting.get("technical", {}).get("approval_mode") in (None, "each_item")
    assert time.monotonic() - started < 60
    stopped = bridge.dispatch("get_run", {"run_id": waiting["run_id"], "wait_seconds": 120})
    print("default run stopped:", stopped)
    assert stopped["outcome"] not in {"running", "done"}, stopped
    assert _records(mockmed["base_url"]) == []
