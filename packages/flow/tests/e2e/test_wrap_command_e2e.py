"""``openadapt wrap -- COMMAND`` end to end with a real outside process.

The wrapped command is ``examples/wrap_command/agent.py``, a separate Python
process with no model. It reaches OpenAdapt only through the MCP endpoint in
``OPENADAPT_MCP_URL`` with the token in ``OPENADAPT_MCP_TOKEN``. Everything
else is real: the bundled clinic app over HTTP, headless Chromium, the save
gate, the read-back, compile, canary, admission by a registered admitter key
under a person-signed teach authorization, and a later run of the emitted MCP
tool with no model calls and no command.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

pytest.importorskip("playwright")
pytest.importorskip("mcp")
pytest.importorskip("uvicorn")

from openadapt_flow.integrations.agents.recording import load_task  # noqa: E402
from openadapt_flow.mockmed.fault_server import serve  # noqa: E402
from openadapt_flow.teach_authorization_fixture import demo_authority  # noqa: E402
from openadapt_flow.tutorial import _http_json  # noqa: E402
from openadapt_flow.wrap import WrappedTask  # noqa: E402
from openadapt_flow.wrap.process import CommandFailed, ProcessAgent  # noqa: E402

pytestmark = pytest.mark.timeout(900)
AGENT = Path(__file__).resolve().parents[2] / "examples" / "wrap_command" / "agent.py"
INSTRUCTION = (
    "Open the referral for Jane Sample, start a Triage encounter, and save "
    "this note: {note}"
)


@pytest.fixture()
def clinic() -> Iterator[str]:
    base, _db, stop = serve()
    yield base.rstrip("/")
    stop()


def _setup(tmp_path: Path, base: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    from openadapt_flow.jit import authority

    monkeypatch.setenv(authority.KEY_PASSPHRASE_ENV, "e2e passphrase")
    key_dir = tmp_path / "keys"
    pem = tmp_path / "keyfiles" / "ci.pem"
    authority.register_principal(
        key_dir,
        role="admitter_key",
        principal_id="ci:wrap-command",
        public_key_hex=authority.generate_encrypted_key(pem),
    )
    admitter = authority.principal_from_key(authority.load_encrypted_key(pem), key_dir)
    task_file = tmp_path / "task.json"
    task_file.write_text(
        json.dumps(
            {
                "schema": "openadapt.wrap-task/v1",
                "instruction": INSTRUCTION,
                "parameters": ["note"],
                "url": f"{base}/?fault=ok&idempotency=demo#tasks",
                "verifier": {
                    "kind": "rest",
                    "base_url": base,
                    "records_path": "/api/db",
                    "records_key": "records",
                    "records_complete": True,
                },
                "expect": {"patient_id": "p1", "type": "Triage"},
                "on_screen": ["Jane Sample", "MRN P1"],
                "canary_params": [{"note": "Synthetic canary note"}],
                "cache_dir": str(tmp_path / "cache"),
            }
        )
    )
    reset = lambda: _http_json(f"{base}/api/reset", method="POST", body={})  # noqa: E731
    setup = load_task(
        task_file,
        offer=lambda proposal: "ci:wrap-command-reviewer",
        admitter=admitter,
        key_dir=key_dir,
        reset=reset,
    )
    return setup, reset


def _authorize(setup: Any, tmp_path: Path, url: str, note: str) -> None:
    setup.cache.common["teach_authorization"] = demo_authority(
        tmp_path,
        task_id=setup.cache.task_id(setup.instruction, {"note": note}),
        url=url,
        record={"patient_id": "p1", "type": "Triage"},
        inputs={"note": note},
        constants=["Jane Sample", "MRN P1"],
    )


def test_an_outside_command_is_compiled_admitted_and_replayed(
    clinic: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup, reset = _setup(tmp_path, clinic, monkeypatch)
    note = "Synthetic follow-up in two weeks"
    _authorize(setup, tmp_path, setup.cache.common["url"], note)
    command = ProcessAgent([sys.executable, str(AGENT)], timeout_s=300)
    task = WrappedTask(
        cache=setup.cache,
        agent=command,
        instruction=setup.instruction,
        parameters=setup.parameters,
        runs_dir=tmp_path / "runs",
    )
    reset()
    first = task.call("first", {"note": note})
    assert command.exit_code == 0
    assert first["path"] == "agent" and first["status"] == "admitted", first
    assert first["verified"] is True
    # The command's model use is its own: unknown here, never reported as 0.
    assert first["usage_known"] is False and first["cost_usd"] is None

    # A later run uses the admitted program, not the command.
    reset()
    later = WrappedTask(
        cache=setup.cache,
        agent=None,
        instruction=setup.instruction,
        parameters=setup.parameters,
        runs_dir=tmp_path / "runs",
    )
    second = later.call("second", {"note": "Recheck knee in ten days"})
    assert second["path"] == "skill" and second["status"] == "VERIFIED", second
    assert second["model_calls"] == 0 and second["receipt_outcome"] == "VERIFIED"
    rows = _http_json(f"{clinic}/api/db")["records"]
    assert [r["note"] for r in rows if r["patient_id"] == "p1"] == [
        "Recheck knee in ten days"
    ]


def test_a_failing_command_makes_no_program_and_keeps_its_exit_status(
    clinic: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup, reset = _setup(tmp_path, clinic, monkeypatch)
    note = "Synthetic follow-up in two weeks"
    _authorize(setup, tmp_path, setup.cache.common["url"], note)
    command = ProcessAgent([sys.executable, "-c", "raise SystemExit(7)"], timeout_s=60)
    task = WrappedTask(
        cache=setup.cache,
        agent=command,
        instruction=setup.instruction,
        parameters=setup.parameters,
        runs_dir=tmp_path / "runs",
    )
    reset()
    result = task.call("first", {"note": note})
    assert command.exit_code == 7
    # The run ends as a failed capture: no draft, no program.
    assert result["status"] == "capture_failed" and result["verified"] is False
    assert setup.cache.server(setup.instruction, {"note": note}) is None
    assert _http_json(f"{clinic}/api/db")["records"] == []


def test_the_endpoint_refuses_a_request_without_the_token(
    clinic: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child that probes without the token gets 401 and exits nonzero."""

    setup, reset = _setup(tmp_path, clinic, monkeypatch)
    note = "Synthetic follow-up in two weeks"
    _authorize(setup, tmp_path, setup.cache.common["url"], note)
    probe = (
        "import os, sys, urllib.request, urllib.error\n"
        "req = urllib.request.Request(os.environ['OPENADAPT_MCP_URL'], data=b'{}',"
        " method='POST', headers={'Authorization': 'Bearer wrong',"
        " 'Content-Type': 'application/json'})\n"
        "try:\n    urllib.request.urlopen(req, timeout=10)\nexcept urllib.error.HTTPError as e:\n"
        "    sys.exit(0 if e.code == 401 else 3)\n"
        "sys.exit(4)\n"
    )
    command = ProcessAgent([sys.executable, "-c", probe], timeout_s=60)
    task = WrappedTask(
        cache=setup.cache,
        agent=command,
        instruction=setup.instruction,
        parameters=setup.parameters,
        runs_dir=tmp_path / "runs",
    )
    reset()
    result = task.call("first", {"note": note})
    # The probe saw 401 and exited 0; it saved nothing, so nothing compiles.
    assert command.exit_code == 0
    assert result["verified"] is False
    assert _http_json(f"{clinic}/api/db")["records"] == []


def test_command_failed_carries_the_status() -> None:
    assert CommandFailed(5).exit_code == 5


def test_the_cli_runs_the_command_and_run_never_starts_an_agent(
    clinic: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """The real ``openadapt wrap ... -- COMMAND`` path, with no person attending.

    Run 1 is guarded, read back and compiled, and stays a draft (only a
    person at the terminal confirms checks). ``wrap run`` then refuses the
    draft without starting anything.
    """

    from openadapt_flow.approval_mode.approver_roster import ROOT_ENV
    from openadapt_flow.teach_authorization_fixture import LocalAuthorizer
    from openadapt_flow.teach_scope import origin_of
    from openadapt_flow.wrap import cli

    setup, reset = _setup(tmp_path, clinic, monkeypatch)
    note = "Synthetic follow-up in two weeks"
    key_dir = tmp_path / "keys"
    signer = LocalAuthorizer(key_dir, "ci:wrap-command-approver")
    monkeypatch.setenv(ROOT_ENV, signer.root)
    statement = signer.issue(
        tmp_path / "teach-authorization",
        {
            "task": {
                "task_id": setup.cache.task_id(setup.instruction, {"note": note}),
                "summary": "Save a note",
            },
            "purposes": ["teach", "repair"],
            "origins": [str(origin_of(setup.cache.common["url"]))],
            "inputs": {"note": {"value": note}},
            "constants": [{"value": "Jane Sample"}, {"value": "MRN P1"}],
            "writes": {
                "record_kind": "encounter",
                "identity": {
                    "patient_id": {"value": "p1"},
                    "type": {"value": "Triage"},
                },
                "fields": {},
            },
        },
    )
    reset()
    common = [
        "--task",
        str(tmp_path / "task.json"),
        "--param",
        "note=" + note,
        "--key-dir",
        str(key_dir),
    ]
    code = cli.main(
        [
            *common,
            "--teach-authorization",
            str(statement),
            "--approver-root",
            signer.root,
            "--",
            sys.executable,
            str(AGENT),
        ]
    )
    out = capsys.readouterr().out
    first = json.loads(out.strip().splitlines()[-1])
    # Run 1 was read back (verified), but it is a draft, not a program.
    assert code == 0 and first["path"] == "agent", first
    assert first["status"] == "draft", first
    rows = _http_json(f"{clinic}/api/db")["records"]
    assert [r["note"] for r in rows] == [note]

    assert cli.main(["run", *common]) == 2
    assert "DRAFT_NOT_ADMITTED" in capsys.readouterr().err
