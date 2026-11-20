"""Browser test support belongs to the checkout, independently of its subject."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = ROOT / "packages/flow/tests"


@pytest.fixture
def support(monkeypatch):
    name = "_browser_fixture_source_tests"
    package = ModuleType(name)
    package.__path__ = [str(SUPPORT)]
    monkeypatch.setitem(sys.modules, name, package)
    guard = importlib.import_module(f"{name}.headless_guard")
    attached = importlib.import_module(f"{name}._attached_chromium")
    try:
        yield SimpleNamespace(guard=guard, attached=attached)
    finally:
        for loaded in list(sys.modules):
            if loaded.startswith(name + "."):
                del sys.modules[loaded]


def _launch(support, entrypoint, monkeypatch, args, calls=None):
    if calls is None:
        calls = []
    if entrypoint == "playwright":
        guard = support.guard._LaunchGuard()

        def launch(browser_type, **options):
            calls.append(options)
            return options

        browser = SimpleNamespace(name="chromium")
        guarded = guard.sync_launch(launch, persistent=False)
        guarded(browser, headless=True, executable_path="/fixture/browser", args=args)
        return calls[0]["args"]

    def popen(command, **options):
        calls.append(command)
        return SimpleNamespace(stderr=None, poll=lambda: 0, wait=lambda **_kw: 0)

    monkeypatch.setattr(support.attached.subprocess, "Popen", popen)
    with support.attached.owned_chromium(Path("/fixture/browser"), args, "about:blank"):
        pass
    assert calls[0][0] == "/fixture/browser"
    assert calls[0][-1] == "about:blank"
    return calls[0][1:-1]


@pytest.mark.parametrize("entrypoint", ["playwright", "attached"])
@pytest.mark.parametrize("foreign_helper", ["missing", "poisoned"])
def test_both_fixture_entrypoints_ignore_the_installed_subject_policy(
    support, monkeypatch, tmp_path, entrypoint, foreign_helper
):
    foreign = ModuleType("openadapt_flow._browser_readiness")
    foreign.__file__ = str(tmp_path / "site-packages/foreign/_browser_readiness.py")
    if foreign_helper == "poisoned":

        def poison(*_args):
            raise AssertionError("the installed subject supplied fixture policy")

        foreign.chromium_launch_args = poison
    monkeypatch.setitem(sys.modules, foreign.__name__, foreign)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("PWDEBUG", raising=False)
    args = ["--headless=new", "--fixture-option", "--use-mock-keychain"]
    original = list(args)
    actual = _launch(support, entrypoint, monkeypatch, args)
    assert actual == [
        "--headless=new",
        "--fixture-option",
        "--use-mock-keychain",
        "--password-store=basic",
    ]
    assert args == original
    assert sys.modules[foreign.__name__] is foreign
    assert support.attached.chromium_launch_args is support.guard.chromium_launch_args


@pytest.mark.parametrize("entrypoint", ["playwright", "attached"])
def test_missing_checkout_support_refuses_before_either_launch(
    support, monkeypatch, tmp_path, entrypoint
):
    monkeypatch.setattr(support.guard, "_SOURCE_READINESS", tmp_path / "missing.py")
    calls = []
    with pytest.raises(FileNotFoundError):
        _launch(support, entrypoint, monkeypatch, [], calls)
    assert calls == []


@pytest.mark.parametrize("entrypoint", ["playwright", "attached"])
def test_invalid_checkout_support_has_no_installed_subject_fallback(
    support, monkeypatch, tmp_path, entrypoint
):
    invalid = tmp_path / "invalid.py"
    invalid.write_text("# No browser launch policy in this source.\n")
    monkeypatch.setattr(support.guard, "_SOURCE_READINESS", invalid)
    calls = []
    with pytest.raises(AttributeError, match="chromium_launch_args"):
        _launch(support, entrypoint, monkeypatch, [], calls)
    assert calls == []


@pytest.mark.parametrize(
    "path,flow,seal",
    [
        ("conftest.py", True, True),
        ("packages/flow/tests/headless_guard.py", True, True),
        ("packages/flow/tests/_attached_chromium.py", True, True),
        ("packages/flow/openadapt_flow/_browser_readiness.py", True, True),
        ("tests/test_browser_fixture_source.py", True, True),
        ("packages/seal-view/src/main.ts", False, True),
        ("packages/program-graph/src/ProgramWorkbench.tsx", False, True),
        ("tests/test_seal_view.py", False, True),
        ("packages/flow/openadapt_flow/registry.py", True, False),
        ("docs/unrelated.md", False, False),
    ],
)
def test_actual_selector_covers_shared_support_and_existing_consumers(
    tmp_path, path, flow, seal
):
    workflow = yaml.load(
        (ROOT / ".github/workflows/packages.yml").read_text(), Loader=yaml.BaseLoader
    )
    script = next(
        step["run"]
        for step in workflow["jobs"]["scope"]["steps"]
        if step.get("id") == "classify"
    )
    commands = tmp_path / "bin"
    commands.mkdir()
    git = commands / "git"
    git.write_text(
        '#!/bin/sh\ncase "$1" in\n'
        "cat-file) exit 0;;\n"
        'diff) printf "%s\\n" "$FIXTURE_CHANGED_PATH";;\n'
        "*) exit 7;;\nesac\n"
    )
    git.chmod(0o755)
    output = tmp_path / "outputs"
    env = dict(
        os.environ,
        PATH=f"{commands}{os.pathsep}{os.defpath}",
        EVENT_NAME="push",
        BEFORE_SHA="a" * 40,
        GROUP_BASE_SHA="",
        GITHUB_OUTPUT=str(output),
        FIXTURE_CHANGED_PATH=path,
    )
    subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", script],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["flow"] == str(flow).lower()
    assert values["seal_view"] == str(seal).lower()
    assert values["types"] == "false"  # The test must not take the all() fallback.
