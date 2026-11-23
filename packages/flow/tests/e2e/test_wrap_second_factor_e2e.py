"""Wrap a task whose sign-in asks for an authenticator code, headless.

The target is the synthetic notes app with a second sign-in page
(:mod:`tests.e2e._sign_in_app` with a TOTP secret). The scripted agent types
:data:`openadapt_flow.second_factor.ONE_TIME_CODE_MARKER` into the code
field; it never has a code. A local command source plays the person: it
computes the current code from the synthetic secret. It asserts:

* the code field (``autocomplete="one-time-code"``) becomes a one-time step
  found from the page alone: no task input, no secret parameter and no vault
  key name it, and publish asks no caller for it;
* no file holds a code or the TOTP secret;
* the canary and a cached run get a fresh code from the source and read
  their note back VERIFIED, and a declining source stops the next run at the
  code page with ``second_factor_declined`` before anything is typed there.
"""

from __future__ import annotations

import json
import re
import stat
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

from openadapt_flow import second_factor as sf
from openadapt_flow.integrations.agents.recording import load_task
from openadapt_flow.integrations.agents.skills import FrameworkAgent
from openadapt_flow.ir import Workflow
from openadapt_flow.wrap import WrappedTask
from tests.e2e._sign_in_app import serve, totp
from tests.e2e.test_wrap_sign_in_e2e import (
    INSTRUCTION,
    PASSWORD,
    TEST_KEY,
    USERNAME,
    _admitter,
    _authorize,
    _inputs,
    _leaks,
    _ref,
    _why,
)

pytest.importorskip("playwright")

#: A synthetic authenticator secret (base32), made up for this test.
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


@pytest.fixture
def app() -> Iterator[tuple[str, Any]]:
    base, state, stop = serve(USERNAME, PASSWORD, TOTP_SECRET)
    yield base, state
    stop()


def _source(tmp_path: Path, *, declined: bool = False) -> str:
    """A command source that computes the current code (the person)."""

    path = tmp_path / ("declining.py" if declined else "authenticator.py")
    flow_root = Path(__file__).resolve().parents[2]
    body = (
        "raise SystemExit(3)\n"
        if declined
        else (
            "import sys\n"
            f"sys.path.insert(0, {str(flow_root)!r})\n"
            "from tests.e2e._sign_in_app import totp\n"
            f"print(totp({TOTP_SECRET!r}))\n"
        )
    )
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return f"{sys.executable} {path}"


def _agent(seen: list[str]) -> FrameworkAgent:
    def run(task: str, tools: Any) -> dict[str, Any]:
        seen.append(task)
        told = _inputs(task)
        listing = tools.observe()
        listing = tools.type_text(told["username"], _ref(listing, "Username"))
        listing = tools.type_text(told["password"], _ref(listing, "Password"))
        listing = tools.click(_ref(listing, "Sign in"))
        seen.append(listing)
        listing = tools.type_text(sf.ONE_TIME_CODE_MARKER, _ref(listing, "Code"))
        listing = tools.click(_ref(listing, "Verify"))
        seen.append(listing)
        listing = tools.type_text(told["note"], _ref(listing, "Note"))
        listing = tools.click(_ref(listing, "Save"))
        tools.finish("saved")
        return {
            "calls": [
                {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                }
            ]
        }

    return FrameworkAgent(run, principal_id="agent:scripted-two-step", model="scripted")


def test_an_authenticator_code_comes_from_a_person_and_never_persists(
    app: tuple[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    base, state = app
    monkeypatch.setenv(sf.COMMAND_ENV, _source(tmp_path))
    admitter, key_dir = _admitter(tmp_path, monkeypatch)
    task_file = tmp_path / "task.json"
    task_file.write_text(
        json.dumps(
            {
                "schema": "openadapt.wrap-task/v1",
                "instruction": INSTRUCTION,
                "parameters": ["username", "password", "note"],
                "url": base,
                "verifier": {
                    "kind": "rest",
                    "base_url": base.rstrip("/"),
                    "records_path": "/api/notes",
                    "records_key": "records",
                    "records_complete": True,
                },
                "expect": {"list": "demo"},
                "on_screen": ["Notes"],
                "canary_params": [
                    {
                        "username": USERNAME,
                        "password": PASSWORD,
                        "note": "two-step canary note",
                    }
                ],
                "cache_dir": str(tmp_path / "cache"),
            }
        )
    )
    setup = load_task(
        task_file,
        offer=lambda proposal: "ci:two-step-reviewer",
        admitter=admitter,
        key_dir=key_dir,
        model_key=TEST_KEY,
    )
    first_values = {
        "username": USERNAME,
        "password": PASSWORD,
        "note": "first two-step note",
    }
    _authorize(setup.cache, setup.instruction, first_values, base, key_dir, monkeypatch)
    seen: list[str] = []
    task = WrappedTask(
        cache=setup.cache,
        agent=_agent(seen),
        instruction=setup.instruction,
        parameters=setup.parameters,
        runs_dir=tmp_path / "runs",
    )
    first = task.call("first-run", first_values)
    assert first["status"] == "admitted" and first["verified"] is True, _why(tmp_path)
    # Run 1 and the canary each typed one fresh code.
    assert state.codes_accepted >= 2 and state.codes_rejected == 0

    program = setup.cache.admitted_program(
        setup.instruction, dict.fromkeys(setup.parameters, "")
    )
    assert program is not None
    workflow = Workflow.load(Path(program["bundle"]))
    (code_step,) = [s for s in workflow.steps if s.one_time]
    assert code_step.action.value == "second_factor" and code_step.one_time == "code"
    assert code_step.param is None and not code_step.secret and code_step.text is None
    assert sorted(workflow.secret_params) == ["password", "username"]
    assert sf.ONE_TIME_CODE_PARAM not in workflow.params

    from openadapt_flow.wrap.publish import stage

    staged = stage(setup.cache, setup.instruction, setup.parameters, key_dir=key_dir)
    assert staged.manifest["parameters"] == ["note"]
    assert staged.manifest["secret_parameters"] == ["password", "username"]
    assert staged.manifest["input_schema"]["required"] == ["note"]
    assert _leaks(staged.staged, [TOTP_SECRET]) == []

    from importlib.metadata import version

    if int(version("mcp").split(".")[0]) < 2:
        pytest.skip("the cached web run needs mcp 2 (sync tools run in a thread)")
    accepted = state.codes_accepted
    second = task.call(
        "second-run",
        {"username": USERNAME, "password": PASSWORD, "note": "second two-step note"},
    )
    assert second.get("path") == "skill", json.dumps(second, default=str)
    assert second["verified"] is True and second["model_calls"] == 0, json.dumps(second)
    assert second["receipt_outcome"] == "VERIFIED", second
    assert state.codes_accepted == accepted + 1

    monkeypatch.setenv(sf.COMMAND_ENV, _source(tmp_path, declined=True))
    saved = len(state.records)
    third = task.call(
        "third-run",
        {"username": USERNAME, "password": PASSWORD, "note": "never saved"},
    )
    assert third["verified"] is not True and third["status"] == "HALTED", third
    latest = max(
        (tmp_path / "cache").rglob("runs/*/report.json"),
        key=lambda path: path.stat().st_mtime,
    )
    halt = json.loads(latest.read_text())["halt"]
    assert halt["reason"].startswith(f"Step '{code_step.id}'"), halt
    assert "second_factor_declined" in halt["reason"], halt
    assert len(state.records) == saved and state.codes_rejected == 0

    # No file and no output holds the secret or any code accepted today.
    out, err = capfd.readouterr()
    assert TOTP_SECRET not in out + err
    hits = [
        path
        for path in _leaks(tmp_path, [TOTP_SECRET])
        if not path.endswith("authenticator.py")
    ]
    assert hits == [], hits
    _assert_no_recent_code(tmp_path)


def _assert_no_recent_code(root: Path) -> None:
    """No run file holds a code from the last few minutes, as a token."""

    import time

    now = time.time()
    codes = {totp(TOTP_SECRET, now - 30 * i) for i in range(0, 20)}
    pattern = re.compile(
        rb"(?<![0-9A-Za-z.])("
        + b"|".join(c.encode() for c in codes)
        + rb")(?![0-9A-Za-z])"
    )
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix in (".png", ".zip", ".whl"):
            continue
        if "authenticator.py" in path.name:
            continue
        found = pattern.search(path.read_bytes())
        assert found is None, (path, "holds a recent code")
