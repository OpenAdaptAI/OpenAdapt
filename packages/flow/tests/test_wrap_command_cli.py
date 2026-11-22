"""``openadapt wrap -- COMMAND`` and ``openadapt wrap run``: argument routing."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow.wrap import cli
from openadapt_flow.wrap.process import ENV_TOKEN, ENV_URL, ProcessAgent


class _Task:
    parameters = ("note",)

    def __init__(self, agent: Any, result: dict[str, Any]) -> None:
        self.agent = agent
        self.result = result
        self.calls: list[tuple[str, dict[str, str]]] = []

    def call(self, request_id: str, values: dict[str, str]) -> dict[str, Any]:
        self.calls.append((request_id, values))
        if isinstance(self.agent, ProcessAgent):
            self.agent.exit_code = self.result.pop("_exit", 0)
        return self.result


def test_the_command_after_the_separator_becomes_the_first_run_agent(monkeypatch):
    seen: dict[str, Any] = {}

    def task(args, parser, *, command_agent=None, replay_only=False):
        seen.update(agent=command_agent, replay_only=replay_only)
        seen["task"] = _Task(command_agent, {"verified": True})
        return seen["task"]

    monkeypatch.setattr(cli, "_task", task)
    code = cli.main(
        ["--task", "t.json", "--param", "note=x", "--", "my-agent", "--param", "y"]
    )
    assert code == 0
    agent = seen["agent"]
    assert isinstance(agent, ProcessAgent)
    # Everything after -- is the command's, including its own flags.
    assert agent.command == ["my-agent", "--param", "y"]
    request_id, values = seen["task"].calls[0]
    assert values == {"note": "x"} and request_id.startswith("wrap-")


def test_the_command_exit_status_is_passed_through(monkeypatch, capsys):
    def task(args, parser, *, command_agent=None, replay_only=False):
        return _Task(command_agent, {"verified": False, "_exit": 7})

    monkeypatch.setattr(cli, "_task", task)
    assert cli.main(["--task", "t.json", "--param", "note=x", "--", "agent"]) == 7


def test_run_never_starts_an_agent(monkeypatch, capsys):
    def task(args, parser, *, command_agent=None, replay_only=False):
        assert replay_only and command_agent is None
        raise cli.NoAdmittedProgram()

    monkeypatch.setattr(cli, "_task", task)
    assert cli.main(["run", "--task", "t.json", "--param", "note=x"]) == 2
    assert "NO_ADMITTED_PROGRAM" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--task", "t.json", "--"],
        ["run", "--task", "t.json", "--", "agent"],
        ["--task", "t.json", "--serve", "--", "agent"],
    ],
)
def test_misused_separators_are_refused(argv):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(argv)
    assert exit_info.value.code == 2


def test_example_flag_after_the_separator_belongs_to_the_command(monkeypatch):
    seen: dict[str, Any] = {}

    def task(args, parser, *, command_agent=None, replay_only=False):
        seen["agent"] = command_agent
        return _Task(command_agent, {"verified": True})

    monkeypatch.setattr(cli, "_task", task)
    assert (
        cli.main(["--task", "t.json", "--param", "note=x", "--", "a", "--example"]) == 0
    )
    assert seen["agent"].command == ["a", "--example"]


def test_process_agent_needs_no_model_key_and_a_command():
    agent = ProcessAgent(["true"])
    assert agent.requires_model_key is False
    with pytest.raises(ValueError):
        ProcessAgent([])


def test_the_command_gets_the_endpoint_and_the_server_closes_after(tmp_path):
    """A real child: it sees the URL and token, and the endpoint stops after."""

    import sys
    import urllib.error
    import urllib.request

    from openadapt_flow.integrations.agents.skills import BrowserTools

    out = tmp_path / "env.txt"
    script = (
        "import os, pathlib\n"
        f"pathlib.Path({str(out)!r}).write_text("
        f"os.environ[{ENV_URL!r}] + '\\n' + str(len(os.environ[{ENV_TOKEN!r}])))\n"
    )
    agent = ProcessAgent([sys.executable, "-c", script], timeout_s=60)
    tools = BrowserTools(SimpleNamespace())
    assert agent._run_command("Task: x", tools) is None
    url, token_length = out.read_text().splitlines()
    assert url.startswith("http://127.0.0.1:") and url.endswith("/mcp")
    assert int(token_length) >= 32
    assert tools.summary is not None
    with pytest.raises((urllib.error.URLError, ConnectionError, OSError)):
        urllib.request.urlopen(url, timeout=2)


def test_the_command_gets_no_openadapt_settings_or_keys():
    from openadapt_flow.wrap.process import child_environment

    env = child_environment(
        {
            "PATH": "/bin",
            "ANTHROPIC_API_KEY": "the agent's own key",
            "OPENADAPT_JIT_KEY_PASSPHRASE": "admission key passphrase",
            "OPENADAPT_FLOW_SECRET_PASSWORD": "a sign-in value",
            "openadapt_api_key": "a cloud key",
        }
    )
    assert env == {"PATH": "/bin", "ANTHROPIC_API_KEY": "the agent's own key"}


def test_a_real_child_never_sees_the_admission_passphrase(tmp_path, monkeypatch):
    import sys

    from openadapt_flow.integrations.agents.skills import BrowserTools

    monkeypatch.setenv("OPENADAPT_JIT_KEY_PASSPHRASE", "admission key passphrase")
    out = tmp_path / "seen.txt"
    script = (
        "import os, pathlib\n"
        f"pathlib.Path({str(out)!r}).write_text("
        "repr(sorted(k for k in os.environ if k.startswith('OPENADAPT_'))))\n"
    )
    ProcessAgent([sys.executable, "-c", script], timeout_s=60)._run_command(
        "Task: x", BrowserTools(SimpleNamespace())
    )
    assert out.read_text() == repr(sorted([ENV_URL, ENV_TOKEN, "OPENADAPT_TASK"]))


def test_run_names_a_draft_that_was_not_admitted(monkeypatch, capsys, tmp_path):
    def task(args, parser, *, command_agent=None, replay_only=False):
        raise cli.NoAdmittedProgram(tmp_path / "task-dir")

    monkeypatch.setattr(cli, "_task", task)
    assert cli.main(["run", "--task", "t.json", "--param", "note=x"]) == 2
    err = capsys.readouterr().err
    assert "DRAFT_NOT_ADMITTED" in err and str(tmp_path / "task-dir") in err


def test_a_retried_wrapped_run_gets_the_same_request_id(monkeypatch):
    ids: list[str] = []

    def task(args, parser, *, command_agent=None, replay_only=False):
        t = _Task(command_agent, {"verified": True})
        t.call = lambda request_id, values: (
            ids.append(request_id)
            or {  # type: ignore[method-assign]
                "verified": True
            }
        )
        return t

    monkeypatch.setattr(cli, "_task", task)
    argv = ["--task", "t.json", "--param", "note=x", "--", "my-agent"]
    assert cli.main(argv) == 0
    assert cli.main(argv) == 0
    assert cli.main(["--task", "t.json", "--param", "note=y", "--", "my-agent"]) == 0
    assert ids[0] == ids[1] and ids[0] != ids[2]
    assert all(i.startswith("wrap-") for i in ids)
    # An explicit --request-id still wins.
    assert cli.main(["--task", "t.json", "--request-id", "r1", "--", "my-agent"]) == 0
    assert ids[-1] == "r1"


def test_every_credential_the_verifier_reads_is_withheld_from_the_command(tmp_path):
    from openadapt_flow.wrap.process import child_environment, verify_env_names

    verify = tmp_path / "verify.yaml"
    verify.write_text(
        "verifier:\n"
        "  base_url: ${APP_URL}\n"
        "  headers: {Authorization: 'Bearer ${APP_TOKEN:-none}'}\n"
        "  frame:\n"
        "    database:\n"
        "      read: {sql_password_env: DB_PASSWORD, dsn_env: [DB_DSN]}\n"
        "  auth: {basic_env: APP_BASIC}\n",
        encoding="utf-8",
    )
    names = verify_env_names(verify)
    assert names == {"APP_URL", "APP_TOKEN", "DB_PASSWORD", "DB_DSN", "APP_BASIC"}
    env = child_environment(
        {
            "PATH": "/bin",
            "ANTHROPIC_API_KEY": "the agent's own key",
            "DB_PASSWORD": "s3cret",
            "app_token": "tok",
            "APP_BASIC": "u:p",
            "DB_DSN": "postgres://x",
            "APP_URL": "http://127.0.0.1",
        },
        names,
    )
    assert env == {"PATH": "/bin", "ANTHROPIC_API_KEY": "the agent's own key"}


def test_a_real_child_never_sees_a_verifier_credential(tmp_path, monkeypatch):
    import sys

    from openadapt_flow.integrations.agents.skills import BrowserTools

    monkeypatch.setenv("DB_PASSWORD", "s3cret")
    out = tmp_path / "seen.txt"
    script = (
        "import os, pathlib\n"
        f"pathlib.Path({str(out)!r}).write_text(repr(os.environ.get('DB_PASSWORD')))\n"
    )
    ProcessAgent(
        [sys.executable, "-c", script], timeout_s=60, withheld_env={"DB_PASSWORD"}
    )._run_command("Task: x", BrowserTools(SimpleNamespace()))
    assert out.read_text() == "None"


def _wait_gone(pid: int, timeout_s: float = 10.0) -> bool:
    import os
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _group_script(pid_file: Any) -> str:
    # The command starts a grandchild in its own group and waits forever.
    return (
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
        "time.sleep(600)\n"
    )


@pytest.mark.skipif(__import__("os").name != "posix", reason="process groups")
def test_the_command_timeout_ends_the_whole_process_group(tmp_path):
    import sys
    import time

    from openadapt_flow.wrap.process import CommandFailed, _run_group

    pid_file = tmp_path / "grandchild.pid"
    started = time.monotonic()
    with pytest.raises(CommandFailed):
        _run_group([sys.executable, "-c", _group_script(pid_file)], {}, 2.0)
    assert time.monotonic() - started < 30
    assert _wait_gone(int(pid_file.read_text()))


@pytest.mark.skipif(__import__("os").name != "posix", reason="process groups")
@pytest.mark.parametrize("how", ["SIGTERM", "SIGINT"])
def test_a_signal_to_openadapt_ends_the_wrapped_command_group(tmp_path, how):
    import os
    import signal
    import subprocess
    import sys
    import time

    pid_file = tmp_path / "grandchild.pid"
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import sys\n"
        "from openadapt_flow.wrap import cli\n"
        "from openadapt_flow.wrap.process import _run_group\n"
        "with cli._commands_stopped_on_signal(True):\n"
        f"    _run_group([sys.executable, '-c', {_group_script(pid_file)!r}], {{}}, 600)\n",
        encoding="utf-8",
    )
    parent = subprocess.Popen(
        [sys.executable, str(driver)], env=dict(os.environ), stderr=subprocess.PIPE
    )
    deadline = time.monotonic() + 30
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    grandchild = int(pid_file.read_text())
    parent.send_signal(getattr(signal, how))
    parent.wait(timeout=30)
    assert _wait_gone(grandchild)
