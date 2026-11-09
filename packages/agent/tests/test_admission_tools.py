"""MCP admission tools: prepare, list, decide. Made-up values only.

The tools are off unless the operator passes --admissions. A person decides
in the client's form; with --delegate-approvals and a person-signed
--approver-grant the agent decides as that approver. An agent never decides
an admission it prepared or without a grant that verifies, and the record
names who decided (and, for an agent, the grant and who signed it).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import anyio
import mcp.types as types
import pytest
from PIL import Image

from openadapt_agent.admission import AdmissionDesk
from openadapt_agent.bridge import AgentBridge, BridgeError
from openadapt_agent.mcp import build_server
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr

TOOLS = {"prepare_admission", "list_admissions", "decide_admission"}


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _recording(root: Path, name: str = "rec1") -> None:
    folder = root / "recordings" / name
    (folder / "templates").mkdir(parents=True)
    (folder / "templates" / "save.png").write_bytes(_png())
    workflow = Workflow(
        name="qualified-write",
        params={"record_id": "example", "note": "example"},
        surface="web",
        steps=[
            Step(
                id="save",
                intent="Save the record",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/save.png",
                    region=(10, 10, 40, 20),
                    click_point=(30, 20),
                    ocr_text="Save",
                    structured_identity="record identity",
                ),
                expect=[Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Saved")],
                effects=[
                    Effect(
                        kind=EffectKind.FIELD_EQUALS,
                        match={"id": ValueExpr(param="record_id")},
                        field="note",
                        value=ValueExpr(param="note"),
                        idempotency_key=ValueExpr(param="record_id"),
                        risk="irreversible",
                    )
                ],
                risk="irreversible",
                identity_armed=True,
            )
        ],
    )
    workflow.save(folder)
    (folder / "meta.json").write_text(
        json.dumps(
            {
                "app_url": "http://127.0.0.1:9/",
                "application": "MockMed",
                "application_version": "tutorial",
                "viewport": [1280, 800],
                "surface": "web",
                "params": dict(workflow.params),
            }
        )
    )


def _grant_file(root: Path, delegates=("agent:reviewer",)) -> Path:
    """A grant a made-up registered person signed, letting agents approve saves."""

    from datetime import datetime, timedelta, timezone

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from openadapt_flow.approval_mode.approver import ApproverPolicyV1, StepClass
    from openadapt_flow.approval_mode.signed_policy import ApproverGrant, sign_grant
    from openadapt_flow.jit.authority import public_hex, register_principal

    key = Ed25519PrivateKey.generate()
    register_principal(
        root / "keys",
        role="policy_signer",
        principal_id="person-0001",
        public_key_hex=public_hex(key),
    )
    allowed = {c.value: ["person"] for c in StepClass}
    allowed[StepClass.CONSEQUENTIAL.value] = ["person", "agent"]
    start = datetime.now(timezone.utc) - timedelta(minutes=1)
    grant = ApproverGrant(
        grant_id="grant-0001",
        signer="person-0001",
        signed_at=start.isoformat(),
        expires_at=(start + timedelta(days=30)).isoformat(),
        approver_policy=ApproverPolicyV1.model_validate({"allowed": allowed}),
        delegates=delegates,
    )
    return sign_grant(grant, key).save(root / "grant.json")


def _bridge(bundles_root, runner_config, root, *, delegate=None, grant=None):
    desk = AdmissionDesk(root, delegate_id=delegate, approver_grant=grant, key_dir=root / "keys")
    return AgentBridge(bundles_root, runner_config, admission_desk=desk)


def _names(bridge):
    return {spec.name for spec in bridge.list_tool_specs()}


def test_tools_are_off_without_admissions(bundles_root, runner_config):
    bridge = AgentBridge(bundles_root, runner_config)
    assert not _names(bridge) & TOOLS
    with pytest.raises(BridgeError, match="off"):
        bridge.dispatch("list_admissions", {})


def test_prepare_stops_once_and_names_the_preparer(bundles_root, runner_config, tmp_path):
    _recording(tmp_path)
    bridge = _bridge(bundles_root, runner_config, tmp_path, delegate="agent:intake-bot")
    assert TOOLS <= _names(bridge)
    view = bridge.dispatch("prepare_admission", {"recordings": ["rec1"], "name": "w"})
    assert view["status"] == "needs_decision"
    assert view["prepared_by"] == {"kind": "agent", "id": "agent:intake-bot", "route": "mcp"}
    assert {"step_id": "save", "kind": "field_equals", "tier": 1} in view["checks"]
    assert view["decided"] is False
    listed = bridge.dispatch("list_admissions", {})["admissions"]
    assert [row["admission"] for row in listed] == [view["admission"]]


def test_an_agent_cannot_decide_what_it_prepared(bundles_root, runner_config, tmp_path):
    _recording(tmp_path)
    bridge = _bridge(bundles_root, runner_config, tmp_path, delegate="agent:intake-bot")
    view = bridge.dispatch("prepare_admission", {"recordings": ["rec1"], "name": "w"})
    args = {
        "admission": view["admission"],
        "pending_sha256": view["pending_sha256"],
        "decision": "accept",
    }
    with pytest.raises(BridgeError, match="self-approval"):
        bridge.dispatch("decide_admission", args)
    ungranted = _bridge(bundles_root, runner_config, tmp_path, delegate="agent:reviewer")
    with pytest.raises(BridgeError, match="approver_grant_missing"):
        ungranted.dispatch("decide_admission", args)
    grant = _grant_file(tmp_path)
    reviewer = _bridge(
        bundles_root, runner_config, tmp_path, delegate="agent:reviewer", grant=grant
    )
    result = reviewer.dispatch("decide_admission", args)
    assert result["decided_by"]["kind"] == "agent"
    assert result["decided_by"]["id"] == "agent:reviewer"
    assert result["decided_by"]["on_behalf_of"] == "person-0001"
    assert result["decided_by"]["policy_ref"].startswith("sha256:")
    assert result["approval_mode"] == "problems_only"
    assert result["prepared_by"]["id"] == "agent:intake-bot"
    assert result["oracle_gate_passed"] is True
    record = json.loads(
        (tmp_path / "admissions" / view["admission"] / "admission-decision.json").read_text()
    )
    assert record["decided_by"]["id"] == "agent:reviewer"


def test_without_a_delegate_the_bridge_never_decides(bundles_root, runner_config, tmp_path):
    _recording(tmp_path)
    bridge = _bridge(bundles_root, runner_config, tmp_path)
    view = bridge.dispatch("prepare_admission", {"recordings": ["rec1"], "name": "w"})
    with pytest.raises(BridgeError, match="person must decide"):
        bridge.dispatch(
            "decide_admission",
            {
                "admission": view["admission"],
                "pending_sha256": view["pending_sha256"],
                "decision": "accept",
            },
        )


def test_a_delegate_cannot_decide_what_an_unidentified_client_prepared(
    bundles_root, runner_config, tmp_path
):
    _recording(tmp_path)
    # Prepared on a server without --delegate-approvals: the preparer is
    # "mcp-client", which may be the same agent that holds the delegate id.
    view = _bridge(bundles_root, runner_config, tmp_path).dispatch(
        "prepare_admission", {"recordings": ["rec1"], "name": "w"}
    )
    assert view["prepared_by"]["id"] == "mcp-client"
    reviewer = _bridge(
        bundles_root,
        runner_config,
        tmp_path,
        delegate="agent:reviewer",
        grant=_grant_file(tmp_path),
    )
    with pytest.raises(BridgeError, match="unidentified"):
        reviewer.dispatch(
            "decide_admission",
            {
                "admission": view["admission"],
                "pending_sha256": view["pending_sha256"],
                "decision": "accept",
            },
        )
    assert not (tmp_path / "admissions" / view["admission"] / "admission-decision.json").exists()


def test_a_tampered_pending_file_is_listed_but_never_shown(bundles_root, runner_config, tmp_path):
    _recording(tmp_path)
    bridge = _bridge(bundles_root, runner_config, tmp_path, delegate="agent:intake-bot")
    view = bridge.dispatch("prepare_admission", {"recordings": ["rec1"], "name": "w"})
    path = tmp_path / "admissions" / view["admission"] / "admission-pending.json"
    data = json.loads(path.read_text())
    data["prepared_by"] = {"kind": "person", "id": "pat", "route": "cli"}
    path.write_text(json.dumps(data))
    listed = bridge.dispatch("list_admissions", {})["admissions"]
    assert listed == [{"admission": view["admission"], "status": "unverified"}]


def test_paths_stay_inside_the_admissions_folder(bundles_root, runner_config, tmp_path):
    bridge = _bridge(bundles_root, runner_config, tmp_path, delegate="agent:x")
    for bad in ("../etc", "/tmp", "a/b"):
        with pytest.raises(BridgeError):
            bridge.dispatch("prepare_admission", {"recordings": [bad], "name": "w"})


def test_regulated_pack_refuses_an_agent(bundles_root, runner_config, tmp_path):
    _recording(tmp_path)
    person_server = _bridge(bundles_root, runner_config, tmp_path)
    view = person_server.dispatch(
        "prepare_admission",
        {"recordings": ["rec1"], "name": "w", "policy_pack": "regulated"},
    )
    if view["status"] != "needs_decision":
        pytest.skip("regulated pack halted the proposal for this fixture")
    agent = _bridge(bundles_root, runner_config, tmp_path, delegate="agent:reviewer")
    with pytest.raises(BridgeError, match="needs a person"):
        agent.dispatch(
            "decide_admission",
            {
                "admission": view["admission"],
                "pending_sha256": view["pending_sha256"],
                "decision": "accept",
            },
        )


def _wire(model):
    return model.model_dump(by_alias=True, mode="json")


@pytest.mark.parametrize("answer", ["accept", "decline", "unsupported"])
def test_person_decides_in_the_client_form(
    answer, bundles_root, runner_config, tmp_path, mcp_client
):
    _recording(tmp_path)
    bridge = _bridge(bundles_root, runner_config, tmp_path)
    view = bridge.dispatch("prepare_admission", {"recordings": ["rec1"], "name": "w"})
    prompts = []

    async def elicit(context, params):
        prompts.append(_wire(params))
        return types.ElicitResult(action=answer, content={"confirmed": answer == "accept"})

    async def call():
        kwargs = {} if answer == "unsupported" else {"elicitation_callback": elicit}
        async with mcp_client(build_server(bridge), **kwargs) as client:
            return _wire(
                await client.call_tool(
                    "decide_admission",
                    {
                        "admission": view["admission"],
                        "pending_sha256": view["pending_sha256"],
                        "decision": "accept",
                    },
                )
            )

    result = anyio.run(call)
    decision = tmp_path / "admissions" / view["admission"] / "admission-decision.json"
    if answer == "accept":
        assert result["isError"] is False
        payload = json.loads(result["content"][0]["text"])
        assert payload["decided_by"] == {"kind": "person", "id": "mcp-form", "route": "mcp"}
        assert "Proposed checks" in prompts[0]["message"]
        assert decision.is_file()
        return
    assert result["isError"] is True
    assert not decision.exists()
