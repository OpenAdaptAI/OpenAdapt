"""A person approves each save: Desktop's side of Flow's ``each_item`` gate.

Flow (openadapt-flow PR 486) stops a run before each save, writes an approval
request with a screenshot of the filled form, and waits. Desktop shows the
request next to the item's own values and answers it through
``openadapt-flow item-approval``. These tests use scripted Flow stand-ins, so
every request, screenshot and report comes from the test setup. All values
are made up.
"""

from __future__ import annotations

import hashlib
import io
import json
import threading
import time
from pathlib import Path

import pytest

from openadapt_desktop.config import EngineConfig
from openadapt_desktop.db import IndexDB
from openadapt_desktop.dispatch import EngineDispatcher, EngineServices
from openadapt_desktop.flow_bridge import APPROVAL_WAIT_S, FlowBridge, FlowResult
from tests.test_engine.test_work_items import _verified_report

PNG = b"\x89PNG\r\n\x1a\n" + b"filled form"
REQUEST_ID = "0" * 31 + "1"
REQUEST_SHA = "sha256:" + "a" * 64


def _fake_pinned_flow(monkeypatch) -> None:
    """Stand in for the pinned openadapt-flow installed next to this Python."""

    monkeypatch.setattr(
        "openadapt_desktop.flow_bridge._pinned_flow_command",
        lambda: ["/usr/bin/openadapt-flow"],
    )


class _Storage:
    def get_usage_summary(self) -> dict:
        return {}


class _Audit:
    def log(self, *_a, **_k) -> None:
        pass


def _request_row(run_dir: Path, *, status: str = "waiting", digest: str | None = None) -> dict:
    shot = run_dir / "item-approval" / "frame.png"
    return {
        "request_id": REQUEST_ID,
        "request_sha256": REQUEST_SHA,
        "status": status,
        "step_id": "step-1",
        "step_intent": "Click Save",
        "screenshot": str(shot),
        "screenshot_sha256": digest or hashlib.sha256(PNG).hexdigest(),
        "created_at": "2026-10-14T12:00:00+00:00",
        "expires_at": "2026-10-14T16:00:00+00:00",
    }


class _GatedFlow:
    """A Flow that stops before the save until Desktop answers.

    ``run`` asks the dispatcher for the waiting request (as the window
    would, while the run waits), lets ``on_wait`` answer it, then writes the
    report Flow would write for that answer.
    """

    def __init__(self, dispatcher: EngineDispatcher, on_wait) -> None:
        self.dispatcher = dispatcher
        self.on_wait = on_wait
        self.rows: list[dict] = []
        self.answers: list[dict] = []
        self.seen: list[dict] = []
        self.run_kwargs: dict = {}

    def ensure_browser_runtime(self, _progress) -> None:
        pass

    def supports_approval_gate(self) -> bool:
        return True

    def item_approval_show(self, run_dir: Path) -> list[dict]:
        return [dict(row) for row in self.rows]

    def item_approval_answer(self, run_dir: Path, **kwargs) -> dict:
        self.answers.append(kwargs)
        for row in self.rows:
            row["status"] = "approved" if kwargs["decision"] == "approve" else "declined"
        return {
            "ok": True,
            "request_id": kwargs["request_id"],
            "decision": kwargs["decision"],
            "answer_sha256": "sha256:" + "b" * 64,
            "resume_required": False,
        }

    def run(self, bundle, config, out_dir=None, params_file=None, env_overrides=None, **kwargs):
        self.run_kwargs = kwargs
        (out_dir / "item-approval").mkdir()
        (out_dir / "item-approval" / "frame.png").write_bytes(PNG)
        self.rows = [_request_row(out_dir)]
        shown = self.dispatcher.dispatch("get_item_approval", {"workflow_id": "wf-note"})
        self.seen.append(shown)
        self.on_wait(self, shown)
        approved = any(answer["decision"] == "approve" for answer in self.answers)
        report = _verified_report(
            transaction="VERIFIED" if approved else "HALTED_BEFORE_EFFECT",
            execution="VERIFIED" if approved else "HALTED",
            halt=None
            if approved
            else {
                "state_id": "step-1",
                "intent": "Click Save",
                "reason": "A person declined this save",
            },
        )
        report["approval"] = {"decision": "approved" if approved else "declined"}
        (out_dir / "report.json").write_text(json.dumps(report))
        return FlowResult(ok=approved, returncode=0 if approved else 1, out_dir=out_dir)


@pytest.fixture
def task(tmp_path: Path):
    flow_ir = pytest.importorskip("openadapt_flow.ir")
    config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
    config.data_dir.mkdir(parents=True, exist_ok=True)
    db = IndexDB(tmp_path / "index.db")
    db.initialize()
    bundle = config.data_dir / "bundles" / "wf-note"
    flow_ir.Workflow(
        name="Add a claim note",
        params={"member_id": "M-0001", "note": "Reviewed"},
        steps=[flow_ir.Step(id="step-1", intent="Click Save", action=flow_ir.ActionKind.CLICK)],
    ).save(bundle)
    db.insert_capture("cap-1", str(tmp_path / "cap"), "2026-10-08T00:00:00+00:00")
    db.insert_bundle("wf-note", str(bundle), capture_id="cap-1")
    events: list[tuple[str, dict]] = []
    services = EngineServices(config, db=db, storage=_Storage(), audit=_Audit())
    disp = EngineDispatcher(
        config, services=services, emit=lambda event, data: events.append((event, data))
    )
    yield disp, events
    db.close()


def _run(disp: EngineDispatcher, member: str = "M-4821") -> dict:
    return disp.dispatch(
        "run_item",
        {
            "workflow_id": "wf-note",
            "inputs": {"member_id": member, "note": "Reviewed"},
            "target": {"backend": "web", "url": "http://127.0.0.1:9300/"},
        },
    )


def _approve(flow: _GatedFlow, shown: dict) -> None:
    request = shown["request"]
    flow.answer = flow.dispatcher.dispatch(
        "answer_item_approval",
        {
            "workflow_id": "wf-note",
            "decision": "approve",
            "request_id": request["request_id"],
            "request_sha256": request["request_sha256"],
        },
    )


def test_run_it_waits_for_a_person_and_saves_after_approval(task) -> None:
    disp, events = task
    flow = _GatedFlow(disp, _approve)
    disp.services._flow_bridge = flow

    result = _run(disp)

    assert flow.run_kwargs == {"approval_mode": "each_item", "approval_wait_s": APPROVAL_WAIT_S}
    (shown,) = flow.seen
    assert shown["waiting"] is True
    request = shown["request"]
    # The person sees the item's own values next to the filled form.
    assert request["values"] == [
        {"name": "member_id", "value": "M-4821"},
        {"name": "note", "value": "Reviewed"},
    ]
    assert request["screenshot"].startswith("data:image/png;base64,")
    assert request["step_intent"] == "Click Save"
    # The run stops waiting 10 minutes after the request.
    assert request["answer_by"] == "2026-10-14T12:10:00+00:00"
    assert flow.answer == {"ok": True, "decision": "approved"}
    (answer,) = flow.answers
    assert answer == {
        "decision": "approve",
        "request_id": REQUEST_ID,
        "request_sha256": REQUEST_SHA,
        "reason": None,
    }
    assert result["item"]["result"] == "done_checked"
    # Nothing is waiting once the run ends.
    assert disp.dispatch("get_item_approval", {"workflow_id": "wf-note"}) == {
        "ok": True,
        "running": False,
        "waiting": False,
    }
    # Events say a run may wait; they never carry a value.
    assert ("item_approval", {"workflow_id": "wf-note", "state": "watching"}) in events
    assert ("item_approval", {"workflow_id": "wf-note", "state": "closed"}) in events
    assert "M-4821" not in json.dumps(events)


def test_dont_save_records_a_reason_and_saves_nothing(task) -> None:
    disp, _events = task

    def decline(flow: _GatedFlow, shown: dict) -> None:
        request = shown["request"]
        missing_reason = disp.dispatch(
            "answer_item_approval",
            {
                "workflow_id": "wf-note",
                "decision": "decline",
                "request_id": request["request_id"],
                "request_sha256": request["request_sha256"],
            },
        )
        assert missing_reason["ok"] is False
        flow.answer = disp.dispatch(
            "answer_item_approval",
            {
                "workflow_id": "wf-note",
                "decision": "decline",
                "reason": "wrong_record",
                "request_id": request["request_id"],
                "request_sha256": request["request_sha256"],
            },
        )

    flow = _GatedFlow(disp, decline)
    disp.services._flow_bridge = flow

    result = _run(disp)

    assert flow.answer == {"ok": True, "decision": "declined"}
    assert [answer["reason"] for answer in flow.answers] == ["wrong_record"]
    assert result["item"]["result"] == "stopped_before_saving"
    assert result["item"]["nothing_written"] is True


def test_a_request_that_isnt_the_one_shown_is_never_answered(task) -> None:
    disp, _events = task

    def tamper(flow: _GatedFlow, shown: dict) -> None:
        request = shown["request"]
        flow.answer = disp.dispatch(
            "answer_item_approval",
            {
                "workflow_id": "wf-note",
                "decision": "approve",
                "request_id": request["request_id"],
                "request_sha256": "sha256:" + "c" * 64,
            },
        )
        flow.rows[0]["status"] = "expired"
        flow.late = disp.dispatch(
            "answer_item_approval",
            {
                "workflow_id": "wf-note",
                "decision": "approve",
                "request_id": request["request_id"],
                "request_sha256": request["request_sha256"],
            },
        )

    flow = _GatedFlow(disp, tamper)
    disp.services._flow_bridge = flow

    _run(disp)

    assert flow.answer["ok"] is False
    assert flow.late["ok"] is False
    assert flow.answers == []


def test_a_screenshot_that_doesnt_match_the_request_is_not_shown(task) -> None:
    disp, _events = task

    def check(flow: _GatedFlow, shown: dict) -> None:
        flow.rows = [_request_row(Path(flow.rows[0]["screenshot"]).parents[1], digest="0" * 64)]
        flow.mismatch = disp.dispatch("get_item_approval", {"workflow_id": "wf-note"})
        outside = Path(flow.rows[0]["screenshot"]).parents[2] / "elsewhere.png"
        outside.write_bytes(PNG)
        row = _request_row(Path(flow.rows[0]["screenshot"]).parents[1])
        row["screenshot"] = str(outside)
        flow.rows = [row]
        flow.outside = disp.dispatch("get_item_approval", {"workflow_id": "wf-note"})

    flow = _GatedFlow(disp, check)
    disp.services._flow_bridge = flow

    _run(disp)

    assert flow.mismatch["request"]["screenshot"] is None
    assert flow.outside["request"]["screenshot"] is None


def test_an_answer_after_the_run_stopped_waiting_is_not_a_save(task) -> None:
    disp, _events = task

    def late(flow: _GatedFlow, shown: dict) -> None:
        def answer(run_dir, **kwargs):
            flow.answers.append(kwargs)
            return {
                "ok": True,
                "request_id": REQUEST_ID,
                "decision": "approve",
                "answer_sha256": "sha256:" + "b" * 64,
                "resume_required": True,
            }

        flow.item_approval_answer = answer
        request = shown["request"]
        flow.answer = disp.dispatch(
            "answer_item_approval",
            {
                "workflow_id": "wf-note",
                "decision": "approve",
                "request_id": request["request_id"],
                "request_sha256": request["request_sha256"],
            },
        )
        flow.answers.clear()

    flow = _GatedFlow(disp, late)
    disp.services._flow_bridge = flow

    _run(disp)

    assert flow.answer["ok"] is False
    assert "already stopped waiting" in flow.answer["error"]


def test_a_missing_approver_key_tells_the_person_how_to_set_it_up(task) -> None:
    disp, _events = task

    def unsigned(flow: _GatedFlow, shown: dict) -> None:
        def answer(run_dir, **kwargs):
            flow.answers.append(kwargs)
            return {
                "ok": False,
                "code": "human_answer_unsigned",
                "message": "This computer has no approver key, so nothing was approved.",
            }

        flow.item_approval_answer = answer
        request = shown["request"]
        flow.answer = disp.dispatch(
            "answer_item_approval",
            {
                "workflow_id": "wf-note",
                "decision": "approve",
                "request_id": request["request_id"],
                "request_sha256": request["request_sha256"],
            },
        )
        flow.answers.clear()

    flow = _GatedFlow(disp, unsigned)
    disp.services._flow_bridge = flow

    _run(disp)

    assert flow.answer["ok"] is False
    assert flow.answer["code"] == "human_answer_unsigned"
    assert "no approver key" in flow.answer["error"]


def test_no_run_means_nothing_to_approve(task) -> None:
    disp, _events = task
    answer = disp.dispatch(
        "answer_item_approval",
        {
            "workflow_id": "wf-note",
            "decision": "approve",
            "request_id": REQUEST_ID,
            "request_sha256": REQUEST_SHA,
        },
    )
    assert answer["ok"] is False
    assert "isn't waiting" in answer["error"]


def test_run_it_never_starts_on_a_flow_that_cant_wait_for_a_person(task) -> None:
    disp, _events = task

    class OldFlow:
        ran = False

        def supports_approval_gate(self) -> bool:
            return False

        def run(self, *_a, **_k):
            self.ran = True
            raise AssertionError("must not reach Flow")

    flow = OldFlow()
    disp.services._flow_bridge = flow

    result = _run(disp)

    assert flow.ran is False
    assert result["report"]["pre_action_refusal"] is True
    assert "can't stop for your approval" in result["report"]["error"]
    assert result["item"]["result"] == "did_not_start"


# -------------------------------------------------------------- the IPC wire


def test_the_wire_answers_an_approval_while_a_run_waits(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """A run holds the command queue; the approval still gets through."""

    from openadapt_desktop.ipc import IPCHandler

    config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
    handler = IPCHandler(config=config)
    answered = threading.Event()

    def long_run(**_params):
        assert answered.wait(timeout=10), "the approval never reached the engine"
        return {"ok": True, "saved": True}

    def approve(**_params):
        answered.set()
        return {"ok": True, "decision": "approved"}

    handler.dispatcher._handlers["run_item"] = long_run
    handler.dispatcher._handlers["answer_item_approval"] = approve
    lines = [
        json.dumps({"id": "1", "cmd": "run_item", "params": {}}),
        json.dumps({"id": "2", "cmd": "answer_item_approval", "params": {}}),
        json.dumps({"id": "3", "cmd": "get_status", "params": {}}),
    ]
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + "\n"))

    started = time.monotonic()
    handler.run()

    assert time.monotonic() - started < 10
    responses = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    by_id = {item.get("id"): item for item in responses if "id" in item}
    assert by_id["1"]["data"] == {"ok": True, "saved": True}
    assert by_id["2"]["data"] == {"ok": True, "decision": "approved"}
    # Everything else still runs in order, after the run.
    order = [item["id"] for item in responses if item.get("id") in {"1", "3"}]
    assert order == ["1", "3"]


# ------------------------------------------------------------ the Flow bridge


def _bridge(
    calls: list, *, returncode: int = 0, stdout: str = "", envs: list | None = None
) -> FlowBridge:
    class Proc:
        def __init__(self) -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def run(cmd, capture_output=True, text=True, timeout=None, env=None):
        calls.append(cmd)
        if envs is not None:
            envs.append(env)
        return Proc()

    return FlowBridge(runner=run)


@pytest.fixture
def approver_key(monkeypatch, tmp_path: Path):
    """This computer's approver key, in a throwaway folder and fake keychain."""

    from openadapt_desktop import approver_key as module

    monkeypatch.setattr(module, "KEY_DIR", tmp_path / "approver")
    return module.setup_approver_key()


def test_bridge_asks_flow_to_wait_before_each_save(monkeypatch, tmp_path: Path) -> None:
    _fake_pinned_flow(monkeypatch)
    calls: list = []
    bridge = _bridge(calls)
    bridge.run(
        tmp_path / "b",
        tmp_path / "c.yaml",
        out_dir=tmp_path / "r",
        approval_mode="each_item",
        approval_wait_s=600,
    )
    bridge.qualify_run_case(
        tmp_path / "b",
        tmp_path / "c.yaml",
        case_id="practice-1",
        inputs_file=tmp_path / "i.json",
        campaign_id="c",
        run_id="r",
        out_dir=tmp_path / "r",
        approval_wait_s=600,
    )
    run, case = calls
    assert run[run.index("--approval-mode") + 1] == "each_item"
    assert run[run.index("--approval-wait") + 1] == "600"
    assert "--approval-mode" not in case
    assert case[case.index("--approval-wait") + 1] == "600"


def test_bridge_signs_a_signed_out_persons_answer_from_run_it(
    monkeypatch, tmp_path: Path, approver_key
) -> None:
    _fake_pinned_flow(monkeypatch)
    calls: list = []
    envs: list = []
    ok = json.dumps(
        {
            "ok": True,
            "request_id": REQUEST_ID,
            "decision": "decline",
            "answer_sha256": "sha256:" + "b" * 64,
            "resume_required": False,
        }
    )
    bridge = _bridge(calls, stdout=ok, envs=envs)

    answer = bridge.item_approval_answer(
        tmp_path,
        decision="decline",
        request_id=REQUEST_ID,
        request_sha256=REQUEST_SHA,
        reason="screen_problem",
    )

    assert answer["ok"] is True
    (command,) = calls
    assert command[1:4] == ["item-approval", "decline", str(tmp_path)]
    assert command[command.index("--decided-by") + 1] == "human"
    assert command[command.index("--route") + 1] == "desktop_run_it"
    assert "--unauthenticated" in command
    assert "--approver-id" not in command
    assert command[command.index("--reason") + 1] == "screen_problem"
    # Signed with Desktop's approver key; the passphrase reaches Flow only
    # through the child's environment.
    assert command[command.index("--approver-key") + 1] == str(approver_key.key_path)
    (env,) = envs
    passphrase = env["OPENADAPT_JIT_KEY_PASSPHRASE"]
    assert passphrase and not any(passphrase in part for part in command)
    with pytest.raises(ValueError):
        bridge.item_approval_answer(
            tmp_path,
            decision="decline",
            request_id=REQUEST_ID,
            request_sha256=REQUEST_SHA,
            reason="other",
        )
    with pytest.raises(ValueError):
        bridge.item_approval_answer(
            tmp_path, decision="approve", request_id="../x", request_sha256=REQUEST_SHA
        )
    with pytest.raises(ValueError):
        bridge.item_approval_answer(
            tmp_path, decision="approve", request_id=REQUEST_ID, request_sha256="a" * 64
        )


def test_bridge_sends_no_answer_without_an_approver_key(monkeypatch, tmp_path: Path) -> None:
    from openadapt_desktop import approver_key as module

    monkeypatch.setattr(module, "KEY_DIR", tmp_path / "approver")
    _fake_pinned_flow(monkeypatch)
    calls: list = []
    answer = _bridge(calls, stdout="{}").item_approval_answer(
        tmp_path, decision="approve", request_id=REQUEST_ID, request_sha256=REQUEST_SHA
    )
    assert answer["ok"] is False
    assert answer["code"] == "human_answer_unsigned"
    assert "approver-key setup" in answer["message"]
    assert calls == []  # never an unsigned answer


def test_approver_key_setup_is_encrypted_private_and_stable(
    tmp_path: Path, approver_key, fake_keyring
) -> None:
    import os

    from cryptography.hazmat.primitives import serialization

    from openadapt_desktop.approver_key import setup_approver_key

    assert approver_key.created is True
    assert len(approver_key.public_key) == 64
    data = approver_key.key_path.read_bytes()
    with pytest.raises(TypeError):  # encrypted: no passphrase, no key
        serialization.load_pem_private_key(data, password=None)
    if os.name != "nt":
        assert approver_key.key_path.stat().st_mode & 0o777 == 0o600
    assert not any(
        value.encode() in data for value in fake_keyring._store.values()
    )  # the passphrase lives in the keychain, not next to the key
    again = setup_approver_key()
    assert again.created is False
    assert again.public_key == approver_key.public_key
    command = again.register_command("front-desk")
    assert "jit register --role approver --principal front-desk" in command
    assert approver_key.public_key in command


def test_the_portal_gets_its_own_approver_key(tmp_path: Path, approver_key) -> None:
    from openadapt_desktop.approver_key import (
        approver_key_environment,
        portal_approver_environment,
        setup_approver_key,
    )

    assert portal_approver_environment() == {}  # not set up yet
    portal = setup_approver_key("portal")
    assert portal.key_path != approver_key.key_path
    assert portal.public_key != approver_key.public_key
    assert "--principal decision-portal" in portal.register_command()
    env = portal_approver_environment()
    assert env["OPENADAPT_PORTAL_APPROVER_KEY"] == str(portal.key_path)
    desktop = approver_key_environment()
    assert desktop is not None
    assert env["OPENADAPT_JIT_KEY_PASSPHRASE"] != desktop[1]["OPENADAPT_JIT_KEY_PASSPHRASE"]


def test_approver_key_setup_fails_closed_without_a_keychain(
    monkeypatch, tmp_path: Path
) -> None:
    from openadapt_desktop import approver_key as module

    monkeypatch.setattr(module, "KEY_DIR", tmp_path / "approver")
    monkeypatch.setattr("openadapt_desktop.auth.store._keyring", lambda: None)
    with pytest.raises(module.ApproverKeyError):
        module.setup_approver_key()
    assert not module.approver_key_path().exists()
    assert module.approver_key_environment() is None


def test_bridge_reports_a_refused_answer(
    monkeypatch, tmp_path: Path, approver_key
) -> None:
    _fake_pinned_flow(monkeypatch)
    refused = json.dumps({"ok": False, "code": "request_expired", "message": "expired"})
    bridge = _bridge([], returncode=2, stdout=refused)
    answer = bridge.item_approval_answer(
        tmp_path, decision="approve", request_id=REQUEST_ID, request_sha256=REQUEST_SHA
    )
    assert answer == {"ok": False, "code": "request_expired", "message": "expired"}
    garbled = _bridge([], returncode=0, stdout="not json")
    assert (
        garbled.item_approval_answer(
            tmp_path, decision="approve", request_id=REQUEST_ID, request_sha256=REQUEST_SHA
        )["ok"]
        is False
    )


def test_bridge_reads_the_drill_campaign_or_flows_refusal(monkeypatch, tmp_path: Path) -> None:
    _fake_pinned_flow(monkeypatch)
    campaign = {
        "schema": "openadapt.local-drill-campaign/v1",
        "from_case_id": "practice-1",
        "drills": [{"case_id": "drill-wrong-record-step_012", "kind": "wrong_identity"}],
        "skipped": [],
    }
    calls: list = []
    ok = _bridge(calls, stdout=json.dumps(campaign)).qualify_drills(tmp_path)
    assert ok == {"ok": True, "campaign": campaign}
    assert calls[0][1:] == ["qualify", "drills", str(tmp_path), "--json"]
    refused = _bridge(
        [],
        returncode=2,
        stdout="qualify drills REFUSED: the practice case has no inputs. Nothing was changed.",
    ).qualify_drills(tmp_path)
    assert refused["ok"] is False
    assert "practice case has no inputs" in refused["error"]
    assert _bridge([], stdout='{"schema": "other"}').qualify_drills(tmp_path)["ok"] is False


def test_bridge_lists_requests_only_in_flows_shape(monkeypatch, tmp_path: Path) -> None:
    _fake_pinned_flow(monkeypatch)
    listing = {"schema": "openadapt.item-approval-list/v1", "requests": [_request_row(tmp_path)]}
    rows = _bridge([], stdout=json.dumps(listing)).item_approval_show(tmp_path)
    assert rows[0]["request_id"] == REQUEST_ID
    with pytest.raises(ValueError):
        _bridge([], stdout='{"requests": []}').item_approval_show(tmp_path)
    with pytest.raises(ValueError):
        _bridge([], returncode=2, stdout="").item_approval_show(tmp_path)
