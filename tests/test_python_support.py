"""Python version support and launcher-form next steps.

Two install bugs motivate this file.

1. Every launcher from 1.7.0 to 1.16.0 declared Requires-Python <3.13 because
   openadapt-flow, openadapt-agent, and Flow's OCR dependency do. pip on
   Python 3.13 and 3.14 skipped those releases, backtracked to openadapt
   1.3.0 (the newest release with no upper bound and no engine dependency),
   and installed an unrelated pre-compiler CLI with no error. `openadapt
   quickstart` then failed with "No such command". The launcher now installs
   on any Python >=3.10, every engine requirement carries a marker, and every
   engine command on an unsupported interpreter prints how to install with
   Python 3.12.
2. The Flow tutorial prints `openadapt-flow record ...` as its next step. The
   uv installer exposes only `openadapt`, so that command failed there. The
   launcher rewrites Flow's printed commands to `openadapt flow ...`, which
   works for every install method.
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner
from packaging.requirements import Requirement

from openadapt import cli
from openadapt.cli import main as cli_main

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 CI
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
ENGINE_PACKAGES = {"openadapt-flow", "openadapt-agent"}
OLDEST, NEWEST = cli._SUPPORTED_PYTHON
FIRST_UNSUPPORTED = (NEWEST[0], NEWEST[1] + 1)


def _dotted(version: tuple[int, int]) -> str:
    return f"{version[0]}.{version[1]}"


def _all_requirements() -> list[Requirement]:
    strings = list(PROJECT["dependencies"])
    for extra in PROJECT["optional-dependencies"].values():
        strings.extend(extra)
    return [Requirement(item) for item in strings]


def _environment(python: str, platform: str = "linux") -> dict[str, str]:
    return {
        "python_version": python,
        "python_full_version": f"{python}.0",
        "sys_platform": platform,
        "extra": "",
    }


# ---------------------------------------------------------------------------
# Metadata: the launcher installs everywhere, the engine only where it runs.
# ---------------------------------------------------------------------------


def test_requires_python_has_no_upper_bound():
    assert PROJECT["requires-python"] == f">={_dotted(OLDEST)}"


def test_classifiers_list_exactly_the_supported_minor_versions():
    listed = {
        classifier.rsplit(" :: ", 1)[1]
        for classifier in PROJECT["classifiers"]
        if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", classifier)
    }
    expected = {_dotted((3, minor)) for minor in range(OLDEST[1], NEWEST[1] + 1)}
    assert listed == expected


def test_every_engine_requirement_is_gated_to_supported_python():
    engine = [req for req in _all_requirements() if req.name in ENGINE_PACKAGES]
    assert engine, "the launcher must still depend on the engine"
    for requirement in engine:
        assert requirement.marker is not None, str(requirement)
        for platform in ("linux", "darwin", "win32"):
            unsupported = _environment(_dotted(FIRST_UNSUPPORTED), platform)
            assert not requirement.marker.evaluate(unsupported), str(requirement)
            assert not requirement.marker.evaluate(_environment("3.14", platform)), str(
                requirement
            )


@pytest.mark.parametrize("minor", range(OLDEST[1], NEWEST[1] + 1))
def test_base_install_keeps_the_engine_on_supported_python(minor):
    base = [Requirement(item) for item in PROJECT["dependencies"]]
    environment = _environment(f"3.{minor}")
    installed = {
        req.name
        for req in base
        if req.marker is None or req.marker.evaluate(environment)
    }
    assert ENGINE_PACKAGES <= installed


def test_base_install_on_unsupported_python_is_the_launcher_alone():
    base = [Requirement(item) for item in PROJECT["dependencies"]]
    environment = _environment(_dotted(FIRST_UNSUPPORTED))
    installed = {
        req.name
        for req in base
        if req.marker is None or req.marker.evaluate(environment)
    }
    assert installed == {"click"}


def test_engine_markers_match_the_cli_supported_range():
    marker = f'python_version < "{_dotted(FIRST_UNSUPPORTED)}"'
    for requirement in _all_requirements():
        if requirement.name in ENGINE_PACKAGES:
            assert marker in str(requirement.marker), str(requirement)
            # Platform-specific extras still apply on their own platform.
            assert any(
                requirement.marker.evaluate(_environment(_dotted(NEWEST), platform))
                for platform in ("linux", "darwin", "win32")
            ), str(requirement)


def test_ci_guard_runs_on_the_first_unsupported_pythons():
    workflow = (ROOT / ".github" / "workflows" / "main.yml").read_text()
    guard = workflow[workflow.index("unsupported-python-guard:") :]
    for python in (_dotted(FIRST_UNSUPPORTED), "3.14"):
        assert f"'{python}'" in guard


def test_ci_guard_resolves_the_launcher_by_name():
    """Installing the wheel file alone can't catch pip choosing an old release."""
    workflow = (ROOT / ".github" / "workflows" / "main.yml").read_text()
    guard = workflow[workflow.index("unsupported-python-guard:") :]
    resolve = guard.index("--find-links dist")
    assert resolve < guard.index("pip install dist/*.whl")
    for spec in ("openadapt", "'openadapt[browser]'", "'openadapt[capture]'"):
        assert spec in guard[guard.index("for spec in") : resolve]


# ---------------------------------------------------------------------------
# Runtime: every engine command explains the fix on an unsupported Python.
# ---------------------------------------------------------------------------


@pytest.fixture()
def unsupported_python(monkeypatch):
    # Patch the launcher's view only: replacing sys.version_info itself sends
    # pytest and the standard library down 3.14-only code paths.
    monkeypatch.setattr("openadapt.cli._python_minor", lambda: (3, 14))


@pytest.fixture()
def supported_python(monkeypatch):
    # Lets the tests below run unchanged in the CI guard on Python 3.13+.
    monkeypatch.setattr("openadapt.cli._python_minor", lambda: NEWEST)


def test_python_minor_reads_the_running_interpreter():
    assert cli._python_minor() == tuple(sys.version_info[:2])


@pytest.fixture()
def no_engine_import(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name.startswith("openadapt_flow"):
            pytest.fail(f"{name} must not be imported on unsupported Python")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)


def _assert_remedy(output: str) -> None:
    assert "OpenAdapt needs Python 3.10–3.12" in output
    assert "This is Python 3.14" in output
    assert "uv tool install --python 3.12 openadapt" in output


@pytest.mark.parametrize(
    "argv",
    [
        ["flow", "tutorial"],
        ["flow", "record", "--url", "https://example.test", "--out", "rec"],
        ["flow", "--help"],
        ["quickstart", "--break-it"],
        ["connect", "--pairing", "ABC123"],
    ],
)
def test_engine_commands_print_the_remedy(unsupported_python, no_engine_import, argv):
    result = CliRunner().invoke(cli_main, argv)

    assert result.exit_code == 1, result.output
    _assert_remedy(result.output)
    assert "pip install --upgrade openadapt" not in result.output
    assert "needs a newer openadapt-flow" not in result.output


@pytest.mark.parametrize("argv", [[], ["--help"]])
def test_top_level_help_starts_with_the_remedy(unsupported_python, argv):
    result = CliRunner().invoke(cli_main, argv)

    _assert_remedy(result.output)
    assert result.output.index("OpenAdapt needs Python") < result.output.index("Usage:")


def test_supported_python_help_has_no_remedy(supported_python):
    result = CliRunner().invoke(cli_main, ["--help"])

    assert result.exit_code == 0
    assert "OpenAdapt needs Python" not in result.output


def test_doctor_names_the_interpreter_instead_of_missing_packages(
    unsupported_python, no_engine_import
):
    result = CliRunner().invoke(cli_main, ["doctor"])

    assert result.exit_code == 1
    assert "[UNSUPPORTED]" in result.output
    _assert_remedy(result.output)
    assert "[MISSING]" not in result.output


def test_deploy_preflight_fails_on_unsupported_python(unsupported_python):
    result = CliRunner().invoke(cli_main, ["deploy"])

    assert result.exit_code != 0
    assert "[UNSUPPORTED] Python" in result.output
    assert "uv tool install --python 3.12 openadapt" in result.output
    assert "Preflight passed" not in result.output
    assert "[MISSING]" not in result.output
    assert "pip install --upgrade openadapt" not in result.output


def test_version_still_works_on_unsupported_python(unsupported_python):
    result = CliRunner().invoke(cli_main, ["version"])

    assert result.exit_code == 0
    assert "openadapt:" in result.output


# ---------------------------------------------------------------------------
# Printed next steps use the launcher spelling.
# ---------------------------------------------------------------------------

VERBS = frozenset({"record", "tutorial", "explain", "qualify", "certify"})


@pytest.mark.parametrize(
    ("printed", "expected"),
    [
        (
            "\nNext: record your own workflow:\n"
            "  openadapt-flow record --backend web --url <your app>",
            "\nNext: record your own workflow:\n"
            "  openadapt flow record --backend web --url <your app>",
        ),
        (
            "watch the engine halt:\n  openadapt-flow tutorial --break-it",
            "watch the engine halt:\n  openadapt flow tutorial --break-it",
        ),
        (
            "Run `openadapt-flow qualify certify` again.",
            "Run `openadapt flow qualify certify` again.",
        ),
        ("next: openadapt-flow explain run-1", "next: openadapt flow explain run-1"),
        # Package names, versions, and other tools stay exactly as printed.
        ("pip install openadapt-flow", "pip install openadapt-flow"),
        ("openadapt-flow 1.35.1", "openadapt-flow 1.35.1"),
        (
            "pip install 'openadapt-flow[console]'",
            "pip install 'openadapt-flow[console]'",
        ),
        ("uvx openadapt-flow record", "uvx openadapt-flow record"),
        ("my-openadapt-flow record", "my-openadapt-flow record"),
        ("openadapt-flow web recording", "openadapt-flow web recording"),
        ("openadapt-flow recording", "openadapt-flow recording"),
    ],
)
def test_launcher_spelling(printed, expected):
    assert cli._launcher_spelling(printed, VERBS) == expected


def test_stream_rewrites_writes_and_delegates_everything_else():
    target = io.StringIO()
    stream = cli._LauncherSpellingStream(target, VERBS)

    written = stream.write("  openadapt-flow record --url x\n")
    stream.writelines(["openadapt-flow tutorial\n"])
    stream.flush()

    assert written == len("  openadapt-flow record --url x\n")
    assert target.getvalue() == (
        "  openadapt flow record --url x\nopenadapt flow tutorial\n"
    )
    assert stream.isatty() is False


def _require_openadapt_flow():
    return pytest.importorskip("openadapt_flow", reason="openadapt-flow not installed")


def test_installed_flow_verbs_include_the_first_run_commands():
    _require_openadapt_flow()
    verbs = cli._flow_verbs()
    assert {"record", "tutorial", "compile", "replay"} <= verbs


def test_flow_verbs_fall_back_when_the_parser_is_unavailable(monkeypatch):
    _require_openadapt_flow()
    import openadapt_flow.__main__ as flow_main_module

    monkeypatch.delattr(flow_main_module, "build_parser")
    assert cli._flow_verbs() == cli._FALLBACK_FLOW_VERBS


def _fake_flow(monkeypatch, printed: str):
    _require_openadapt_flow()
    import openadapt_flow.__main__ as flow_main_module

    def fake_main(argv):
        print(printed)
        return 0

    monkeypatch.setattr(flow_main_module, "main", fake_main)


def test_tutorial_next_step_prints_the_launcher_form(monkeypatch, supported_python):
    _fake_flow(
        monkeypatch,
        "\nNext: record your own workflow:\n"
        "  openadapt-flow record --backend web --url <your app>",
    )

    result = CliRunner().invoke(cli_main, ["flow", "tutorial"])

    assert result.exit_code == 0, result.output
    assert "  openadapt flow record --backend web --url <your app>" in result.output
    assert "openadapt-flow record" not in result.output


def test_quickstart_next_step_prints_the_launcher_form(monkeypatch, supported_python):
    # The engine's own tutorial (older Flow, or engine-only flags) prints its
    # next steps; the launcher respells them.
    monkeypatch.setattr("openadapt.cli._flow_runs_quickstart", lambda: False)
    _fake_flow(monkeypatch, "  openadapt-flow record --backend web --url <your app>")

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli_main, ["quickstart", "--out", "qs"])

    assert result.exit_code == 0, result.output
    assert "openadapt flow record --backend web" in result.output
    assert "openadapt-flow record" not in result.output


def test_other_flow_verbs_print_engine_output_unchanged(monkeypatch, supported_python):
    _fake_flow(monkeypatch, '{"next_command": "openadapt-flow explain run-1"}')

    result = CliRunner().invoke(cli_main, ["flow", "replay", "bundle"])

    assert result.exit_code == 0, result.output
    assert '{"next_command": "openadapt-flow explain run-1"}' in result.output


# ---------------------------------------------------------------------------
# The agent next step appears only when the installed agent can run it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("installed", "offered"),
    [("2.0.1", False), ("2.0.2", True), ("2.1.0", True), ("3.0.0", True)],
)
def test_agent_tutorial_serve_needs_a_release_after_2_0_1(
    monkeypatch, installed, offered
):
    monkeypatch.setattr("importlib.metadata.version", lambda _name: installed)
    assert cli._agent_serves_tutorial() is offered


def test_agent_tutorial_serve_is_not_offered_without_the_agent(monkeypatch):
    from importlib.metadata import PackageNotFoundError

    def missing(_name):
        raise PackageNotFoundError(_name)

    monkeypatch.setattr("importlib.metadata.version", missing)
    assert cli._agent_serves_tutorial() is False


@pytest.mark.parametrize("serves", [False, True])
def test_quickstart_offers_the_agent_command_only_when_it_works(
    monkeypatch, supported_python, serves
):
    from openadapt.quickstart.story import Outcome

    printed = []

    def fake(root, *, steps, **kwargs):
        printed.append(steps(Outcome(root=root)).agent)
        return Outcome(root=root)

    monkeypatch.setattr("openadapt.cli._flow_runs_quickstart", lambda: True)
    monkeypatch.setattr("openadapt.quickstart.run_quickstart", fake)
    monkeypatch.setattr("openadapt.cli._agent_serves_tutorial", lambda: serves)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli_main, ["quickstart", "--out", "qs"])

    assert result.exit_code == 0, result.output
    assert (printed == ["openadapt agent serve --allow-run"]) is serves
    if not serves:
        assert printed == ["openadapt agent serve --bundles qs/bundle"]
