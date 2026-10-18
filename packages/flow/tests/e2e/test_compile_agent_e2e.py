"""compile-agent end to end on MockMed, with a fake Claude client and no network.

The agent is the real :class:`ClaudeAgent` loop in selective-vision mode. Its
client is a fake that reads the structural listing the harness sends and
answers with tool calls, like a model would, so the loop, the harness, the
gate, the JIT capture and the admission all run for real. It asserts:

* run 1 is recorded, passes its record check against the system of record,
  the proposed checks are confirmed by a person, and the program is admitted
  by a registered agent under a signed policy that allows self-admission;
* the agent saw no screenshot except the one after the save (the gate);
* the skill, the MCP server and the CLI line are emitted;
* run 2 replays with zero model calls, ends VERIFIED, and gets a VERIFIED
  verify-run receipt; compiling again does not call the agent;
* an agent that opens the wrong patient is refused at the save and nothing
  is written or compiled;
* the admitted program, run against an app that files the note on another
  patient, stops and is not VERIFIED.
"""

from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Optional

import pytest

from openadapt_flow.compile_agent import (
    ClaudeAgent,
    FirstRunCompiler,
    compile_first_run,
    run_compiled,
)
from openadapt_flow.jit.keys import CustomerModelKey
from openadapt_flow.mockmed.fault_server import serve
from openadapt_flow.tutorial import _http_json, _records

PROMPT = (
    "Open the referral for Jane Sample, start a Triage encounter, and save "
    "this note: {note}"
)
NOTE = "Synthetic follow-up in two weeks"
NEW_NOTE = "Recheck knee in ten days"
ON_SCREEN = ["Jane Sample", "MRN P1"]
EXPECT = {"patient_id": "p1", "type": "Triage"}
AGENT_ID = "agent:compile-agent-e2e"
TEST_KEY = CustomerModelKey("anthropic", "env:TEST_ONLY", "not-a-real-key")


@pytest.fixture(scope="module")
def mockmed() -> Iterator[str]:
    base, _db, stop = serve()
    yield base.rstrip("/")
    stop()


def _reset(base: str):
    return lambda: _http_json(f"{base}/api/reset", method="POST", body={})


def _verify_yaml(tmp_path: Path, base: str) -> Path:
    path = tmp_path / "verify.yaml"
    path.write_text(
        "verifier:\n"
        "  kind: rest\n"
        f"  base_url: {base}\n"
        "  records_path: /api/db\n"
        "  records_key: records\n"
        "  timeout_s: 2.0\n"
        "  poll_interval_s: 0.05\n"
    )
    return path


def _policy_agent(key_dir: Path, base: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A registered agent, under a policy a person signed that lets it admit
    what it recorded itself."""

    from openadapt_flow.jit import authority

    monkeypatch.setenv(authority.KEY_PASSPHRASE_ENV, "e2e passphrase")
    files = key_dir.parent / "keyfiles"
    keys = {}
    for role, pid, name in (
        ("policy_signer", "dana.signer", "signer"),
        ("agent", AGENT_ID, "agent"),
    ):
        pub = authority.generate_encrypted_key(files / f"{name}.pem")
        authority.register_principal(
            key_dir, role=role, principal_id=pid, public_key_hex=pub
        )
        keys[name] = authority.load_encrypted_key(files / f"{name}.pem")
    now = datetime.now(timezone.utc)
    policy = authority.AdmissionPolicy(
        policy_id="mockmed-notes",
        signer="dana.signer",
        signed_at=(now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        allowed_hosts=(base.split("://", 1)[1],),
        allowed_actions=("click", "type"),
        allowed_effects=("record_written", "field_equals"),
        min_clean_canaries=2,
        agent_principals=(AGENT_ID,),
        allow_self_admission=True,
    )
    signed = authority.sign_policy(policy, keys["signer"])
    return authority.principal_from_key(keys["agent"], key_dir, policy=signed)


class FakeClaude:
    """Answers the harness's listings with the tool calls a model would make.

    It finds refs in the listing text only, so it fails the way a model would
    if the listing stopped naming the controls.
    """

    def __init__(self, patient: str, note: str) -> None:
        self.patient = patient
        self.note = note
        self.calls = 0
        self.messages = SimpleNamespace(create=self.create)

    @staticmethod
    def _listing(messages: list[dict[str, Any]]) -> str:
        last = messages[-1]["content"]
        texts: list[str] = []
        for block in last:
            if block.get("type") == "text":
                texts.append(block["text"])
            for inner in block.get("content") or []:
                if isinstance(inner, dict) and inner.get("type") == "text":
                    texts.append(inner["text"])
        return "\n".join(texts)

    @staticmethod
    def _ref(listing: str, name: str, near: Optional[str] = None) -> str:
        for line in listing.splitlines():
            if near and near not in line:
                continue
            found = re.search(r"\[(e\d+)\] \S+ \"" + re.escape(name) + '"', line)
            if found:
                return found.group(1)
        raise AssertionError(f"no {name!r} in listing:\n{listing}")

    def create(self, **kw: Any) -> Any:
        self.calls += 1
        assert kw["model"] == "claude-opus-5-5"
        listing = self._listing(kw["messages"])
        if "Encounter saved" in listing:
            tool = ("finish", {"summary": "saved"})
        elif "# Referral Tasks" in listing:
            tool = ("click", {"ref": self._ref(listing, "Open", self.patient)})
        elif "# New Encounter" in listing:
            if not re.search(r'"Triage" \([^)]*selected', listing):
                tool = ("click", {"ref": self._ref(listing, "Triage")})
            elif f'value="{self.note}"' not in listing:
                ref = self._ref(listing, "Note")
                tool = ("type_text", {"text": self.note, "ref": ref})
            else:
                tool = ("click", {"ref": self._ref(listing, "Save Encounter")})
        elif "New Encounter" in listing:
            tool = ("click", {"ref": self._ref(listing, "New Encounter")})
        else:
            raise AssertionError(f"unexpected listing:\n{listing}")
        name, args = tool
        return SimpleNamespace(
            content=[
                {
                    "type": "tool_use",
                    "id": f"toolu_{self.calls}",
                    "name": name,
                    "input": args,
                }
            ],
            stop_reason="tool_use",
            usage=SimpleNamespace(
                input_tokens=len(str(kw["messages"])) // 4,
                output_tokens=30,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )


def _without_agent_args(tmp_path: Path, base: str) -> dict[str, Any]:
    common = _common(tmp_path, base)
    common.pop("on_warning")
    return common


def _common(tmp_path: Path, base: str) -> dict[str, Any]:
    return dict(
        url=f"{base}/?fault=ok&idempotency=demo#tasks",
        verify=_verify_yaml(tmp_path, base),
        expect=EXPECT,
        on_screen=ON_SCREEN,
        task_id="mockmed.compile_agent_note",
        cache_dir=tmp_path / "cache",
        key_dir=tmp_path / "keys",
        model_key=TEST_KEY,
        reset=_reset(base),
        on_warning=lambda text: None,
    )


def test_compile_agent_then_replay_with_no_model_calls(
    mockmed: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admitter = _policy_agent(tmp_path / "keys", mockmed, monkeypatch)
    fake = FakeClaude("Jane Sample", NOTE)
    # The registered agent drives run 1 and admits it: self-admission, which
    # its signed policy allows.
    agent = ClaudeAgent(client=fake, principal_id=AGENT_ID)
    result = compile_first_run(
        PROMPT,
        params={"note": NOTE},
        agent=agent,
        confirm_checks="dana.reviewer",
        drop_fields=["key", "source"],
        admitter=admitter,
        canary_params=[{"note": "Canary test note"}],
        **_common(tmp_path, mockmed),
    )
    assert result.status == "admitted", result.detail
    calls_after_run1 = fake.calls
    assert result.run1["agent"]["model_calls"] == calls_after_run1 == 6
    harness = result.run1["harness"]
    # Selective vision: no screenshot asked for; one at the save gate.
    assert harness["screenshots_requested"] == 0
    assert harness["screenshots_at_gate"] == 1
    assert harness["screenshots_every_turn"] == 0
    assert harness["gate_checks"] == 1 and harness["gate_refusals"] == 0
    assert result.run1["gates"][0]["decision"] == "passed_no_hold"
    assert result.record_check is not None and result.record_check["passed"]
    assert result.admission is not None
    assert result.admission["mode"] == "agent_policy"
    assert result.admission["admitted_by"] == AGENT_ID
    assert len(result.admission["canaries"]) == 2
    assert all(
        c["status"] == "VERIFIED" and c["receipt_outcome"] == "VERIFIED"
        for c in result.admission["canaries"]
    )
    confirmed = result.confirmed_checks
    assert confirmed is not None and "needs_operator_confirmation" not in (
        confirmed.read_text()
    )
    # What it is called through.
    skill = Path(result.emitted["skill"]) / "SKILL.md"
    assert "compile-agent run" in skill.read_text() and NOTE not in skill.read_text()
    server = Path(result.emitted["mcp_server"]).read_text()
    ast.parse(server)
    assert "run_compiled" in server and NOTE not in server
    assert result.emitted["cli"].endswith("--param note=<note>")

    # Run 2: the program, a new note, no model calls, read back and receipted.
    replay = run_compiled(result.dir, reset=_reset(mockmed), note=NEW_NOTE)
    assert replay.verified, replay.to_dict()
    assert replay.model_calls == 0 and fake.calls == calls_after_run1
    assert replay.receipt_outcome == "VERIFIED"
    rows = _records(mockmed)
    assert [(r["patient_id"], r["type"], r["note"]) for r in rows] == [
        ("p1", "Triage", NEW_NOTE)
    ]

    # Compiling again uses the admitted program; the agent is not called.
    again = compile_first_run(
        PROMPT,
        params={"note": NOTE},
        agent=agent,
        admitter=admitter,
        **_common(tmp_path, mockmed),
    )
    assert again.status == "admitted" and fake.calls == calls_after_run1
    # Run 1 never runs again on an admitted task, not even step by step.
    from openadapt_flow.jit import AdmissionError

    stepwise = FirstRunCompiler(
        PROMPT, params=["note"], **_without_agent_args(tmp_path, mockmed)
    )
    with pytest.raises(AdmissionError, match="runs only once"):
        stepwise.record(agent, {"note": NOTE})

    # The admitted program against an app that files the note on patient p2.
    common = _common(tmp_path, mockmed)
    common["url"] = f"{mockmed}/?fault=wrong_record&idempotency=demo#tasks"
    common.pop("model_key")
    common.pop("on_warning")
    wrong = FirstRunCompiler(PROMPT, params=["note"], **common).run(note=NEW_NOTE)
    assert not wrong.verified and wrong.model_calls == 0
    assert wrong.status == "HALTED" and wrong.inbox_item is not None
    assert fake.calls == calls_after_run1


def test_wrong_patient_is_refused_at_the_save(
    mockmed: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admitter = _policy_agent(tmp_path / "keys", mockmed, monkeypatch)
    fake = FakeClaude("Alex Testcase", NOTE)  # opens the wrong chart
    result = compile_first_run(
        PROMPT,
        params={"note": NOTE},
        agent=ClaudeAgent(client=fake),
        confirm_checks="dana.reviewer",
        admitter=admitter,
        **_common(tmp_path, mockmed),
    )
    assert result.status == "refused_wrong_record", result.detail
    assert "MRN P1" in (result.detail or "")
    assert result.run1["gates"][0]["decision"] == "refused_wrong_record"
    assert _records(mockmed) == []  # nothing was saved
    assert not (result.dir / "program").exists()  # nothing was compiled
    assert result.emitted == {}


SCRIPT = {
    "principal_id": "agent:scripted-e2e",
    "steps": [
        {"click": "Open", "near": "Jane Sample"},
        {"click": "New Encounter"},
        {"click": "Triage"},
        {"type": "{note}", "into": "Note"},
        {"click": "Save Encounter"},
    ],
}


def test_cli_scripted_agent_person_confirms_key_admits(
    mockmed: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import json

    from openadapt_flow.__main__ import build_parser
    from openadapt_flow.compile_agent.cli import cmd_compile_agent
    from openadapt_flow.jit import authority

    # The CLI has no reset hook (a real app is never reset). MockMed's demo
    # idempotency key makes a second note on p1 a duplicate, so reset it
    # before each run here, the way the Python API's reset= does.
    original = FirstRunCompiler._on_reset

    def reset_first(self: FirstRunCompiler) -> None:
        _reset(mockmed)()
        original(self)

    monkeypatch.setattr(FirstRunCompiler, "_on_reset", reset_first)
    monkeypatch.setenv(authority.KEY_PASSPHRASE_ENV, "e2e passphrase")
    key_dir = tmp_path / "keys"
    pem = tmp_path / "keyfiles" / "ci.pem"
    authority.register_principal(
        key_dir,
        role="admitter_key",
        principal_id="ci:compile-agent",
        public_key_hex=authority.generate_encrypted_key(pem),
    )
    script = tmp_path / "script.json"
    script.write_text(json.dumps(SCRIPT))
    argv = [
        "compile-agent",
        PROMPT,
        "--url",
        f"{mockmed}/?fault=ok&idempotency=demo#tasks",
        "--verify",
        str(_verify_yaml(tmp_path, mockmed)),
        "--param",
        f"note={NOTE}",
        "--expect",
        "patient_id=p1",
        "--on-screen",
        "Jane Sample",
        "--on-screen",
        "MRN P1",
        "--agent",
        str(script),
        "--task-id",
        "mockmed.compile_agent_cli",
        "--cache",
        str(tmp_path / "cache"),
        "--key-dir",
        str(key_dir),
        "--admit-key",
        str(pem),
        "--drop-field",
        "key",
        "--canary-param",
        "note=Canary test note",
        "--json",
    ]
    # No terminal: run 1 passes, and the checks wait for a person.
    args = build_parser().parse_args(argv)
    assert cmd_compile_agent(args, interactive=lambda: False) == 3
    first = json.loads(capsys.readouterr().out)
    assert first["status"] == "draft" and "next" in first
    assert first["record_check"]["passed"]

    # A person at a terminal confirms; the registered key admits. The agent
    # is not run again (the draft is waiting).
    answers = iter(["dana.reviewer", "ok"])
    args = build_parser().parse_args([*argv, "--no-hold"])
    assert (
        cmd_compile_agent(args, ask=lambda _: next(answers), interactive=lambda: True)
        == 0
    )
    out = capsys.readouterr().out
    assert "Proposed record checks" in out and "patient_id = p1" in out
    admitted = json.loads(out[out.index("\n{") + 1 :])
    assert admitted["status"] == "admitted", admitted["detail"]
    assert admitted["admission"]["mode"] == "admitter_key"
    task_dir = next((tmp_path / "cache").glob("mockmed.compile_agent_cli-*"))
    emitted = json.loads((task_dir / "compiled" / "emitted.json").read_text())
    assert emitted["cli"].startswith("openadapt-flow compile-agent run ")

    run = build_parser().parse_args(
        ["compile-agent", "run", str(task_dir), "--param", f"note={NEW_NOTE}", "--json"]
    )
    assert cmd_compile_agent(run) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["verified"] and replay["model_calls"] == 0
    assert replay["receipt_outcome"] == "VERIFIED"
